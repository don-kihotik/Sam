from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from sqlalchemy import func, select

from app.db.models import (
    Athlete,
    CatalogSource,
    Correction,
    DailyState,
    Invitation,
    Message,
    Outbox,
    Plan,
    ProcessingRun,
    SharedChat,
    SharedChatMembership,
    Workout,
)
from app.membership import (
    create_group_invitation,
    create_invitation,
    ensure_preference,
    is_group_member,
    redeem_invitation,
    redeem_invitation_details,
    revoke_group_access,
)
from app.prompts import PROMPT_VERSION
from app.scheduling import enqueue_morning_plans
from app.telegram import TelegramRuntime
from app.training_commands import handle_training_command


class PrivateMessage:
    def __init__(self, text: str, user_id: int):
        self.raw_text = text
        self.telegram_chat_id = user_id


@pytest.mark.asyncio
async def test_invitation_is_one_time_expiring_and_private(database, settings):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        token, _ = await create_invitation(
            session,
            created_by=alexey,
            alexey_telegram_user_id=settings.alexey_telegram_user_id,
            now=datetime(2026, 9, 24, tzinfo=UTC),
        )
        await session.commit()

    async with database.sessions() as session:
        invited = await redeem_invitation(
            session,
            token=token,
            telegram_user_id=303,
            display_name="Third friend",
            chat_id=303,
            now=datetime(2026, 9, 24, tzinfo=UTC),
        )
        assert invited.profile == {}
        assert (await ensure_preference(session, invited)).shared_chat_id is None
        await session.commit()

    async with database.sessions() as session:
        with pytest.raises(ValueError):
            await redeem_invitation(
                session,
                token=token,
                telegram_user_id=404,
                display_name="Other",
                chat_id=404,
                now=datetime(2026, 9, 24, tzinfo=UTC),
            )
        expired, _ = await create_invitation(
            session,
            created_by=await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101)),
            alexey_telegram_user_id=101,
            now=datetime(2026, 9, 24, tzinfo=UTC) - timedelta(days=8),
            expires_in=timedelta(days=1),
        )
        with pytest.raises(ValueError):
            await redeem_invitation(
                session,
                token=expired,
                telegram_user_id=405,
                display_name="Expired",
                chat_id=405,
                now=datetime(2026, 9, 24, tzinfo=UTC),
            )
        with pytest.raises(ValueError):
            await redeem_invitation(
                session,
                token="anything",
                telegram_user_id=406,
                display_name="Group",
                chat_id=-100123,
                now=datetime(2026, 9, 24, tzinfo=UTC),
            )


@pytest.mark.asyncio
async def test_reply_invite_registers_chat_is_target_bound_and_replays_after_consent(
    database, settings
):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        token, invitation = await create_group_invitation(
            session,
            created_by=alexey,
            alexey_telegram_user_id=settings.alexey_telegram_user_id,
            telegram_chat_id=-200456,
            chat_title="Climbing buddies",
            target_telegram_user_id=303,
            pending_message={"telegram_payload": {"message_id": 7, "text": "Привет)"}},
            now=now,
        )
        assert invitation.shared_chat_id == -200456
        assert invitation.target_telegram_user_id == 303
        assert (await session.get(SharedChat, -200456)).title == "Climbing buddies"
        assert await is_group_member(
            session, athlete_id=alexey.id, telegram_chat_id=-200456
        )
        await session.commit()

    async with database.sessions() as session:
        with pytest.raises(ValueError):
            await redeem_invitation_details(
                session,
                token=token,
                telegram_user_id=404,
                display_name="Wrong person",
                chat_id=404,
                now=now,
            )

    async with database.sessions() as session:
        result = await redeem_invitation_details(
            session,
            token=token,
            telegram_user_id=303,
            display_name="Dmitry Olshevsky",
            chat_id=303,
            now=now,
        )
        assert result.created is True
        assert result.pending_message["telegram_payload"]["text"] == "Привет)"
        assert await is_group_member(
            session, athlete_id=result.athlete.id, telegram_chat_id=-200456
        )
        assert result.invitation.payload == {}
        await session.commit()

    async with database.sessions() as session:
        stored = await session.get(Invitation, invitation.id)
        membership = await session.get(
            SharedChatMembership,
            {"telegram_chat_id": -200456, "athlete_id": stored.redeemed_by},
        )
        # Telegram IDs and internal athlete IDs intentionally differ.
        assert membership is None
        invited = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 303))
        assert await is_group_member(
            session, athlete_id=invited.id, telegram_chat_id=-200456
        )


