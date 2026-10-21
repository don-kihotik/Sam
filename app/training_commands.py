"""Authenticated, deterministic Telegram training commands.

Command handlers only make bounded database changes.  Free-form onboarding and
training reports remain normal durable messages so the record pipeline retains
their evidence and attribution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Athlete, CatalogSource, Correction, Plan
from app.membership import create_invitation, ensure_preference
from app.planning import (
    begin_log,
    confirm_planned_session,
    latest_session_plan,
    planning_context,
    render_training_plan,
)


@dataclass(slots=True)
class CommandResult:
    text: str
    payload: dict = field(default_factory=dict)
    model_request: str | None = None


_COMMAND = re.compile(r"^/(?P<name>[a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(?P<arg>.*))?$", re.IGNORECASE)
_TRAINING_COMMANDS = {
    "start",
    "whoami",
    "setup",
    "invite",
    "today",
    "week",
    "program",
    "session",
    "log",
    "confirm",
    "skip",
    "export",
    "adjust",
    "shorten",
    "replace",
    "reschedule",
    "morning",
    "timezone",
    "sharing",
    "catalog",
}


def session_buttons(plan_id: int) -> dict:
    return {
        "buttons": [
            [
                {"text": "Сессия", "data": f"train:session:{plan_id}"},
                {"text": "Лог", "data": f"train:log:{plan_id}"},
                {"text": "Markdown", "data": f"train:export:{plan_id}"},
            ],
            [
                {"text": "Изменить", "data": f"train:adjust:{plan_id}"},
                {"text": "Сократить", "data": f"train:shorten:{plan_id}"},
            ],
            [
                {"text": "Заменить", "data": f"train:replace:{plan_id}"},
                {"text": "Перенести", "data": f"train:reschedule:{plan_id}"},
                {"text": "Пропустить", "data": f"train:skip:{plan_id}"},
            ],
        ]
    }


# Kept as a local compatibility alias while services uses the public helper for
# newly generated session-plan replies.
_buttons = session_buttons


async def _owned_plan(session: AsyncSession, athlete_id: int, raw_id: str) -> Plan | None:
    try:
        plan_id = int(raw_id)
    except ValueError:
        return None
    if plan_id < 1:
        return None
    plan = await session.get(Plan, plan_id)
    return plan if plan and plan.athlete_id == athlete_id and plan.status == "active" else None


def _message_chat_id(message: object) -> int | None:
    return getattr(message, "telegram_chat_id", None)


async def handle_training_command(
    session: AsyncSession,
    *,
    athlete: Athlete,
    message: object,
    settings: Settings,
    today: date,
) -> CommandResult | None:
    """Handle one private command or return ``None`` for ordinary model flow."""
    raw = (getattr(message, "raw_text", None) or getattr(message, "text", None) or "").strip()
    match = _COMMAND.match(raw)
    if not match:
        return None
    name = match.group("name").casefold()
    arg = (match.group("arg") or "").strip()
    if name == "whoami":
        # Kept here for durable command users; runtime may reply immediately too.
        return CommandResult(f"Твой Telegram user ID: {athlete.telegram_user_id}")
    if name == "history" and (not arg or re.fullmatch(r"[0-9]{1,12}", arg)):
        from app.memory import render_history_audit

        # Same owner/chat-aware renderer as the natural-language audit route,
        # without paying for extraction before a deterministic lookup.
        return CommandResult(
            await render_history_audit(
                session,
                athlete=athlete,
                today=today,
                chat_id=_message_chat_id(message),
                offset=int(arg or "0"),
            )
        )
    if name not in _TRAINING_COMMANDS:
        # Other legacy/general commands retain their normal
        # processor routing and are not subject to this private-only surface.
        return None

    preference = await ensure_preference(session, athlete)
    chat_id = _message_chat_id(message)
    # The documented training surface is private.  This check also prevents a
    # callback in a shared group from becoming a route to private plan IDs.
    if chat_id is None or chat_id != athlete.telegram_user_id:
        return CommandResult("Эта команда доступна только в личном чате с ботом.")
    if preference.private_chat_id is None:
        preference.private_chat_id = chat_id
    elif preference.private_chat_id != chat_id:
        return CommandResult("Команда доступна только из твоего привязанного личного чата.")

    if name == "start":
        return CommandResult(
            "Добро пожаловать. Личный профиль и история сохранены. При необходимости начни с /setup: "
            "цель, доступные дни/время и снаряжение."
        )

    if name == "setup":
        if arg:
            return CommandResult(
                "Сохраняю это как твою личную анкету. Уточню цели, доступные дни и снаряжение.",
                model_request=(
                    "Личная анкета спортсмена. Извлеки только явно сказанные цели, "
                    f"доступность и снаряжение из сообщения: {arg}"
                ),
            )
        return CommandResult(
            "Напиши одной фразой: цель, доступные дни/время и снаряжение. "
            "Например: «Боулдеринг, хочу V5; могу вт/чт/сб вечером; есть турник и резинки»."
        )

    if name == "invite":
        try:
            token, invitation = await create_invitation(
                session,
                created_by=athlete,
                alexey_telegram_user_id=settings.alexey_telegram_user_id,
                now=datetime.now(UTC),
            )
        except PermissionError:
            return CommandResult(
                "Создавать приглашения может только настроенный администратор Alexey."
            )
        expiry = invitation.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        return CommandResult(
            "Одноразовое приглашение (не пересылай в группу):\n"
            f"/start {token}\nДействует до {expiry}. Новый профиль будет пустым и приватным."
        )

    if name == "today":
        plan = await latest_session_plan(session, athlete_id=athlete.id, day=today)
        if plan is None:
            return CommandResult(
                "На сегодня активной сессии нет. Собираю личный черновик на сегодня из анкеты.",
                model_request="Составь безопасный личный план-сессию на сегодня из анкеты спортсмена.",
            )
        return CommandResult(render_training_plan(plan), _buttons(plan.id))

    if name in {"week", "program"}:
        context = await planning_context(session, athlete_id=athlete.id, today=today, public=False)
        desired = context.get(name) if isinstance(context, dict) else None
        if not desired:
            return CommandResult(
                "Активного плана пока нет. Составляю безопасный черновик из твоей личной анкеты.",
                model_request=f"Составь личный {name} тренировок с учётом сохранённой анкеты.",
            )
        if isinstance(desired, dict):
            plan = await _owned_plan(session, athlete.id, str(desired.get("id", "")))
            text = (
                render_training_plan(plan)
                if plan
                else desired.get("summary") or json.dumps(desired, ensure_ascii=False, indent=2)
            )
        else:
            text = str(desired)
        return CommandResult(text)

    if name == "session":
        plan = await _owned_plan(session, athlete.id, arg)
        if plan is None or plan.scope != "session":
            return CommandResult("Сессия не найдена или недоступна.")
        return CommandResult(render_training_plan(plan), _buttons(plan.id))

    if name == "log":
        plan = await _owned_plan(session, athlete.id, arg)
        if plan is None or plan.scope != "session":
            return CommandResult("План для лога не найден или недоступен.")
        try:
            prompt = await begin_log(
                session, athlete_id=athlete.id, plan_id=plan.id, now=datetime.now(UTC)
            )
        except (PermissionError, ValueError):
            return CommandResult("Не удалось открыть этот лог.")
        return CommandResult(
            f"{prompt}\n\nПосле этого напиши фактические результаты простыми словами. "
            "«Готово» само по себе не записывает веса, повторы или попытки.",
            {
                "buttons": [
                    [
                        {
                            "text": "Подтвердить ровно эти значения",
                            "data": f"train:confirm:{plan.id}",
                        }
                    ],
                    [{"text": "Открыть сессию", "data": f"train:session:{plan.id}"}],
                ]
            },
        )

    if name == "confirm":
        plan = await _owned_plan(session, athlete.id, arg)
        if plan is None or plan.scope != "session":
            return CommandResult("План для подтверждения не найден или недоступен.")
        try:
            workout, status = await confirm_planned_session(
                session,
                athlete_id=athlete.id,
                plan_id=plan.id,
                message=message,
                today=today,
                now=datetime.now(UTC),
            )
        except (PermissionError, ValueError):
            return CommandResult("Не удалось подтвердить этот план.")
        if status == "needs_log_confirmation":
            return CommandResult("Сначала открой /log для этой сессии и проверь показанные числа.")
        if status == "needs_actual_details":
            return CommandResult(
                "В плане нет точных чисел для переноса. Напиши, что ты фактически сделал."
            )
        if status == "already_recorded":
            return CommandResult(
                "Эта тренировка уже записана; напиши поправку, если нужно что-то изменить."
            )
        if workout is None:
            return CommandResult(
                "Не удалось сохранить подтверждение. Напиши фактические результаты."
            )
        return CommandResult(
            f"Записал фактическую тренировку #{workout.id} ровно с подтверждёнными значениями плана."
        )

    if name == "skip":
        try:
            plan_id = int(arg)
        except ValueError:
            return CommandResult("Сессия не найдена или недоступна.")
        plan = await session.get(Plan, plan_id)
        if plan is None or plan.athlete_id != athlete.id or plan.scope != "session":
            return CommandResult("Сессия не найдена или недоступна.")
        if plan.status == "skipped":
            return CommandResult("Эта сессия уже отмечена как пропущенная.")
        if plan.status != "active":
            return CommandResult("Эту сессию уже нельзя пропустить.")
        source_message_id = getattr(message, "id", None)
        if not isinstance(source_message_id, int):
            return CommandResult("Не удалось безопасно отметить пропуск. Повтори команду.")
        plan.status = "skipped"
        session.add(
            Correction(
                athlete_id=athlete.id,
                source_message_id=source_message_id,
                target_kind="plan",
                target_id=plan.id,
                payload={"action": "skip", "plan_id": plan.id},
                status="applied",
            )
        )
        return CommandResult("Сессия отмечена как пропущенная. Фактическая тренировка не создана.")

    if name in {"adjust", "shorten", "replace", "reschedule"}:
        plan = await _owned_plan(session, athlete.id, arg)
        if plan is None or plan.scope != "session":
            return CommandResult("Сессия не найдена или недоступна.")
        wording = {
            "adjust": "что именно изменить и на какие числа",
            "shorten": "что убрать или сколько минут оставить",
            "replace": "какой блок заменить и чем",
            "reschedule": "новую дату в формате YYYY-MM-DD",
        }[name]
        return CommandResult(
            f"Для сессии #{plan.id} напиши, {wording}. Ничего не меняю без этого уточнения.",
            model_request=(
                f"Пользователь открыл действие {name} для личной сессии #{plan.id}. "
                "Не изменяй и не сохраняй план: попроси только недостающие точные величины или дату."
            ),
        )

    if name == "export":
        plan = await _owned_plan(session, athlete.id, arg)
        if plan is None:
            return CommandResult("План для экспорта не найден или недоступен.")
        text = render_training_plan(plan, detailed=True)
        return CommandResult(
            "Подробности занятия — в файле.",
            {"document": {"filename": f"sam-plan-{plan.id}.md", "text": text}},
        )

    if name == "morning":
        if arg.casefold() == "off":
            preference.morning_time = None
            return CommandResult("Утренний план отключён.")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", arg):
            return CommandResult("Формат: /morning HH:MM или /morning off")
        preference.morning_time = arg
        return CommandResult(f"Утренний план включён на {arg} по твоему часовому поясу.")

    if name == "timezone":
        try:
            timezone = ZoneInfo(arg)
        except (ZoneInfoNotFoundError, ValueError):
            return CommandResult(
                "Не знаю такой часовой пояс. Используй IANA-имя, например America/Toronto."
            )
        athlete.timezone = timezone.key
        return CommandResult(f"Часовой пояс: {timezone.key}.")

    if name == "sharing":
        value = arg.casefold()
        if value not in {"on", "off"}:
            return CommandResult("Формат: /sharing on или /sharing off")
        if value == "on":
            if settings.telegram_allowed_chat_id is None:
                return CommandResult("Общий чат не настроен; приватность не менялась.")
            preference.shared_chat_id = settings.telegram_allowed_chat_id
            return CommandResult(
                "Ответы на твои вопросы в настроенной группе включены: их видят участники группы. "
                "Сэм обсуждает только тренировки автора вопроса, не чужие записи. Личные заметки и фото остаются приватными."
            )
        preference.shared_chat_id = None
        return CommandResult(
            "Общий доступ отключён. Новые данные остаются только в личном контексте."
        )

    if name == "catalog":
        value = arg.casefold()
        if not value or value == "status":
            queued = await session.scalar(
                select(func.count())
                .select_from(CatalogSource)
                .where(CatalogSource.athlete_id == athlete.id)
            )
            mode = "включён" if preference.catalog_mode else "выключен"
            queue = f"В личной очереди: {queued} фото." if queued else "Личная очередь фото пуста."
            return CommandResult(f"Режим каталога {mode}. {queue}")
        if value not in {"on", "off"}:
            return CommandResult("Формат: /catalog on или /catalog off")
        preference.catalog_mode = value == "on"
        return CommandResult(
            "Режим каталога включён: следующие фото будут только поставлены в личную очередь каталога."
            if preference.catalog_mode
            else "Режим каталога выключен."
        )

    # Other non-training slash commands continue through
    # the normal durable processor.
    return None
