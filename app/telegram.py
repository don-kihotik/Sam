from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from io import BytesIO

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import Message as TelegramMessage
from aiogram.types import ReplyParameters, Video, VideoNote
from sqlalchemy import func, or_, select

from app.ai import AIService
from app.config import Settings
from app.db.models import FollowUp, Message, Outbox, ProcessingRun
from app.db.session import Database
from app.delivery import split_reply
from app.services import IncomingMessage, MessageProcessor, is_directly_addressed
from app.video import extract_video_frames

logger = logging.getLogger(__name__)

_BOLD_PATTERN = re.compile(r"\*\*([^*\n]+)\*\*")
_ACTIONABLE_REQUEST = re.compile(
    r"(?:\?|^\s*(?:что|как|почему|зачем|когда|где|можешь|скажи|посоветуй|объясни|"
    r"проверь|разбери|посмотри|запомни|добавь|исправь|переведи)\b)",
    re.IGNORECASE,
)
_VIDEO_ANALYSIS_REQUEST = re.compile(
    r"(?:разбер\w*|посмотр\w*|проанализ\w*|техник\w*|ошибк\w*|"
    r"что\s+(?:не\s+так|видно)|где\s+.*(?:сил|ошиб))",
    re.IGNORECASE,
)
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024


def known_user_ids(settings: Settings) -> set[int]:
    return {
        user_id
        for user_id in (
            settings.alexey_telegram_user_id,
            settings.andrey_telegram_user_id,
        )
        if user_id is not None
    }


def is_allowed_chat(
    settings: Settings,
    *,
    chat_id: int,
    chat_type: ChatType,
    user_id: int,
) -> bool:
    """Allow the shared group and private chats with registered athletes only."""
    if chat_id == settings.telegram_allowed_chat_id:
        return True
    return chat_type == ChatType.PRIVATE and user_id in known_user_ids(settings)


def format_telegram_reply(text: str) -> str:
    """Render Sam's limited Markdown safely as Telegram HTML."""
    escaped = html.escape(text)
    return _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)


def looks_like_actionable_request(text: str) -> bool:
    return bool(_ACTIONABLE_REQUEST.search(text.strip()))


def should_process_video(text: str, *, chat_type: ChatType) -> bool:
    """Analyze private videos and group videos carrying an explicit request only."""
    if chat_type == ChatType.PRIVATE:
        return True
    return bool(is_directly_addressed(text) or _VIDEO_ANALYSIS_REQUEST.search(text))


def should_force_response(
    text: str,
    *,
    chat_type: ChatType,
    is_reply_to_bot: bool,
    allow_transcription_aliases: bool = False,
    media_requested: bool = False,
) -> bool:
    if chat_type == ChatType.PRIVATE or media_requested:
        return True
    if is_directly_addressed(text, allow_transcription_aliases=allow_transcription_aliases):
        return True
    return is_reply_to_bot and looks_like_actionable_request(text)


def find_video(message: TelegramMessage) -> tuple[Video | VideoNote | None, TelegramMessage]:
    if message.video or message.video_note:
        return message.video or message.video_note, message
    if message.reply_to_message and (
        message.reply_to_message.video or message.reply_to_message.video_note
    ):
        replied = message.reply_to_message
        return replied.video or replied.video_note, replied
    return None, message


def compose_video_context(*, request: str, transcript: str | None, analysis: str) -> str:
    parts = [request.strip() or "Пользователь прислал видео для разбора."]
    if transcript:
        parts.append(f"Расшифровка звука из видео:\n{transcript}")
    parts.append(
        f"Автоматические наблюдения по выбранным кадрам, а не по непрерывному видео:\n{analysis}"
    )
    return "\n\n".join(parts)


class PermanentMediaError(RuntimeError):
    pass


