"""Recover and reconcile an explicitly selected historical screenshot batch.

The default is a read-only preview. ``--apply`` stages private CatalogSource
rows and requeues only matching terminal ``unsupported_media`` runs. It never
sends Telegram messages, creates workouts, or publishes protocols.

After OCR, ``--reconcile`` compares source titles with the versioned catalog.
Only an unambiguous normalized-title match is linked automatically; fuzzy
candidates remain review suggestions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import receive_catalog_source
from app.config import get_settings
from app.db.models import (
    Athlete,
    CatalogProtocol,
    CatalogSource,
    Message,
    Outbox,
    ProcessingRun,
    Workout,
)

RECOVERY_VERSION = "historical-catalog-v1"
RECONCILIATION_VERSION = "catalog-title-v1"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _photo_payload(message: Message) -> dict[str, Any] | None:
    payload = (message.attachment_data or {}).get("telegram_payload", {})
    photo = payload.get("photo") if isinstance(payload, dict) else None
    return payload if isinstance(photo, list) and photo else None


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))


def _title_score(source: str, candidate: str) -> float:
    left, right = _normalize_title(source), _normalize_title(candidate)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    left_tokens, right_tokens = set(left.split()), set(right.split())
    union = left_tokens | right_tokens
    token_score = len(left_tokens & right_tokens) / len(union) if union else 0.0
    return round(max(SequenceMatcher(None, left, right).ratio(), token_score), 4)


async def _selected_messages(
    session: AsyncSession, *, telegram_user_id: int, batch_date: date
) -> tuple[Athlete, list[Message]]:
    athlete = await session.scalar(
        select(Athlete).where(
            Athlete.telegram_user_id == telegram_user_id,
            Athlete.active.is_(True),
        )
    )
    if athlete is None:
        raise ValueError("Active athlete not found")
    rows = (
        await session.scalars(
            select(Message)
            .where(
                Message.athlete_id == athlete.id,
                Message.telegram_chat_id == telegram_user_id,
                Message.telegram_user_id == telegram_user_id,
                Message.direction == "incoming",
            )
            .order_by(Message.telegram_timestamp, Message.id)
        )
    ).all()
    timezone = ZoneInfo(athlete.timezone)
    messages = [
        message
        for message in rows
        if _aware(message.telegram_timestamp).astimezone(timezone).date() == batch_date
        and _photo_payload(message) is not None
    ]
    return athlete, messages


async def recover_batch(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    batch_date: date,
    apply: bool = False,
    manifest: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Preview or stage/requeue one owner's private photo batch, idempotently."""
    athlete, messages = await _selected_messages(
        session, telegram_user_id=telegram_user_id, batch_date=batch_date
    )
    message_ids = [message.id for message in messages]
    runs = {
        row.message_id: row
        for row in (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.message_id.in_(message_ids or [-1]))
            )
        ).all()
    }
    sources = {
        row.source_message_id: row
        for row in (
            await session.scalars(
                select(CatalogSource).where(CatalogSource.source_message_id.in_(message_ids or [-1]))
            )
        ).all()
    }
    manifest = manifest or {}
    outside = set(manifest) - set(message_ids)
    if outside:
        raise ValueError("Manifest includes sources outside the selected owner/date")

    albums = Counter()
    for message in messages:
        payload = _photo_payload(message) or {}
        albums[str(payload.get("media_group_id") or f"single:{message.id}")] += 1
    terminal = [
        message
        for message in messages
        if (run := runs.get(message.id)) is not None
        and run.status == "failed"
        and run.error == "unsupported_media"
    ]
    before = {
        "workouts": await session.scalar(select(func.count()).select_from(Workout)),
        "outbox": await session.scalar(select(func.count()).select_from(Outbox)),
    }
    staged = requeued = mapped = deduplicated = 0
    if apply:
        for message in messages:
            source = sources.get(message.id)
            if source is None:
                source = await receive_catalog_source(session, message=message)
                sources[message.id] = source
                if source.source_message_id == message.id:
                    staged += 1
                else:
                    deduplicated += 1
            if message.id in manifest:
                protocol = await session.scalar(
                    select(CatalogProtocol)
                    .where(CatalogProtocol.slug == manifest[message.id])
                    .order_by(CatalogProtocol.version.desc())
                    .limit(1)
                )
                if protocol is None:
                    raise ValueError("Manifest references missing catalog protocol")
                if source.protocol_id != protocol.id:
                    source.protocol_id = protocol.id
                    mapped += 1
            run = runs.get(message.id)
            if (
                run is not None
                and run.status == "failed"
                and run.error == "unsupported_media"
                and source.status == "received"
            ):
                recovery = (message.processing_data or {}).get("catalog_recovery", {})
                message.processing_data = {
                    **(message.processing_data or {}),
                    "catalog_recovery": {
                        **recovery,
                        "version": RECOVERY_VERSION,
                        "batch_date": batch_date.isoformat(),
                        "original_status": recovery.get("original_status", run.status),
                        "original_error": recovery.get("original_error", run.error),
                        "staged_at": recovery.get("staged_at", datetime.now(UTC).isoformat()),
                    },
                }
                run.status = "received"
                run.attempts = 0
                run.next_attempt_at = None
                run.error = None
                requeued += 1
        await session.flush()
        after = {
            "workouts": await session.scalar(select(func.count()).select_from(Workout)),
            "outbox": await session.scalar(select(func.count()).select_from(Outbox)),
        }
        if after != before:
            raise RuntimeError("Catalog recovery changed workouts or Telegram outbox")

    status_counts = Counter(
        sources[message.id].status if message.id in sources else "not_staged"
        for message in messages
    )
    return {
        "athlete_id": athlete.id,
        "batch_date": batch_date.isoformat(),
        "screenshots": len(messages),
        "albums": len(albums),
        "album_sizes": sorted(albums.values(), reverse=True),
        "terminal_unsupported_media": len(terminal),
        "source_statuses": dict(sorted(status_counts.items())),
        "newly_staged": staged,
        "deduplicated_messages": deduplicated,
        "requeued": requeued,
        "manifest_links": mapped,
        "applied": apply,
        "workouts_changed": 0,
        "messages_sent": 0,
    }


