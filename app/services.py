from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import AIService
from app.analytics import grade_rank, refresh_analytics_snapshots
from app.coaching import (
    align_session_plan,
    generate_checked_reply,
    intent_question,
    requested_session_minutes,
    useful_followup,
    validate_catalog_choices,
)
from app.config import Settings
from app.conversation_tasks import complete_planning_task, sync_open_planning_task
from app.db.models import (
    Athlete,
    BacklogItem,
    BodyMeasurement,
    Correction,
    DailyState,
    Event,
    Fact,
    FollowUp,
    Memory,
    Message,
    Outbox,
    Plan,
    ProcessingRun,
    TrainingPreference,
    Workout,
    WorkoutEntry,
)
from app.grades import normalize_grade
from app.membership import ensure_preference, is_group_member
from app.memory import (
    ContextBuilder,
    is_history_audit_request,
    recent_message_text,
    render_history_audit,
    semantic_history,
)
from app.onboarding import (
    complete_first_session,
    first_session_clarification,
    first_session_context,
    first_session_minutes,
    first_session_missing,
    first_session_pending,
    profile_onboarding_reply,
)
from app.planning import save_plan
from app.prompts import PROMPT_VERSION
from app.readiness import recent_illness_unresolved
from app.records import retire_related, retract_edited_source, save_session
from app.request_policy import preflight_request
from app.schemas import CoachReply, CorrectionCandidate, MessageExtraction, PlanCandidate
from app.session_intent import resolve_session_intent
from app.training_commands import handle_training_command, session_buttons
from app.training_profile import apply_training_profile

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
    is_edit: bool = False


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
    if (
        is_reply_to_sam
        or lowered.startswith("/")
        or re.search(
            r"(?:^|[,!?]\s*)(?:сэм|sam)(?:[,!:?]|\s+(?:ты|что|как|помоги|запомни|проверь|напомни|разбери|да|please|can|what|help))",
            lowered,
        )
    ):
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
        self.context_builder = context_builder or ContextBuilder(
            settings.recent_message_limit, group_chat_id=settings.telegram_allowed_chat_id
        )

    async def process(self, session: AsyncSession, incoming: IncomingMessage) -> ProcessResult:
        athlete = await session.scalar(
            select(Athlete).where(
                Athlete.telegram_user_id == incoming.telegram_user_id, Athlete.active.is_(True)
            )
        )
        if athlete is None:
            return ProcessResult(ignored_unknown_user=True)
        preference = await ensure_preference(session, athlete)
        if incoming.telegram_chat_id < 0 and not await is_group_member(
            session,
            athlete_id=athlete.id,
            telegram_chat_id=incoming.telegram_chat_id,
        ):
            queued_run = await session.scalar(
                select(ProcessingRun)
                .join(Message, Message.id == ProcessingRun.message_id)
                .where(
                    Message.athlete_id == athlete.id,
                    Message.telegram_chat_id == incoming.telegram_chat_id,
                    Message.telegram_message_id == incoming.telegram_message_id,
                    ProcessingRun.status.in_(["received", "started", "retry"]),
                )
            )
            if queued_run:
                queued_run.status, queued_run.error = "completed", "sharing_revoked"
                await session.commit()
            return ProcessResult(ignored_unknown_user=True)
        if incoming.telegram_chat_id > 0:
            if incoming.telegram_chat_id != athlete.telegram_user_id:
                return ProcessResult(ignored_unknown_user=True)
            preference.private_chat_id = incoming.telegram_chat_id
        message = await session.scalar(
            select(Message).where(
                Message.telegram_chat_id == incoming.telegram_chat_id,
                Message.telegram_message_id == incoming.telegram_message_id,
            )
        )
        if message:
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == message.id)
            )
            if run and run.status == "completed" and not incoming.is_edit:
                return ProcessResult(duplicate=True, message_id=message.id, athlete_id=athlete.id)
            if run is None:
                run = ProcessingRun(message_id=message.id)
                session.add(run)
        else:
            message = Message(
                telegram_message_id=incoming.telegram_message_id,
                telegram_chat_id=incoming.telegram_chat_id,
                telegram_user_id=incoming.telegram_user_id,
                athlete_id=athlete.id,
                direction="incoming",
                message_type=incoming.message_type,
                telegram_timestamp=incoming.timestamp,
                revision=1,
            )
            session.add(message)
            await session.flush()
            run = ProcessingRun(message_id=message.id)
            session.add(run)
        message.raw_text = incoming.text
        message.transcript = incoming.transcript
        message.reply_to_message_id = incoming.reply_to_message_id
        message.message_type = incoming.message_type
        message.attachment_data = {
            **(message.attachment_data or {}),
            **(incoming.attachment_metadata or {}),
        }
        run.status = "started"
        run.prompt_version = PROMPT_VERSION
        run.extraction_model = self.settings.extraction_model
        run.coaching_model = self.settings.sam_model
        run.attempts = (run.attempts or 0) + 1
        await session.commit()
        message_id, athlete_id = message.id, athlete.id
        try:
            today = incoming.timestamp.astimezone(ZoneInfo(athlete.timezone)).date()
            denied = await preflight_request(
                session, athlete=athlete, message=message, ai=self.ai, settings=self.settings
            )
            if denied:
                addressed = incoming.telegram_chat_id > 0 or is_directly_addressed(
                    incoming.text,
                    is_reply_to_sam=incoming.is_reply_to_sam,
                    allow_transcription_aliases=incoming.message_type in {"voice", "video_note"},
                )
                if addressed:
                    session.add(
                        Outbox(
                            source_message_id=message.id,
                            revision=message.revision,
                            athlete_id=athlete.id,
                            telegram_chat_id=incoming.telegram_chat_id,
                            reply_to_message_id=incoming.telegram_message_id,
                            text=denied,
                        )
                    )
                run.status, run.error, run.next_attempt_at = "completed", None, None
                run.extracted = {
                    "policy": (message.processing_data or {})
                    .get("request_policy", {})
                    .get("decision")
                }
                await session.commit()
                return ProcessResult(
                    reply=denied if addressed else None,
                    message_id=message.id,
                    athlete_id=athlete.id,
                    mutations=[],
                )
            command = await handle_training_command(
                session, athlete=athlete, message=message, settings=self.settings, today=today
            )
            if command and not command.model_request:
                session.add(
                    Outbox(
                        source_message_id=message.id,
                        revision=message.revision,
                        athlete_id=athlete.id,
                        telegram_chat_id=incoming.telegram_chat_id,
                        reply_to_message_id=incoming.telegram_message_id,
                        text=command.text,
                        payload=command.payload,
                    )
                )
                run.status, run.error, run.next_attempt_at = "completed", None, None
                run.extracted = {"command": incoming.text.split()[0]}
                await session.commit()
                return ProcessResult(
                    reply=command.text, message_id=message.id, athlete_id=athlete.id, mutations=[]
                )
            recent = await recent_message_text(
                session,
                athlete.id,
                limit=24,
                chat_id=incoming.telegram_chat_id,
                exclude_message_id=message.id,
            )
            planning_task = await sync_open_planning_task(
                session, preference=preference, message=message, now=incoming.timestamp,
            )
            followup = await session.scalar(
                select(FollowUp)
                .where(
                    FollowUp.athlete_id == athlete.id,
                    FollowUp.telegram_chat_id == incoming.telegram_chat_id,
                    FollowUp.status == "pending",
                    FollowUp.expires_at > incoming.timestamp,
                )
                .order_by(desc(FollowUp.id))
                .limit(1)
            )
            session_context = await self.context_builder.extraction_context(
                session,
                athlete=athlete,
                chat_id=incoming.telegram_chat_id,
                reply_to_message_id=incoming.reply_to_message_id,
                followup=followup,
            )
            if planning_task:
                session_context += "\n" + planning_task.model_context()
            intent = await resolve_session_intent(
                session, athlete_id=athlete.id, chat_id=incoming.telegram_chat_id,
                text=incoming.text, today=today, now=incoming.timestamp,
                current_message_id=message.id,
            )
            first_pending = first_session_pending(preference)
            first_missing = (
                first_session_missing(incoming.text, session_date=intent.date)
                if first_pending
                else ()
            )
            first_minutes = first_session_minutes(incoming.text) if first_pending else None
            first_context = ""
            if first_pending and not first_missing and intent.date and first_minutes:
                first_context = first_session_context(
                    session_date=intent.date, minutes=first_minutes
                )
                session_context += first_context
            intent_target = await session.get(Plan, intent.target_plan_id) if intent.target_plan_id else None
            illness_unresolved = await recent_illness_unresolved(
                session, athlete_id=athlete.id, chat_id=incoming.telegram_chat_id, now=incoming.timestamp,
            )
            intent_context = "\nSESSION INTENT (verified calendar and owned conversation link):\n" + json.dumps({
                "date": str(intent.date) if intent.date else None,
                "weekday": intent.date.strftime("%A") if intent.date else None,
                "target_plan_id": intent.target_plan_id,
                "ambiguity": intent.ambiguity,
                "current_session": intent_target.content if intent_target else None,
                "recent_reported_illness_without_explicit_recovery": illness_unresolved,
                "requested_max_minutes": first_minutes
                or requested_session_minutes(incoming.text),
            }, ensure_ascii=False)
            session_context += intent_context
            if command and command.model_request:
                session_context += (
                    "\nCOMMAND INTENT (not factual evidence):\n" + command.model_request
                )
            addressed = (
                is_directly_addressed(
                    incoming.text,
                    is_reply_to_sam=incoming.is_reply_to_sam,
                    allow_transcription_aliases=incoming.message_type in {"voice", "video_note"},
                )
                or incoming.telegram_chat_id > 0
            )
            extraction = await self.ai.extract(
                text=incoming.text,
                athlete_name=athlete.name,
                local_date=today.isoformat(),
                timezone_name=athlete.timezone,
                recent_context=recent + "\n\n" + session_context,
                directly_addressed=addressed,
            )
            remove_inferred_numeric_ratings(incoming.text, extraction)
            if extraction.plan.action != "none" and extraction.plan.scope == "session":
                if intent.ambiguity:
                    extraction.plan.action = "none"
                elif addressed or extraction.conversation_intent == "continuation" or extraction.should_respond:
                    # One writer per coached session: the extractor records facts,
                    # the coach constructs the complete validated revision. Saving
                    # both would supersede the target before the coach can use it.
                    intent_context += "\nSession changes requested; the coach must return the full structured revision."
                    extraction.plan.action = "none"
                else:
                    extraction.plan = align_session_plan(extraction.plan, intent, intent_target)
            for extra in extraction.additional_workouts:
                remove_inferred_numeric_ratings(
                    incoming.text, MessageExtraction(normalized_text=incoming.text, workout=extra)
                )
            message.normalized_text = extraction.normalized_text
            mutations = []
            if incoming.is_edit:
                mutations.extend(await retract_edited_source(session, message))
            if extraction.answers_follow_up and followup:
                extraction.workout.target_workout_id = (
                    extraction.workout.target_workout_id or followup.workout_id
                )
                if extraction.workout.present:
                    extraction.workout.session_action = "append"
                followup.status = "answered"
            run.extracted = extraction.model_dump(mode="json")
            payload = (incoming.attachment_metadata or {}).get("telegram_payload", {})
            evidence_text = incoming.text
            if (incoming.attachment_metadata or {}).get("vision_analysis"):
                evidence_text = " ".join(
                    filter(
                        None, [payload.get("text") or payload.get("caption"), incoming.transcript]
                    )
                )
            mutations.extend(
                await self._apply_extraction(
                    session,
                    athlete=athlete,
                    message=message,
                    extraction=extraction,
                    today=today,
                    source_text=evidence_text,
                )
            )
            try:
                message.embedding = await self.ai.embed(incoming.text)
            except Exception:  # noqa: BLE001 - embeddings are optional
                mutations.append({"kind": "embedding", "status": "unavailable"})
            onboarding_step = None
            if incoming.telegram_chat_id > 0:
                onboarding_step = profile_onboarding_reply(
                    preference, now=incoming.timestamp
                )
                if onboarding_step is None and first_pending and first_missing:
                    onboarding_step = first_session_clarification(first_missing)
            # Banter is stored as conversation, never used as a coaching interrupt.
            should_reply = (
                addressed
                or extraction.conversation_intent == "continuation"
                or (extraction.should_respond and not extraction.is_banter)
                or onboarding_step is not None
            )
            if extraction.is_banter and not addressed:
                should_reply = False
            needs_clarification = any(
                m.get("status") == "needs_session_clarification" for m in mutations
            )
            reply = None
            reply_payload = {}
            if should_reply or needs_clarification:
                if onboarding_step is not None:
                    reply = onboarding_step.text
                    reply_payload = onboarding_step.payload
                    coach_reply = CoachReply(text=reply)
                elif intent.ambiguity and not extraction.workout.present:
                    reply = intent_question(intent.ambiguity)
                    coach_reply = CoachReply(text=reply)
                elif needs_clarification:
                    reply = "Уточни, к какой тренировке относится поправка — дата или ответ на её запись?"
                    coach_reply = CoachReply(text=reply)
                elif is_history_audit_request(incoming.text):
                    reply = await render_history_audit(
                        session,
                        athlete=athlete,
                        today=today,
                        chat_id=incoming.telegram_chat_id,
                        offset=int(incoming.text.split()[1])
                        if re.fullmatch(r"/history\s+\d+", incoming.text.strip())
                        else 0,
                    )
                    coach_reply = CoachReply(text=reply)
                else:
                    matches = await semantic_history(
                        session,
                        athlete_id=athlete.id,
                        query_embedding=message.embedding,
                        exclude_message_id=message.id,
                        chat_id=incoming.telegram_chat_id,
                    )
                    context = await self.context_builder.build(
                        session,
                        athlete=athlete,
                        today=today,
                        current_message=incoming.text,
                        semantic_matches=matches,
                        chat_id=incoming.telegram_chat_id,
                        extraction_summary=json.dumps(
                            {
                                "candidate": extraction.model_dump(mode="json", exclude_none=True),
                                "mutation_results": mutations,
                            },
                            ensure_ascii=False,
                        ),
                        session_date=intent.date or today,
                    )
                    context += intent_context
                    context += first_context
                    if planning_task:
                        context += "\n" + planning_task.model_context()
                    if command and command.model_request:
                        context += "\nCOMMAND INTENT:\n" + command.model_request
                    async def save_candidate(candidate):
                        async with session.begin_nested():
                            if (
                                planning_task
                                and planning_task.requested_scope == "program"
                                and candidate.scope == "program"
                                and candidate.action == "create"
                            ):
                                existing_program_query = select(Plan).where(
                                    Plan.athlete_id == athlete.id,
                                    Plan.scope == "program",
                                    Plan.status == "active",
                                )
                                if incoming.telegram_chat_id < 0:
                                    existing_program_query = existing_program_query.where(
                                        Plan.visibility == "group"
                                    )
                                existing_program = await session.scalar(
                                    existing_program_query.order_by(desc(Plan.id)).limit(1)
                                )
                                if existing_program:
                                    candidate = candidate.model_copy(
                                        update={
                                            "action": "update",
                                            "target_plan_id": existing_program.id,
                                        },
                                        deep=True,
                                    )
                            await validate_catalog_choices(
                                session, candidate, illness_unresolved=illness_unresolved, as_of=today,
                            )
                            record = await self._upsert_plan(
                                session, athlete_id=athlete.id, candidate=candidate,
                                source_message_id=message.id,
                                evidence=[candidate.summary or "Generated plan"], today=today,
                                max_duration_minutes=first_minutes
                                or requested_session_minutes(incoming.text),
                            )
                            if planning_task and record.scope == planning_task.requested_scope:
                                record.source_message_ids = list(dict.fromkeys(
                                    [*planning_task.source_message_ids, message.id]
                                ))
                            return record

                    checked = await generate_checked_reply(
                        ai=self.ai, context=context, safety_identifier=f"telegram-{athlete.id}",
                        message_id=message.id, intent=intent, target=intent_target,
                        save_candidate=save_candidate, request_text=incoming.text,
                        planning_task=planning_task,
                    )
                    coach_reply, reply = checked.reply, checked.text
                    run.extracted = {**run.extracted, "coaching_diagnostics": checked.diagnostics}
                    if checked.record:
                        if planning_task and checked.record.scope == planning_task.requested_scope:
                            complete_planning_task(
                                preference, planning_task, plan_id=checked.record.id,
                            )
                        mutations.append({"kind": "plan", "id": checked.record.id, "status": "saved"})
                        if first_pending and checked.record.scope == "session":
                            complete_first_session(
                                preference,
                                plan_id=checked.record.id,
                                now=incoming.timestamp,
                            )
                        if checked.record.scope == "session" and incoming.telegram_chat_id > 0:
                            reply_payload = session_buttons(checked.record.id)
                pending_questions = list(await session.scalars(select(FollowUp.question).where(
                    FollowUp.athlete_id == athlete.id,
                    FollowUp.telegram_chat_id == incoming.telegram_chat_id,
                    FollowUp.status.in_(["pending", "awaiting_delivery"]),
                    FollowUp.expires_at > incoming.timestamp,
                )))
                question = useful_followup(coach_reply.follow_up_question, pending_questions)
                if question and not extraction.is_banter:
                    if question.casefold() not in (reply or "").casefold():
                        reply = (reply or "") + "\n\n" + question
                    workout_id = coach_reply.follow_up_workout_id
                    if workout_id:
                        target = await session.get(Workout, workout_id)
                        if not target or target.athlete_id != athlete.id:
                            workout_id = None
                    # One pending question per athlete/chat, with a bounded lifetime.
                    pending = (
                        await session.scalars(
                            select(FollowUp).where(
                                FollowUp.athlete_id == athlete.id,
                                FollowUp.telegram_chat_id == incoming.telegram_chat_id,
                                FollowUp.status.in_(["pending", "awaiting_delivery"]),
                            )
                        )
                    ).all()
                    for p in pending:
                        p.status = "superseded"
                    session.add(
                        FollowUp(
                            athlete_id=athlete.id,
                            telegram_chat_id=incoming.telegram_chat_id,
                            source_message_id=message.id,
                            workout_id=workout_id,
                            question=question,
                            status="awaiting_delivery",
                            expires_at=incoming.timestamp + timedelta(days=2),
                        )
                    )
                if reply:
                    existing_reply = await session.scalar(
                        select(Outbox).where(
                            Outbox.source_message_id == message.id,
                            Outbox.revision == message.revision,
                        )
                    )
                    if existing_reply is None:
                        session.add(
                            Outbox(
                                source_message_id=message.id,
                                revision=message.revision,
                                athlete_id=athlete.id,
                                telegram_chat_id=incoming.telegram_chat_id,
                                reply_to_message_id=incoming.telegram_message_id,
                                text=reply,
                                payload=reply_payload,
                            )
                        )
            run.mutations = mutations
            await refresh_analytics_snapshots(session, athlete.id, today)
            run.status, run.error, run.next_attempt_at = "completed", None, None
            await session.commit()
            return ProcessResult(
                reply=reply, message_id=message_id, athlete_id=athlete_id, mutations=mutations
            )
        except Exception as exc:
            await session.rollback()
            # IDs are captured before rollback; expired ORM attributes cannot be accessed here.
            run = await session.scalar(
                select(ProcessingRun).where(ProcessingRun.message_id == message_id)
            )
            if run:
                run.status = "failed" if run.attempts >= 5 else "retry"
                run.error = type(exc).__name__  # never store API URLs/tokens/response bodies
                run.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 5 * 2**run.attempts)
                )
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
        reply_to_message_id: int | None = None,
        extract_artifacts: bool = False,
    ) -> Message:
        existing = await session.scalar(
            select(Message).where(
                Message.telegram_chat_id == telegram_chat_id,
                Message.telegram_message_id == telegram_message_id,
            )
        )
        if existing:
            return existing
        message = Message(
            telegram_message_id=telegram_message_id,
            telegram_chat_id=telegram_chat_id,
            athlete_id=athlete_id,
            direction="outgoing",
            message_type="text",
            raw_text=text,
            normalized_text=text,
            processing_data={"response_policy": PROMPT_VERSION},
            reply_to_message_id=reply_to_message_id,
            telegram_timestamp=timestamp or datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        await session.commit()
        return message

    async def record_unprocessed(
        self, session: AsyncSession, incoming: IncomingMessage, *, status: str, reason: str
    ) -> ProcessResult:
        athlete = await session.scalar(
            select(Athlete).where(
                Athlete.telegram_user_id == incoming.telegram_user_id, Athlete.active.is_(True)
            )
        )
        if not athlete:
            return ProcessResult(ignored_unknown_user=True)
        message = await session.scalar(
            select(Message).where(
                Message.telegram_chat_id == incoming.telegram_chat_id,
                Message.telegram_message_id == incoming.telegram_message_id,
            )
        )
        if message:
            return ProcessResult(duplicate=True, message_id=message.id, athlete_id=athlete.id)
        message = Message(
            telegram_message_id=incoming.telegram_message_id,
            telegram_chat_id=incoming.telegram_chat_id,
            telegram_user_id=incoming.telegram_user_id,
            athlete_id=athlete.id,
            raw_text=incoming.text,
            transcript=incoming.transcript,
            message_type=incoming.message_type,
            reply_to_message_id=incoming.reply_to_message_id,
            attachment_data=incoming.attachment_metadata or {},
            telegram_timestamp=incoming.timestamp,
        )
        session.add(message)
        await session.flush()
        session.add(
            ProcessingRun(
                message_id=message.id,
                status=status,
                extracted={"pipeline_outcome": reason},
                error=reason if status == "failed" else None,
            )
        )
        await session.commit()
        return ProcessResult(message_id=message.id, athlete_id=athlete.id, mutations=[])

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
        changes = []
        public = message.telegram_chat_id < 0
        visibility = "group" if public else "private"
        if not extraction.is_banter:
            changes.extend(
                await apply_training_profile(
                    session,
                    athlete=athlete,
                    message=message,
                    candidate=extraction.training_profile,
                    text=source_text,
                )
            )
        for index, c in enumerate([extraction.workout, *extraction.additional_workouts]):
            if not c.present or extraction.is_banter:
                continue
            if index:
                c.session_action = "new"
            w, status = await save_session(
                session,
                athlete_id=athlete.id,
                message=message,
                candidate=c,
                source_text=source_text,
                today=today,
            )
            changes.append({"kind": "workout", "id": w.id if w else None, "status": status})
            if w is not None and message.telegram_chat_id > 0:
                preference = await session.get(TrainingPreference, athlete.id)
                if preference and w.plan_id and w.plan_id == preference.log_plan_id:
                    preference.log_workout_id = w.id
        state = extraction.daily_state
        if (
            state.present
            and valid_evidence(source_text, state.evidence)
            and not extraction.is_banter
        ):
            day = state.date or today
            record = await session.scalar(
                select(DailyState).where(
                    DailyState.athlete_id == athlete.id, DailyState.date == day
                )
            )
            values = state.model_dump(exclude={"present", "date", "confidence"}, exclude_none=True)
            # Notes retain origin visibility; shared context uses only numeric measurements.
            values["_visibility"] = visibility
            if record is None:
                record = DailyState(
                    athlete_id=athlete.id, date=day, values=values, source_message_ids=[message.id]
                )
                session.add(record)
            else:
                record.values = {**record.values, **values}
                record.source_message_ids = list(
                    dict.fromkeys([*record.source_message_ids, message.id])
                )
            await session.flush()
            changes.append({"kind": "daily_state", "id": record.id})
        if extraction.weight_kg is not None and valid_evidence(
            source_text, extraction.weight_evidence
        ):
            session.add(
                BodyMeasurement(
                    athlete_id=athlete.id,
                    date=today,
                    weight_kg=extraction.weight_kg,
                    source_message_id=message.id,
                )
            )
            changes.append({"kind": "weight", "status": "saved"})
        for fact in extraction.profile_facts:
            if not valid_evidence(source_text, fact.evidence) or extraction.is_banter:
                continue
            existing = (
                await session.scalars(
                    select(Fact).where(
                        Fact.athlete_id == athlete.id,
                        Fact.field == fact.field,
                        Fact.status == "active",
                    )
                )
            ).all()
            for old in existing:
                old.status = "superseded"
            session.add(
                Fact(
                    athlete_id=athlete.id,
                    field=fact.field[:100],
                    value=fact.value,
                    source_message_id=message.id,
                    visibility=visibility,
                    confidence=1,
                    status="active",
                )
            )
            changes.append({"kind": "profile_fact", "field": fact.field})
        for candidate in extraction.corrections:
            if extraction.is_banter:
                continue
            correction = await self._apply_correction(session, athlete, message, candidate, today)
            changes.append({"kind": "correction", "id": correction.id, "status": correction.status})
        backlog = extraction.backlog
        if (
            backlog.action == "create"
            and backlog.title
            and backlog.item_type
            and not extraction.is_banter
        ):
            exists = await session.scalar(
                select(BacklogItem).where(BacklogItem.original_message_id == message.id)
            )
            if not exists:
                item = BacklogItem(
                    created_by=athlete.id,
                    type=backlog.item_type,
                    title=backlog.title,
                    description=backlog.description,
                    original_message_id=message.id,
                    notes={"visibility": visibility},
                )
                session.add(item)
                await session.flush()
                changes.append({"kind": "backlog", "id": item.id})
        memory = extraction.memory
        if (
            memory.create
            and memory.summary
            and valid_evidence(source_text, memory.evidence)
            and not extraction.is_banter
        ):
            if memory.supersedes_id:
                prior = await session.get(Memory, memory.supersedes_id)
                if prior and prior.athlete_id == athlete.id:
                    prior.status = "retired"
            row = Memory(
                athlete_id=athlete.id,
                date=today,
                kind=memory.kind,
                summary=memory.summary,
                tags=memory.tags,
                source_message_ids=[message.id],
                importance=memory.importance,
                confidence=memory.confidence,
                status="hypothesis" if memory.kind == "coach" else "likely",
                visibility=visibility,
            )
            session.add(row)
            await session.flush()
            changes.append({"kind": "memory", "id": row.id})
        event = extraction.event
        if (
            event.action != "none"
            and valid_evidence(source_text, event.evidence)
            and not extraction.is_banter
        ):
            targets = [athlete]
            # A shared trip can be mentioned, but one athlete never updates
            # another athlete's calendar/profile through conversation.
            for person in targets:
                old = (
                    await session.scalars(
                        select(Event).where(
                            Event.athlete_id == person.id,
                            Event.status == "active",
                            Event.shared_key == "guided-multipitch"
                            if event.shared
                            else Event.athlete_id == person.id,
                        )
                    )
                ).all()
                for e in old:
                    e.status = "superseded"
                prior_details = old[-1].details if old else {}
                row = Event(
                    athlete_id=person.id,
                    name=event.name or "Guided multipitch trip",
                    date=event.date,
                    date_precision=event.date_precision,
                    details={
                        **prior_details,
                        "date_label": event.date_label,
                        "route_type": event.route_type or prior_details.get("route_type"),
                        "guided": event.guided
                        if event.guided is not None
                        else prior_details.get("guided"),
                    },
                    evidence=event.evidence,
                    source_message_ids=[message.id],
                    visibility=visibility,
                    shared_key="guided-multipitch" if event.shared else None,
                )
                session.add(row)
                await session.flush()
                changes.append({"kind": "event", "id": row.id})
                plans = (
                    await session.scalars(
                        select(Plan).where(Plan.athlete_id == person.id, Plan.status == "active")
                    )
                ).all()
                for p in plans:
                    p.content = {
                        **p.content,
                        "review_reason": "target_event_changed",
                        "target_date_label": event.date_label,
                    }
        if extraction.plan.action != "none" and valid_evidence(
            source_text, extraction.plan.evidence
        ):
            try:
                async with session.begin_nested():
                    p = await self._upsert_plan(
                        session,
                        athlete_id=athlete.id,
                        candidate=extraction.plan,
                        source_message_id=message.id,
                        evidence=extraction.plan.evidence,
                        today=today,
                    )
                changes.append({"kind": "plan", "id": p.id})
            except (ValueError, PermissionError) as exc:
                changes.append({"kind": "plan", "status": "rejected_invalid_plan", "reason": str(exc)[:1200]})
        await session.flush()
        return changes

    async def _upsert_plan(
        self,
        session: AsyncSession,
        *,
        athlete_id: int,
        candidate: PlanCandidate,
        source_message_id: int,
        evidence: list[str],
        today: date,
        max_duration_minutes: int | None = None,
    ) -> Plan:
        source = await session.get(Message, source_message_id)
        public = bool(source and source.telegram_chat_id < 0)
        return await save_plan(
            session,
            athlete_id=athlete_id,
            candidate=candidate,
            source_message_id=source_message_id,
            evidence=evidence,
            today=today,
            visibility="group" if public else "private",
            max_duration_minutes=max_duration_minutes,
        )

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
            status="needs_session_clarification",
        )
        session.add(correction)
        if candidate.evidence and not valid_evidence(message.raw_text or "", candidate.evidence):
            correction.status = "rejected_no_current_evidence"
            await session.flush()
            return correction
        model = {
            "workout": Workout,
            "daily_state": DailyState,
            "memory": Memory,
            "fact": Fact,
            "athlete": Fact,
            "plan": Plan,
            "event": Event,
        }[candidate.target_kind]
        query = select(model).where(model.athlete_id == athlete.id)
        if candidate.target_id:
            query = query.where(model.id == candidate.target_id)
        elif hasattr(model, "date"):
            query = query.where(model.date == (candidate.target_date or today))
        elif candidate.field and model is Fact:
            query = query.where(Fact.field == candidate.field)
        else:
            await session.flush()
            return correction
        if hasattr(model, "status"):
            query = query.where(
                model.status.not_in(["void", "retired", "superseded", "void_duplicate"])
            )
        rows = (await session.scalars(query)).all()
        if len(rows) != 1:
            await session.flush()
            return correction
        target = rows[0]
        before = {"type": candidate.target_kind, "id": target.id}
        if model is Workout:
            before.update(
                {
                    "athlete_id": target.athlete_id,
                    "date": str(target.date),
                    "details": target.structured_details,
                    "notes": target.notes,
                }
            )
            old_sources = list(target.source_message_ids)
            # Validate ambiguous entry selection before making ANY mutations.
            if candidate.field in {"grade", "original_grade"} and not candidate.negate:
                matches = [
                    e
                    for e in target.entries
                    if candidate.old_value is None or e.original_grade == str(candidate.old_value)
                ]
                if len(matches) != 1:
                    await session.flush()
                    return correction
            if candidate.correct_athlete_name:
                correction.status = "rejected_cross_athlete"
                await session.flush()
                return correction
            if candidate.negate:
                target.status = "void"
            elif candidate.field in {"grade", "original_grade"}:
                matches = [
                    e
                    for e in target.entries
                    if candidate.old_value is None or e.original_grade == str(candidate.old_value)
                ]
                if len(matches) != 1:
                    await session.flush()
                    return correction
                e = matches[0]
                e.grade_system, e.original_grade = normalize_grade(
                    e.grade_system, str(candidate.new_value)
                )
                e.grade_rank = grade_rank(e.grade_system, e.original_grade)
            elif candidate.field in {"duration_minutes", "rpe", "notes", "type", "date"}:
                value = candidate.new_value
                if candidate.field == "date":
                    value = date.fromisoformat(str(value))
                setattr(target, candidate.field, value)
            elif candidate.field == "pain_status" and candidate.new_value in {
                "unknown",
                "reported",
                "reported_none",
            }:
                target.pain_status = candidate.new_value
            elif candidate.field:
                target.structured_details = {
                    **target.structured_details,
                    candidate.field: candidate.new_value,
                }
            # A corrected session becomes a fresh baseline for subsequent appended contributions.
            details = {**target.structured_details}
            details.pop("contributions", None)
            details["climbing"] = [entry_to_dict(e) for e in target.entries]
            target.structured_details = details
            target.source_message_ids = list(dict.fromkeys([*old_sources, message.id]))
            target.fingerprint = None
            await retire_related(session, athlete.id, old_sources)
        elif model is DailyState and candidate.field:
            before["values"] = target.values
            target.values = {**target.values, candidate.field: candidate.new_value}
        elif model is Memory:
            before["summary"] = target.summary
            if candidate.negate:
                target.status = "retired"
            elif candidate.new_value is not None:
                target.summary = str(candidate.new_value)
                target.confidence = min(candidate.confidence, 0.8)
                target.source_message_ids = [*target.source_message_ids, message.id]
        elif model is Fact:
            before["value"] = target.value
            target.status = "superseded"
            if not candidate.negate and candidate.new_value is not None:
                session.add(
                    Fact(
                        athlete_id=athlete.id,
                        field=target.field,
                        value=candidate.new_value,
                        source_message_id=message.id,
                        visibility=target.visibility,
                        status="active",
                    )
                )
        elif model in {Plan, Event} and candidate.negate:
            target.status = "superseded"
        else:
            await session.flush()
            return correction
        correction.target_id = target.id
        correction.payload = {**correction.payload, "before": before}
        correction.status = "applied"
        await session.flush()
        return correction


def entry_to_dict(e: WorkoutEntry) -> dict:
    return {
        "discipline": e.discipline,
        "grade_system": e.grade_system,
        "original_grade": e.original_grade,
        "count": e.count,
        "completed": None if e.completed_count is None else e.completed_count > 0,
        "attempts": e.attempts,
        "route_length_m": e.route_length_m,
        "notes": e.notes,
        "wall_style": e.wall_style,
        "movement_style": e.movement_style,
    }
