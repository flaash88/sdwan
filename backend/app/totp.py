"""TOTP (RFC 6238, HMAC-SHA1, 30 s, 6 Stellen) und Wiederherstellungscodes – ohne zusätzliche Abhängigkeit."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # ±1 Zeitschritt Toleranz (Uhrabweichung)
RECOVERY_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def hotp(key: bytes, counter: int, digits: int = DIGITS, digest: str = "sha1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, digest)).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def code_at(secret: str, t: float | None = None) -> str:
    return hotp(_key(secret), int((time.time() if t is None else t) // STEP))


def verify(secret: str, code: str, last_step: int | None, t: float | None = None) -> int | None:
    """Gültiger Code → verwendeter Zeitschritt (zum Speichern gegen Wiederverwendung), sonst None."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != DIGITS:
        return None
    now = int((time.time() if t is None else t) // STEP)
    key = _key(secret)
    for step in range(now - WINDOW, now + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue  # bereits verwendeter (oder älterer) Code
        if hmac.compare_digest(hotp(key, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str) -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits={DIGITS}&period={STEP}"


def generate_recovery_codes() -> list[str]:
    return ["-".join("".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(4)) for _ in range(2)) for _ in range(RECOVERY_COUNT)]


def hash_recovery(code: str) -> str:
    return hashlib.sha256(code.strip().lower().replace(" ", "").encode()).hexdigest()
