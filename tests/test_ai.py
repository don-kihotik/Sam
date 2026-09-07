from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from openai.lib._pydantic import to_strict_json_schema

from app.ai import AIService
from app.prompts import TRANSCRIPTION_PROMPT
from app.schemas import MessageExtraction


def test_extraction_schema_contains_only_strict_objects():
    schema = to_strict_json_schema(MessageExtraction)

    def assert_strict_objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                assert_strict_objects(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict_objects(value)

    assert_strict_objects(schema)


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
