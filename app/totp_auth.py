from __future__ import annotations

import hashlib
import hmac
import os
import re
from time import time as _current_time
from urllib.parse import quote, urlencode

import pyotp
from cryptography.fernet import Fernet, InvalidToken


TOTP_DIGITS = 6
TOTP_INTERVAL_SECONDS = 30
TOTP_WINDOW = 1
TOTP_ISSUER = "Arti CRM"


def _fernet_from_environment() -> Fernet:
    raw_key = os.environ.get("CRM_TOTP_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise ValueError("TOTP encryption is unavailable")
    try:
        return Fernet(raw_key.encode("ascii"))
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("TOTP encryption is unavailable") from exc


def generate_secret() -> str:
    return pyotp.random_base32(length=32)


def build_otpauth_uri(secret: str, username: str) -> str:
    label = f"{quote(TOTP_ISSUER)}:{quote(username)}"
    query = urlencode(
        {
            "secret": secret,
            "issuer": TOTP_ISSUER,
            "algorithm": "SHA1",
            "digits": str(TOTP_DIGITS),
            "period": str(TOTP_INTERVAL_SECONDS),
        }
    )
    return f"otpauth://totp/{label}?{query}"


def encrypt_secret(secret: str) -> str:
    if not secret:
        raise ValueError("TOTP secret is unavailable")
    return _fernet_from_environment().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        raise ValueError("TOTP credential is unavailable")
    try:
        return _fernet_from_environment().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("TOTP credential is unavailable") from exc


def verify_code(
    secret: str,
    code: str,
    *,
    at_time: float | None = None,
    window: int = TOTP_WINDOW,
) -> int | None:
    if window < 0 or window > TOTP_WINDOW:
        raise ValueError("TOTP verification window is invalid")
    if not re.fullmatch(r"\d{6}", str(code or "")):
        return None
    timestamp = _current_time() if at_time is None else float(at_time)
    current_step = int(timestamp // TOTP_INTERVAL_SECONDS)
    try:
        totp = pyotp.TOTP(
            secret,
            digits=TOTP_DIGITS,
            interval=TOTP_INTERVAL_SECONDS,
            digest=hashlib.sha1,
        )
        for step in range(current_step + window, current_step - window - 1, -1):
            if hmac.compare_digest(totp.at(step * TOTP_INTERVAL_SECONDS), code):
                return step
    except Exception:
        return None
    return None
