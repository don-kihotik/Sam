"""Private athlete membership and multi-group invitation primitives.

These functions intentionally do not commit. The caller owns the transaction
that records an inbound Telegram update alongside any membership mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Athlete,
    Invitation,
    SharedChat,
    SharedChatMembership,
    TrainingPreference,
)
from app.onboarding import start_profile_onboarding


@dataclass(slots=True)
class InvitationRedemption:
    athlete: Athlete
    invitation: Invitation
    created: bool
    pending_message: dict


def _token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def ensure_preference(session: AsyncSession, athlete: Athlete) -> TrainingPreference:
    """Return an athlete's preference row, defaulting to private-only settings."""
    preference = await session.get(TrainingPreference, athlete.id)
    if preference is None:
        preference = TrainingPreference(
            athlete_id=athlete.id,
            profile={},
            private_chat_id=None,
            shared_chat_id=None,
            morning_time=None,
            catalog_mode=False,
        )
        session.add(preference)
        await session.flush()
    return preference


async def ensure_shared_chat(
    session: AsyncSession,
    *,
    telegram_chat_id: int,
    title: str | None,
    created_by: Athlete,
) -> SharedChat:
    if telegram_chat_id >= 0:
        raise ValueError("A shared Telegram chat must have a negative chat ID.")
    chat = await session.get(SharedChat, telegram_chat_id)
    if chat is None:
        chat = SharedChat(
            telegram_chat_id=telegram_chat_id,
            title=(title or "").strip()[:200] or None,
            created_by=created_by.id,
            active=True,
        )
        session.add(chat)
        await session.flush()
    else:
        chat.active = True
        if title and title.strip():
            chat.title = title.strip()[:200]
    return chat


async def grant_group_access(
    session: AsyncSession,
    *,
    athlete: Athlete,
    telegram_chat_id: int,
    role: str = "member",
) -> SharedChatMembership:
    chat = await session.get(SharedChat, telegram_chat_id)
    if chat is None or not chat.active:
        raise ValueError("Shared chat is not registered.")
    key = {"telegram_chat_id": telegram_chat_id, "athlete_id": athlete.id}
    membership = await session.get(SharedChatMembership, key)
    if membership is None:
        membership = SharedChatMembership(**key, role=role, active=True)
        session.add(membership)
    else:
        membership.active = True
        if role == "admin":
            membership.role = "admin"
    await session.flush()
    return membership


async def is_group_member(
    session: AsyncSession, *, athlete_id: int, telegram_chat_id: int
) -> bool:
    membership = await session.get(
        SharedChatMembership,
        {"telegram_chat_id": telegram_chat_id, "athlete_id": athlete_id},
    )
    if membership is None or not membership.active:
        return False
    chat = await session.get(SharedChat, telegram_chat_id)
    return bool(chat and chat.active)


async def revoke_group_access(
    session: AsyncSession, *, athlete_id: int, telegram_chat_id: int | None = None
) -> int:
    rows = (
        await session.scalars(
            select(SharedChatMembership).where(
                SharedChatMembership.athlete_id == athlete_id,
                SharedChatMembership.active.is_(True),
            )
        )
    ).all()
    changed = 0
    for membership in rows:
        if telegram_chat_id is None or membership.telegram_chat_id == telegram_chat_id:
            membership.active = False
            changed += 1
    return changed


async def create_invitation(
    session: AsyncSession,
    *,
    created_by: Athlete,
    alexey_telegram_user_id: int | None,
    now: datetime,
    expires_in: timedelta = timedelta(days=7),
    target_telegram_user_id: int | None = None,
    shared_chat_id: int | None = None,
    payload: dict | None = None,
) -> tuple[str, Invitation]:
    """Create an opaque invitation, only for the configured numeric admin."""
    if alexey_telegram_user_id is None or created_by.telegram_user_id != alexey_telegram_user_id:
        raise PermissionError("Only the configured Alexey admin can create invitations.")
    token = token_urlsafe(24)
    invitation = Invitation(
        token_hash=_token_hash(token),
        created_by=created_by.id,
        expires_at=now + expires_in,
        target_telegram_user_id=target_telegram_user_id,
        shared_chat_id=shared_chat_id,
        payload=payload or {},
    )
    session.add(invitation)
    await session.flush()
    return token, invitation