async def reconcile_batch(
    session: AsyncSession,
    *,
    telegram_user_id: int,
    batch_date: date,
    apply: bool = False,
) -> dict[str, Any]:
    """Classify OCR sources against the catalog without inventing protocols."""
    _athlete, messages = await _selected_messages(
        session, telegram_user_id=telegram_user_id, batch_date=batch_date
    )
    message_ids = [message.id for message in messages]
    sources = (
        await session.scalars(
            select(CatalogSource)
            .where(CatalogSource.source_message_id.in_(message_ids or [-1]))
            .order_by(CatalogSource.id)
        )
    ).all()
    catalog_rows = (
        await session.scalars(
            select(CatalogProtocol).order_by(
                CatalogProtocol.slug, CatalogProtocol.version.desc()
            )
        )
    ).all()
    latest: dict[str, CatalogProtocol] = {}
    for protocol in catalog_rows:
        latest.setdefault(protocol.slug, protocol)
    protocols = list(latest.values())
    exact: dict[str, list[CatalogProtocol]] = {}
    for protocol in protocols:
        exact.setdefault(_normalize_title(protocol.title), []).append(protocol)

    exact_by_source: dict[int, list[CatalogProtocol]] = {}
    album_positions: dict[str, list[tuple[int, CatalogSource]]] = {}
    for position, source in enumerate(sources):
        extracted = source.extracted or {}
        title = extracted.get("title")
        exact_by_source[source.id] = (
            exact.get(_normalize_title(title), []) if isinstance(title, str) else []
        )
        if source.album_id:
            album_positions.setdefault(source.album_id, []).append((position, source))

    counts = Counter()
    screen_types = Counter()
    examples: dict[str, list[str]] = {}
    linked = 0
    for position, source in enumerate(sources):
        extracted = source.extracted or {}
        title = extracted.get("title")
        screen_type = extracted.get("screen_type")
        screen_types[str(screen_type or "missing")] += 1
        candidates: list[dict[str, Any]] = []
        matches = exact_by_source[source.id]
        if screen_type == "unreadable":
            status = "unreadable"
        elif screen_type == "category_list":
            # An index is useful source evidence, but is not itself a protocol.
            status = "reference_only"
        elif len(matches) == 1:
            status = "matched_existing"
            candidates = [{"protocol_id": matches[0].id, "score": 1.0}]
        elif len(matches) > 1:
            status = "conflict"
            candidates = [{"protocol_id": item.id, "score": 1.0} for item in matches]
        else:
            if isinstance(title, str) and title.strip():
                ranked = sorted(
                    ((_title_score(title, protocol.title), protocol) for protocol in protocols),
                    key=lambda item: item[0],
                    reverse=True,
                )[:3]
                candidates = [
                    {"protocol_id": protocol.id, "score": score, "evidence": "title"}
                    for score, protocol in ranked
                    if score >= 0.45
                ]
            if candidates and candidates[0]["score"] >= 0.82:
                status = "possible_match"
            else:
                # Album adjacency is only a review hint. It never creates a
                # protocol link because one Telegram album may hold many cards.
                anchors: list[tuple[int, CatalogProtocol]] = []
                for anchor_position, anchor in album_positions.get(source.album_id or "", []):
                    anchor_matches = exact_by_source[anchor.id]
                    if len(anchor_matches) == 1 and anchor.id != source.id:
                        anchors.append((abs(position - anchor_position), anchor_matches[0]))
                anchors.sort(key=lambda item: item[0])
                if anchors and anchors[0][0] <= 2 and (
                    len(anchors) == 1 or anchors[1][0] != anchors[0][0]
                ):
                    status = "supplements_existing"
                    candidates = [
                        {
                            "protocol_id": anchors[0][1].id,
                            "score": None,
                            "evidence": "album_adjacency",
                            "distance": anchors[0][0],
                        }
                    ]
                elif not isinstance(title, str) or not title.strip() or screen_type == "other":
                    status = "needs_context"
                else:
                    status = "new_candidate"
        counts[status] += 1
        label = title.strip() if isinstance(title, str) and title.strip() else f"source:{source.id}"
        examples.setdefault(status, [])
        if len(examples[status]) < 8:
            examples[status].append(label)
        reconciliation = {
            "version": RECONCILIATION_VERSION,
            "status": status,
            "candidates": candidates,
        }
        if apply:
            source.extracted = {**extracted, "reconciliation": reconciliation}
            if status == "matched_existing" and source.protocol_id is None:
                source.protocol_id = candidates[0]["protocol_id"]
                linked += 1
    return {
        "batch_date": batch_date.isoformat(),
        "screenshots": len(messages),
        "staged_sources": len(sources),
        "duplicate_messages": len(messages) - len(sources),
        "awaiting_ocr": sum(source.status == "received" for source in sources),
        "screen_types": dict(sorted(screen_types.items())),
        "classifications": dict(sorted(counts.items())),
        "examples": dict(sorted(examples.items())),
        "exact_links_added": linked,
        "applied": apply,
    }


