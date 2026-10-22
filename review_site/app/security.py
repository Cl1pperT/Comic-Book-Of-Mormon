"""Passwords, one-time tokens, CSRF tokens and a small login rate limiter."""
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

MIN_PASSWORD = 10
_hasher = PasswordHasher()
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value):
    email = (value or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise ValueError("Please enter a valid email address.")
    return email


def check_password(value):
    if len(value or "") < MIN_PASSWORD:
        raise ValueError(f"Passwords need at least {MIN_PASSWORD} characters.")
    return value


def hash_password(password):
    return _hasher.hash(password)


def verify_password(password, stored):
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def new_token(nbytes=24):
    return secrets.token_urlsafe(nbytes)


def new_invite_code():
    """Short, readable and unambiguous (no 0/O or 1/I): 12 characters from a 31-letter alphabet."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(12))


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def same(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


def csrf_token(session):
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


class RateLimiter:
    """At most `limit` failures per `window` seconds for a key (in memory: one server process)."""

    def __init__(self, limit=8, window=900):
        self.limit, self.window = limit, window
        self.failures = defaultdict(deque)

    def _prune(self, key, now):
        events = self.failures[key]
        while events and now - events[0] > self.window:
            events.popleft()
        return events

    def blocked(self, key, now=None):
        return len(self._prune(key, now or time.monotonic())) >= self.limit

    def fail(self, key, now=None):
        now = now or time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key):
        self.failures.pop(key, None)
