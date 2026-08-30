def test_whoami_command_normalization():
    messages = ["/whoami", "/WHOAMI", "/whoami@samclimbs_bot"]
    assert all(text.split("@", 1)[0].strip().lower() == "/whoami" for text in messages)