@pytest.mark.asyncio
async def test_non_admin_cannot_register_a_group(database, settings):
    async with database.sessions() as session:
        andrey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 202))
        with pytest.raises(PermissionError):
            await create_group_invitation(
                session,
                created_by=andrey,
                alexey_telegram_user_id=settings.alexey_telegram_user_id,
                telegram_chat_id=-200999,
                chat_title="Unauthorized",
                target_telegram_user_id=303,
                pending_message={},
                now=datetime(2026, 10, 21, tzinfo=UTC),
            )
        assert await session.get(SharedChat, -200999) is None


@pytest.mark.asyncio
async def test_existing_athlete_can_join_another_group_without_duplicate_profile(database, settings):
    now = datetime(2026, 10, 21, tzinfo=UTC)
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        before = await session.scalar(select(func.count()).select_from(Athlete))
        token, _ = await create_group_invitation(
            session,
            created_by=alexey,
            alexey_telegram_user_id=101,
            telegram_chat_id=-200789,
            chat_title="Second group",
            target_telegram_user_id=202,
            pending_message={},
            now=now,
        )
        await session.commit()
    async with database.sessions() as session:
        result = await redeem_invitation_details(
            session,
            token=token,
            telegram_user_id=202,
            display_name="Andrey",
            chat_id=202,
            now=now,
        )
        assert result.created is False
        assert await session.scalar(select(func.count()).select_from(Athlete)) == before
        assert await is_group_member(
            session, athlete_id=result.athlete.id, telegram_chat_id=-200789
        )


@pytest.mark.asyncio
async def test_group_add_button_redeems_and_queues_the_replied_message(database, settings):
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    runtime._bot_username = "samclimbs_bot"
    original_payload = {
        "message_id": 777,
        "date": 1792555140,
        "chat": {"id": -200456, "type": "supergroup", "title": "Climbing buddies"},
        "from": {"id": 303, "is_bot": False, "first_name": "Dmitry"},
        "text": "Привет)",
    }
    target = SimpleNamespace(
        from_user=SimpleNamespace(id=303, is_bot=False, first_name="Dmitry"),
        model_dump=lambda **_: original_payload,
    )
    group_message = SimpleNamespace(
        text="/add",
        from_user=SimpleNamespace(id=101, is_bot=False, first_name="Alexey"),
        chat=SimpleNamespace(
            id=-200456, type=ChatType.SUPERGROUP, title="Climbing buddies"
        ),
        reply_to_message=target,
        reply=AsyncMock(),
    )
    private_message = None
    try:
        assert await runtime._maybe_handle_group_add(group_message)
        markup = group_message.reply.await_args.kwargs["reply_markup"]
        link = markup.inline_keyboard[0][0].url
        token = link.split("start=", 1)[1]
        private_message = SimpleNamespace(
            text=f"/start {token}",
            from_user=SimpleNamespace(
                id=303,
                is_bot=False,
                first_name="Dmitry",
                full_name="Dmitry Olshevsky",
            ),
            chat=SimpleNamespace(id=303, type=ChatType.PRIVATE),
            reply=AsyncMock(),
        )
        await runtime._handle_message(private_message)
        assert "отдельный профиль" in private_message.reply.await_args.args[0]
        assert "Исходное сообщение" in private_message.reply.await_args.args[0]
        async with database.sessions() as session:
            athlete = await session.scalar(
                select(Athlete).where(Athlete.telegram_user_id == 303)
            )
            assert athlete is not None
            assert await is_group_member(
                session, athlete_id=athlete.id, telegram_chat_id=-200456
            )
            queued = await session.scalar(
                select(Message).where(
                    Message.telegram_chat_id == -200456,
                    Message.telegram_message_id == 777,
                )
            )
            assert queued is not None and queued.athlete_id == athlete.id
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == queued.id)
            )
            assert run.status == "received"
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_private_command_cannot_read_another_athletes_session(database, settings):
    async with database.sessions() as session:
        alexey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        plan = Plan(
            athlete_id=alexey.id,
            scope="session",
            start_date=date(2026, 9, 24),
            end_date=date(2026, 9, 24),
            content={"summary": "Alexey private", "blocks": []},
        )
        session.add(plan)
        await session.commit()
        andrey = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 202))
        result = await handle_training_command(
            session=session,
            athlete=andrey,
            message=PrivateMessage(f"/session {plan.id}", 202),
            settings=settings,
            today=date(2026, 9, 24),
        )
        assert "не найдена" in result.text


