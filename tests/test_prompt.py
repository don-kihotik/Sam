from app.prompts import (
    EXTRACTION_SYSTEM_PROMPT,
    SAM_SYSTEM_PROMPT,
    TRANSCRIPTION_PROMPT,
    VIDEO_ANALYSIS_SYSTEM_PROMPT,
)


def test_persona_prompt_has_core_guardrails():
    assert "не путай Алексея с Андреем" in SAM_SYSTEM_PROMPT
    assert "Не хвали автоматически" in SAM_SYSTEM_PROMPT
    assert "Ты не врач" in SAM_SYSTEM_PROMPT
    assert "2–6 предложениях" in SAM_SYSTEM_PROMPT
    assert "Не используй эмодзи" in SAM_SYSTEM_PROMPT
    assert "маркером «•»" in SAM_SYSTEM_PROMPT
    assert "биомеханика" in SAM_SYSTEM_PROMPT
    assert "Кадры идут по времени" in VIDEO_ANALYSIS_SYSTEM_PROMPT
    assert "не оценивай силу хвата" in VIDEO_ANALYSIS_SYSTEM_PROMPT


def test_voice_and_extraction_prompts_cover_climbing_vocabulary_and_numeric_guardrail():
    assert "Сэм" in TRANSCRIPTION_PROMPT
    assert "автостраховка" in TRANSCRIPTION_PROMPT
    assert "не превращай качественные слова" in EXTRACTION_SYSTEM_PROMPT
    assert "3 по 5.8 и 2 по 5.9" in EXTRACTION_SYSTEM_PROMPT
