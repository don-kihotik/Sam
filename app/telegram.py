from __future__ import annotations

import asyncio
import html
import logging
import re
from io import BytesIO

from aiogram import Bot, Dispatcher, Router
from aiogram.enums import ParseMode
from aiogram.types import Message as TelegramMessage

from app.ai import AIService
from app.config import Settings
from app.db.session import Database
from app.services import IncomingMessage, MessageProcessor, is_directly_addressed

logger = logging.getLogger(__name__)

_BOLD_PATTERN = re.compile(r"\*\*([^*\n]+)\*\*")


def format_telegram_reply(text: str) -> str:
    """Render Sam's limited Markdown safely as Telegram HTML."""
    escaped = html.escape(text)
    return _BOLD_PATTERN.sub(r"<b>\1</b>", escaped)


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
        if not (message.text or message.voice):
            return

        if message.text and message.text.split("@", 1)[0].strip().lower() == "/whoami":
            await message.reply(f"Твой Telegram user ID: {message.from_user.id}")
            return

        text = message.text or ""
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

        is_reply = bool(
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == self._bot_id
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