async def create_group_invitation(
    session: AsyncSession,
    *,
    created_by: Athlete,
    alexey_telegram_user_id: int | None,
    telegram_chat_id: int,
    chat_title: str | None,
    target_telegram_user_id: int,
    pending_message: dict,
    now: datetime,
) -> tuple[str, Invitation]:
    """Register the current chat and bind a one-click invite to the replied user."""
    if (
        alexey_telegram_user_id is None
        or created_by.telegram_user_id != alexey_telegram_user_id
    ):
        raise PermissionError("Only the configured Alexey admin can create invitations.")
    if target_telegram_user_id <= 0 or target_telegram_user_id == created_by.telegram_user_id:
        raise ValueError("Choose another Telegram user.")
    await ensure_shared_chat(
        session,
        telegram_chat_id=telegram_chat_id,
        title=chat_title,
        created_by=created_by,
    )
    await grant_group_access(
        session,
        athlete=created_by,
        telegram_chat_id=telegram_chat_id,
        role="admin",
    )
    return await create_invitation(
        session,
        created_by=created_by,
        alexey_telegram_user_id=alexey_telegram_user_id,
        now=now,
        target_telegram_user_id=target_telegram_user_id,
        shared_chat_id=telegram_chat_id,
        payload={"pending_message": pending_message},
    )


def _athlete_name(display_name: str, telegram_user_id: int) -> str:
    """A stable unique internal name; Telegram display names are not authorization."""
    cleaned = " ".join(display_name.split()).strip()[:48]
    return f"{cleaned or 'Athlete'} #{telegram_user_id}"


async def redeem_invitation(
    session: AsyncSession,
    *,
    token: str,
    telegram_user_id: int,
    display_name: str,
    chat_id: int,
    now: datetime,
) -> Athlete:
    """Compatibility wrapper returning only the redeemed athlete."""
    return (
        await redeem_invitation_details(
            session,
            token=token,
            telegram_user_id=telegram_user_id,
            display_name=display_name,
            chat_id=chat_id,
            now=now,
        )
    ).athlete


async def redeem_invitation_details(
    session: AsyncSession,
    *,
    token: str,
    telegram_user_id: int,
    display_name: str,
    chat_id: int,
    now: datetime,
) -> InvitationRedemption:
    """Redeem a private deep link and atomically grant its bound group."""
    if chat_id != telegram_user_id:
        raise ValueError("Invitation links can only be redeemed in a private chat.")
    if not token or len(token) > 256:
        raise ValueError("Invitation link is invalid or expired.")
    invitation = await session.scalar(
        select(Invitation)
        .where(Invitation.token_hash == _token_hash(token))
        .with_for_update()
    )
    if (
        invitation is None
        or invitation.redeemed_by is not None
        or _aware(invitation.expires_at) <= _aware(now)
        or (
            invitation.target_telegram_user_id is not None
            and invitation.target_telegram_user_id != telegram_user_id
        )
    ):
        raise ValueError("Invitation link is invalid, expired, or already used.")
    athlete = await session.scalar(
        select(Athlete).where(Athlete.telegram_user_id == telegram_user_id)
    )
    created = athlete is None
    if athlete is None:
        athlete = Athlete(
            name=_athlete_name(display_name, telegram_user_id),
            telegram_user_id=telegram_user_id,
            profile={},
            # The configured default is a location default, not copied person data.
            timezone="America/Toronto",
            active=True,
        )
        session.add(athlete)
        await session.flush()
    elif not athlete.active:
        raise ValueError("This Telegram account is inactive.")
    preference = await ensure_preference(session, athlete)
    preference.private_chat_id = chat_id
    if created:
        start_profile_onboarding(preference, now=now)
    if invitation.shared_chat_id is not None:
        await grant_group_access(
            session,
            athlete=athlete,
            telegram_chat_id=invitation.shared_chat_id,
        )
    pending = dict((invitation.payload or {}).get("pending_message") or {})
    invitation.redeemed_by = telegram_user_id
    # The quoted Telegram payload is needed only until consent; do not retain it.
    invitation.payload = {}
    await session.flush()
    return InvitationRedemption(
        athlete=athlete,
        invitation=invitation,
        created=created,
        pending_message=pending,
    )
