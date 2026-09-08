from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import UTC, datetime
from io import BytesIO

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message as TelegramMessage
from aiogram.types import Video, VideoNote

from app.ai import AIService
from app.config import Settings
from app.db.session import Database
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
    return bool(
        is_directly_addressed(text)
        or _VIDEO_ANALYSIS_REQUEST.search(text)
    )


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
    if is_directly_addressed(
        text, allow_transcription_aliases=allow_transcription_aliases
    ):
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


class TelegramRuntime:
    def __init__(self, settings: Settings, database: Database, ai: AIService):
        self.settings = settings
        self.database = database
        self.bot = Bot(settings.telegram_bot_token)
        self.dispatcher = Dispatcher()
        self.router = Router()
        self.processor = MessageProcessor(settings, ai)
        self._task: asyncio.Task | None = None
        self._bot_id: int | None = None
        self.last_update_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self.last_media_error: str | None = None
        self.router.message.register(self._handle_message)
        self.dispatcher.include_router(self.router)

    async def start(self) -> None:
        me = await self.bot.get_me()
        self._bot_id = me.id
        logger.info("Starting Telegram polling as @%s", me.username)
        self._task = asyncio.create_task(
            self.dispatcher.start_polling(self.bot, handle_signals=False),
            name="telegram-polling",
        )
        self._task.add_done_callback(self._polling_finished)

    @property
    def polling_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_details(self) -> dict[str, str | bool | None]:
        return {
            "polling_running": self.polling_running,
            "last_update_at": self.last_update_at.isoformat() if self.last_update_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
            "last_media_error": self.last_media_error,
        }

    def _polling_finished(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.last_error = f"polling: {type(error).__name__}: {error}"
            logger.error("Telegram polling stopped: %s", error)

    async def stop(self) -> None:
        await self.dispatcher.stop_polling()
        if self._task:
            await self._task
        await self.bot.session.close()

    async def _record_media_outcome(
        self,
        message: TelegramMessage,
        *,
        text: str,
        message_type: str,
        attachment: dict,
        status: str,
        reason: str,
        transcript: str | None = None,
    ) -> None:
        if message.from_user is None:
            return
        incoming = IncomingMessage(
            telegram_message_id=message.message_id,
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user.id,
            timestamp=message.date,
            text=text,
            transcript=transcript,
            message_type=message_type,
            reply_to_message_id=(
                message.reply_to_message.message_id if message.reply_to_message else None
            ),
            attachment_metadata=attachment,
        )
        async with self.database.sessions() as session:
            await self.processor.record_unprocessed(
                session,
                incoming,
                status=status,
                reason=reason,
            )

    async def _handle_message(self, message: TelegramMessage) -> None:
        if message.from_user is None or message.from_user.is_bot:
            return
        if not is_allowed_chat(
            self.settings,
            chat_id=message.chat.id,
            chat_type=message.chat.type,
            user_id=message.from_user.id,
        ):
            logger.warning("Ignoring update from non-allowlisted chat %s", message.chat.id)
            return
        video, video_message = find_video(message)
        if not (message.text or message.caption or message.voice or video):
            return
        self.last_update_at = datetime.now(UTC)

        if message.text and message.text.split("@", 1)[0].strip().lower() == "/whoami":
            await message.reply(f"Твой Telegram user ID: {message.from_user.id}")
            return

        text = message.text or message.caption or ""
        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == self._bot_id
        )
        current_message_has_video = bool(message.video or message.video_note)
        video_requested = bool(video and should_process_video(text, chat_type=message.chat.type))
        if video and not video_requested:
            if current_message_has_video:
                message_type = "video_note" if message.video_note else "video"
                attachment = {
                    "file_id": video.file_id,
                    "file_unique_id": video.file_unique_id,
                    "telegram_file_size_bytes": getattr(video, "file_size", None),
                    "duration_seconds": getattr(video, "duration", None),
                    "media_status": "ignored_not_requested",
                }
                await self._record_media_outcome(
                    message,
                    text=text,
                    message_type=message_type,
                    attachment=attachment,
                    status="completed",
                    reason="video_ignored_not_requested",
                )
                self.last_success_at = datetime.now(UTC)
                return
            # A casual text reply to somebody else's video remains a normal text message.
            video = None

        transcript = None
        attachment: dict = {}
        message_type = "text"
        if message.voice:
            message_type = "voice"
            buffer = BytesIO()
            await self.bot.download(message.voice, destination=buffer)
            audio = buffer.getvalue()
            attachment = {
                "file_id": message.voice.file_id,
                "file_unique_id": message.voice.file_unique_id,
                "duration_seconds": message.voice.duration,
                "mime_type": message.voice.mime_type,
                "size_bytes": len(audio),
                "transcription_model": self.settings.transcription_model,
            }
            try:
                transcript = await self.processor.ai.transcribe(audio)
                text = transcript
            except Exception:
                logger.exception("Voice transcription failed")
                await message.reply(
                    "Не смог нормально разобрать голосовое. Можешь повторить текстом?"
                )
                return

        if video:
            if message.from_user.id not in known_user_ids(self.settings):
                return

            message_type = "video_note" if video_message.video_note else "video"
            telegram_file_size = getattr(video, "file_size", None)
            attachment = {
                "file_id": video.file_id,
                "file_unique_id": video.file_unique_id,
                "telegram_file_size_bytes": telegram_file_size,
                "duration_seconds": getattr(video, "duration", None),
                "source_message_id": video_message.message_id,
            }
            if telegram_file_size and telegram_file_size > TELEGRAM_DOWNLOAD_LIMIT_BYTES:
                size_mb = telegram_file_size / (1024 * 1024)
                reason = (
                    f"telegram_file_too_large: {telegram_file_size} bytes exceeds "
                    f"{TELEGRAM_DOWNLOAD_LIMIT_BYTES} byte Bot API limit"
                )
                attachment["media_status"] = "failed_too_large"
                await self._record_media_outcome(
                    message,
                    text=text,
                    message_type=message_type,
                    attachment=attachment,
                    status="failed",
                    reason=reason,
                )
                self.last_media_error = reason
                await message.reply(
                    f"Видео весит {size_mb:.1f} MB, а Telegram отдаёт обычному боту "
                    "файлы только до 20 MB. Пришли более короткую или сжатую версию."
                )
                return

            buffer = BytesIO()
            try:
                await self.bot.download(video, destination=buffer)
                video_bytes = buffer.getvalue()
            except Exception as exc:
                reason = f"telegram_video_download_failed: {type(exc).__name__}: {exc}"
                attachment["media_status"] = "failed_download"
                await self._record_media_outcome(
                    message,
                    text=text,
                    message_type=message_type,
                    attachment=attachment,
                    status="failed",
                    reason=reason,
                )
                self.last_media_error = reason
                logger.exception(
                    "Telegram video download failed: message=%s file_size=%s",
                    message.message_id,
                    telegram_file_size,
                )
                await message.reply(
                    "Не смог скачать запрошенное видео из Telegram. Ошибку и параметры файла "
                    "сохранил; повторная отправка нужна только после проверки причины."
                )
                return

            filename = getattr(video, "file_name", None) or f"{message_type}.mp4"
            duration = getattr(video, "duration", None)
            attachment = {
                **attachment,
                "duration_seconds": duration,
                "mime_type": getattr(video, "mime_type", None) or "video/mp4",
                "size_bytes": len(video_bytes),
                "source_message_id": video_message.message_id,
                "transcription_model": self.settings.transcription_model,
                "vision_model": self.settings.sam_model,
            }

            try:
                transcript = await self.processor.ai.transcribe(video_bytes, filename=filename)
            except Exception as exc:  # noqa: BLE001 - a silent video is still useful
                logger.info("Video audio transcription unavailable: %s", exc)
                attachment["transcription_status"] = "unavailable"

            try:
                frames = await extract_video_frames(
                    video_bytes,
                    duration_seconds=duration,
                    max_frames=self.settings.video_max_frames,
                )
                analysis = await self.processor.ai.analyze_video_frames(
                    frames,
                    caption=text,
                    transcript=transcript or "",
                    safety_identifier=f"telegram-video-{message.from_user.id}",
                )
            except Exception as exc:
                reason = f"video_frame_analysis_failed: {type(exc).__name__}: {exc}"
                attachment["media_status"] = "failed_analysis"
                await self._record_media_outcome(
                    message,
                    text=text,
                    message_type=message_type,
                    attachment=attachment,
                    status="failed",
                    reason=reason,
                    transcript=transcript,
                )
                self.last_media_error = reason
                logger.exception("Video frame analysis failed")
                await message.reply(
                    "Не смог нормально разобрать кадры из видео. Попробуй другой файл или ракурс."
                )
                return

            attachment["analyzed_frame_count"] = len(frames)
            attachment["vision_analysis"] = analysis
            attachment["media_status"] = "analyzed"
            self.last_media_error = None
            text = compose_video_context(request=text, transcript=transcript, analysis=analysis)

        addressed = should_force_response(
            text,
            chat_type=message.chat.type,
            is_reply_to_bot=is_reply_to_bot,
            allow_transcription_aliases=message_type == "voice",
            media_requested=video_requested,
        )
        incoming = IncomingMessage(
            telegram_message_id=message.message_id,
            telegram_chat_id=message.chat.id,
            telegram_user_id=message.from_user.id,
            timestamp=message.date,
            text=text,
            transcript=transcript,
            message_type=message_type,
            reply_to_message_id=(
                message.reply_to_message.message_id if message.reply_to_message else None
            ),
            attachment_metadata=attachment,
            is_reply_to_sam=addressed,
        )
        try:
            async with self.database.sessions() as session:
                result = await self.processor.process(session, incoming)
            self.last_success_at = datetime.now(UTC)
            self.last_error = None
            if result.reply and result.athlete_id:
                sent = await message.reply(
                    format_telegram_reply(result.reply),
                    parse_mode=ParseMode.HTML,
                )
                async with self.database.sessions() as session:
                    await self.processor.save_outgoing(
                        session,
                        telegram_message_id=sent.message_id,
                        telegram_chat_id=sent.chat.id,
                        athlete_id=result.athlete_id,
                        text=result.reply,
                        timestamp=sent.date,
                    )
        except Exception as exc:
            self.last_error = f"message {message.message_id}: {type(exc).__name__}: {exc}"
            logger.exception(
                "Message processing failed for Telegram message %s", message.message_id
            )
            if addressed:
                await message.reply(
                    "Не обработал это сообщение — записал ошибку. Попробуй ещё раз чуть позже."
                )
