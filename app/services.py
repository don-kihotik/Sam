from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIService
from app.analytics import grade_rank, refresh_analytics_snapshots
from app.config import Settings
from app.db.models import (
    Athlete,
    BacklogItem,
    BodyMeasurement,
    Correction,
    DailyState,
    Event,
    Fact,
    Memory,
    Message,
    Plan,
    ProcessingRun,
    Workout,
    WorkoutEntry,
)
from app.maintenance import fingerprint_existing
from app.memory import ContextBuilder, recent_message_text, semantic_history
from app.prompts import PROMPT_VERSION
from app.schemas import CorrectionCandidate, MessageExtraction, PlanCandidate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class IncomingMessage:
    telegram_message_id: int
    telegram_chat_id: int
    telegram_user_id: int
    timestamp: datetime
    text: str
    message_type: str = "text"
    transcript: str | None = None
    reply_to_message_id: int | None = None
    attachment_metadata: dict[str, Any] | None = None
    is_reply_to_sam: bool = False


@dataclass(slots=True)
class ProcessResult:
    duplicate: bool = False
    ignored_unknown_user: bool = False
    reply: str | None = None
    message_id: int | None = None
    athlete_id: int | None = None
    mutations: list[dict[str, Any]] | None = None


_TRANSCRIPTION_ADDRESS_ALIASES = re.compile(r"(?<!\w)санта(?!\w)", re.IGNORECASE)
_DIRECT_ADDRESS = re.compile(r"(?<!\w)(?:сэм|sam)(?!\w)", re.IGNORECASE)
_RATING_WORDS = {
    0: "ноль",
    1: "один",
    2: "два",
    3: "три",
    4: "четыре",
    5: "пять",
    6: "шесть",
    7: "семь",
    8: "восемь",
    9: "девять",
    10: "десять",
}
_RATING_CUE = (
    r"(?:памп\w*|забит\w*|забил\w*|боль\w*|болит\w*|болезнен\w*|rpe|"
    r"нагрузк\w*|интенсивност\w*|энерги\w*|сон\w*|мотивац\w*|стресс\w*|устал\w*)"
)


def is_directly_addressed(
    text: str,
    *,
    is_reply_to_sam: bool = False,
    allow_transcription_aliases: bool = False,
) -> bool:
    lowered = text.strip().lower()
    if is_reply_to_sam or lowered.startswith("/") or _DIRECT_ADDRESS.search(lowered):
        return True
    if not allow_transcription_aliases:
        return False
    opening = lowered[:160]
    return bool(_TRANSCRIPTION_ADDRESS_ALIASES.search(opening) or "для тебя" in opening)


def _score_was_explicitly_stated(text: str, value: float) -> bool:
    integer_value = int(value)
    if value == integer_value:
        number = rf"{integer_value}(?:[.,]0)?"
        word = _RATING_WORDS.get(integer_value)
        score = rf"(?:{number}|{word})" if word else number
    else:
        score = re.escape(str(value)).replace(r"\.", "[.,]")

    explicit_scale = rf"(?<!\w){score}\s*(?:/|из)\s*10(?!\w)"
    direct_rating = rf"{_RATING_CUE}\s*(?:(?:на|уровень|оценка)\s*)?{score}(?!\w)"
    linked_rating = rf"{_RATING_CUE}[^.!?\n]{{0,30}}\bна\s+{score}(?!\w)"
    return bool(re.search(rf"(?:{explicit_scale}|{direct_rating}|{linked_rating})", text.lower()))


