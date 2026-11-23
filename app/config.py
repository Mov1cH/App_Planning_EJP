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

# Envoi d'e-mails (codes de vérification, mot de passe oublié), par SMTP.
#  - Gmail : SMTP_SERVER=smtp.gmail.com, SMTP_PORT=465, EMAIL_ADDRESS=adresse, EMAIL_PASSWORD=mot de passe d'application
#  - Brevo : SMTP_SERVER=smtp-relay.brevo.com, SMTP_PORT=587, EMAIL_ADDRESS=identifiant SMTP Brevo,
#            EMAIL_PASSWORD=clé SMTP, EMAIL_FROM=adresse d'expédition validée dans Brevo
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("EMAIL_ADDRESS", "")
SMTP_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "") or SMTP_USER
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASSWORD)

# URL publique de l'application (utilisée dans les e-mails). Facultatif.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# Durée de validité d'un lien de réinitialisation (secondes)
RESET_TOKEN_MAX_AGE = 60 * 60

MIN_PASSWORD_LENGTH = 8

# Couleurs d'accent proposées (choisies par chaque église dans « Mon compte »)
COULEURS = {
    "flamme": {"nom": "Orange flamme", "acc": "#CC3D17", "soft": "#FCECE6", "light": "#FFD3C2", "ink": "#9E2E10"},
    "bleu": {"nom": "Bleu électrique", "acc": "#1D33E6", "soft": "#ECEFFE", "light": "#C9D0FF", "ink": "#1426B0"},
    "bordeaux": {"nom": "Bordeaux", "acc": "#6B1E3A", "soft": "#F6EAEF", "light": "#E8C6D2", "ink": "#521529"},
    "emeraude": {"nom": "Vert", "acc": "#0F3D26", "soft": "#E6F0EA", "light": "#C8DCCF", "ink": "#0A2C1B"},
}

# Organisations : ICC (église principale) et EJP (église des jeunes rattachée à ICC)
ORGANISATIONS = {
    "ejp": {
        "nom": "EJP", "nom_long": "Église Jeunes Prodiges", "couleur": "emeraude",
        "mark": "/static/brand/ejp-noir.png", "complet": "/static/logo.png",
        "exemple": "EJP Saint-Étienne",
    },
    "icc": {
        "nom": "ICC", "nom_long": "Impact Centre Chrétien", "couleur": "emeraude",
        "mark": "/static/brand/icc-noir.png", "complet": "/static/brand/icc-noir.png",
        "exemple": "ICC Saint-Étienne",
    },
}
COULEUR_DEFAUT = os.getenv("COULEUR", "emeraude") if os.getenv("COULEUR", "emeraude") in COULEURS else "emeraude"
