from app.telegram import format_telegram_reply


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