def _manifest(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    mapping: dict[int, str] = {}
    for entry in json.loads(path.read_text(encoding="utf-8")):
        for source_id in entry["message_ids"]:
            if source_id in mapping:
                raise ValueError("Duplicate source in manifest")
            mapping[source_id] = entry["slug"]
    return mapping


async def import_batch(args: argparse.Namespace) -> dict[str, Any]:
    from app.db.session import Database

    settings = get_settings()
    telegram_user_id = args.telegram_user_id or settings.alexey_telegram_user_id
    if telegram_user_id is None:
        raise ValueError("--telegram-user-id or ALEXEY_TELEGRAM_USER_ID is required")
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            if args.reconcile:
                result = await reconcile_batch(
                    session,
                    telegram_user_id=telegram_user_id,
                    batch_date=args.date,
                    apply=args.apply,
                )
            else:
                result = await recover_batch(
                    session,
                    telegram_user_id=telegram_user_id,
                    batch_date=args.date,
                    apply=args.apply,
                    manifest=_manifest(args.manifest),
                )
            if args.apply:
                await session.commit()
            else:
                await session.rollback()
            return result
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegram-user-id", type=int)
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--reconcile", action="store_true")
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(asyncio.run(import_batch(parser.parse_args())), ensure_ascii=False))


if __name__ == "__main__":
    main()
