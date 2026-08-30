from app.prompts import SAM_SYSTEM_PROMPT


def test_persona_prompt_has_core_guardrails():
    assert "не путай Алексея с Андреем" in SAM_SYSTEM_PROMPT
    assert "Не хвали автоматически" in SAM_SYSTEM_PROMPT
    assert "Ты не врач" in SAM_SYSTEM_PROMPT
    assert "2–6 предложениях" in SAM_SYSTEM_PROMPT
    assert "Не используй эмодзи" in SAM_SYSTEM_PROMPT
    assert "маркером «•»" in SAM_SYSTEM_PROMPT
