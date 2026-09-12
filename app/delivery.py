"""Bound Telegram replies by UTF-16 length without cutting formatting markers."""

import re


def split_reply(text: str, limit: int = 3500) -> list[str]:
    # Long replies use plain text, avoiding unmatched bold across split boundaries.
    if len(text.encode("utf-16-le")) // 2 <= limit:
        return [text]
    text = text.replace("**", "")
    parts, current = [], ""
    for token in re.findall(r"\S+\s*|\s+", text):
        if len((current + token).encode("utf-16-le")) // 2 > limit:
            if current.strip():
                parts.append(current.rstrip())
                current = ""
            while len(token.encode("utf-16-le")) // 2 > limit:
                cut = limit // 2
                parts.append(token[:cut])
                token = token[cut:]
        current += token
    if current.strip():
        parts.append(current.rstrip())
    return parts
