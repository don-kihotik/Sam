"""Conservative, source-scoped readiness signals.

This module deliberately records no medical conclusion.  It only answers whether
an allowed, recent message from the athlete has explicitly reported an illness
without a later, explicit full-recovery statement in the same chat.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message

_RU_ILLNESS = re.compile(
    r"\b(?:приболел(?:а)?|болею|заболел(?:а)?|простудил(?:ся|ась))\b",
    re.IGNORECASE,
)
_EN_ILLNESS = re.compile(
    r"\bi\s+(?:(?:am|was)\s+)?(?:ill|sick)|\bi['’]m\s+(?:ill|sick)|"
    r"\bi\s+(?:got|have|caught)\s+(?:a\s+)?(?:cold|flu|sick)|"
    r"\bi\s+came\s+down\s+with\s+(?:a\s+)?(?:cold|flu)",
    re.IGNORECASE,
)
_RU_RECOVERY = re.compile(
    r"\b(?:полностью\s+выздоровел(?:а)?|поправил(?:ся|ась)|полностью\s+здоров(?:а)?)\b",
    re.IGNORECASE,
)
_EN_RECOVERY = re.compile(r"\bi\s+(?:am\s+)?fully\s+recovered\b|\bi['’]m\s+fully\s+recovered\b", re.IGNORECASE)
_NEGATED_RECOVERY = re.compile(
    r"\b(?:не|not|haven['’]t|hasn['’]t)\b", re.IGNORECASE
)
_FIRST_PERSON_FILLERS = r"(?:правда|немного|чуть|слегка|совсем|недавно|кажется|видимо)"
_HYPOTHETICAL_PREFIX = re.compile(
    r"\b(?:если|когда|пока|как\s+только|if|when|once|until)\b", re.IGNORECASE
)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _is_allowed(message: Message) -> bool:
    decision = (message.processing_data or {}).get("request_policy", {}).get("decision")
    return decision in {None, "allow"}


def _text(message: Message) -> str:
    return message.raw_text or message.transcript or message.normalized_text or ""


def _is_sentence_initial(text: str, start: int) -> bool:
    """Allow Russian omitted-pronoun forms only at a sentence start.

    This permits a direct report such as ``Приболел`` while rejecting
    ``Андрей приболел``.  Elsewhere we require the explicit ``я`` form.
    """
    prefix = text[:start].rstrip()
    return not prefix or prefix[-1] in ".!\n"


def _sentence_before(text: str, start: int) -> str:
    """Return text in the current clause before a matched statement."""
    return text[_sentence_start(text, start) : start]


def _sentence_start(text: str, start: int) -> int:
    return max(text.rfind(mark, 0, start) for mark in ".!?\n") + 1


def _is_question_clause(text: str, start: int) -> bool:
    """Only reject a question containing the matched statement, not later ones."""
    end = len(text)
    for mark in ".!?\n":
        found = text.find(mark, start)
        if found != -1:
            end = min(end, found + 1)
    return "?" in text[_sentence_start(text, start) : end]


def _direct_first_person(text: str, start: int) -> bool:
    """Recognize ``я`` with a short filler run, never a peer's report."""
    before = _sentence_before(text, start)
    if _HYPOTHETICAL_PREFIX.search(before):
        return False
    return bool(
        re.search(
            rf"\bя\b(?:\s*[,—-]?\s*{_FIRST_PERSON_FILLERS}){{0,4}}\s*[,—-]?\s*$",
            before,
            re.IGNORECASE,
        )
    )


def _illness_report(text: str) -> bool:
    for match in _RU_ILLNESS.finditer(text):
        if _is_question_clause(text, match.start()):
            continue
        if _direct_first_person(text, match.start()) or _is_sentence_initial(text, match.start()):
            return True
    return any(
        not _is_question_clause(text, match.start())
        and not _HYPOTHETICAL_PREFIX.search(_sentence_before(text, match.start()))
        for match in _EN_ILLNESS.finditer(text)
    )


def _recovery_report(text: str) -> bool:
    for match in _RU_RECOVERY.finditer(text):
        if _is_question_clause(text, match.start()):
            continue
        before = text[max(0, match.start() - 24) : match.start()]
        if _NEGATED_RECOVERY.search(before):
            continue
        if _direct_first_person(text, match.start()) or _is_sentence_initial(text, match.start()):
            return True
    for match in _EN_RECOVERY.finditer(text):
        if _is_question_clause(text, match.start()):
            continue
        before = text[max(0, match.start() - 24) : match.start()]
        if not _NEGATED_RECOVERY.search(before) and not _HYPOTHETICAL_PREFIX.search(
            _sentence_before(text, match.start())
        ):
            return True
    return False


async def recent_illness_unresolved(
    session: AsyncSession,
    athlete_id: int,
    chat_id: int,
    now: datetime,
) -> bool:
    """Return a bounded, conservative illness-context flag for one athlete/chat.

    Only incoming messages belonging to the athlete, in this chat, from the last
    seven days through ``now`` are considered.  A later explicit recovery clears
    an earlier report; absence of a report never implies recovery.
    """
    current = _utc(now)
    cutoff = current - timedelta(days=7)
    rows = (
        await session.scalars(
            select(Message)
            .where(
                Message.athlete_id == athlete_id,
                Message.telegram_chat_id == chat_id,
                Message.direction == "incoming",
                Message.telegram_timestamp >= cutoff,
                Message.telegram_timestamp <= current,
            )
            .order_by(Message.telegram_timestamp, Message.id)
        )
    ).all()

    unresolved = False
    for message in rows:
        # SQLite can return a naive timestamp, so retain the time window after
        # loading as well as in SQL.
        stamp = _utc(message.telegram_timestamp)
        if not cutoff <= stamp <= current or not _is_allowed(message):
            continue
        text = _text(message)
        if _illness_report(text):
            unresolved = True
        elif _recovery_report(text):
            unresolved = False
    return unresolved