def remove_inferred_numeric_ratings(text: str, extraction: MessageExtraction) -> list[str]:
    """Drop 0–10 ratings that are not explicitly present in the user's words."""
    removed: list[str] = []
    workout = extraction.workout
    for field in ("rpe", "pump"):
        value = getattr(workout, field)
        if value is not None and not _score_was_explicitly_stated(text, value):
            setattr(workout, field, None)
            removed.append(f"workout.{field}")
    explicit_pain = []
    for score in workout.pain:
        if _score_was_explicitly_stated(text, score.value):
            explicit_pain.append(score)
        else:
            removed.append(f"workout.pain:{score.area}")
    workout.pain = explicit_pain

    state = extraction.daily_state
    for field in ("energy", "sleep_quality", "motivation", "stress", "general_fatigue"):
        value = getattr(state, field)
        if value is not None and not _score_was_explicitly_stated(text, value):
            setattr(state, field, None)
            removed.append(f"daily_state.{field}")
    explicit_soreness = []
    for score in state.soreness:
        if _score_was_explicitly_stated(text, score.value):
            explicit_soreness.append(score)
        else:
            removed.append(f"daily_state.soreness:{score.area}")
    state.soreness = explicit_soreness
    if removed:
        extraction.uncertainty_notes.append(
            "Числовые рейтинги без явно названной пользователем цифры удалены: "
            + ", ".join(removed)
        )
    return removed


def _normalized_evidence(value: str) -> str:
    return " ".join(value.casefold().split()).strip(" .,!?:;—–-\"'«»")


def valid_evidence(text: str, evidence: list[str]) -> list[str]:
    source = _normalized_evidence(text)
    return [quote for quote in evidence if quote and _normalized_evidence(quote) in source]


def _workout_fingerprint(athlete_id: int, workout_date: date, workout: Any) -> str:
    def normalized_discipline(entry: Any) -> str | None:
        discipline = (entry.discipline or "").casefold().replace(" ", "_")
        wall_style = (entry.wall_style or "").casefold().replace(" ", "_")
        if "auto" in discipline or "auto" in wall_style:
            return "auto_belay"
        if "rope" in discipline:
            return "roped_climbing"
        if "boulder" in discipline:
            return "bouldering"
        return discipline or None

    climbing = sorted(
        (
            normalized_discipline(entry),
            entry.grade_system
            or ("V" if (entry.original_grade or "").upper().startswith("V") else None),
            entry.original_grade,
            entry.count,
            entry.attempts,
        )
        for entry in workout.climbing
    )
    payload = {
        "athlete": athlete_id,
        "date": workout_date.isoformat(),
        "duration": workout.duration_minutes,
        "rpe": workout.rpe,
        "pump": workout.pump,
        "climbing": climbing,
    }
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _looks_like_plan(text: str) -> bool:
    lowered = text.casefold()
    day_hits = sum(day in lowered for day in ("пн", "вт", "ср", "чт", "пт", "сб", "вс"))
    return day_hits >= 3 or ("план" in lowered and len(text) >= 180)