@pytest.mark.asyncio
async def test_morning_reminder_is_timezone_aware_and_deduplicated(database):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        athlete.timezone = "America/Toronto"
        preference = await ensure_preference(session, athlete)
        preference.private_chat_id = 101
        preference.morning_time = "08:00"
        session.add(
            Plan(
                athlete_id=athlete.id,
                scope="session",
                start_date=date(2026, 9, 24),
                end_date=date(2026, 9, 24),
                content={"summary": "Today", "blocks": []},
            )
        )
        assert (
            await enqueue_morning_plans(session, now=datetime(2026, 9, 24, 12, 0, tzinfo=UTC)) == 1
        )
        assert (
            await enqueue_morning_plans(session, now=datetime(2026, 9, 24, 12, 10, tzinfo=UTC)) == 0
        )
        await session.commit()
        outbox = (await session.scalars(select(Outbox).where(Outbox.dedupe_key.is_not(None)))).all()
        assert len(outbox) == 1
        assert outbox[0].source_message_id is None and outbox[0].reply_to_message_id is None


@pytest.mark.asyncio
async def test_callback_is_durable_idempotent_and_bound_to_clicking_user(database, settings):
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    callback = SimpleNamespace(
        id="callback-1",
        data="train:session:1",
        from_user=SimpleNamespace(id=101),
        message=SimpleNamespace(
            message_id=88,
            chat=SimpleNamespace(id=101, type="private"),
        ),
        answer=AsyncMock(),
    )
    try:
        await runtime._handle_callback(callback)
        await runtime._handle_callback(callback)
        async with database.sessions() as session:
            rows = (
                await session.scalars(select(Message).where(Message.message_type == "callback"))
            ).all()
            assert len(rows) == 1
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == rows[0].id)
            )
            assert run.status == "received"
        denied = SimpleNamespace(
            id="callback-2",
            data="train:session:1",
            from_user=SimpleNamespace(id=202),
            message=SimpleNamespace(
                message_id=89,
                chat=SimpleNamespace(id=101, type="private"),
            ),
            answer=AsyncMock(),
        )
        await runtime._handle_callback(denied)
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.message_type == "callback")
                )
                == 1
            )
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_group_history_is_deterministic_and_not_blocked_by_private_command_gate(
    database, settings
):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        history = await handle_training_command(
            session=session,
            athlete=athlete,
            message=PrivateMessage("/history", -100123),
            settings=settings,
            today=date(2026, 9, 24),
        )
        assert history and "В базе" in history.text
        assert history.model_request is None
        private_only = await handle_training_command(
            session=session,
            athlete=athlete,
            message=PrivateMessage("/today", -100123),
            settings=settings,
            today=date(2026, 9, 24),
        )
        assert "личном чате" in private_only.text


