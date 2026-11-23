"""Envoi d'e-mails transactionnels (non bloquant)."""
import asyncio
import logging
import smtplib
from email.mime.text import MIMEText

from . import config

log = logging.getLogger("planning.email")


def _send(destinataire: str, sujet: str, corps: str) -> bool:
    if not config.EMAIL_ENABLED:
        log.warning("E-mail non envoyé à %s (EMAIL_ADDRESS / EMAIL_PASSWORD non configurés).", destinataire)
        return False
    try:
        msg = MIMEText(corps, _charset="utf-8")
        msg["Subject"] = sujet
        msg["From"] = f"Planning des cultes <{config.EMAIL_FROM}>"
        msg["To"] = destinataire
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT, timeout=10)
        else:  # 587 / 2525 : connexion simple puis chiffrement STARTTLS
            server = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=10)
            server.starttls()
        with server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("E-mail « %s » envoyé à %s", sujet, destinataire)
        return True
    except Exception:  # noqa: BLE001 — on ne doit jamais faire planter la requête
        log.exception("Échec de l'envoi de l'e-mail à %s", destinataire)
        return False


async def envoyer_email_reinitialisation(destinataire: str, lien: str) -> None:
    corps = (
        "Bonjour,\n\n"
        "Vous avez demandé la réinitialisation de votre mot de passe sur Planning des cultes.\n\n"
        f"Cliquez sur ce lien (valable 1 heure) pour en choisir un nouveau :\n{lien}\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet e-mail."
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _send, destinataire, "Réinitialisation de votre mot de passe – Planning des cultes", corps)


async def envoyer_code_verification(destinataire: str, code: str, nom_eglise: str) -> bool:
    corps = (
        "Bonjour,\n\n"
        f"Voici votre code pour confirmer l'adresse email de l'espace « {nom_eglise} » sur Planning des cultes :\n\n"
        f"    {code}\n\n"
        "Ce code est valable 30 minutes.\n\n"
        "Si vous n'avez pas créé de compte, ignorez simplement cet e-mail : aucun compte ne sera activé sans ce code."
    )
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send, destinataire, f"{code} est votre code de vérification – Planning des cultes", corps)