class MessageProcessor:
    def __init__(
        self, settings: Settings, ai: AIService, context_builder: ContextBuilder | None = None
    ):
        self.settings = settings
        self.ai = ai
        self.context_builder = context_builder or ContextBuilder(settings.recent_message_limit)

    async def process(self, session: AsyncSession, incoming: IncomingMessage) -> ProcessResult:
        existing = await session.scalar(
            select(Message).where(
                Message.telegram_chat_id == incoming.telegram_chat_id,
                Message.telegram_message_id == incoming.telegram_message_id,
            )
        )
        if existing:
            return ProcessResult(
                duplicate=True, message_id=existing.id, athlete_id=existing.athlete_id
            )

        athlete = await session.scalar(
            select(Athlete).where(
                Athlete.telegram_user_id == incoming.telegram_user_id,
                Athlete.active.is_(True),
            )
        )
        if athlete is None:
            return ProcessResult(ignored_unknown_user=True)

        message = Message(
            telegram_message_id=incoming.telegram_message_id,
            telegram_chat_id=incoming.telegram_chat_id,
            telegram_user_id=incoming.telegram_user_id,
            athlete_id=athlete.id,
            reply_to_message_id=incoming.reply_to_message_id,
            direction="incoming",
            message_type=incoming.message_type,
            raw_text=incoming.text if incoming.message_type == "text" else None,
            transcript=incoming.transcript,
            attachment_data=incoming.attachment_metadata or {},
            telegram_timestamp=incoming.timestamp,
        )
        session.add(message)
        await session.flush()
        run = ProcessingRun(
            message_id=message.id,
            extraction_model=self.settings.extraction_model,
            coaching_model=self.settings.sam_model,
            prompt_version=PROMPT_VERSION,
        )
        session.add(run)
        await session.commit()

        addressed = is_directly_addressed(
            incoming.text,
            is_reply_to_sam=incoming.is_reply_to_sam,
            allow_transcription_aliases=incoming.message_type == "voice",
        )
        local_today = incoming.timestamp.astimezone(ZoneInfo(athlete.timezone)).date()
        try:
            recent = await recent_message_text(session, athlete.id, limit=12)
            extraction = await self.ai.extract(
                text=incoming.text,
                athlete_name=athlete.name,
                local_date=local_today.isoformat(),
                timezone_name=athlete.timezone,
                recent_context=recent,
                directly_addressed=addressed,
            )
            remove_inferred_numeric_ratings(incoming.text, extraction)
            message.normalized_text = extraction.normalized_text
            run.extracted = extraction.model_dump(mode="json")
            mutations = await self._apply_extraction(
                session,
                athlete=athlete,
                message=message,
                extraction=extraction,
                today=local_today,
                source_text=incoming.text,
            )
            try:
                message.embedding = await self.ai.embed(extraction.normalized_text)
            except Exception as exc:  # noqa: BLE001 - optional embeddings must not block writes
                logger.warning("Embedding failed for message %s: %s", message.id, exc)
                mutations.append({"kind": "embedding", "status": "failed"})

            should_reply = (
                addressed or extraction.should_respond or extraction.backlog.action != "none"
            )
            reply = None
            if should_reply:
                query_embedding = message.embedding
                matches = await semantic_history(
                    session,
                    athlete_id=athlete.id,
                    query_embedding=query_embedding,
                    exclude_message_id=message.id,
                )
                context = await self.context_builder.build(
                    session,
                    athlete=athlete,
                    today=local_today,
                    current_message=incoming.text,
                    semantic_matches=matches,
                    extraction_summary=json.dumps(
                        {
                            "candidate": extraction.model_dump(mode="json", exclude_none=True),
                            "mutation_results": mutations,
                        },
                        ensure_ascii=False,
                    ),
                )
                reply = await self.ai.coach(
                    context=context,
                    safety_identifier=f"telegram-{athlete.id}",
                )
            run.mutations = mutations
            await refresh_analytics_snapshots(session, athlete.id, local_today)
            run.status = "completed"
            await session.commit()
            return ProcessResult(
                reply=reply,
                message_id=message.id,
                athlete_id=athlete.id,
                mutations=mutations,
            )
        except Exception as exc:
            await session.rollback()
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == message.id)
            )
            if run:
                run.status = "failed"
                run.error = f"{type(exc).__name__}: {exc}"
                await session.commit()
            raise

    async def save_outgoing(
        self,
        session: AsyncSession,
        *,
        telegram_message_id: int,
        telegram_chat_id: int,
        athlete_id: int,
        text: str,
        timestamp: datetime | None = None,
    ) -> Message:
        message = Message(
            telegram_message_id=telegram_message_id,
            telegram_chat_id=telegram_chat_id,
            telegram_user_id=None,
            athlete_id=athlete_id,
            direction="outgoing",
            message_type="text",
            raw_text=text,
            normalized_text=text,
            telegram_timestamp=timestamp or datetime.now(UTC),
        )
        session.add(message)
        try:
            message.embedding = await self.ai.embed(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding failed for outgoing message: %s", exc)
        await session.flush()
        if _looks_like_plan(text):
            try:
                artifacts = await self.ai.extract_coach_artifacts(text)
                evidence = valid_evidence(text, artifacts.plan.evidence)
                if artifacts.plan.action != "none" and evidence:
                    await self._upsert_plan(
                        session,
                        athlete_id=athlete_id,
                        candidate=artifacts.plan,
                        source_message_id=message.id,
                        evidence=evidence,
                        today=(timestamp or datetime.now(UTC)).date(),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not persist outgoing plan: %s", exc)
        await session.commit()
        return message

    async def _apply_extraction(
        self,
        session: AsyncSession,
        *,
        athlete: Athlete,
        message: Message,
        extraction: MessageExtraction,
        today: date,
        source_text: str,
    ) -> list[dict[str, Any]]:
        mutations: list[dict[str, Any]] = []
        workout = extraction.workout
        workout_evidence = valid_evidence(source_text, workout.evidence)
        if workout.present and workout_evidence:
            workout_date = workout.date or today
            fingerprint = _workout_fingerprint(athlete.id, workout_date, workout)
            record = await session.scalar(
                select(Workout).where(
                    Workout.athlete_id == athlete.id,
                    Workout.fingerprint == fingerprint,
                    Workout.status == "active",
                )
            )
            if record is None:
                created = True
                pain_status = workout.pain_status
                if workout.pain:
                    pain_status = "reported"
                record = Workout(
                    athlete_id=athlete.id,
                    date=workout_date,
                    date_precision=workout.date_precision,
                    type=workout.workout_type or "other",
                    duration_minutes=workout.duration_minutes,
                    rpe=workout.rpe,
                    notes=workout.notes,
                    structured_details={
                        "pump": workout.pump,
                        "pain": [x.model_dump() for x in workout.pain],
                        "climbing": [x.model_dump(exclude_none=True) for x in workout.climbing],
                        "confidence": workout.confidence,
                        "date_inferred": workout.date_inferred,
                    },
                    pain_status=pain_status,
                    evidence=workout_evidence,
                    fingerprint=fingerprint,
                    source_message_ids=[message.id],
                )
                session.add(record)
                await session.flush()
                for entry in workout.climbing:
                    session.add(
                        WorkoutEntry(
                            workout_id=record.id,
                            discipline=entry.discipline,
                            grade_system=entry.grade_system,
                            original_grade=entry.original_grade,
                            grade_rank=grade_rank(entry.grade_system, entry.original_grade),
                            count=entry.count,
                            completed_count=(entry.count if entry.completed is True else 0)
                            if entry.completed is not None
                            else None,
                            attempts=entry.attempts,
                            wall_style=entry.wall_style,
                            movement_style=entry.movement_style,
                            notes=entry.notes,
                        )
                    )
                mutations.append({"kind": "workout", "id": record.id, "status": "created"})
            else:
                created = False
                record.source_message_ids = list(
                    dict.fromkeys([*record.source_message_ids, message.id])
                )
                record.evidence = list(dict.fromkeys([*record.evidence, *workout_evidence]))
                mutations.append({"kind": "workout", "id": record.id, "status": "duplicate_merged"})
            if created:
                await self._save_fact(
                    session,
                    athlete.id,
                    message.id,
                    "workout",
                    {"id": record.id, "date": workout_date.isoformat(), "type": record.type},
                    workout.date_inferred,
                    workout.confidence,
                )
        elif workout.present:
            mutations.append({"kind": "workout", "status": "rejected_no_current_evidence"})

        state = extraction.daily_state
        state_evidence = valid_evidence(source_text, state.evidence)
        if state.present and state_evidence:
            state_date = state.date or today
            values = state.model_dump(exclude={"present", "date", "confidence"}, exclude_none=True)
            record = await session.scalar(
                select(DailyState).where(
                    DailyState.athlete_id == athlete.id, DailyState.date == state_date
                )
            )
            if record is None:
                record = DailyState(
                    athlete_id=athlete.id,
                    date=state_date,
                    values=values,
                    source_message_ids=[message.id],
                )
                session.add(record)
            else:
                record.values = {**record.values, **values}
                record.source_message_ids = [*record.source_message_ids, message.id]
            await session.flush()
            mutations.append({"kind": "daily_state", "id": record.id})
        elif state.present:
            mutations.append({"kind": "daily_state", "status": "rejected_no_current_evidence"})

        if extraction.weight_kg is not None and valid_evidence(
            source_text, extraction.weight_evidence
        ):
            measurement = BodyMeasurement(
                athlete_id=athlete.id,
                date=today,
                weight_kg=extraction.weight_kg,
                source_message_id=message.id,
            )
            session.add(measurement)
            await session.flush()
            mutations.append({"kind": "weight", "id": measurement.id})
        elif extraction.weight_kg is not None:
            mutations.append({"kind": "weight", "status": "rejected_no_current_evidence"})

        for candidate in extraction.corrections:
            correction = await self._apply_correction(session, athlete, message, candidate, today)
            mutations.append(
                {"kind": "correction", "id": correction.id, "status": correction.status}
            )

        backlog = extraction.backlog
        if backlog.action == "create" and backlog.title and backlog.item_type:
            item = BacklogItem(
                created_by=athlete.id,
                type=backlog.item_type,
                title=backlog.title,
                description=backlog.description,
                original_message_id=message.id,
            )
            session.add(item)
            await session.flush()
            mutations.append({"kind": "backlog", "id": item.id})

        memory = extraction.memory
        memory_evidence = valid_evidence(source_text, memory.evidence)
        if memory.create and memory.summary and memory_evidence:
            record = Memory(
                athlete_id=athlete.id,
                date=today,
                kind=memory.kind,
                summary=memory.summary,
                tags=memory.tags,
                source_message_ids=[message.id],
                importance=memory.importance,
                confidence=memory.confidence,
                status="hypothesis" if memory.kind == "coach" else "likely",
            )
            try:
                record.embedding = await self.ai.embed(memory.summary)
            except Exception as exc:  # noqa: BLE001 - optional embeddings must not block writes
                logger.warning("Embedding failed for memory: %s", exc)
            session.add(record)
            await session.flush()
            mutations.append({"kind": "memory", "id": record.id})
        elif memory.create:
            mutations.append({"kind": "memory", "status": "rejected_no_current_evidence"})

        event = extraction.event
        event_evidence = valid_evidence(source_text, event.evidence)
        if event.action != "none" and event_evidence:
            active_events = (
                await session.scalars(
                    select(Event).where(Event.athlete_id == athlete.id, Event.status == "active")
                )
            ).all()
            for active in active_events:
                active.status = "superseded"
            record = Event(
                athlete_id=athlete.id,
                name=event.name or (active_events[0].name if active_events else "Climbing goal"),
                date=event.date,
                date_precision=event.date_precision,
                details={
                    "date_label": event.date_label,
                    "route_type": event.route_type,
                    "guided": event.guided,
                },
                evidence=event_evidence,
                source_message_ids=[message.id],
            )
            session.add(record)
            profile = dict(athlete.profile)
            profile["goals"] = [
                f"{record.name}: {event.date_label or (event.date.isoformat() if event.date else 'date unknown')}"
            ]
            athlete.profile = profile
            await session.flush()
            mutations.append({"kind": "event", "id": record.id})
        elif event.action != "none":
            mutations.append({"kind": "event", "status": "rejected_no_current_evidence"})

        plan = extraction.plan
        plan_evidence = valid_evidence(source_text, plan.evidence)
        if plan.action != "none" and plan_evidence:
            record = await self._upsert_plan(
                session,
                athlete_id=athlete.id,
                candidate=plan,
                source_message_id=message.id,
                evidence=plan_evidence,
                today=today,
            )
            mutations.append({"kind": "plan", "id": record.id})
        elif plan.action != "none":
            mutations.append({"kind": "plan", "status": "rejected_no_current_evidence"})

        await session.flush()
        return mutations

    async def _upsert_plan(
        self,
        session: AsyncSession,
        *,
        athlete_id: int,
        candidate: PlanCandidate,
        source_message_id: int,
        evidence: list[str],
        today: date,
    ) -> Plan:
        current = await session.scalar(
            select(Plan)
            .where(Plan.athlete_id == athlete_id, Plan.status == "active")
            .order_by(desc(Plan.created_at))
            .limit(1)
        )
        content = {
            "summary": candidate.summary,
            "current_focus": candidate.focus,
            "weekly_schedule": candidate.weekly_schedule,
            "guardrails": candidate.guardrails,
        }
        if current is not None:
            current.status = "superseded"
            content = {**current.content, **{key: value for key, value in content.items() if value}}
        record = Plan(
            athlete_id=athlete_id,
            status="active",
            start_date=candidate.start_date or today,
            end_date=candidate.end_date or (current.end_date if current else None),
            content=content,
            evidence=evidence,
            source_message_ids=[source_message_id],
        )
        session.add(record)
        await session.flush()
        return record

    async def _save_fact(
        self,
        session: AsyncSession,
        athlete_id: int,
        source_message_id: int,
        field: str,
        value: Any,
        inferred: bool,
        confidence: float,
    ) -> Fact:
        fact = Fact(
            athlete_id=athlete_id,
            field=field,
            value=value,
            source_message_id=source_message_id,
            inferred=inferred,
            confidence=confidence,
        )
        session.add(fact)
        return fact

    async def _apply_correction(
        self,
        session: AsyncSession,
        athlete: Athlete,
        message: Message,
        candidate: CorrectionCandidate,
        today: date,
    ) -> Correction:
        correction = Correction(
            athlete_id=athlete.id,
            source_message_id=message.id,
            target_kind=candidate.target_kind,
            payload=candidate.model_dump(mode="json"),
        )
        session.add(correction)

        if candidate.target_kind == "workout":
            target_date = candidate.target_date or today
            workout = await session.scalar(
                select(Workout)
                .where(
                    Workout.athlete_id == athlete.id,
                    Workout.date == target_date,
                    Workout.status == "active",
                )
                .order_by(desc(Workout.created_at))
                .limit(1)
            )
            if workout:
                if candidate.correct_athlete_name:
                    corrected_athlete = await session.scalar(
                        select(Athlete).where(Athlete.name == candidate.correct_athlete_name)
                    )
                    if corrected_athlete:
                        workout.athlete_id = corrected_athlete.id
                if candidate.negate:
                    workout.status = "void"
                elif candidate.field:
                    if candidate.field in {"type", "duration_minutes", "rpe", "notes", "date"}:
                        setattr(workout, candidate.field, candidate.new_value)
                    elif candidate.field in {"grade", "original_grade"}:
                        details = dict(workout.structured_details)
                        climbing = [dict(item) for item in details.get("climbing", [])]
                        for entry in reversed(climbing):
                            if candidate.old_value is None or entry.get("original_grade") == str(
                                candidate.old_value
                            ):
                                entry["original_grade"] = str(candidate.new_value)
                                break
                        details["climbing"] = climbing
                        workout.structured_details = details
                        for entry in reversed(workout.entries):
                            if candidate.old_value is None or entry.original_grade == str(
                                candidate.old_value
                            ):
                                entry.original_grade = str(candidate.new_value)
                                entry.grade_rank = grade_rank(
                                    entry.grade_system, entry.original_grade
                                )
                                break
                    else:
                        workout.structured_details = {
                            **workout.structured_details,
                            candidate.field: candidate.new_value,
                        }
                workout.fingerprint = fingerprint_existing(workout)
                correction.target_id = workout.id
                correction.status = "applied"
        elif candidate.target_kind == "daily_state":
            target_date = candidate.target_date or today
            state = await session.scalar(
                select(DailyState).where(
                    DailyState.athlete_id == athlete.id, DailyState.date == target_date
                )
            )
            if state and candidate.field:
                state.values = {**state.values, candidate.field: candidate.new_value}
                correction.target_id = state.id
                correction.status = "applied"

        await session.flush()
        return correction
