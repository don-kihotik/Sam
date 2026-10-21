"""Private athlete membership and invitation primitives.

These functions intentionally do not commit.  The caller owns the transaction that
records an inbound Telegram update alongside any membership mutation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Athlete, Invitation, TrainingPreference


def _token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


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


async def create_invitation(
    session: AsyncSession,
    *,
    created_by: Athlete,
    alexey_telegram_user_id: int | None,
    now: datetime,
    expires_in: timedelta = timedelta(days=7),
) -> tuple[str, Invitation]:
    """Create an unredeemed opaque invitation, only for the configured numeric admin."""
    if alexey_telegram_user_id is None or created_by.telegram_user_id != alexey_telegram_user_id:
        raise PermissionError("Only the configured Alexey admin can create invitations.")
    token = token_urlsafe(24)
    invitation = Invitation(
        token_hash=_token_hash(token),
        created_by=created_by.id,
        expires_at=now + expires_in,
    )
    session.add(invitation)
    await session.flush()
    return token, invitation


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
    """Atomically redeem one valid token in a private Telegram chat.

    ``chat_id == telegram_user_id`` is Telegram's private-chat invariant and
    prevents a deep-link token being redeemed from a group conversation.
    """
    if chat_id != telegram_user_id:
        raise ValueError("Invitation links can only be redeemed in a private chat.")
    if not token or len(token) > 256:
        raise ValueError("Invitation link is invalid or expired.")

    existing = await session.scalar(
        select(Athlete).where(Athlete.telegram_user_id == telegram_user_id)
    )
    if existing is not None:
        raise ValueError("This Telegram account is already registered.")

    # Conditional update makes a token single-use even if two updates race.
    claimed = await session.execute(
        update(Invitation)
        .where(
            Invitation.token_hash == _token_hash(token),
            Invitation.redeemed_by.is_(None),
            Invitation.expires_at > now,
        )
        .values(redeemed_by=telegram_user_id)
    )
    if claimed.rowcount != 1:
        raise ValueError("Invitation link is invalid, expired, or already used.")

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
    preference = await ensure_preference(session, athlete)
    preference.private_chat_id = chat_id
    return athlete
