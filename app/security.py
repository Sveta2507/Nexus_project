import hashlib
import hmac
import json
import secrets
import time
from typing import Any

PBKDF2_ITERATIONS = 390_000


# повторне хешування із випадковою сіллю ускладнює перебір викрадених хешів
def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if not 8 <= len(password) <= 200:
        raise ValueError("Пароль повинен містити від 8 до 200 символів")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    # порівняння за сталий час не видає правильний хеш через тривалість перевірки
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(36)


def token_digest(token: str) -> str:
    # у базі зберігається лише хеш сесійного токена, а не сам токен
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> bytes:
    # стабільний порядок ключів дає однакові байти для обчислення hmac на всіх вузлах
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def federation_signature(secret: str, timestamp: str, body: bytes) -> str:
    payload = timestamp.encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_federation_signature(
    secret: str, timestamp: str, body: bytes, signature: str, tolerance_seconds: int = 90
) -> bool:
    # обмеження часу дії підпису захищає від повторного надсилання старого запиту
    try:
        if abs(time.time() - int(timestamp)) > tolerance_seconds:
            return False
    except (TypeError, ValueError):
        return False
    expected = federation_signature(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
