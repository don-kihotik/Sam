from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.ai import AIService
from app.prompts import TRANSCRIPTION_PROMPT


async def test_transcription_uses_climbing_vocabulary_prompt(settings):
    client = MagicMock()
    client.audio.transcriptions.create = AsyncMock(
        return_value=SimpleNamespace(text="  Сэм, сегодня лазал.  ")
    )
    service = AIService(settings, client=client)

    result = await service.transcribe(b"audio", filename="voice.ogg")

    assert result == "Сэм, сегодня лазал."
    request = client.audio.transcriptions.create.await_args.kwargs
    assert request["prompt"] == TRANSCRIPTION_PROMPT
    assert request["file"] == ("voice.ogg", b"audio", "audio/ogg")
