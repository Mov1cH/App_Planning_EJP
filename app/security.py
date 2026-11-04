"""Mots de passe, jetons de réinitialisation et limitation des tentatives de connexion."""
import secrets
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from passlib.context import CryptContext

from .config import RESET_TOKEN_MAX_AGE, SECRET_KEY

# pbkdf2_sha256 pour les nouveaux mots de passe ; bcrypt reste accepté pour les
# comptes existants et sera migré automatiquement à la prochaine connexion.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated=["bcrypt"])


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Retourne (valide, nouveau_hash_si_migration_nécessaire)."""
    if not hashed:
        return False, None
    try:
        return pwd_context.verify_and_update(password, hashed)
    except (ValueError, TypeError):
        return False, None


# --- Jetons de réinitialisation : signés + horodatés + à usage unique ---
_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="reset-password")


def new_reset_token() -> Tuple[str, str]:
    """Retourne (jeton brut à stocker en base, jeton signé à mettre dans le lien)."""
    raw = secrets.token_urlsafe(32)
    return raw, _serializer.dumps(raw)


def read_reset_token(signed: str) -> Optional[str]:
    try:
        return _serializer.loads(signed, max_age=RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


# --- Limitation simple des tentatives (en mémoire, par adresse IP) ---
class RateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    def _prune(self, key: str) -> Deque[float]:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        return hits

    def is_blocked(self, key: str) -> bool:
        return len(self._prune(key)) >= self.max_attempts

    def hit(self, key: str) -> None:
        self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


login_limiter = RateLimiter(max_attempts=8, window_seconds=10 * 60)
email_limiter = RateLimiter(max_attempts=5, window_seconds=60 * 60)
