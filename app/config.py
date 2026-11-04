"""Configuration centralisée, lue depuis les variables d'environnement.

En local, un fichier `.env` à la racine du projet est chargé automatiquement
(voir `.env.example`). En production (Render…), on définit ces variables dans
le tableau de bord de l'hébergeur.
"""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Charge un fichier .env minimaliste (clé=valeur) sans dépendance externe."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "oui"}


# Environnement : "production" active les cookies sécurisés (HTTPS)
ENV = os.getenv("ENV", "development").lower()
IS_PRODUCTION = ENV == "production"

# Base de données : Postgres en production, SQLite en local
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'planning_ejp.db'}")

# Clé de signature des cookies de session. OBLIGATOIRE en production.
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise RuntimeError("La variable d'environnement SECRET_KEY est obligatoire en production.")
    # En local : clé aléatoire (les sessions sont perdues à chaque redémarrage, c'est normal)
    SECRET_KEY = secrets.token_urlsafe(48)

# Inscription publique de nouvelles églises
ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", True)

# Compte administrateur créé au premier démarrage s'il n'existe pas encore
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_CHURCH_NAME = os.getenv("ADMIN_CHURCH_NAME", "EJP ST Etienne")

# Envoi d'e-mails (mot de passe oublié). Pour Gmail : créer un « mot de passe d'application ».
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("EMAIL_ADDRESS", "")
SMTP_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD)

# URL publique de l'application (utilisée dans les e-mails). Facultatif.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# Durée de validité d'un lien de réinitialisation (secondes)
RESET_TOKEN_MAX_AGE = 60 * 60

MIN_PASSWORD_LENGTH = 8
