from __future__ import annotations

import asyncio
import html
import logging
import re
from io import BytesIO

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.types import Message as TelegramMessage
from aiogram.types import Video, VideoNote

from app.ai import AIService
from app.config import Settings
from app.db.session import Database
from app.services import IncomingMessage, MessageProcessor, is_directly_addressed
from app.video import extract_video_frames

logger = logging.getLogger(__name__)

_BOLD_PATTERN = re.compile(r"\*\*([^*\n]+)\*\*")


def format_telegram_reply(text: str) -> str:
    """Render Sam's limited Markdown safely as Telegram HTML."""
    escaped = html.escape(text)
    return _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)


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
        "Автоматические наблюдения по выбранным кадрам, а не по непрерывному видео:\n"
        f"{analysis}"
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

    async def stop(self) -> None:
        await self.dispatcher.stop_polling()
        if self._task:
            await self._task
        await self.bot.session.close()

    async def _handle_message(self, message: TelegramMessage) -> None:
        if message.chat.id != self.settings.telegram_allowed_chat_id:
            logger.warning("Ignoring update from non-allowlisted chat %s", message.chat.id)
            return
        if message.from_user is None or message.from_user.is_bot:
            return
        video, video_message = find_video(message)
        if not (message.text or message.caption or message.voice or video):
            return

        if message.text and message.text.split("@", 1)[0].strip().lower() == "/whoami":
            await message.reply(f"Твой Telegram user ID: {message.from_user.id}")
            return

        text = message.text or message.caption or ""
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
            known_user_ids = {
                user_id
                for user_id in (
                    self.settings.alexey_telegram_user_id,
                    self.settings.andrey_telegram_user_id,
                )
                if user_id is not None
            }
            if message.from_user.id not in known_user_ids:
                return

            message_type = "video_note" if video_message.video_note else "video"
            buffer = BytesIO()
            try:
                await self.bot.download(video, destination=buffer)
                video_bytes = buffer.getvalue()
            except Exception:
                logger.exception("Telegram video download failed")
                await message.reply("Не смог скачать видео. Попробуй отправить его ещё раз.")
                return

            filename = getattr(video, "file_name", None) or f"{message_type}.mp4"
            duration = getattr(video, "duration", None)
            attachment = {
                "file_id": video.file_id,
                "file_unique_id": video.file_unique_id,
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
            except Exception:
                logger.exception("Video frame analysis failed")
                await message.reply(
                    "Не смог нормально разобрать кадры из видео. Попробуй другой файл или ракурс."
                )
                return

            attachment["analyzed_frame_count"] = len(frames)
            attachment["vision_analysis"] = analysis
            text = compose_video_context(request=text, transcript=transcript, analysis=analysis)

        is_reply_to_bot = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == self._bot_id
        )
        is_reply = bool(video or is_reply_to_bot)
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
            is_reply_to_sam=is_reply,
        )
        addressed = is_directly_addressed(text, is_reply_to_sam=is_reply)
        try:
            async with self.database.sessions() as session:
                result = await self.processor.process(session, incoming)
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
        except Exception:
            logger.exception(
                "Message processing failed for Telegram message %s", message.message_id
            )
            if addressed:
                await message.reply(
                    "Не обработал это сообщение — записал ошибку. Попробуй ещё раз чуть позже."
                )
