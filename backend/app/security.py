"""Passwörter, JWT, symmetrische Verschlüsselung für Secrets in der DB."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import uuid
from functools import lru_cache
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet

from app.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_access_token(user_id: uuid.UUID, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + dt.timedelta(minutes=s.access_token_expire_minutes),
        "typ": "access",
        **(extra or {}),
    }
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    s = get_settings()
    return jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])


@lru_cache
def _fernet() -> Fernet:
    s = get_settings()
    key = s.encryption_key
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("enc:" + s.secret_key).encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def generate_token(nbytes: int = 24) -> str:
    """URL-sicherer Zufallstoken (für Pairing, Remote-Sessions)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_password(length: int = 24) -> str:
    # RouterOS-sicher: keine Quotes/Backslashes/$ im Passwort (Script-Escaping)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