class TelegramRuntime:
    def __init__(self, settings: Settings, database: Database, ai: AIService):
        self.settings, self.database = settings, database
        self.bot = Bot(settings.telegram_bot_token)
        self.dispatcher = Dispatcher()
        self.router = Router()
        self.processor = MessageProcessor(settings, ai)
        self._task: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._bot_id: int | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self.last_update_at = self.last_success_at = None
        self.last_error = self.last_media_error = None
        self.router.message.register(self._handle_message)
        self.router.edited_message.register(self._handle_message)
        self.dispatcher.include_router(self.router)

    async def start(self) -> None:
        me = await self.bot.get_me()
        self._bot_id = me.id
        self._worker = asyncio.create_task(self._work(), name="sam-worker")
        self._task = asyncio.create_task(
            # Do not acknowledge the next update until this handler has durably saved it.
            self.dispatcher.start_polling(self.bot, handle_signals=False, handle_as_tasks=False),
            name="telegram-polling",
        )
        self._task.add_done_callback(self._polling_finished)

    @property
    def polling_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_details(self) -> dict:
        return {
            "polling_running": self.polling_running,
            "worker_running": self._worker is not None and not self._worker.done(),
            "last_update_at": self.last_update_at.isoformat() if self.last_update_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
            "last_media_error": self.last_media_error,
        }

    def _polling_finished(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            self.last_error = f"polling:{type(task.exception()).__name__}"

    async def stop(self) -> None:
        if self.polling_running:
            await self.dispatcher.stop_polling()
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        if self._task:
            with suppress(asyncio.CancelledError):
                await self._task
        await self.bot.session.close()

    async def _handle_message(self, message: TelegramMessage) -> None:
        if not message.from_user or message.from_user.is_bot:
            return
        if not is_allowed_chat(
            self.settings,
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            user_id=message.from_user.id,
        ):
            return
        if message.text and message.text.split("@", 1)[0].strip().lower() == "/whoami":
            await message.reply(f"Твой Telegram user ID: {message.from_user.id}")
            return
        if message.from_user.id not in known_user_ids(self.settings):
            return
        self.last_update_at = datetime.now(UTC)
        # Serialize ingestion and processing for this small group, including edits.
        async with self._lock:  # noqa: SIM117 - keep ingestion transaction scope explicit
            async with self.database.sessions() as session:
                row = await session.scalar(
                    select(Message).where(
                        Message.telegram_chat_id == message.chat.id,
                        Message.telegram_message_id == message.message_id,
                    )
                )
                payload = message.model_dump(mode="json", exclude_none=True)
                if row:
                    old = row.attachment_data.get("telegram_payload", {})
                    if not message.edit_date or old.get("edit_date") == payload.get("edit_date"):
                        return
                    history = list(row.processing_data.get("edit_history", []))
                    history.append(
                        {
                            "revision": row.revision,
                            "text": row.raw_text,
                            "transcript": row.transcript,
                        }
                    )
                    row.processing_data = {"edit_history": history, "is_edit": True}
                    row.revision += 1
                    row.raw_text = message.text or message.caption or ""
                    row.transcript = None
                    row.embedding = None
                    row.attachment_data = {"telegram_payload": payload}
                    run = await session.scalar(
                        select(ProcessingRun).where(ProcessingRun.message_id == row.id)
                    )
                    if run:
                        run.status, run.attempts, run.next_attempt_at = "received", 0, None
                    else:
                        session.add(ProcessingRun(message_id=row.id, status="received"))
                    pending = (
                        await session.scalars(
                            select(Outbox).where(
                                Outbox.source_message_id == row.id,
                                Outbox.status.in_(["pending", "retry"]),
                            )
                        )
                    ).all()
                    for item in pending:
                        item.status = "superseded"
                    questions = (
                        await session.scalars(
                            select(FollowUp).where(
                                FollowUp.source_message_id == row.id,
                                FollowUp.status.in_(["pending", "awaiting_delivery"]),
                            )
                        )
                    ).all()
                    for question in questions:
                        question.status = "superseded"
                    await session.commit()
                else:
                    incoming = IncomingMessage(
                        telegram_message_id=message.message_id,
                        telegram_chat_id=message.chat.id,
                        telegram_user_id=message.from_user.id,
                        timestamp=message.date,
                        text=message.text or message.caption or "",
                        message_type=message.content_type,
                        reply_to_message_id=message.reply_to_message.message_id
                        if message.reply_to_message
                        else None,
                        attachment_metadata={"telegram_payload": payload},
                    )
                    await self.processor.record_unprocessed(
                        session, incoming, status="received", reason="queued"
                    )
        self._wake.set()

    async def _work(self) -> None:
        while True:
            try:
                async with self._lock:
                    await self._process_next()
                    await self._deliver_next()
                self._wake.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the durable worker alive
                self.last_error = type(exc).__name__
                logger.error("Worker operation failed: %s", type(exc).__name__)
                await asyncio.sleep(2)

    async def _process_next(self) -> None:
        async with self.database.sessions() as session:
            row = await session.scalar(
                select(Message)
                .join(ProcessingRun)
                .where(
                    ProcessingRun.status.in_(["received", "started", "retry"]),
                    or_(
                        ProcessingRun.next_attempt_at.is_(None),
                        ProcessingRun.next_attempt_at <= datetime.now(UTC),
                    ),
                )
                .order_by(Message.telegram_timestamp, Message.id)
                .limit(1)
            )
            if not row:
                return
            row_id = row.id
            incoming = None
            try:
                payload = row.attachment_data.get("telegram_payload")
                if payload:
                    telegram = TelegramMessage.model_validate(payload)
                    incoming = await self._prepare(telegram, row)
                else:
                    # Historical interrupted text/voice processing can be recovered too.
                    incoming = IncomingMessage(
                        telegram_message_id=row.telegram_message_id,
                        telegram_chat_id=row.telegram_chat_id,
                        telegram_user_id=row.telegram_user_id,
                        timestamp=row.telegram_timestamp,
                        text=row.raw_text or row.transcript or row.normalized_text or "",
                        message_type=row.message_type,
                        transcript=row.transcript,
                        reply_to_message_id=row.reply_to_message_id,
                    )
                if incoming is None:
                    run = await session.scalar(
                        select(ProcessingRun).where(ProcessingRun.message_id == row_id)
                    )
                    run.status, run.error = "completed", None
                    await session.commit()
                else:
                    incoming.is_edit = bool(row.processing_data.get("is_edit"))
                    # Persist successful transcription/analysis before the next API operation.
                    row.attachment_data = {
                        **row.attachment_data,
                        **(incoming.attachment_metadata or {}),
                    }
                    row.transcript = incoming.transcript
                    await session.commit()
                    await self.processor.process(session, incoming)
                    row.processing_data = {**row.processing_data, "is_edit": False}
                    await session.commit()
                self.last_success_at, self.last_error = datetime.now(UTC), None
            except PermanentMediaError as exc:
                run = await session.scalar(
                    select(ProcessingRun).where(ProcessingRun.message_id == row_id)
                )
                run.status, run.error = "failed", "unsupported_media"
                self.last_media_error = "unsupported_media"
                # Explicitly requested unsupported media gets a durable explanation.
                session.add(
                    Outbox(
                        source_message_id=row_id,
                        revision=row.revision,
                        athlete_id=row.athlete_id,
                        telegram_chat_id=row.telegram_chat_id,
                        reply_to_message_id=row.telegram_message_id,
                        text=str(exc),
                    )
                )
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - classify media/provider failures for retry
                await session.rollback()
                run = await session.scalar(
                    select(ProcessingRun).where(ProcessingRun.message_id == row_id)
                )
                if run and run.status not in {"retry", "failed"}:
                    run.attempts += 1
                    run.status = "failed" if run.attempts >= 5 else "retry"
                    run.error = type(exc).__name__
                    run.next_attempt_at = datetime.now(UTC) + timedelta(
                        seconds=min(300, 5 * 2**run.attempts)
                    )
                    await session.commit()
                self.last_error = f"processing:{type(exc).__name__}"
                logger.warning("Message %s processing deferred: %s", row_id, type(exc).__name__)

    async def _prepare(self, message: TelegramMessage, row: Message) -> IncomingMessage | None:
        text = message.text or message.caption or ""
        attachment = dict(row.attachment_data)
        transcript = row.transcript
        video, source = find_video(message)
        if not video:
            for candidate in [message, message.reply_to_message]:
                if (
                    candidate
                    and candidate.document
                    and (candidate.document.mime_type or "").startswith("video/")
                ):
                    video, source = candidate.document, candidate
                    break
        requested = bool(video and should_process_video(text, chat_type=message.chat.type))
        circular = bool(source.video_note) if source else False
        if video and not requested and not circular:
            if source is message:
                attachment["media_status"] = "ignored_not_requested"
                row.attachment_data = attachment
                return None
            video = None
        if message.photo and not text:
            if message.chat.id < 0:
                return None
            raise PermanentMediaError(
                "Фото сохранил, но разбор скриншотов пока не подключён. Нужные показатели можно написать или наговорить."
            )
        if message.voice:
            if transcript is None:
                buffer = BytesIO()
                await self.bot.download(message.voice, destination=buffer)
                transcript = await self.processor.ai.transcribe(buffer.getvalue())
                attachment.update(
                    file_id=message.voice.file_id,
                    transcription_model=self.settings.transcription_model,
                )
            text = transcript
        elif video:
            size = getattr(video, "file_size", None)
            attachment.update(
                file_id=video.file_id,
                telegram_file_size_bytes=size,
                source_message_id=source.message_id,
            )
            if size and size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
                raise PermanentMediaError(
                    f"Видео весит {size / 1048576:.1f} MB. Telegram отдаёт этому боту файлы до 20 MB; "
                    "пришли короткий фрагмент или сжатую версию."
                )
            analysis = attachment.get("vision_analysis") if requested else None
            if (requested and not analysis) or (circular and transcript is None):
                buffer = BytesIO()
                await self.bot.download(video, destination=buffer)
                video_bytes = buffer.getvalue()
                if circular:
                    transcript = await self.processor.ai.transcribe(
                        video_bytes, filename="video.mp4"
                    )
                if requested:
                    frames = await extract_video_frames(
                        video_bytes,
                        duration_seconds=getattr(video, "duration", None),
                        max_frames=self.settings.video_max_frames,
                    )
                    analysis = await self.processor.ai.analyze_video_frames(
                        frames,
                        caption=text,
                        transcript=transcript or "",
                        safety_identifier=f"telegram-video-{message.from_user.id}",
                    )
                    attachment.update(vision_analysis=analysis, analyzed_frame_count=len(frames))
            if requested:
                text = compose_video_context(
                    request=text, transcript=transcript, analysis=analysis or ""
                )
            else:
                text = transcript or ""
            attachment["media_status"] = "analyzed" if requested else "transcribed"
        if not text:
            return None
        reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == self._bot_id
        )
        return IncomingMessage(
            telegram_message_id=message.message_id,
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user.id,
            timestamp=message.date,
            text=text,
            transcript=transcript,
            message_type="voice"
            if message.voice
            else ("video_note" if circular else ("video" if video else "text")),
            reply_to_message_id=message.reply_to_message.message_id
            if message.reply_to_message
            else None,
            attachment_metadata=attachment,
            is_reply_to_sam=should_force_response(
                text,
                chat_type=message.chat.type,
                is_reply_to_bot=reply_to_bot,
                allow_transcription_aliases=bool(message.voice or circular),
                media_requested=requested,
            ),
        )

    async def _deliver_next(self) -> None:
        async with self.database.sessions() as session:
            item = await session.scalar(
                select(Outbox)
                .where(
                    Outbox.status.in_(["pending", "retry"]),
                    or_(
                        Outbox.next_attempt_at.is_(None),
                        Outbox.next_attempt_at <= datetime.now(UTC),
                    ),
                )
                .order_by(Outbox.id)
                .limit(1)
            )
            if item is None:
                return
            parts = split_reply(item.text)
            item.attempts += 1
            await session.commit()
            try:
                for part in parts[len(item.sent_message_ids) :]:
                    sent = await self.bot.send_message(
                        chat_id=item.telegram_chat_id,
                        text=format_telegram_reply(part),
                        parse_mode=ParseMode.HTML,
                        reply_parameters=ReplyParameters(
                            message_id=item.reply_to_message_id, allow_sending_without_reply=True
                        ),
                    )
                    item.sent_message_ids = [*item.sent_message_ids, sent.message_id]
                    await self.processor.save_outgoing(
                        session,
                        telegram_message_id=sent.message_id,
                        telegram_chat_id=item.telegram_chat_id,
                        athlete_id=item.athlete_id,
                        text=part,
                        timestamp=sent.date,
                        reply_to_message_id=item.reply_to_message_id,
                    )
                item.status, item.error = "sent", None
                questions = (
                    await session.scalars(
                        select(FollowUp).where(
                            FollowUp.source_message_id == item.source_message_id,
                            FollowUp.status == "awaiting_delivery",
                        )
                    )
                ).all()
                for question in questions:
                    question.status = "pending"
                self.last_error = None
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - durable delivery failure accounting
                item.error = type(exc).__name__
                item.status = (
                    "failed"
                    if isinstance(exc, TelegramBadRequest) or item.attempts >= 5
                    else "retry"
                )
                delay = (
                    exc.retry_after
                    if isinstance(exc, TelegramRetryAfter)
                    else min(300, 5 * 2**item.attempts)
                )
                item.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                await session.commit()
                self.last_error = f"delivery:{type(exc).__name__}"

    async def queue_health(self) -> dict:
        async with self.database.sessions() as session:
            pending = await session.scalar(
                select(func.count())
                .select_from(ProcessingRun)
                .where(ProcessingRun.status.in_(["received", "started", "retry"]))
            )
            failed = await session.scalar(
                select(func.count())
                .select_from(ProcessingRun)
                .where(ProcessingRun.status == "failed")
            )
            unsent = await session.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.status.in_(["pending", "retry", "failed"]))
            )
            return {
                "pending_messages": pending,
                "failed_messages": failed,
                "unsent_replies": unsent,
            }
