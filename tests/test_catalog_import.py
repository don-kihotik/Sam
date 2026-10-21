from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import func, select

from app.catalog import seed_catalog
from app.catalog_import import RECOVERY_VERSION, reconcile_batch, recover_batch
from app.db.models import (
    Athlete,
    CatalogProtocol,
    CatalogSource,
    Message,
    Outbox,
    ProcessingRun,
    Workout,
)
from app.telegram import TelegramRuntime


async def _historical_photo(
    session, *, athlete: Athlete, message_id: int, unique_id: str, album_id: str | None
) -> Message:
    payload = {
        "message_id": message_id,
        "date": int(datetime(2026, 9, 24, 16, 0, tzinfo=UTC).timestamp()),
        "chat": {"id": athlete.telegram_user_id, "type": "private"},
        "from": {"id": athlete.telegram_user_id, "is_bot": False, "first_name": athlete.name},
        "photo": [
            {
                "file_id": f"file-{unique_id}",
                "file_unique_id": unique_id,
                "width": 100,
                "height": 200,
            }
        ],
    }
    if album_id:
        payload["media_group_id"] = album_id
    message = Message(
        telegram_message_id=message_id,
        telegram_chat_id=athlete.telegram_user_id,
        telegram_user_id=athlete.telegram_user_id,
        athlete_id=athlete.id,
        direction="incoming",
        message_type="photo",
        telegram_timestamp=datetime(2026, 9, 24, 16, 0, tzinfo=UTC),
        attachment_data={"telegram_payload": payload},
    )
    session.add(message)
    await session.flush()
    session.add(
        ProcessingRun(message_id=message.id, status="failed", error="unsupported_media")
    )
    return message


async def test_historical_catalog_recovery_is_previewable_safe_and_idempotent(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        first = await _historical_photo(
            session, athlete=athlete, message_id=1001, unique_id="old-1", album_id="album-a"
        )
        second = await _historical_photo(
            session, athlete=athlete, message_id=1002, unique_id="old-2", album_id="album-a"
        )
        await session.commit()
        first_id, second_id = first.id, second.id

    async with database.sessions() as session:
        preview = await recover_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24)
        )
        assert preview["screenshots"] == 2
        assert preview["albums"] == 1
        assert preview["terminal_unsupported_media"] == 2
        assert preview["source_statuses"] == {"not_staged": 2}
        assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 0

        applied = await recover_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24), apply=True
        )
        assert applied["newly_staged"] == applied["requeued"] == 2
        assert applied["workouts_changed"] == applied["messages_sent"] == 0
        await session.commit()

    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 2
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 0
        assert await session.scalar(select(func.count()).select_from(Workout)) == 1
        for message_id in (first_id, second_id):
            saved = await session.get(Message, message_id)
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == message_id)
            )
            assert saved.processing_data["catalog_recovery"]["version"] == RECOVERY_VERSION
            assert saved.processing_data["catalog_recovery"]["original_error"] == "unsupported_media"
            assert run.status == "received" and run.error is None

        replay = await recover_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24), apply=True
        )
        assert replay["newly_staged"] == replay["requeued"] == 0


async def test_reconciliation_only_auto_links_exact_title(database):
    async with database.sessions() as session:
        await seed_catalog(session)
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        exact_message = await _historical_photo(
            session, athlete=athlete, message_id=1011, unique_id="exact", album_id=None
        )
        fuzzy_message = await _historical_photo(
            session, athlete=athlete, message_id=1012, unique_id="fuzzy", album_id=None
        )
        exact = CatalogSource(
            athlete_id=athlete.id,
            source_message_id=exact_message.id,
            file_unique_id="exact",
            status="needs_review",
            extracted={"screen_type": "protocol", "title": "Static Holds - B"},
        )
        fuzzy = CatalogSource(
            athlete_id=athlete.id,
            source_message_id=fuzzy_message.id,
            file_unique_id="fuzzy",
            status="needs_review",
            extracted={"screen_type": "protocol", "title": "Static Hold B variation"},
        )
        session.add_all([exact, fuzzy])
        await session.flush()

        preview = await reconcile_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24)
        )
        assert preview["classifications"]["matched_existing"] == 1
        assert exact.protocol_id is None and "reconciliation" not in exact.extracted

        applied = await reconcile_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24), apply=True
        )
        assert applied["exact_links_added"] == 1
        protocol = await session.scalar(
            select(CatalogProtocol).where(CatalogProtocol.title == "Static Holds - B")
        )
        assert exact.protocol_id == protocol.id
        assert exact.extracted["reconciliation"]["status"] == "matched_existing"
        assert fuzzy.protocol_id is None
        assert fuzzy.extracted["reconciliation"]["status"] in {
            "possible_match",
            "new_candidate",
        }


async def test_recovery_deduplicates_same_telegram_file_without_losing_run(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        first = await _historical_photo(
            session, athlete=athlete, message_id=1021, unique_id="same-file", album_id="album-x"
        )
        second = await _historical_photo(
            session, athlete=athlete, message_id=1022, unique_id="same-file", album_id="album-x"
        )
        await session.commit()
        first_id, second_id = first.id, second.id

    async with database.sessions() as session:
        result = await recover_batch(
            session, telegram_user_id=101, batch_date=date(2026, 9, 24), apply=True
        )
        assert result["newly_staged"] == 1
        assert result["deduplicated_messages"] == 1
        assert result["requeued"] == 2
        assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 1
        source = await session.scalar(select(CatalogSource))
        assert source.source_message_id == first_id
        second_run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.message_id == second_id)
        )
        assert second_run.status == "received"


async def test_worker_completes_recovered_duplicate_without_reply_or_second_ocr(
    database, settings
):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        first = await _historical_photo(
            session, athlete=athlete, message_id=1031, unique_id="duplicate", album_id="album-y"
        )
        second = await _historical_photo(
            session, athlete=athlete, message_id=1032, unique_id="duplicate", album_id="album-y"
        )
        await session.flush()
        session.add(
            CatalogSource(
                athlete_id=athlete.id,
                source_message_id=first.id,
                file_unique_id="duplicate",
                album_id="album-y",
                status="needs_review",
                extracted={"screen_type": "protocol", "title": "Already read"},
            )
        )
        first_run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.message_id == first.id)
        )
        second_run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.message_id == second.id)
        )
        first_run.status, first_run.error = "completed", None
        second.processing_data = {
            "catalog_recovery": {"version": RECOVERY_VERSION, "original_error": "unsupported_media"}
        }
        second_run.status, second_run.error = "received", None
        await session.commit()
        second_id = second.id

    ai = SimpleNamespace(read_catalog_photo=AsyncMock())
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock()
    try:
        await runtime._process_next()
        async with database.sessions() as session:
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == second_id)
            )
            assert run.status == "completed" and run.error is None
            assert await session.scalar(select(func.count()).select_from(CatalogSource)) == 1
            assert await session.scalar(select(func.count()).select_from(Outbox)) == 0
        ai.read_catalog_photo.assert_not_awaited()
        runtime.bot.download.assert_not_awaited()
    finally:
        await runtime.bot.session.close()
