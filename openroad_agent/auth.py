"""Local authentication helpers for the Chippilot web UI."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from openroad_agent.storage import ChippilotStore


COOKIE_NAME = "chippilot_session"
TOKEN_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
_PBKDF2_ITERATIONS = 260_000


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    username: str
    created_at: str
    disabled_password: bool = False


class AuthStore:
    """SQLite-backed local user store with signed-cookie sessions."""

    def __init__(self, work_dir: str) -> None:
        self.work_dir = Path(work_dir)
        self.users_path = self.work_dir / "users.json"
        self.users_root = self.work_dir / "users"
        self.store = ChippilotStore(self.work_dir)
        self._initialized = False
        self._secret = os.environ.get("CHIPPILOT_SECRET_KEY")
        if not self._secret:
            self._secret = secrets.token_urlsafe(32)
            print(
                "WARNING: CHIPPILOT_SECRET_KEY is not set; using an ephemeral "
                "development secret. Existing web logins will be invalid after restart."
            )

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.users_root.mkdir(parents=True, exist_ok=True)
        self.store.initialize()
        self.migrate_users_json()
        self.ensure_admin_placeholder()
        self.migrate_legacy_sessions()
        self.index_existing_sessions()
        self.store.fail_interrupted_jobs("Server restarted before this job finished.")
        self._initialized = True

    def public_user(self, user: dict[str, Any]) -> AuthUser:
        return AuthUser(
            user_id=user["user_id"],
            username=user["username"],
            created_at=user.get("created_at", ""),
            disabled_password=bool(user.get("disabled_password")),
        )

    def user_root(self, user: AuthUser | dict[str, Any]) -> Path:
        user_id = user.user_id if isinstance(user, AuthUser) else user["user_id"]
        root = self.users_root / user_id
        root.mkdir(parents=True, exist_ok=True)
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        return root

    def get_user_by_id(self, user_id: str) -> AuthUser | None:
        raw = self.store.get_user_by_id(user_id)
        return self.public_user(raw) if raw else None

    def get_user_record_by_username(self, username: str) -> dict[str, Any] | None:
        return self.store.get_user_by_username(username)

    def register(self, username: str, password: str) -> AuthUser:
        username = username.strip()
        self._validate_username(username)
        self._validate_password(password)

        existing = self.get_user_record_by_username(username)
        if existing:
            if existing.get("disabled_password") and existing["username"].lower() == "admin":
                existing.update(self._password_fields(password))
                existing["disabled_password"] = False
                existing["created_at"] = existing.get("created_at") or self._now()
                self.store.upsert_user(existing)
                self.user_root(existing)
                return self.public_user(existing)
            raise ValueError("Username is already registered.")

        user_id = self._safe_user_id(username)
        if self.store.user_exists(user_id):
            user_id = f"{user_id}_{secrets.token_hex(3)}"

        raw = {
            "user_id": user_id,
            "username": username,
            "created_at": self._now(),
            **self._password_fields(password),
        }
        self.store.create_user(raw)
        self.user_root(raw)
        return self.public_user(raw)

    def authenticate(self, username: str, password: str) -> AuthUser | None:
        raw = self.get_user_record_by_username(username.strip())
        if not raw or raw.get("disabled_password"):
            return None
        salt = base64.urlsafe_b64decode(raw["salt"].encode("ascii"))
        expected = raw["password_hash"]
        actual = self._hash_password(password, salt)
        if not hmac.compare_digest(actual, expected):
            return None
        self.user_root(raw)
        return self.public_user(raw)

    def sign_user_token(self, user: AuthUser) -> str:
        issued_at = str(int(time.time()))
        payload = f"{user.user_id}.{issued_at}"
        signature = self._sign(payload)
        return f"{payload}.{signature}"

    def verify_user_token(self, token: str | None) -> AuthUser | None:
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        user_id, issued_at, signature = parts
        payload = f"{user_id}.{issued_at}"
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            if time.time() - int(issued_at) > TOKEN_MAX_AGE_SECONDS:
                return None
        except ValueError:
            return None
        return self.get_user_by_id(user_id)

    def ensure_admin_placeholder(self) -> None:
        existing = self.store.get_user_by_id("admin")
        if existing:
            self.user_root(existing)
            return
        admin_password = os.environ.get("CHIPPILOT_ADMIN_PASSWORD")
        raw = {
            "user_id": "admin",
            "username": "admin",
            "created_at": self._now(),
        }
        if admin_password:
            raw.update(self._password_fields(admin_password))
        else:
            raw.update(
                {
                    "password_hash": "",
                    "salt": "",
                    "disabled_password": True,
                }
            )
        self.store.upsert_user(raw)
        self.user_root(raw)

    def migrate_users_json(self) -> None:
        if not self.users_path.exists():
            return
        try:
            with self.users_path.open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        users = data.get("users", {})
        if not isinstance(users, dict):
            return
        for raw in users.values():
            if not isinstance(raw, dict) or not raw.get("user_id") or not raw.get("username"):
                continue
            raw.setdefault("created_at", self._now())
            raw.setdefault("password_hash", "")
            raw.setdefault("salt", "")
            raw["disabled_password"] = bool(raw.get("disabled_password"))
            self.store.upsert_user(raw)

    def migrate_legacy_sessions(self) -> None:
        legacy_root = self.work_dir / "sessions"
        if not legacy_root.is_dir():
            return
        admin_root = self.users_root / "admin" / "sessions"
        admin_root.mkdir(parents=True, exist_ok=True)

        for path in sorted(legacy_root.iterdir()):
            if not path.is_dir():
                continue
            target = admin_root / path.name
            if target.exists():
                continue
            shutil.move(str(path), str(target))
            self._index_session_dir("admin", target)

        try:
            legacy_root.rmdir()
        except OSError:
            pass

    def index_existing_sessions(self) -> None:
        if not self.users_root.is_dir():
            return
        for user_dir in sorted(self.users_root.iterdir()):
            sessions_dir = user_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for session_dir in sorted(sessions_dir.iterdir()):
                if session_dir.is_dir():
                    self._index_session_dir(user_dir.name, session_dir)

    def _index_session_dir(self, user_id: str, session_dir: Path) -> None:
        meta_path = session_dir / "session.json"
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                with meta_path.open() as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                meta = {}
        session_name = meta.get("session_name") or session_dir.name
        if session_name != session_dir.name:
            session_name = session_dir.name
        self.store.upsert_session(
            user_id=user_id,
            session_name=session_name,
            path=session_dir,
            name_hint=meta.get("name_hint", ""),
            created_at=str(meta.get("created", "")),
        )

    def _read_users(self) -> dict[str, Any]:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        if not self.users_path.exists():
            return {"users": {}}
        with self.users_path.open() as f:
            data = json.load(f)
        if "users" not in data or not isinstance(data["users"], dict):
            return {"users": {}}
        return data

    def _write_users(self, data: dict[str, Any]) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.users_path.with_suffix(".json.tmp")
        with tmp.open("w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.users_path)

    def _password_fields(self, password: str) -> dict[str, str | bool]:
        salt = secrets.token_bytes(16)
        return {
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
            "password_hash": self._hash_password(password, salt),
            "disabled_password": False,
        }

    def _hash_password(self, password: str, salt: bytes) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            _PBKDF2_ITERATIONS,
        )
        return base64.urlsafe_b64encode(digest).decode("ascii")

    def _sign(self, payload: str) -> str:
        digest = hmac.new(
            self._secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _validate_username(self, username: str) -> None:
        if not USERNAME_PATTERN.match(username):
            raise ValueError("Username must be 3-32 characters: letters, numbers, _ or -.")

    def _validate_password(self, password: str) -> None:
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters.")

    def _safe_user_id(self, username: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", username.lower()).strip("_") or secrets.token_hex(4)

    def _now(self) -> str:
        return datetime.utcnow().isoformat() + "Z"
