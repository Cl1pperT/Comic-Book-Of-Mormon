"""Users, invites and password resets."""
from datetime import timedelta

from sqlalchemy import select

from ..constants import ROLES
from ..models import Invite, PasswordReset, User, utcnow
from ..security import (check_password, hash_password, new_invite_code, new_token, normalize_email, token_hash,
                        verify_password)
from .audit import log


class AccountError(ValueError):
    """A problem with sign-up or login that can be shown to the person."""


def create_user(db, email, display_name, password, role="reviewer", credit_ok=False, can_choose=False):
    email = normalize_email(email)
    name = (display_name or "").strip()
    if not name or len(name) > 80:
        raise AccountError("Please enter your name.")
    try:
        check_password(password)
    except ValueError as exc:
        raise AccountError(str(exc)) from None
    if role not in ROLES:
        raise AccountError("Unknown role")
    if db.scalar(select(User).where(User.email == email)):
        raise AccountError("That email is already registered. Try logging in.")
    user = User(email=email, display_name=name, password_hash=hash_password(password), role=role, credit_ok=credit_ok,
                can_choose=can_choose)
    db.add(user)
    db.flush()
    return user


def create_invite(db, actor, role="reviewer", max_uses=1, days=14, note="", can_choose=False):
    if role not in ROLES:
        raise AccountError("Unknown role")
    invite = Invite(code=new_invite_code(), role=role, max_uses=max(1, int(max_uses)), note=note.strip()[:200],
                    expires_at=utcnow() + timedelta(days=days) if days else None,
                    created_by_id=actor.id if actor else None, can_choose=can_choose)
    db.add(invite)
    db.flush()
    log(db, actor, "invite-create", "invite", invite.code,
        f"role={role} uses={invite.max_uses}{' can-choose' if can_choose else ''}")
    return invite


def usable_invite(db, code, now=None):
    now = now or utcnow()
    invite = db.scalar(select(Invite).where(Invite.code == (code or "").strip().upper()))
    if (invite is None or invite.revoked or invite.uses >= invite.max_uses
            or (invite.expires_at and invite.expires_at < now)):
        return None
    return invite


def register(db, code, email, display_name, password, credit_ok):
    invite = usable_invite(db, code)
    if invite is None:
        raise AccountError("That invite code isn't valid. Ask for a new one.")
    user = create_user(db, email, display_name, password, role=invite.role, credit_ok=credit_ok,
                       can_choose=invite.can_choose)
    invite.uses += 1
    return user


def authenticate(db, email, password):
    try:
        email = normalize_email(email)
    except ValueError:
        return None
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(password or "", user.password_hash):
        return None
    user.last_login_at = utcnow()
    return user


def start_reset(db, actor, user, hours=48):
    """A one-time link token for the admin to pass on. Returns the raw token (only its hash is stored)."""
    token = new_token()
    db.add(PasswordReset(user_id=user.id, token_hash=token_hash(token), expires_at=utcnow() + timedelta(hours=hours)))
    log(db, actor, "password-reset-link", "user", user.id)
    return token


def reset_for(db, token, now=None):
    now = now or utcnow()
    row = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == token_hash(token or "")))
    if row is None or row.used_at or row.expires_at < now:
        return None
    return row


def finish_reset(db, row, password):
    try:
        check_password(password)
    except ValueError as exc:
        raise AccountError(str(exc)) from None
    user = db.get(User, row.user_id)
    user.password_hash = hash_password(password)
    row.used_at = utcnow()
    return user
