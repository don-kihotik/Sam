from __future__ import annotations

import asyncio
import html
import logging
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Video,
    VideoNote,
)
from aiogram.types import Message as TelegramMessage
from sqlalchemy import case, func, or_, select

from app.ai import AIService
from app.config import Settings
from app.db.models import (
    Athlete,
    CatalogSource,
    FollowUp,
    Message,
    Outbox,
    ProcessingRun,
    SharedChat,
    TrainingPreference,
)
from app.db.session import Database
from app.delivery import split_reply
from app.membership import (
    create_group_invitation,
    ensure_preference,
    is_group_member,
    redeem_invitation_details,
)
from app.planning import latest_session_plan, render_training_plan
from app.prompts import SELF_ONLY_PROMPT_VERSIONS
from app.scheduling import enqueue_morning_plans
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
_CALLBACK_DATA = re.compile(
    r"^train:(session|log|export|confirm|adjust|shorten|replace|reschedule|skip):(\d+)$"
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
        self._scheduler: asyncio.Task | None = None
        self._bot_id: int | None = None
        self._bot_username: str | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self.last_update_at = self.last_success_at = None
        self.last_error = self.last_media_error = None
        self.router.message.register(self._handle_message)
        self.router.edited_message.register(self._handle_message)
        self.router.callback_query.register(self._handle_callback)
        self.dispatcher.include_router(self.router)

    async def start(self) -> None:
        me = await self.bot.get_me()
        self._bot_id, self._bot_username = me.id, me.username
        self._worker = asyncio.create_task(self._work(), name="sam-worker")
        self._scheduler = asyncio.create_task(self._run_scheduler(), name="sam-reminders")
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
        if self._scheduler:
            self._scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await self._scheduler
        if self._task:
            with suppress(asyncio.CancelledError):
                await self._task
        await self.bot.session.close()

    async def _handle_message(self, message: TelegramMessage) -> None:
        if not message.from_user or message.from_user.is_bot:
            return
        if await self._maybe_handle_group_add(message):
            return
        if message.text and message.text.split("@", 1)[0].strip().lower() == "/whoami":
            suffix = (
                f"\nID этого чата: {message.chat.id}"
                if message.chat.type != ChatType.PRIVATE
                else ""
            )
            await message.reply(f"Твой Telegram user ID: {message.from_user.id}{suffix}")
            return
        self.last_update_at = datetime.now(UTC)
        # Serialize ingestion and processing for this small group, including edits.
        async with self._lock:  # noqa: SIM117 - keep ingestion transaction scope explicit
            async with self.database.sessions() as session:
                athlete = await session.scalar(
                    select(Athlete).where(
                        Athlete.telegram_user_id == message.from_user.id,
                        Athlete.active.is_(True),
                    )
                )
                start = (message.text or "").strip().split(maxsplit=1)
                invite_start = bool(
                    message.chat.type == ChatType.PRIVATE
                    and len(start) == 2
                    and start[0].split("@", 1)[0].casefold() == "/start"
                )
                # A deep link is also meaningful for an existing athlete: it
                # grants a second group without creating a duplicate profile.
                if invite_start:
                    try:
                        redemption = await redeem_invitation_details(
                            session,
                            token=start[1],
                            telegram_user_id=message.from_user.id,
                            display_name=message.from_user.full_name,
                            chat_id=message.chat.id,
                            now=datetime.now(UTC),
                        )
                    except (PermissionError, ValueError):
                        await session.rollback()
                        await message.reply(
                            "Эта ссылка недействительна, уже использована или предназначена другому человеку."
                        )
                        return
                    athlete = redemption.athlete
                    shared_chat = (
                        await session.get(SharedChat, redemption.invitation.shared_chat_id)
                        if redemption.invitation.shared_chat_id is not None
                        else None
                    )
                    await session.commit()
                    replayed = False
                    saved = redemption.pending_message.get("telegram_payload")
                    if isinstance(saved, dict):
                        try:
                            original = TelegramMessage.model_validate(saved)
                        except ValueError:
                            original = None
                        if (
                            original is not None
                            and original.from_user
                            and original.from_user.id == message.from_user.id
                            and original.chat.id == redemption.invitation.shared_chat_id
                        ):
                            recorded = await self.processor.record_unprocessed(
                                session,
                                IncomingMessage(
                                    telegram_message_id=original.message_id,
                                    telegram_chat_id=original.chat.id,
                                    telegram_user_id=original.from_user.id,
                                    timestamp=original.date,
                                    text=original.text or original.caption or "",
                                    message_type=original.content_type,
                                    reply_to_message_id=(
                                        original.reply_to_message.message_id
                                        if original.reply_to_message
                                        else None
                                    ),
                                    attachment_metadata={"telegram_payload": saved},
                                ),
                                status="received",
                                reason="onboarding_replay",
                            )
                            replayed = recorded.message_id is not None and not recorded.duplicate
                    group_name = shared_chat.title if shared_chat and shared_chat.title else "группе"
                    extra = " Исходное сообщение уже поставил в обработку." if replayed else ""
                    await message.reply(
                        f"Готово: твой отдельный профиль подключён к {group_name}.{extra}\n\n"
                        "Я могу запоминать тренировки, составлять и адаптировать планы, "
                        "а затем показывать прогресс. В группе обсуждаю только данные автора сообщения; "
                        "личный контекст остаётся в личном чате.\n\n"
                        "Теперь напиши одной фразой: цель в лазании, текущий уровень, "
                        "сколько раз в неделю можешь тренироваться и какое оборудование доступно."
                    )
                    if replayed:
                        self._wake.set()
                    return
                # Group membership alone never introduces an unknown account.
                if athlete is None:
                    return
                elif message.chat.type == ChatType.PRIVATE:
                    preference = await ensure_preference(session, athlete)
                    if preference.private_chat_id is None:
                        preference.private_chat_id = message.chat.id
                        await session.commit()
                else:
                    if not await is_group_member(
                        session,
                        athlete_id=athlete.id,
                        telegram_chat_id=message.chat.id,
                    ):
                        return
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
                    # Keep durable policy reservations.  An edit is a new
                    # revision, not a way to erase prior paid-work quota.
                    row.processing_data = {
                        **(row.processing_data or {}),
                        "edit_history": history,
                        "is_edit": True,
                    }
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

    async def _maybe_handle_group_add(self, message: TelegramMessage) -> bool:
        """Handle Alexey's `/add` reply even before a new group is trusted."""
        parts = (message.text or "").strip().split(maxsplit=1)
        if not parts:
            return False
        raw = parts[0]
        if raw.split("@", 1)[0].casefold() != "/add":
            return False
        if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.reply("Используй /add ответом на сообщение друга в общем чате.")
            return True
        if message.from_user.id != self.settings.alexey_telegram_user_id:
            await message.reply("Добавлять участников может только администратор Alexey.")
            return True
        target_message = message.reply_to_message
        target = target_message.from_user if target_message else None
        if target is None or target.is_bot or target.id == message.from_user.id:
            await message.reply("Ответь командой /add именно на сообщение друга.")
            return True
        async with self._lock, self.database.sessions() as session:
            admin = await session.scalar(
                select(Athlete).where(
                    Athlete.telegram_user_id == message.from_user.id,
                    Athlete.active.is_(True),
                )
            )
            if admin is None:
                await message.reply("Администратор не зарегистрирован у Сэма.")
                return True
            token, invitation = await create_group_invitation(
                session,
                created_by=admin,
                alexey_telegram_user_id=self.settings.alexey_telegram_user_id,
                telegram_chat_id=message.chat.id,
                chat_title=message.chat.title,
                target_telegram_user_id=target.id,
                pending_message={
                    "telegram_payload": target_message.model_dump(
                        mode="json", exclude_none=True
                    )
                },
                now=datetime.now(UTC),
            )
            await session.commit()
        username = self._bot_username
        if not username:
            me = await self.bot.get_me()
            username = me.username
        if not username:
            await message.reply("Приглашение создано, но у бота не настроено имя пользователя.")
            return True
        link = f"https://t.me/{username}?start={token}"
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Подключить Сэма", url=link)]
            ]
        )
        expiry = invitation.expires_at.astimezone(UTC).strftime("%d.%m %H:%M UTC")
        await message.reply(
            f"{target.first_name}, нажми кнопку — Сэм создаст отдельный профиль, "
            f"подключит этот чат и разберёт исходное сообщение. Ссылка действует до {expiry}.",
            reply_markup=keyboard,
        )
        return True

    async def _handle_callback(self, callback: CallbackQuery) -> None:
        """Turn an authenticated button click into a normal durable command."""
        if not callback.from_user or not callback.message or not callback.data:
            return
        match = _CALLBACK_DATA.fullmatch(callback.data)
        if match is None:
            await callback.answer("Кнопка больше не поддерживается.", show_alert=True)
            return
        if callback.message.chat.type != ChatType.PRIVATE:
            await callback.answer("Открой эту команду в личном чате.", show_alert=True)
            return
        action, raw_id = match.groups()
        async with self._lock, self.database.sessions() as session:
            athlete = await session.scalar(
                select(Athlete).where(
                    Athlete.telegram_user_id == callback.from_user.id,
                    Athlete.active.is_(True),
                )
            )
            if athlete is None or callback.message.chat.id != callback.from_user.id:
                await callback.answer("Нет доступа.", show_alert=True)
                return
            preference = await ensure_preference(session, athlete)
            if preference.private_chat_id not in {None, callback.message.chat.id}:
                await callback.answer("Нет доступа.", show_alert=True)
                return
            # Telegram callback IDs are unique.  A deterministic negative
            # message id lets the existing Message/ProcessingRun dedupe path
            # process a duplicate callback exactly once.
            synthetic_id = -int(sha256(callback.id.encode()).hexdigest()[:15], 16)
            incoming = IncomingMessage(
                telegram_message_id=synthetic_id,
                telegram_chat_id=callback.message.chat.id,
                telegram_user_id=callback.from_user.id,
                timestamp=datetime.now(UTC),
                text=f"/{action} {raw_id}",
                message_type="callback",
                reply_to_message_id=callback.message.message_id,
                attachment_metadata={
                    "callback_id": callback.id,
                    "callback_data": callback.data,
                },
            )
            recorded = await self.processor.record_unprocessed(
                session, incoming, status="received", reason="callback"
            )
            if recorded.message_id is not None:
                row = await session.get(Message, recorded.message_id)
                if row is not None:
                    row.processing_data = {
                        **(row.processing_data or {}),
                        "training_action": "confirm_plan" if action == "confirm" else action,
                        "plan_id": int(raw_id),
                    }
                    await session.commit()
        await callback.answer("Запрос принят.")
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

    async def _run_scheduler(self) -> None:
        """A small independent timer keeps reminders from waiting on model work."""
        while True:
            try:
                async with self._lock, self.database.sessions() as session:
                    await enqueue_morning_plans(session, now=datetime.now(UTC))
                    await session.commit()
                await asyncio.sleep(20)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - scheduler must not kill polling
                self.last_error = f"scheduler:{type(exc).__name__}"
                logger.warning("Reminder scheduling deferred: %s", type(exc).__name__)
                await asyncio.sleep(20)

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
                # Historical catalog recovery is deliberately background work:
                # a new user message must never wait behind 159 screenshots.
                .order_by(
                    case(
                        (
                            Message.processing_data["catalog_recovery"]["version"]
                            .as_string()
                            == "historical-catalog-v1",
                            1,
                        ),
                        else_=0,
                    ),
                    Message.telegram_timestamp,
                    Message.id,
                )
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
                    # Catalog mode deliberately stages raw photos and never feeds
                    # image/OCR content into workout extraction or the model.
                    preference = await session.get(TrainingPreference, row.athlete_id)
                    staged_catalog_source = await session.scalar(
                        select(CatalogSource.id).where(CatalogSource.source_message_id == row.id)
                    )
                    historical_catalog = (
                        (row.processing_data or {}).get("catalog_recovery", {}).get("version")
                        == "historical-catalog-v1"
                    )
                    if (
                        telegram.photo
                        and telegram.chat.type == ChatType.PRIVATE
                        and preference
                        and (
                            preference.catalog_mode
                            or staged_catalog_source is not None
                            or historical_catalog
                        )
                    ):
                        from app.catalog import receive_catalog_source

                        source = await receive_catalog_source(session, message=row)
                        # Receipt is durable before downloading or OCR. A later
                        # provider failure leaves a received source for retry.
                        await session.commit()
                        if source.status == "received":
                            athlete = await session.get(Athlete, row.athlete_id)
                            refusal = await self._preflight_media_request(
                                session,
                                athlete=athlete,
                                row=row,
                                request_text=(
                                    telegram.caption or "Материал для каталога упражнений"
                                ),
                            )
                            if refusal:
                                raise PermanentMediaError(refusal)
                            photo = telegram.photo[-1]
                            buffer = BytesIO()
                            await self.bot.download(photo, destination=buffer)
                            extracted = await self.processor.ai.read_catalog_photo(
                                buffer.getvalue()
                            )
                            # Preserve operator recovery/provenance metadata while
                            # replacing only fields produced by OCR.
                            source.extracted = {**(source.extracted or {}), **extracted}
                            source.status = "needs_review"
                        run = await session.scalar(
                            select(ProcessingRun).where(ProcessingRun.message_id == row_id)
                        )
                        run.status, run.error = "completed", None
                        await session.commit()
                        self.last_success_at, self.last_error = datetime.now(UTC), None
                        return
                    incoming = await self._prepare(session, telegram, row)
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
                # In a shared chat, an unsolicited voice/video failure should
                # not create a noisy bot reply.  Private messages and direct
                # requests still receive a durable, useful explanation.
                reply_to_sam = False
                try:
                    saved = row.attachment_data.get("telegram_payload", {})
                    parent = saved.get("reply_to_message", {})
                    reply_to_sam = (
                        self._bot_id is not None
                        and parent.get("from", {}).get("id") == self._bot_id
                    )
                except AttributeError:
                    reply_to_sam = False
                historical_catalog = (
                    (row.processing_data or {}).get("catalog_recovery", {}).get("version")
                    == "historical-catalog-v1"
                )
                explain = not historical_catalog and (
                    row.telegram_chat_id > 0
                    or is_directly_addressed(
                        row.raw_text or row.transcript or "",
                        is_reply_to_sam=reply_to_sam,
                        allow_transcription_aliases=row.message_type in {"voice", "video_note"},
                    )
                )
                if explain:
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

    async def _preflight_media_request(
        self, session, *, athlete: Athlete | None, row: Message, request_text: str
    ) -> str | None:
        """Run policy with the actual request while preserving the raw source text."""
        if athlete is None:
            return "Не удалось проверить автора запроса. Пришли материал заново в личный чат."
        recovery = (row.processing_data or {}).get("catalog_recovery", {})
        if recovery.get("version") == "historical-catalog-v1":
            # This operator-selected private batch was already scoped by owner
            # and date. Do not charge old screenshots against the athlete's
            # live request quota or spend a classifier call per image.
            return None
        from app.request_policy import preflight_request

        return await preflight_request(
            session,
            athlete=athlete,
            message=row,
            ai=self.processor.ai,
            settings=self.settings,
            text=request_text,
        )

    async def _reserve_media_budget(
        self, session, *, athlete: Athlete | None, row: Message
    ) -> str | None:
        """Reserve the per-message allowance before downloading/transcribing media."""
        if athlete is None:
            return "Не удалось проверить автора запроса. Пришли материал заново в личный чат."
        from app.request_policy import reserve_budget

        return await reserve_budget(session, athlete=athlete, message=row, settings=self.settings)

    async def _prepare(
        self, session, message: TelegramMessage, row: Message
    ) -> IncomingMessage | None:
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
        circular = bool(source.video_note) if source else False
        requested = bool(video and should_process_video(text, chat_type=message.chat.type))
        # Private regular videos opt into visual analysis by being sent.  A
        # circular video is voice-like instead: without an explicit technique
        # request, transcribe and scope-check its spoken report only.
        if circular and not _VIDEO_ANALYSIS_REQUEST.search(text):
            requested = False
        if (
            video
            and source is not message
            and (not source or not source.from_user or source.from_user.id != message.from_user.id)
        ):
            raise PermanentMediaError(
                "Можно разбирать только своё видео. Пришли свой ролик с коротким запросом."
            )
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
            duration = getattr(message.voice, "duration", None)
            if not isinstance(duration, int) or duration <= 0:
                raise PermanentMediaError(
                    "Неизвестна длительность голосового сообщения; пришли короткую запись."
                )
            if duration > self.settings.policy_audio_max_seconds:
                raise PermanentMediaError(
                    "Голосовое слишком длинное для разбора. Пришли фрагмент до 3 минут."
                )
            athlete = await session.get(Athlete, row.athlete_id)
            refusal = await self._reserve_media_budget(session, athlete=athlete, row=row)
            if refusal:
                raise PermanentMediaError(refusal)
            if transcript is None:
                buffer = BytesIO()
                await self.bot.download(message.voice, destination=buffer)
                transcript = await self.processor.ai.transcribe(buffer.getvalue())
                # Keep a completed transcription even when subsequent scope
                # policy refuses it; it is needed to determine whether a group
                # refusal was explicitly addressed to Sam.
                row.transcript = transcript
                attachment.update(
                    file_id=message.voice.file_id,
                    transcription_model=self.settings.transcription_model,
                )
            text = transcript
            refusal = await self._preflight_media_request(
                session, athlete=athlete, row=row, request_text=transcript or ""
            )
            if refusal:
                raise PermanentMediaError(refusal)
        elif video:
            duration = getattr(video, "duration", None)
            if not isinstance(duration, int) or duration <= 0:
                raise PermanentMediaError(
                    "Неизвестна длительность видео; пришли короткий фрагмент."
                )
            if duration > self.settings.policy_video_max_seconds:
                raise PermanentMediaError(
                    "Видео слишком длинное для разбора. Пришли фрагмент до 5 минут."
                )
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
            # A regular video needs an explicit request.  A circular video is
            # voice-like: preserve its transcript-only path for existing users.
            if message.chat.type == ChatType.PRIVATE and not circular and not text.strip():
                raise PermanentMediaError(
                    "Опиши, что в твоей технике нужно разобрать, и пришли короткое своё видео."
                )
            athlete = await session.get(Athlete, row.athlete_id)
            if circular:
                refusal = await self._reserve_media_budget(session, athlete=athlete, row=row)
                if refusal:
                    raise PermanentMediaError(refusal)
            if requested:
                refusal = await self._preflight_media_request(
                    session, athlete=athlete, row=row, request_text=text
                )
                if refusal:
                    raise PermanentMediaError(refusal)
            analysis = attachment.get("vision_analysis") if requested else None
            if (requested and not analysis) or (circular and transcript is None):
                buffer = BytesIO()
                await self.bot.download(video, destination=buffer)
                video_bytes = buffer.getvalue()
                if circular:
                    transcript = await self.processor.ai.transcribe(
                        video_bytes, filename="video.mp4"
                    )
                    row.transcript = transcript
                    # A caption may have been allowed, but it cannot authorize
                    # unrelated spoken content.  Keep the budget reservation
                    # while forcing a new scope decision for the transcript.
                    if "request_policy" in (row.processing_data or {}):
                        row.processing_data = {
                            key: value
                            for key, value in row.processing_data.items()
                            if key != "request_policy"
                        }
                    refusal = await self._preflight_media_request(
                        session,
                        athlete=athlete,
                        row=row,
                        request_text=transcript or "",
                    )
                    if refusal:
                        raise PermanentMediaError(refusal)
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
            # Model-produced replies generated under an earlier privacy policy
            # can contain context which newer retrieval rules would not expose.
            # Keep the audit record, but never deliver that stale response.
            if item.source_message_id is not None:
                source_run = await session.scalar(
                    select(ProcessingRun).where(ProcessingRun.message_id == item.source_message_id)
                )
                if (
                    source_run is not None
                    and source_run.prompt_version
                    and source_run.prompt_version not in SELF_ONLY_PROMPT_VERSIONS
                    and (source_run.extraction_model or source_run.coaching_model)
                ):
                    item.status, item.error = "superseded", "obsolete_response_policy"
                    await session.commit()
                    return
            if await self._suppress_stale_reminder(session, item):
                await session.commit()
                return
            parts = split_reply(item.text)
            item.attempts += 1
            await session.commit()
            try:
                payload = item.payload or {}
                document = payload.get("document")
                if document and not item.sent_message_ids:
                    filename, content = document.get("filename"), document.get("text")
                    if not isinstance(filename, str) or not isinstance(content, str):
                        raise ValueError("invalid document payload")
                    sent_doc = await self.bot.send_document(
                        chat_id=item.telegram_chat_id,
                        document=BufferedInputFile(content.encode("utf-8"), filename=filename),
                        reply_parameters=self._reply_parameters(item.reply_to_message_id),
                    )
                    item.sent_message_ids = [*item.sent_message_ids, sent_doc.message_id]
                    # Persist the irreversible document send before attempting
                    # its accompanying message, so a later retry cannot resend it.
                    await self.processor.save_outgoing(
                        session,
                        telegram_message_id=sent_doc.message_id,
                        telegram_chat_id=item.telegram_chat_id,
                        athlete_id=item.athlete_id,
                        text=f"[document] {filename}",
                        timestamp=sent_doc.date,
                        reply_to_message_id=item.reply_to_message_id,
                    )
                    await session.commit()
                sent_text_parts = len(item.sent_message_ids) - (1 if document else 0)
                for part in parts[sent_text_parts:]:
                    markup = self._keyboard(payload) if sent_text_parts == 0 else None
                    sent = await self.bot.send_message(
                        chat_id=item.telegram_chat_id,
                        text=format_telegram_reply(part),
                        parse_mode=ParseMode.HTML,
                        reply_parameters=self._reply_parameters(item.reply_to_message_id),
                        reply_markup=markup,
                    )
                    item.sent_message_ids = [*item.sent_message_ids, sent.message_id]
                    sent_text_parts += 1
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

    @staticmethod
    def _reply_parameters(reply_to_message_id: int | None) -> ReplyParameters | None:
        return (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None and reply_to_message_id > 0
            else None
        )

    @staticmethod
    def _keyboard(payload: dict) -> InlineKeyboardMarkup | None:
        rows = payload.get("buttons")
        if not isinstance(rows, list):
            return None
        keyboard: list[list[InlineKeyboardButton]] = []
        for row in rows:
            if not isinstance(row, list):
                return None
            buttons: list[InlineKeyboardButton] = []
            for button in row:
                if not isinstance(button, dict):
                    return None
                text, data = button.get("text"), button.get("data")
                if (
                    not isinstance(text, str)
                    or not isinstance(data, str)
                    or not _CALLBACK_DATA.fullmatch(data)
                ):
                    return None
                buttons.append(InlineKeyboardButton(text=text[:64], callback_data=data))
            if buttons:
                keyboard.append(buttons)
        return InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None

    async def _suppress_stale_reminder(self, session, item: Outbox) -> bool:
        """Check opt-in and the current session again immediately before send."""
        athlete = await session.get(Athlete, item.athlete_id)
        # Group posts are optional sharing too. Do not deliver an already
        # queued reply after a user revokes group sharing.
        if item.telegram_chat_id < 0 and (
            athlete is None
            or not athlete.active
            or not await is_group_member(
                session,
                athlete_id=item.athlete_id,
                telegram_chat_id=item.telegram_chat_id,
            )
        ):
            item.status, item.error = "superseded", "group_sharing_revoked"
            return True
        if not item.dedupe_key or not item.dedupe_key.startswith("morning:"):
            return False
        parts = item.dedupe_key.split(":")
        if len(parts) != 3:
            item.status, item.error = "superseded", "invalid_reminder_key"
            return True
        try:
            athlete_id = int(parts[1])
            local_day = datetime.fromisoformat(parts[2]).date()
        except ValueError:
            item.status, item.error = "superseded", "invalid_reminder_key"
            return True
        athlete = await session.get(Athlete, athlete_id)
        preference = await session.get(TrainingPreference, athlete_id)
        if (
            athlete is None
            or not athlete.active
            or preference is None
            or preference.morning_time is None
            or preference.private_chat_id != item.telegram_chat_id
        ):
            item.status, item.error = "superseded", "reminder_opt_out"
            return True
        try:
            current_day = datetime.now(UTC).astimezone(ZoneInfo(athlete.timezone)).date()
        except ZoneInfoNotFoundError:
            item.status, item.error = "superseded", "invalid_reminder_timezone"
            return True
        if current_day != local_day:
            item.status, item.error = "superseded", "stale_reminder_day"
            return True
        plan = await latest_session_plan(session, athlete_id=athlete_id, day=local_day)
        if plan is None or plan.status != "active":
            item.status, item.error = "superseded", "stale_reminder_plan"
            return True
        queued_plan_id = (item.payload or {}).get("reminder_plan_id")
        if queued_plan_id != plan.id:
            previous = queued_plan_id
            item.text = "План на сегодня:\n\n" + render_training_plan(plan)
            item.payload = {
                **(item.payload or {}),
                "reminder_plan_id": plan.id,
                "reminder_replaced_from": previous,
                "buttons": [
                    [
                        {"text": "Открыть сессию", "data": f"train:session:{plan.id}"},
                        {"text": "Начать лог", "data": f"train:log:{plan.id}"},
                    ]
                ],
            }
        return False

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
