from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from threading import RLock

import bcrypt

from .persistence import ensure_relational_schema

SESSION_COOKIE_NAME = "travel_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
_EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}$")
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"travelassistant-invalid-password", bcrypt.gensalt(rounds=12))


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def validate_email(email: str) -> str | None:
    normalized = normalize_email(email)
    if len(normalized) > 254 or not _EMAIL_PATTERN.fullmatch(normalized):
        return "请输入有效的邮箱地址"
    _, domain = normalized.rsplit("@", 1)
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return "请输入有效的邮箱地址"
    return None


def validate_password(password: str) -> str | None:
    if len(password) < 8:
        return "密码至少需要 8 个字符"
    if len(password.encode("utf-8")) > 72:
        return "密码不能超过 72 个字节"
    return None


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, db_path: str, persistence_enabled: bool) -> None:
        self.db_path = db_path
        self.persistence_enabled = persistence_enabled
        self._lock = RLock()
        self._accounts: dict[str, tuple[str, str]] = {}
        self._sessions: dict[str, tuple[str, int]] = {}
        if self.persistence_enabled:
            ensure_relational_schema(self.db_path)

    def email_exists(self, email: str) -> bool:
        normalized = normalize_email(email)
        with self._lock:
            if not self.persistence_enabled:
                return normalized in self._accounts
            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT 1 FROM auth_accounts WHERE email = ?",
                    (normalized,),
                ).fetchone()
            return row is not None

    def create_account(self, email: str, user_id: str, password: str) -> bool:
        normalized = normalize_email(email)
        password_hash = _hash_password(password)
        with self._lock:
            if not self.persistence_enabled:
                if normalized in self._accounts:
                    return False
                self._accounts[normalized] = (user_id, password_hash)
                return True
            try:
                with sqlite3.connect(self.db_path) as connection:
                    connection.execute(
                        "INSERT INTO auth_accounts (email, user_id, password_hash) VALUES (?, ?, ?)",
                        (normalized, user_id, password_hash),
                    )
            except sqlite3.IntegrityError:
                return False
            return True

    def authenticate(self, email: str, password: str) -> str | None:
        normalized = normalize_email(email)
        if len(password.encode("utf-8")) > 72:
            _verify_password("invalid-password", _DUMMY_PASSWORD_HASH.decode("utf-8"))
            return None
        with self._lock:
            if not self.persistence_enabled:
                account = self._accounts.get(normalized)
            else:
                with sqlite3.connect(self.db_path) as connection:
                    row = connection.execute(
                        "SELECT user_id, password_hash FROM auth_accounts WHERE email = ?",
                        (normalized,),
                    ).fetchone()
                account = (str(row[0]), str(row[1])) if row else None

        password_hash = account[1] if account else _DUMMY_PASSWORD_HASH.decode("utf-8")
        password_matches = _verify_password(password, password_hash)
        return account[0] if account and password_matches else None

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        digest = _token_hash(token)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        with self._lock:
            if not self.persistence_enabled:
                self._remove_expired_memory_sessions()
                self._sessions[digest] = (user_id, expires_at)
                return token
            with sqlite3.connect(self.db_path) as connection:
                connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (int(time.time()),))
                connection.execute(
                    "INSERT INTO auth_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                    (digest, user_id, expires_at),
                )
        return token

    def user_id_for_session(self, token: str) -> str | None:
        if not token:
            return None
        digest = _token_hash(token)
        now = int(time.time())
        with self._lock:
            if not self.persistence_enabled:
                session = self._sessions.get(digest)
                if not session:
                    return None
                if session[1] <= now:
                    self._sessions.pop(digest, None)
                    return None
                return session[0]

            with sqlite3.connect(self.db_path) as connection:
                row = connection.execute(
                    "SELECT user_id, expires_at FROM auth_sessions WHERE token_hash = ?",
                    (digest,),
                ).fetchone()
                if not row:
                    return None
                if int(row[1]) <= now:
                    connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (digest,))
                    return None
                return str(row[0])

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        digest = _token_hash(token)
        with self._lock:
            if not self.persistence_enabled:
                self._sessions.pop(digest, None)
                return
            with sqlite3.connect(self.db_path) as connection:
                connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (digest,))

    def _remove_expired_memory_sessions(self) -> None:
        now = int(time.time())
        self._sessions = {
            digest: session
            for digest, session in self._sessions.items()
            if session[1] > now
        }