@pytest.mark.asyncio
async def test_group_revocation_blocks_ingestion_before_processing_run(database, settings):
    from aiogram.types import Message as TelegramMessage

    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        (await ensure_preference(session, athlete)).shared_chat_id = None
        await revoke_group_access(session, athlete_id=athlete.id)
        await session.commit()
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    message = TelegramMessage.model_validate(
        {
            "message_id": 490,
            "date": 1790208000,
            "chat": {"id": -100123, "type": "supergroup", "title": "shared"},
            "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
            "text": "Сэм, привет",
        }
    )
    try:
        await runtime._handle_message(message)
        async with database.sessions() as session:
            assert (
                await session.scalar(select(Message).where(Message.telegram_message_id == 490))
                is None
            )
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_document_delivery_persists_document_before_text_retry(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        session.add(
            Outbox(
                athlete_id=athlete.id,
                telegram_chat_id=101,
                reply_to_message_id=None,
                text="Export ready",
                payload={"document": {"filename": "plan.md", "text": "# plan"}},
            )
        )
        await session.commit()
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    runtime.bot.send_document = AsyncMock(
        return_value=SimpleNamespace(message_id=900, date=datetime.now(UTC))
    )
    runtime.bot.send_message = AsyncMock(
        side_effect=[
            RuntimeError("temporary"),
            SimpleNamespace(message_id=901, date=datetime.now(UTC)),
        ]
    )
    try:
        await runtime._deliver_next()
        async with database.sessions() as session:
            item = await session.scalar(select(Outbox).order_by(Outbox.id.desc()))
            assert item.status == "retry" and item.sent_message_ids == [900]
            item.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        await runtime._deliver_next()
        assert runtime.bot.send_document.await_count == 1
        async with database.sessions() as session:
            item = await session.scalar(select(Outbox).order_by(Outbox.id.desc()))
            assert item.status == "sent" and item.sent_message_ids == [900, 901]
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_queued_group_delivery_is_suppressed_after_sharing_revocation(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        preference = await ensure_preference(session, athlete)
        preference.shared_chat_id = None
        await revoke_group_access(session, athlete_id=athlete.id)
        session.add(
            Outbox(
                athlete_id=athlete.id,
                telegram_chat_id=-100123,
                reply_to_message_id=None,
                text="Private-derived reply",
            )
        )
        await session.commit()
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    runtime.bot.send_message = AsyncMock()
    try:
        await runtime._deliver_next()
        runtime.bot.send_message.assert_not_called()
        async with database.sessions() as session:
            item = await session.scalar(select(Outbox).order_by(Outbox.id.desc()))
            assert item.status == "superseded" and item.error == "group_sharing_revoked"
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_skip_is_audited_and_never_creates_an_actual_workout(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        plan = Plan(
            athlete_id=athlete.id,
            scope="session",
            start_date=date(2026, 9, 24),
            end_date=date(2026, 9, 24),
            content={"summary": "optional", "blocks": []},
        )
        session.add(plan)
        await session.flush()
        message = Message(
            telegram_message_id=777,
            telegram_chat_id=101,
            telegram_user_id=101,
            athlete_id=athlete.id,
            raw_text=f"/skip {plan.id}",
            message_type="text",
            telegram_timestamp=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        result = await handle_training_command(
            session=session,
            athlete=athlete,
            message=message,
            settings=settings,
            today=date(2026, 9, 24),
        )
        await session.commit()
        assert "пропущенная" in result.text
        assert plan.status == "skipped"
        assert await session.scalar(select(func.count()).select_from(Workout)) == 1
        state = await session.scalar(
            select(DailyState).where(
                DailyState.athlete_id == athlete.id,
                DailyState.date == plan.start_date,
            )
        )
        assert state.values["session_outcomes"][-1]["plan_id"] == plan.id
        audit = await session.scalar(select(Correction).where(Correction.target_id == plan.id))
        assert audit.payload["action"] == "skip"
        replay = await handle_training_command(
            session=session,
            athlete=athlete,
            message=message,
            settings=settings,
            today=date(2026, 9, 24),
        )
        assert "уже" in replay.text


@pytest.mark.asyncio
async def test_catalog_photo_intake_is_sticky_across_ocr_retry_and_catalog_off(database, settings):
    from aiogram.types import Message as TelegramMessage

    async def download(_photo, destination):
        destination.write(b"catalog-photo")

    ai = SimpleNamespace(
        classify_scope=AsyncMock(return_value=SimpleNamespace(decision="allow")),
        read_catalog_photo=AsyncMock(
            side_effect=[
                {"screen_type": "protocol", "title": "Warmup", "lines": ["3 sets"]},
                RuntimeError("temporary OCR outage"),
                {"screen_type": "exercise", "title": "Retry", "lines": ["5 reps"]},
            ]
        ),
    )
    runtime = TelegramRuntime(settings, database, ai)
    runtime.bot.download = AsyncMock(side_effect=download)

    def photo_message(message_id: int, unique_id: str) -> TelegramMessage:
        return TelegramMessage.model_validate(
            {
                "message_id": message_id,
                "date": 1790208000,
                "chat": {"id": 101, "type": "private", "first_name": "Alexey"},
                "from": {"id": 101, "is_bot": False, "first_name": "Alexey"},
                "photo": [
                    {
                        "file_id": f"file-{unique_id}",
                        "file_unique_id": unique_id,
                        "width": 100,
                        "height": 100,
                    }
                ],
            }
        )

    try:
        async with database.sessions() as session:
            athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
            (await ensure_preference(session, athlete)).catalog_mode = True
            await session.commit()

        await runtime._handle_message(photo_message(801, "catalog-success"))
        await runtime._process_next()
        async with database.sessions() as session:
            source = await session.scalar(
                select(CatalogSource).where(CatalogSource.file_unique_id == "catalog-success")
            )
            assert source.status == "needs_review" and source.extracted["title"] == "Warmup"
            assert await session.scalar(select(func.count()).select_from(Workout)) == 1

        await runtime._handle_message(photo_message(802, "catalog-retry"))
        await runtime._process_next()
        async with database.sessions() as session:
            source = await session.scalar(
                select(CatalogSource).where(CatalogSource.file_unique_id == "catalog-retry")
            )
            run = await session.scalar(
                select(ProcessingRun).join(Message).where(Message.telegram_message_id == 802)
            )
            assert source.status == "received" and run.status == "retry"
            preference = await ensure_preference(session, await session.get(Athlete, 1))
            preference.catalog_mode = False
            run.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        await runtime._process_next()
        async with database.sessions() as session:
            source = await session.scalar(
                select(CatalogSource).where(CatalogSource.file_unique_id == "catalog-retry")
            )
            run = await session.scalar(
                select(ProcessingRun).join(Message).where(Message.telegram_message_id == 802)
            )
            assert source.status == "needs_review" and source.extracted["title"] == "Retry"
            assert run.status == "completed"
            assert await session.scalar(select(func.count()).select_from(Workout)) == 1
        assert ai.read_catalog_photo.await_count == 3
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_delivery_suppresses_old_policy_response_but_delivers_current_one(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        old_message = Message(
            telegram_message_id=901,
            telegram_chat_id=101,
            telegram_user_id=101,
            athlete_id=athlete.id,
            direction="incoming",
            message_type="text",
            telegram_timestamp=datetime.now(UTC),
        )
        current_message = Message(
            telegram_message_id=902,
            telegram_chat_id=101,
            telegram_user_id=101,
            athlete_id=athlete.id,
            direction="incoming",
            message_type="text",
            telegram_timestamp=datetime.now(UTC),
        )
        session.add_all([old_message, current_message])
        await session.flush()
        session.add_all(
            [
                ProcessingRun(
                    message_id=old_message.id,
                    status="completed",
                    prompt_version="v7-peer-context",
                    extraction_model="old-extractor",
                    coaching_model="old-coach",
                ),
                ProcessingRun(
                    message_id=current_message.id, status="completed", prompt_version=PROMPT_VERSION
                ),
                Outbox(
                    source_message_id=old_message.id,
                    revision=1,
                    athlete_id=athlete.id,
                    telegram_chat_id=101,
                    reply_to_message_id=901,
                    text="old response",
                ),
                Outbox(
                    source_message_id=current_message.id,
                    revision=1,
                    athlete_id=athlete.id,
                    telegram_chat_id=101,
                    reply_to_message_id=902,
                    text="current response",
                ),
            ]
        )
        await session.commit()
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    runtime.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=903, date=datetime.now(UTC))
    )
    try:
        await runtime._deliver_next()
        runtime.bot.send_message.assert_not_called()
        await runtime._deliver_next()
        runtime.bot.send_message.assert_awaited_once()
        async with database.sessions() as session:
            items = (await session.scalars(select(Outbox).order_by(Outbox.id))).all()
            assert items[-2].status == "superseded"
            assert items[-2].error == "obsolete_response_policy"
            assert items[-1].status == "sent"
    finally:
        await runtime.bot.session.close()


@pytest.mark.asyncio
async def test_fixed_media_refusal_is_not_suppressed_as_an_old_model_reply(database, settings):
    async with database.sessions() as session:
        athlete = await session.scalar(select(Athlete).where(Athlete.telegram_user_id == 101))
        source = Message(
            telegram_message_id=904,
            telegram_chat_id=101,
            telegram_user_id=101,
            athlete_id=athlete.id,
            direction="incoming",
            message_type="voice",
            telegram_timestamp=datetime.now(UTC),
        )
        session.add(source)
        await session.flush()
        # Raw intake runs have the historical v1 default but no model output.
        session.add(ProcessingRun(message_id=source.id, status="failed", error="unsupported_media"))
        session.add(
            Outbox(
                source_message_id=source.id,
                revision=1,
                athlete_id=athlete.id,
                telegram_chat_id=101,
                reply_to_message_id=904,
                text="Голосовое слишком длинное для разбора.",
            )
        )
        await session.commit()
    runtime = TelegramRuntime(settings, database, SimpleNamespace())
    runtime.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=905, date=datetime.now(UTC))
    )
    try:
        await runtime._deliver_next()
        runtime.bot.send_message.assert_awaited_once()
        async with database.sessions() as session:
            item = await session.scalar(select(Outbox).order_by(Outbox.id.desc()))
            assert item.status == "sent"
    finally:
        await runtime.bot.session.close()
