"""Envoi d'e-mails transactionnels (non bloquant)."""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

from . import config

log = logging.getLogger("planning.email")


def _send(destinataire: str, sujet: str, corps: str) -> None:
    if not config.EMAIL_ENABLED:
        log.warning("E-mail non envoyé à %s (EMAIL_ADDRESS / EMAIL_PASSWORD non configurés).", destinataire)
        return
    try:
        msg = MIMEText(corps, _charset="utf-8")
        msg["Subject"] = sujet
        msg["From"] = f"Planning EJP <{config.SMTP_USER}>"
        msg["To"] = destinataire
        with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT, timeout=10) as server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("E-mail « %s » envoyé à %s", sujet, destinataire)
    except Exception:  # noqa: BLE001 — on ne doit jamais faire planter la requête
        log.exception("Échec de l'envoi de l'e-mail à %s", destinataire)


async def envoyer_email_reinitialisation(destinataire: str, lien: str) -> None:
    corps = (
        "Bonjour,\n\n"
        "Vous avez demandé la réinitialisation de votre mot de passe sur Planning EJP.\n\n"
        f"Cliquez sur ce lien (valable 1 heure) pour en choisir un nouveau :\n{lien}\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet e-mail."
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send, destinataire, "Réinitialisation de votre mot de passe – Planning EJP", corps)
