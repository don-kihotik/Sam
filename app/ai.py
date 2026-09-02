from __future__ import annotations

import base64
import mimetypes
from typing import Any

from openai import AsyncOpenAI

from app.config import Settings
from app.prompts import (
    COACH_ARTIFACTS_SYSTEM_PROMPT,
    EXTRACTION_SYSTEM_PROMPT,
    SAM_SYSTEM_PROMPT,
    TRANSCRIPTION_PROMPT,
    VIDEO_ANALYSIS_SYSTEM_PROMPT,
)
from app.schemas import CoachArtifacts, MessageExtraction


class AIService:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None):
        self.settings = settings
        self.client = client or AsyncOpenAI(api_key=settings.openai_api_key)

    async def extract(
        self,
        *,
        text: str,
        athlete_name: str,
        local_date: str,
        timezone_name: str,
        recent_context: str,
        directly_addressed: bool,
    ) -> MessageExtraction:
        user_input = (
            f"AUTHOR: {athlete_name}\nLOCAL_DATE: {local_date}\nTIMEZONE: {timezone_name}\n"
            f"DIRECTLY_ADDRESSED: {directly_addressed}\n\nRECENT CONTEXT:\n{recent_context}\n\n"
            f"CURRENT MESSAGE:\n{text}"
        )
        response = await self.client.responses.parse(
            model=self.settings.extraction_model,
            instructions=EXTRACTION_SYSTEM_PROMPT,
            input=user_input,
            text_format=MessageExtraction,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("Extraction model returned no parsed result")
        return response.output_parsed

    async def coach(self, *, context: str, safety_identifier: str) -> str:
        response = await self.client.responses.create(
            model=self.settings.sam_model,
            instructions=SAM_SYSTEM_PROMPT,
            input=context,
            store=False,
            safety_identifier=safety_identifier[:64],
        )
        text = response.output_text.strip()
        if not text:
            raise RuntimeError("Coaching model returned an empty response")
        return text

    async def extract_coach_artifacts(self, text: str) -> CoachArtifacts:
        response = await self.client.responses.parse(
            model=self.settings.extraction_model,
            instructions=COACH_ARTIFACTS_SYSTEM_PROMPT,
            input=text,
            text_format=CoachArtifacts,
            store=False,
        )
        if response.output_parsed is None:
            raise RuntimeError("Coach artifact extraction returned no parsed result")
        return response.output_parsed

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        result = await self.client.audio.transcriptions.create(
            model=self.settings.transcription_model,
            file=(filename, audio, content_type),
            prompt=TRANSCRIPTION_PROMPT,
        )
        return result.text.strip()

    async def analyze_video_frames(
        self,
        frames: list[bytes],
        *,
        caption: str = "",
        transcript: str = "",
        safety_identifier: str = "video",
    ) -> str:
        if not frames:
            raise ValueError("At least one video frame is required")

        context = [
            "Ниже идут равномерно выбранные кадры в хронологическом порядке.",
            f"Подпись пользователя: {caption or '[нет]'}",
            f"Расшифровка звука: {transcript or '[нет или неразборчиво]'}",
        ]
        content: list[dict[str, Any]] = [{"type": "input_text", "text": "\n".join(context)}]
        for index, frame in enumerate(frames, start=1):
            encoded = base64.b64encode(frame).decode("ascii")
            content.extend(
                [
                    {"type": "input_text", "text": f"Кадр {index} из {len(frames)}"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/jpeg;base64,{encoded}",
                        "detail": "high",
                    },
                ]
            )

        response = await self.client.responses.create(
            model=self.settings.sam_model,
            instructions=VIDEO_ANALYSIS_SYSTEM_PROMPT,
            input=[{"role": "user", "content": content}],
            store=False,
            safety_identifier=safety_identifier[:64],
        )
        text = response.output_text.strip()
        if not text:
            raise RuntimeError("Video analysis model returned an empty response")
        return text

    async def embed(self, text: str) -> list[float] | None:
        if not self.settings.embeddings_enabled or not text.strip():
            return None
        result = await self.client.embeddings.create(
            model=self.settings.embedding_model,
            input=text,
            dimensions=self.settings.embedding_dimensions,
        )
        return result.data[0].embedding


class FakeAIService:
    """Small test double accepted by MessageProcessor."""

    def __init__(self, extraction: MessageExtraction, reply: str = "Норм."):
        self.extraction = extraction
        self.reply = reply
        self.coach_contexts: list[str] = []

    async def extract(self, **_: Any) -> MessageExtraction:
        return self.extraction

    async def coach(self, *, context: str, safety_identifier: str) -> str:
        self.coach_contexts.append(context)
        return self.reply

    async def extract_coach_artifacts(self, text: str) -> CoachArtifacts:
        return CoachArtifacts()

    async def transcribe(self, audio: bytes, *, filename: str = "voice.ogg") -> str:
        return "тестовая тренировка"

    async def analyze_video_frames(self, frames: list[bytes], **_: Any) -> str:
        return "На кадрах видно спокойное лазание; точная динамика между кадрами не видна."

    async def embed(self, text: str) -> None:
        return None
