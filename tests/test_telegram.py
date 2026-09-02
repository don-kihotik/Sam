from aiogram.enums import ChatType

from app.telegram import compose_video_context, format_telegram_reply, is_allowed_chat


def test_whoami_command_normalization():
    messages = ["/whoami", "/WHOAMI", "/whoami@samclimbs_bot"]
    assert all(text.split("@", 1)[0].strip().lower() == "/whoami" for text in messages)


def test_format_telegram_reply_renders_limited_bold_and_escapes_html():
    text = "План:\n\n• **Пн** — V1 < V2\n• **Вт** — отдых & сон"

    assert format_telegram_reply(text) == (
        "План:\n\n• <b>Пн</b> — V1 &lt; V2\n• <b>Вт</b> — отдых &amp; сон"
    )


def test_format_telegram_reply_leaves_unmatched_markers_as_text():
    assert format_telegram_reply("Не закрытый **маркер") == "Не закрытый **маркер"


def test_compose_video_context_labels_sampled_frames_honestly():
    result = compose_video_context(
        request="Сэм, разбери технику",
        transcript="На этом движении неудобно.",
        analysis="На трёх кадрах корпус далеко от стены.",
    )

    assert "Сэм, разбери технику" in result
    assert "Расшифровка звука" in result
    assert "не по непрерывному видео" in result
    assert "корпус далеко от стены" in result


def test_registered_athlete_can_use_private_chat(settings):
    assert is_allowed_chat(
        settings,
        chat_id=101,
        chat_type=ChatType.PRIVATE,
        user_id=101,
    )


def test_unknown_user_cannot_use_private_chat(settings):
    assert not is_allowed_chat(
        settings,
        chat_id=999,
        chat_type=ChatType.PRIVATE,
        user_id=999,
    )


def test_configured_group_remains_allowed(settings):
    assert is_allowed_chat(
        settings,
        chat_id=-100123,
        chat_type=ChatType.SUPERGROUP,
        user_id=999,
    )
