"""Planning EJP — application de planification des services du culte.

Lancer en local :
    python -m uvicorn main:app --reload
    (ou, accessible depuis un téléphone du même réseau : --host 0.0.0.0 --port 8000)
"""
import json
import os
import re
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import config, jourj, models, services, sheets_sync, stats
from app.database import SessionLocal, engine, get_db
from app.emails import envoyer_code_verification, envoyer_email_reinitialisation
from app.exports import nom_fichier, planning_csv, planning_pdf, stats_pdf
from app.security import (
    email_limiter,
    hash_password,
    login_limiter,
    new_reset_token,
    nouveau_code,
    peut_renvoyer,
    verifier_code,
    read_reset_token,
    verify_password,
)
from app.services import JOURS_FR, MOIS_COURTS, MOIS_FR, nom_complet

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("planning")

# --- Initialisation de la base au démarrage (création des tables + colonnes manquantes) ---
services.migrer_schema()
with SessionLocal() as _db:
    services.assurer_admin(_db)

app = FastAPI(title="Planning EJP", version="3.0.0", docs_url=None, redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "app" / "templates"))


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------
@app.middleware("http")
async def securite(request: Request, call_next):
    # Protection CSRF : un formulaire envoyé depuis un autre site est refusé.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origine = request.headers.get("origin") or request.headers.get("referer")
        if origine:
            hote_origine = urlparse(origine).netloc
            hote = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
            if hote_origine and hote_origine != hote:
                return PlainTextResponse("Requête refusée (origine invalide).", status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if config.IS_PRODUCTION:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    session_cookie="ejp_session",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=config.IS_PRODUCTION,
)


# ---------------------------------------------------------------------------
# Helpers de rendu
# ---------------------------------------------------------------------------
def flash(request: Request, message: str, categorie: str = "succes") -> None:
    request.session.setdefault("_flash", []).append([categorie, message])


def _pop_flashes(request: Request):
    return request.session.pop("_flash", []) if "session" in request.scope else []


def _format_date(d: date, style: str = "long") -> str:
    if style == "court":
        return f"{d.day:02d}/{d.month:02d}"
    if style == "jour":
        return f"{d.day} {MOIS_FR[d.month].lower()}"
    return f"{JOURS_FR[d.weekday()]} {d.day} {MOIS_FR[d.month].lower()} {d.year}"


def icon(nom: str, classe: str = "") -> Markup:
    return Markup(f'<svg class="i {classe}" aria-hidden="true" focusable="false"><use href="#i-{nom}"/></svg>')


def organisation_de(eglise) -> dict:
    cle = getattr(eglise, "organisation", None) or "ejp"
    org = dict(config.ORGANISATIONS.get(cle, config.ORGANISATIONS["ejp"]))
    org["cle"] = cle if cle in config.ORGANISATIONS else "ejp"
    return org


def organisation_actuelle(request: Request) -> Optional[dict]:
    eglise = getattr(request.state, "eglise", None)
    return organisation_de(eglise) if eglise else None


def couleur_actuelle(request: Request) -> str:
    """Clé de la couleur de l'église connectée (ou de l'organisation mémorisée)."""
    eglise = getattr(request.state, "eglise", None)
    if eglise:
        cle = getattr(eglise, "couleur", None) or organisation_de(eglise)["couleur"]
    else:
        org_cookie = request.cookies.get("org", "")
        cle = config.ORGANISATIONS[org_cookie]["couleur"] if org_cookie in config.ORGANISATIONS else config.COULEUR_DEFAUT
    return cle if cle in config.COULEURS else config.COULEUR_DEFAUT


def style_accent(request: Request) -> Markup:
    """Variables CSS de la couleur choisie par l'église connectée."""
    c = config.COULEURS[couleur_actuelle(request)]
    return Markup(f'--acc:{c["acc"]};--acc-soft:{c["soft"]};--acc-light:{c["light"]};--acc-ink:{c["ink"]}')


templates.env.globals.update(
    icon=icon,
    accent=style_accent,
    couleur_cle=couleur_actuelle,
    org=organisation_actuelle,
    ORGANISATIONS=config.ORGANISATIONS,
    COULEURS=config.COULEURS,
    get_flashes=_pop_flashes,
    MOIS_FR=MOIS_FR,
    MOIS_COURTS=MOIS_COURTS,
    JOURS_FR=JOURS_FR,
    nom_complet=nom_complet,
    stats_cle=stats.cle_nom,
    radar=stats.radar,
    ALLOW_REGISTRATION=config.ALLOW_REGISTRATION,
    EMAIL_ENABLED=config.EMAIL_ENABLED,
)
templates.env.filters["date_fr"] = _format_date


def render(request: Request, name: str, status_code: int = 200, **context):
    return templates.TemplateResponse(request=request, name=name, context=context, status_code=status_code)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url=url, status_code=303)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


def nettoyer(valeur: Optional[str]) -> str:
    return (valeur or "").strip()


# ---------------------------------------------------------------------------
# Authentification
# ---------------------------------------------------------------------------
def memoriser_org(response, eglise):
    """Retient ICC ou EJP sur l'appareil pour afficher le bon logo sur la page de connexion."""
    response.set_cookie("org", organisation_de(eglise)["cle"], max_age=60 * 60 * 24 * 365, samesite="lax",
                        secure=config.IS_PRODUCTION, httponly=True)
    return response


class NonConnecte(Exception):
    pass


@app.exception_handler(NonConnecte)
async def _non_connecte(request: Request, exc: NonConnecte):
    return redirect("/login")


@app.exception_handler(StarletteHTTPException)
async def _erreur_http(request: Request, exc: StarletteHTTPException):
    titre = "Page introuvable" if exc.status_code == 404 else "Une erreur est survenue"
    return render(request, "error.html", status_code=exc.status_code, code=exc.status_code, titre=titre)


@app.exception_handler(Exception)
async def _erreur_serveur(request: Request, exc: Exception):
    log.exception("Erreur non gérée sur %s", request.url.path)
    return render(request, "error.html", status_code=500, code=500, titre="Une erreur est survenue")


def get_current_eglise(request: Request, db: Session = Depends(get_db)) -> models.Eglise:
    eglise_id = request.session.get("eglise_id")
    if not eglise_id:
        raise NonConnecte()
    eglise = db.get(models.Eglise, eglise_id)
    if not eglise or not eglise.is_verified:
        request.session.clear()
        raise NonConnecte()
    request.state.eglise = eglise
    return eglise


def trouver_par_email(db: Session, email: str) -> Optional[models.Eglise]:
    return db.query(models.Eglise).filter(func.lower(models.Eglise.email) == email.lower()).first()


def possede(db: Session, modele, objet_id: Optional[int], eglise_id: int):
    """Retourne l'objet s'il appartient bien à l'église connectée, sinon None."""
    if objet_id is None:
        return None
    obj = db.get(modele, objet_id)
    return obj if obj is not None and obj.eglise_id == eglise_id else None


def _charger_eglise(request: Request, db: Session) -> None:
    """Rend l'église connectée disponible (si session) pour les routes publiques."""
    eglise_id = request.session.get("eglise_id")
    if eglise_id and not getattr(request.state, "eglise", None):
        request.state.eglise = db.get(models.Eglise, eglise_id)


@app.get("/favicon.ico", include_in_schema=False)
def favicon(request: Request, db: Session = Depends(get_db)):
    _charger_eglise(request, db)
    cle = couleur_actuelle(request)
    return FileResponse(config.BASE_DIR / "static" / "brand" / "logo" / f"{cle}-32.png", media_type="image/png")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest(request: Request, db: Session = Depends(get_db)):
    _charger_eglise(request, db)
    cle = couleur_actuelle(request)
    c = config.COULEURS[cle]
    return JSONResponse({
        "name": "Planning des cultes", "short_name": "Planning",
        "description": "Planification des services du culte", "lang": "fr",
        "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#f6f5f2", "theme_color": c["acc"],
        "icons": [
            {"src": f"/static/brand/logo/{cle}-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": f"/static/brand/logo/{cle}-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:  # noqa: BLE001
        log.exception("Base de données injoignable")
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)


@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if request.session.get("eglise_id"):
        return redirect("/")
    return render(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
async def post_login(request: Request, email: str = Form(...), mot_de_passe: str = Form(...), db: Session = Depends(get_db)):
    ip = client_ip(request)
    email = nettoyer(email)
    if login_limiter.is_blocked(ip):
        return render(request, "login.html", status_code=429, email=email,
                      erreur="Trop de tentatives. Réessayez dans quelques minutes.")

    eglise = trouver_par_email(db, email)
    valide, nouveau_hash = verify_password(mot_de_passe, eglise.mot_de_passe_hash if eglise else None)
    if not eglise or not valide:
        login_limiter.hit(ip)
        return render(request, "login.html", status_code=400, email=email, erreur="Email ou mot de passe incorrect.")

    if nouveau_hash:  # migration transparente vers l'algorithme de hachage actuel
        eglise.mot_de_passe_hash = nouveau_hash
        db.commit()
    login_limiter.reset(ip)
    request.session.clear()
    if not eglise.is_verified:
        # Adresse jamais confirmée : on renvoie un code avant d'ouvrir l'espace.
        request.session["a_verifier"] = eglise.id
        if peut_renvoyer(eglise.verification_code) == 0:
            await _envoyer_code(request, db, eglise)
        flash(request, "Confirmez d'abord votre adresse email avec le code reçu.", "attention")
        return redirect("/verifier")
    request.session["eglise_id"] = eglise.id
    return memoriser_org(redirect("/"), eglise)


@app.get("/register", response_class=HTMLResponse)
def get_register(request: Request):
    if not config.ALLOW_REGISTRATION:
        return redirect("/login")
    return render(request, "register.html")


@app.post("/register", response_class=HTMLResponse)
async def post_register(
    request: Request,
    nom_eglise: str = Form(...),
    email: str = Form(...),
    mot_de_passe: str = Form(...),
    organisation: str = Form("ejp"),
    db: Session = Depends(get_db),
):
    if not config.ALLOW_REGISTRATION:
        return redirect("/login")
    nom_eglise, email = nettoyer(nom_eglise), nettoyer(email).lower()
    organisation = organisation if organisation in config.ORGANISATIONS else "ejp"
    ctx = {"nom_eglise": nom_eglise, "email": email, "organisation": organisation}

    if not nom_eglise:
        return render(request, "register.html", status_code=400, erreur="Indiquez le nom de l'église.", **ctx)
    if "@" not in email or "." not in email.split("@")[-1]:
        return render(request, "register.html", status_code=400, erreur="Veuillez entrer une adresse email valide.", **ctx)
    if len(mot_de_passe) < config.MIN_PASSWORD_LENGTH:
        return render(request, "register.html", status_code=400,
                      erreur=f"Le mot de passe doit contenir au moins {config.MIN_PASSWORD_LENGTH} caractères.", **ctx)
    if config.IS_PRODUCTION and not config.EMAIL_ENABLED:
        return render(request, "register.html", status_code=503, **ctx,
                      erreur="Les inscriptions sont momentanément fermées : la confirmation par email n'est pas encore "
                             "configurée sur le serveur. Contactez l'administrateur.")
    existant = trouver_par_email(db, email)
    if existant and existant.is_verified:
        return render(request, "register.html", status_code=400, erreur="Cet email est déjà utilisé.", **ctx)

    if existant:
        # Inscription jamais confirmée avec cette adresse : on la reprend avec les nouvelles informations.
        eglise = existant
        eglise.nom_eglise, eglise.mot_de_passe_hash = nom_eglise, hash_password(mot_de_passe)
        eglise.organisation, eglise.couleur = organisation, config.ORGANISATIONS[organisation]["couleur"]
    else:
        eglise = models.Eglise(nom_eglise=nom_eglise, email=email, mot_de_passe_hash=hash_password(mot_de_passe),
                               is_verified=False, organisation=organisation,
                               couleur=config.ORGANISATIONS[organisation]["couleur"])
        db.add(eglise)
        db.flush()
        services.creer_roles_par_defaut(db, eglise.id, organisation)
    db.commit()

    request.session.clear()
    request.session["a_verifier"] = eglise.id
    await _envoyer_code(request, db, eglise)
    return redirect("/verifier")


async def _envoyer_code(request: Request, db: Session, eglise: models.Eglise) -> bool:
    code, stocke = nouveau_code(eglise.id)
    eglise.verification_code = stocke
    db.commit()
    envoye = await envoyer_code_verification(eglise.email, code, eglise.nom_eglise)
    if not envoye and not config.IS_PRODUCTION:
        log.warning("Mode local : code de vérification pour %s = %s", eglise.email, code)
        flash(request, f"Mode local (aucun email envoyé) : votre code est {code}.", "attention")
    elif not envoye:
        flash(request, "L'email n'a pas pu être envoyé. Réessayez dans un instant.", "attention")
    return envoye


def _masquer(email: str) -> str:
    nom, _, domaine = email.partition("@")
    return (nom[:2] + "•" * max(1, len(nom) - 2) if len(nom) > 2 else nom[:1] + "•") + "@" + domaine


def _a_verifier(request: Request, db: Session) -> Optional[models.Eglise]:
    eglise_id = request.session.get("a_verifier")
    eglise = db.get(models.Eglise, eglise_id) if eglise_id else None
    return eglise if eglise and not eglise.is_verified else None


@app.get("/verifier", response_class=HTMLResponse)
def get_verifier(request: Request, db: Session = Depends(get_db)):
    eglise = _a_verifier(request, db)
    if not eglise:
        return redirect("/login")
    return render(request, "verifier.html", email=_masquer(eglise.email))


@app.post("/verifier", response_class=HTMLResponse)
def post_verifier(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    eglise = _a_verifier(request, db)
    if not eglise:
        return redirect("/login")
    ok, stocke, raison = verifier_code(eglise.id, eglise.verification_code, code)
    if not ok:
        eglise.verification_code = stocke
        db.commit()
        erreur = {
            "faux": "Code incorrect. Vérifiez les 6 chiffres reçus par email.",
            "expire": "Ce code a expiré. Demandez-en un nouveau.",
            "bloque": "Trop d'essais avec ce code. Demandez-en un nouveau.",
        }.get(raison, "Demandez un nouveau code.")
        return render(request, "verifier.html", status_code=400, email=_masquer(eglise.email), erreur=erreur)
    eglise.is_verified, eglise.verification_code = True, None
    db.commit()
    request.session.clear()
    request.session["eglise_id"] = eglise.id
    flash(request, "Adresse confirmée. Bienvenue ! Commencez par ajouter vos membres.")
    return memoriser_org(redirect("/membres"), eglise)


@app.post("/verifier/renvoyer", response_class=HTMLResponse)
async def renvoyer_code(request: Request, db: Session = Depends(get_db)):
    eglise = _a_verifier(request, db)
    if not eglise:
        return redirect("/login")
    attente = peut_renvoyer(eglise.verification_code)
    ip = client_ip(request)
    if attente or email_limiter.is_blocked(ip):
        msg = f"Patientez {attente} secondes avant de demander un nouveau code." if attente else \
            "Trop de demandes. Réessayez dans un moment."
        return render(request, "verifier.html", status_code=429, email=_masquer(eglise.email), erreur=msg)
    email_limiter.hit(ip)
    if await _envoyer_code(request, db, eglise):
        flash(request, "Un nouveau code vient d'être envoyé.")
    return redirect("/verifier")


@app.post("/verifier/annuler")
def annuler_verification(request: Request):
    request.session.clear()
    return redirect("/register")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/mot-de-passe-oublie", response_class=HTMLResponse)
def get_mot_de_passe_oublie(request: Request):
    return render(request, "forgot_password.html")


@app.post("/mot-de-passe-oublie", response_class=HTMLResponse)
async def post_mot_de_passe_oublie(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    email = nettoyer(email)
    ip = client_ip(request)
    if not email_limiter.is_blocked(ip):
        email_limiter.hit(ip)
        eglise = trouver_par_email(db, email)
        if eglise:
            brut, signe = new_reset_token()
            eglise.reset_token = brut
            db.commit()
            base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
            await envoyer_email_reinitialisation(eglise.email, f"{base}/reinitialiser-mot-de-passe?token={signe}")
    return render(request, "forgot_password.html", envoye=True)


def _eglise_depuis_jeton(db: Session, token: str) -> Optional[models.Eglise]:
    brut = read_reset_token(token)
    if not brut:
        return None
    return db.query(models.Eglise).filter(models.Eglise.reset_token == brut).first()


@app.get("/reinitialiser-mot-de-passe", response_class=HTMLResponse)
def get_reinitialiser(request: Request, token: str = "", db: Session = Depends(get_db)):
    if not _eglise_depuis_jeton(db, token):
        return render(request, "login.html", erreur="Ce lien de réinitialisation est invalide ou a expiré.")
    return render(request, "reset_password.html", token=token)


@app.post("/reinitialiser-mot-de-passe", response_class=HTMLResponse)
def post_reinitialiser(
    request: Request,
    token: str = Form(...),
    nouveau_mot_de_passe: str = Form(...),
    db: Session = Depends(get_db),
):
    eglise = _eglise_depuis_jeton(db, token)
    if not eglise:
        return render(request, "login.html", erreur="Ce lien de réinitialisation est invalide ou a expiré.")
    if len(nouveau_mot_de_passe) < config.MIN_PASSWORD_LENGTH:
        return render(request, "reset_password.html", token=token, status_code=400,
                      erreur=f"Le mot de passe doit contenir au moins {config.MIN_PASSWORD_LENGTH} caractères.")
    eglise.mot_de_passe_hash = hash_password(nouveau_mot_de_passe)
    eglise.reset_token = None
    eglise.is_verified = True  # le lien reçu par email prouve que l'adresse est bien la sienne
    db.commit()
    return render(request, "login.html", succes="Mot de passe modifié. Vous pouvez vous connecter.")


# ---------------------------------------------------------------------------
# Tableau de bord
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def tableau_de_bord(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    aujourd_hui = date.today()
    dimanche = services.prochain_dimanche(aujourd_hui)
    planning_mois = services.construire_planning(db, eglise.id, dimanche.year, dimanche.month)
    equipe = next(
        (cells for d, cells in planning_mois.par_dimanche().items() if d == dimanche),
        [],
    )
    a_venir = (
        db.query(models.Indisponibilite)
        .options(joinedload(models.Indisponibilite.membre), joinedload(models.Indisponibilite.role))
        .filter(
            models.Indisponibilite.eglise_id == eglise.id,
            models.Indisponibilite.date >= aujourd_hui,
            models.Indisponibilite.date <= aujourd_hui + timedelta(days=31),
        )
        .order_by(models.Indisponibilite.date.asc())
        .limit(8)
        .all()
    )
    stats = {
        "membres": db.query(func.count(models.Membre.id)).filter(models.Membre.eglise_id == eglise.id).scalar(),
        "roles": db.query(func.count(models.Role.id)).filter(models.Role.eglise_id == eglise.id).scalar(),
        "indispos": db.query(func.count(models.Indisponibilite.id))
        .filter(models.Indisponibilite.eglise_id == eglise.id, models.Indisponibilite.date >= aujourd_hui)
        .scalar(),
    }
    return render(
        request, "home.html",
        current_eglise=eglise, dimanche=dimanche, equipe=equipe, planning=planning_mois,
        indispos=a_venir, stats=stats, aujourd_hui=aujourd_hui, jours_restants=(dimanche - aujourd_hui).days,
        jour_j=_jour_j_actif(db, eglise.id, aujourd_hui, dimanche),
    )


def _jour_j_actif(db: Session, eglise_id: int, aujourd_hui: date, dimanche: date) -> bool:
    """Le jour du culte, de 2 h avant la première étape à 30 min après la fin prévue."""
    if aujourd_hui != dimanche:
        return False
    deroule = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise_id, models.Deroule.date == dimanche).first()
    if not deroule:
        return False
    try:
        data = json.loads(deroule.data)
    except (TypeError, ValueError):
        return False
    etapes = _etapes_live(db, eglise_id, dimanche, data)
    if not etapes:
        return False
    debuts = jourj.heures_prevues(dimanche, etapes)
    fin_prevue = debuts[-1] + timedelta(minutes=etapes[-1].duree)
    maintenant = datetime.now(jourj.PARIS)
    return debuts[0] - timedelta(hours=2) <= maintenant <= fin_prevue + timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Profil
# ---------------------------------------------------------------------------
@app.get("/profil", response_class=HTMLResponse)
def get_profil(request: Request, eglise: models.Eglise = Depends(get_current_eglise)):
    return render(request, "profil.html", current_eglise=eglise)


@app.post("/profil/modifier")
def post_modifier_profil(request: Request, nom_eglise: str = Form(...), db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    nom_eglise = nettoyer(nom_eglise)
    if nom_eglise:
        eglise.nom_eglise = nom_eglise
        db.commit()
        flash(request, "Le nom de l'église a été mis à jour.")
    return redirect("/profil")


@app.post("/profil/organisation")
def post_organisation(request: Request, organisation: str = Form(...), db: Session = Depends(get_db),
                      eglise: models.Eglise = Depends(get_current_eglise)):
    if organisation in config.ORGANISATIONS:
        eglise.organisation = organisation
        db.commit()
        flash(request, f"Espace configuré pour {config.ORGANISATIONS[organisation]['nom']}.")
    return memoriser_org(redirect("/profil"), eglise)


@app.post("/profil/couleur")
def post_couleur(request: Request, couleur: str = Form(...), db: Session = Depends(get_db),
                 eglise: models.Eglise = Depends(get_current_eglise)):
    if couleur in config.COULEURS:
        eglise.couleur = couleur
        db.commit()
        flash(request, f"Couleur « {config.COULEURS[couleur]['nom']} » appliquée.")
    return redirect("/profil")


@app.post("/profil/mot-de-passe")
def post_modifier_mot_de_passe(
    request: Request,
    mot_de_passe_actuel: str = Form(...),
    nouveau_mot_de_passe: str = Form(...),
    confirmation_mot_de_passe: str = Form(...),
    db: Session = Depends(get_db),
    eglise: models.Eglise = Depends(get_current_eglise),
):
    valide, _ = verify_password(mot_de_passe_actuel, eglise.mot_de_passe_hash)
    if not valide:
        flash(request, "Le mot de passe actuel est incorrect.", "erreur")
    elif nouveau_mot_de_passe != confirmation_mot_de_passe:
        flash(request, "Les deux nouveaux mots de passe ne correspondent pas.", "erreur")
    elif len(nouveau_mot_de_passe) < config.MIN_PASSWORD_LENGTH:
        flash(request, f"Le mot de passe doit contenir au moins {config.MIN_PASSWORD_LENGTH} caractères.", "erreur")
    else:
        eglise.mot_de_passe_hash = hash_password(nouveau_mot_de_passe)
        db.commit()
        flash(request, "Votre mot de passe a été modifié.")
    return redirect("/profil")


# ---------------------------------------------------------------------------
# Rôles
# ---------------------------------------------------------------------------
def _quota(valeur: int) -> int:
    return max(1, min(int(valeur or 1), 50))


@app.get("/roles", response_class=HTMLResponse)
def get_roles(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    roles = services.roles_ordonnes(db, eglise.id)
    effectifs = dict(
        db.query(models.membre_role_association.c.role_id, func.count())
        .join(models.Role, models.Role.id == models.membre_role_association.c.role_id)
        .filter(models.Role.eglise_id == eglise.id)
        .group_by(models.membre_role_association.c.role_id)
        .all()
    )
    return render(request, "roles.html", current_eglise=eglise, roles=roles, effectifs=effectifs)


@app.post("/roles")
def post_role(request: Request, nom: str = Form(...), quota: int = Form(1), ordre: Optional[int] = Form(None),
              db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    nom = nettoyer(nom)
    if not nom:
        flash(request, "Le nom du rôle est obligatoire.", "erreur")
        return redirect("/roles")
    if ordre is None:
        ordre = (db.query(func.max(models.Role.ordre)).filter(models.Role.eglise_id == eglise.id).scalar() or 0) + 1
    db.add(models.Role(nom=nom, quota=_quota(quota), ordre=ordre, eglise_id=eglise.id))
    db.commit()
    flash(request, f"Rôle « {nom} » créé.")
    return redirect("/roles")


@app.get("/roles/{role_id}/modifier", response_class=HTMLResponse)
def get_modifier_role(role_id: int, request: Request, db: Session = Depends(get_db),
                      eglise: models.Eglise = Depends(get_current_eglise)):
    role = possede(db, models.Role, role_id, eglise.id)
    if not role:
        return redirect("/roles")
    return render(request, "modifier_role.html", current_eglise=eglise, role=role)


@app.post("/roles/{role_id}/modifier")
def post_modifier_role(role_id: int, request: Request, nom: str = Form(...), quota: int = Form(1), ordre: int = Form(0),
                       db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    role = possede(db, models.Role, role_id, eglise.id)
    if role and nettoyer(nom):
        role.nom, role.quota, role.ordre = nettoyer(nom), _quota(quota), ordre
        db.commit()
        flash(request, f"Rôle « {role.nom} » mis à jour.")
    return redirect("/roles")


@app.post("/roles/{role_id}/supprimer")
def supprimer_role(role_id: int, request: Request, db: Session = Depends(get_db),
                   eglise: models.Eglise = Depends(get_current_eglise)):
    role = possede(db, models.Role, role_id, eglise.id)
    if role:
        nom = role.nom
        db.query(models.Indisponibilite).filter(models.Indisponibilite.role_id == role_id).delete(synchronize_session=False)
        db.query(models.Planning).filter(models.Planning.role_id == role_id).delete(synchronize_session=False)
        db.execute(models.membre_role_association.delete().where(models.membre_role_association.c.role_id == role_id))
        db.delete(role)
        db.commit()
        flash(request, f"Rôle « {nom} » supprimé.")
    return redirect("/roles")


# ---------------------------------------------------------------------------
# Membres
# ---------------------------------------------------------------------------
def _roles_de_l_eglise(db: Session, eglise_id: int, ids: List[int]) -> List[models.Role]:
    if not ids:
        return []
    return db.query(models.Role).filter(models.Role.id.in_(ids), models.Role.eglise_id == eglise_id).all()


@app.get("/membres", response_class=HTMLResponse)
def get_membres(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    aujourd_hui = date.today()
    debut, fin = services.bornes_mois(aujourd_hui.year, aujourd_hui.month)
    services_mois = dict(
        db.query(models.Planning.membre_id, func.count())
        .filter(models.Planning.eglise_id == eglise.id, models.Planning.date >= debut, models.Planning.date <= fin)
        .group_by(models.Planning.membre_id)
        .all()
    )
    return render(
        request, "membres.html", current_eglise=eglise,
        membres=services.membres_ordonnes(db, eglise.id), roles=services.roles_ordonnes(db, eglise.id),
        services_mois=services_mois, mois_courant=MOIS_FR[aujourd_hui.month].lower(),
    )


@app.post("/membres")
def post_membre(request: Request, nom: str = Form(...), prenom: Optional[str] = Form(None),
                roles: List[int] = Form(default=[]), db: Session = Depends(get_db),
                eglise: models.Eglise = Depends(get_current_eglise)):
    nom, prenom = nettoyer(nom), nettoyer(prenom) or None
    if not nom:
        flash(request, "Le nom est obligatoire.", "erreur")
        return redirect("/membres")
    membre = models.Membre(nom=nom, prenom=prenom, eglise_id=eglise.id)
    membre.roles = _roles_de_l_eglise(db, eglise.id, roles)
    db.add(membre)
    db.commit()
    flash(request, f"{nom_complet(membre)} a été ajouté(e).")
    return redirect("/membres")


@app.get("/membres/{membre_id}/modifier", response_class=HTMLResponse)
def get_modifier_membre(membre_id: int, request: Request, db: Session = Depends(get_db),
                        eglise: models.Eglise = Depends(get_current_eglise)):
    membre = possede(db, models.Membre, membre_id, eglise.id)
    if not membre:
        return redirect("/membres")
    return render(request, "modifier_membre.html", current_eglise=eglise, membre=membre,
                  roles=services.roles_ordonnes(db, eglise.id))


@app.post("/membres/{membre_id}/modifier")
def post_modifier_membre(membre_id: int, request: Request, nom: str = Form(...), prenom: Optional[str] = Form(None),
                         roles: List[int] = Form(default=[]), db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    membre = possede(db, models.Membre, membre_id, eglise.id)
    if membre and nettoyer(nom):
        membre.nom, membre.prenom = nettoyer(nom), nettoyer(prenom) or None
        membre.roles = _roles_de_l_eglise(db, eglise.id, roles)
        db.commit()
        flash(request, f"{nom_complet(membre)} a été mis(e) à jour.")
    return redirect("/membres")


@app.post("/membres/{membre_id}/supprimer")
def supprimer_membre(membre_id: int, request: Request, db: Session = Depends(get_db),
                     eglise: models.Eglise = Depends(get_current_eglise)):
    membre = possede(db, models.Membre, membre_id, eglise.id)
    if membre:
        nom = nom_complet(membre)
        db.execute(models.membre_role_association.delete().where(models.membre_role_association.c.membre_id == membre_id))
        db.query(models.Indisponibilite).filter(models.Indisponibilite.membre_id == membre_id).delete(synchronize_session=False)
        db.query(models.Planning).filter(models.Planning.membre_id == membre_id).delete(synchronize_session=False)
        db.delete(membre)
        db.commit()
        flash(request, f"{nom} a été supprimé(e).")
    return redirect("/membres")


# ---------------------------------------------------------------------------
# Indisponibilités
# ---------------------------------------------------------------------------
def _parse_dates(valeur: str) -> List[date]:
    dates = []
    for morceau in valeur.replace(";", ",").split(","):
        morceau = morceau.strip()
        if not morceau:
            continue
        try:
            dates.append(datetime.strptime(morceau, "%Y-%m-%d").date())
        except ValueError:
            continue
    return sorted(set(dates))


@app.get("/indisponibilites", response_class=HTMLResponse)
def get_indisponibilites(request: Request, db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    aujourd_hui = date.today()
    toutes = (
        db.query(models.Indisponibilite)
        .options(joinedload(models.Indisponibilite.membre), joinedload(models.Indisponibilite.role))
        .filter(models.Indisponibilite.eglise_id == eglise.id)
        .order_by(models.Indisponibilite.date.asc())
        .all()
    )
    a_venir = defaultdict(list)
    for i in toutes:
        if i.date >= aujourd_hui:
            a_venir[i.date].append(i)
    passees = [i for i in reversed(toutes) if i.date < aujourd_hui]
    return render(
        request, "indisponibilites.html", current_eglise=eglise,
        membres=services.membres_ordonnes(db, eglise.id), roles=services.roles_ordonnes(db, eglise.id),
        a_venir=dict(a_venir), passees=passees, total=len(toutes),
    )


@app.post("/indisponibilites")
def post_indisponibilite(request: Request, membre_id: int = Form(...), date_indispo: str = Form(...),
                         role_ids: List[int] = Form(default=[]), db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    membre = possede(db, models.Membre, membre_id, eglise.id)
    dates = _parse_dates(date_indispo)
    if not membre or not dates:
        flash(request, "Choisissez un membre et au moins une date.", "erreur")
        return redirect("/indisponibilites")

    roles_valides = [r.id for r in _roles_de_l_eglise(db, eglise.id, role_ids)] or [None]
    existantes = {
        (i.date, i.role_id)
        for i in db.query(models.Indisponibilite).filter(
            models.Indisponibilite.membre_id == membre.id, models.Indisponibilite.date.in_(dates)
        )
    }
    ajoutees = 0
    for d in dates:
        for role_id in roles_valides:
            if (d, role_id) not in existantes:
                db.add(models.Indisponibilite(membre_id=membre.id, date=d, role_id=role_id, eglise_id=eglise.id))
                ajoutees += 1
    db.commit()
    flash(request, f"{ajoutees} indisponibilité(s) enregistrée(s) pour {nom_complet(membre)}.")
    return redirect("/indisponibilites")


@app.get("/indisponibilites/{indispo_id}/modifier", response_class=HTMLResponse)
def get_modifier_indisponibilite(indispo_id: int, request: Request, db: Session = Depends(get_db),
                                 eglise: models.Eglise = Depends(get_current_eglise)):
    indispo = possede(db, models.Indisponibilite, indispo_id, eglise.id)
    if not indispo:
        return redirect("/indisponibilites")
    return render(request, "modifier_indisponibilite.html", current_eglise=eglise, indispo=indispo,
                  membres=services.membres_ordonnes(db, eglise.id), roles=services.roles_ordonnes(db, eglise.id))


@app.post("/indisponibilites/{indispo_id}/modifier")
def post_modifier_indisponibilite(indispo_id: int, request: Request, membre_id: int = Form(...),
                                  date_indispo: str = Form(...), role_id: Optional[str] = Form(None),
                                  db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    indispo = possede(db, models.Indisponibilite, indispo_id, eglise.id)
    membre = possede(db, models.Membre, membre_id, eglise.id)
    dates = _parse_dates(date_indispo)
    if indispo and membre and dates:
        role = possede(db, models.Role, int(role_id), eglise.id) if nettoyer(role_id).isdigit() else None
        indispo.membre_id, indispo.date, indispo.role_id = membre.id, dates[0], role.id if role else None
        db.commit()
        flash(request, "Indisponibilité mise à jour.")
    return redirect("/indisponibilites")


@app.post("/indisponibilites/{indispo_id}/supprimer")
def supprimer_indispo(indispo_id: int, request: Request, db: Session = Depends(get_db),
                      eglise: models.Eglise = Depends(get_current_eglise)):
    indispo = possede(db, models.Indisponibilite, indispo_id, eglise.id)
    if indispo:
        db.delete(indispo)
        db.commit()
        flash(request, "Indisponibilité supprimée.")
    return redirect("/indisponibilites")


@app.post("/indisponibilites/supprimer-passees")
def supprimer_indispos_passees(request: Request, db: Session = Depends(get_db),
                               eglise: models.Eglise = Depends(get_current_eglise)):
    n = db.query(models.Indisponibilite).filter(
        models.Indisponibilite.eglise_id == eglise.id, models.Indisponibilite.date < date.today()
    ).delete(synchronize_session=False)
    db.commit()
    flash(request, f"{n} indisponibilité(s) passée(s) supprimée(s).")
    return redirect("/indisponibilites")


@app.post("/indisponibilites/supprimer-tout")
def supprimer_toutes_indispos(request: Request, db: Session = Depends(get_db),
                              eglise: models.Eglise = Depends(get_current_eglise)):
    n = db.query(models.Indisponibilite).filter(models.Indisponibilite.eglise_id == eglise.id).delete(synchronize_session=False)
    db.commit()
    flash(request, f"{n} indisponibilité(s) supprimée(s).")
    return redirect("/indisponibilites")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------
def _mois_valide(annee: Optional[int], mois: Optional[int]):
    aujourd_hui = date.today()
    annee = annee if annee and 2000 <= annee <= 2100 else aujourd_hui.year
    mois = mois if mois and 1 <= mois <= 12 else aujourd_hui.month
    return annee, mois


@app.get("/generateur", response_class=HTMLResponse)
def get_generateur(request: Request, annee: Optional[int] = None, mois: Optional[int] = None,
                   db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    planning = services.construire_planning(db, eglise.id, annee, mois)
    proposition, changements = None, set()
    affectations_proposees = services.lire_proposition(db, eglise.id, annee, mois)
    if affectations_proposees is not None:
        proposition = services.construire_planning(db, eglise.id, annee, mois, proposition=affectations_proposees)
        actuel = {(l.role.id, c.date): c.noms for l in planning.lignes for c in l.cellules}
        changements = {(l.role.id, c.date) for l in proposition.lignes for c in l.cellules
                       if c.noms != actuel.get((l.role.id, c.date))}
    precedent, suivant = services.mois_voisins(annee, mois)
    return render(request, "generateur.html", current_eglise=eglise, planning=planning,
                  proposition=proposition, changements=changements,
                  precedent=precedent, suivant=suivant, aujourd_hui=date.today(),
                  prochain=services.prochain_dimanche())


@app.post("/generer_planning")
def post_generer_planning(request: Request, annee: int = Form(...), mois: int = Form(...),
                          db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    nom_mois = MOIS_FR[mois].lower()
    if services.affectations_actuelles(db, eglise.id, annee, mois):
        # Mois déjà planifié : on propose une autre combinaison, sans toucher au planning validé.
        manquants, nouvelle = services.proposer_planning(db, eglise.id, annee, mois)
        if not nouvelle:
            flash(request, "Pas d'autre combinaison possible avec les membres et absences actuels.", "attention")
        elif manquants:
            flash(request, f"Nouvelle proposition pour {nom_mois} : {manquants} place(s) restent à pourvoir. "
                           "Validez-la pour l'enregistrer.", "attention")
        else:
            flash(request, f"Nouvelle proposition pour {nom_mois}. Validez-la pour l'enregistrer.")
        return redirect(f"/generateur?annee={annee}&mois={mois}")
    manquants = services.generer_planning(db, eglise.id, annee, mois)
    if manquants:
        flash(request, f"Planning de {nom_mois} généré. {manquants} place(s) n'ont pas pu être pourvues "
                       f"(pas assez de membres disponibles).", "attention")
    else:
        flash(request, f"Planning de {nom_mois} {annee} généré : toutes les places sont pourvues.")
    return redirect(f"/generateur?annee={annee}&mois={mois}")


@app.post("/planning/proposition/valider")
def valider_proposition(request: Request, annee: int = Form(...), mois: int = Form(...),
                        db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    if services.valider_proposition(db, eglise.id, annee, mois):
        flash(request, f"Nouveau planning de {MOIS_FR[mois].lower()} {annee} enregistré.")
    return redirect(f"/generateur?annee={annee}&mois={mois}")


@app.post("/planning/proposition/annuler")
def annuler_proposition(request: Request, annee: int = Form(...), mois: int = Form(...),
                        db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    services.supprimer_proposition(db, eglise.id, annee, mois)
    db.commit()
    flash(request, f"Proposition abandonnée : le planning de {MOIS_FR[mois].lower()} reste inchangé.")
    return redirect(f"/generateur?annee={annee}&mois={mois}")


@app.post("/planning/reinitialiser")
def reinitialiser_planning(request: Request, annee: int = Form(...), mois: int = Form(...),
                           db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    debut, fin = services.bornes_mois(annee, mois)
    db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise.id, models.Planning.date >= debut, models.Planning.date <= fin
    ).delete(synchronize_session=False)
    services.supprimer_proposition(db, eglise.id, annee, mois)
    db.commit()
    flash(request, f"Le planning de {MOIS_FR[mois].lower()} {annee} a été vidé.")
    return redirect(f"/generateur?annee={annee}&mois={mois}")


@app.get("/planning/modifier", response_class=HTMLResponse)
def get_modifier_planning(request: Request, date_str: str, role_id: int, db: Session = Depends(get_db),
                          eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return redirect("/generateur")
    role = possede(db, models.Role, role_id, eglise.id)
    if not role:
        return redirect("/generateur")

    du_jour = db.query(models.Planning).options(joinedload(models.Planning.role)).filter(
        models.Planning.eglise_id == eglise.id, models.Planning.date == jour
    ).all()
    assignes = {p.membre_id for p in du_jour if p.role_id == role.id}
    autres_roles = defaultdict(list)
    for p in du_jour:
        if p.role_id != role.id and p.role:
            autres_roles[p.membre_id].append(p.role.nom)

    indispos = db.query(models.Indisponibilite).filter(
        models.Indisponibilite.eglise_id == eglise.id, models.Indisponibilite.date == jour
    ).all()
    indisponibles = {i.membre_id for i in indispos if i.role_id is None or i.role_id == role.id}

    debut, fin = services.bornes_mois(jour.year, jour.month)
    compteur = dict(
        db.query(models.Planning.membre_id, func.count())
        .filter(models.Planning.eglise_id == eglise.id, models.Planning.date >= debut, models.Planning.date <= fin)
        .group_by(models.Planning.membre_id).all()
    )
    eligibles = [m for m in services.membres_ordonnes(db, eglise.id) if role in m.roles]
    # Disponibles d'abord, puis ceux qui ont le moins servi
    eligibles.sort(key=lambda m: (m.id in indisponibles, bool(autres_roles.get(m.id)), compteur.get(m.id, 0), m.nom))

    return render(request, "modifier_planning.html", current_eglise=eglise, role=role, planning_date=jour,
                  membres_eligibles=eligibles, membres_assignes_ids=assignes, indisponibles=indisponibles,
                  autres_roles=autres_roles, compteur=compteur)


@app.post("/planning/modifier")
def post_modifier_planning(request: Request, date_str: str = Form(...), role_id: int = Form(...),
                           membres_ids: List[int] = Form(default=[]), db: Session = Depends(get_db),
                           eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return redirect("/generateur")
    role = possede(db, models.Role, role_id, eglise.id)
    if not role:
        return redirect("/generateur")
    valides = {
        m.id for m in db.query(models.Membre).filter(models.Membre.id.in_(membres_ids or [-1]),
                                                     models.Membre.eglise_id == eglise.id)
    }
    db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise.id, models.Planning.date == jour, models.Planning.role_id == role.id
    ).delete(synchronize_session=False)
    for membre_id in valides:
        db.add(models.Planning(date=jour, role_id=role.id, membre_id=membre_id, eglise_id=eglise.id))
    db.commit()
    flash(request, f"{role.nom} du {_format_date(jour, 'jour')} mis à jour.")
    return redirect(f"/generateur?annee={jour.year}&mois={jour.month}")


# ---------------------------------------------------------------------------
# Déroulé du culte (programme horaire exportable en image)
# ---------------------------------------------------------------------------
def _deroule_valide(data) -> Optional[dict]:
    """Nettoie les données envoyées par l'éditeur. Retourne None si invalides."""
    if not isinstance(data, dict) or not isinstance(data.get("etapes"), list):
        return None
    texte = lambda v, n: str(v or "")[:n]  # noqa: E731
    couleur = texte(data.get("couleur"), 7)
    if not (len(couleur) == 7 and couleur.startswith("#")):
        couleur = "#8B5E3C"
    photo = data.get("photo") or ""
    if not (isinstance(photo, str) and (photo == "" or photo.startswith("data:image/jpeg;base64,")) and len(photo) < 1_500_000):
        photo = ""
    etapes = []
    for e in data["etapes"][:20]:
        if not isinstance(e, dict):
            continue
        try:
            duree = max(0, min(int(e.get("duree") or 0), 600))
        except (TypeError, ValueError):
            duree = 0
        role_id = e.get("role_id")
        etapes.append({
            "debut": texte(e.get("debut"), 5),
            "duree": duree,
            "titre": texte(e.get("titre"), 80),
            "role_id": int(role_id) if str(role_id or "").isdigit() else None,
            "personne": texte(e.get("personne"), 80),
        })
    couleur2 = texte(data.get("couleur2"), 7)
    if not (len(couleur2) == 7 and couleur2.startswith("#")):
        couleur2 = couleur
    modele = data.get("modele") if data.get("modele") in ("ejp", "icc") else "ejp"
    lignes = data.get("pied_lignes") if isinstance(data.get("pied_lignes"), list) else []
    return {"modele": modele, "titre": texte(data.get("titre"), 60), "sous_titre": texte(data.get("sous_titre"), 40),
            "couleur": couleur, "couleur2": couleur2, "photo": photo, "etapes": etapes,
            "pied_titre": texte(data.get("pied_titre"), 60), "pied_lignes": [texte(l, 80) for l in lignes[:6]]}


@app.get("/deroule", response_class=HTMLResponse)
def get_deroule(request: Request, date_str: Optional[str] = None, db: Session = Depends(get_db),
                eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else services.prochain_dimanche()
    except ValueError:
        jour = services.prochain_dimanche()
    roles = services.roles_ordonnes(db, eglise.id)
    org_cle = organisation_de(eglise)["cle"]

    def lire(ligne):
        try:
            d = json.loads(ligne.data)
            return d if isinstance(d, dict) else None
        except (TypeError, ValueError):
            return None

    def completer(d, modele):
        d = dict(d)
        d.setdefault("modele", modele)
        d.setdefault("couleur2", d.get("couleur", "#8B5E3C"))
        d.setdefault("pied_titre", "")
        d.setdefault("pied_lignes", [])
        return d

    precedents = (
        db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id)
        .order_by(models.Deroule.date.desc()).limit(30).all()
    )

    def gabarit(modele):
        """Dernier déroulé enregistré avec ce modèle (étapes, horaires, pied…), sinon le modèle par défaut."""
        for ligne in precedents:
            d = lire(ligne)
            if d is not None and d.get("modele", "ejp") == modele:
                d = dict(d)
                d["photo"] = ""  # la photo reste propre à chaque culte
                return completer(d, modele)
        return completer(services.deroule_par_defaut(roles, modele, eglise.nom_eglise), modele)

    gabarits = {m: gabarit(m) for m in ("ejp", "icc")}
    existant = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id, models.Deroule.date == jour).first()
    data = lire(existant) if existant else None
    if data is not None and data.get("modele", "ejp") == org_cle:
        data = completer(data, org_cle)
        gabarits[org_cle] = data  # le déroulé de ce culte fait foi pour son modèle
    else:
        # Pas encore de déroulé pour ce culte (ou fait avec l'autre modèle avant un changement ICC / EJP)
        existant, data = None, dict(gabarits[org_cle])
    recents = (
        db.query(models.Deroule.date).filter(models.Deroule.eglise_id == eglise.id)
        .order_by(models.Deroule.date.desc()).limit(6).all()
    )
    return render(
        request, "deroule.html", current_eglise=eglise, jour=jour, deja_enregistre=existant is not None,
        data=data, gabarits=gabarits, roles=roles, affectations=services.affectations_du_jour(db, eglise.id, jour),
        recents=[r[0] for r in recents],
    )


@app.post("/deroule")
async def post_deroule(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        corps = await request.json()
        jour = datetime.strptime(str(corps.get("date")), "%Y-%m-%d").date()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "erreur": "Données invalides."}, status_code=400)
    data = _deroule_valide(corps.get("data"))
    if data is None:
        return JSONResponse({"ok": False, "erreur": "Données invalides."}, status_code=400)
    ligne = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id, models.Deroule.date == jour).first()
    if not ligne:
        ligne = models.Deroule(eglise_id=eglise.id, date=jour)
        db.add(ligne)
    ligne.data = json.dumps(data, ensure_ascii=False)
    ligne.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Mode jour J (chronomètre du déroulé)
# ---------------------------------------------------------------------------
def _aware(valeur: Optional[datetime]) -> Optional[datetime]:
    """DateTime venu de la base (naïf, UTC) -> aware UTC."""
    return valeur.replace(tzinfo=timezone.utc) if valeur else None


def _naif(valeur: Optional[datetime]) -> Optional[datetime]:
    """Aware -> naïf UTC pour stockage en base (comme le reste de l'app)."""
    return valeur.astimezone(timezone.utc).replace(tzinfo=None) if valeur else None


def _etapes_live(db: Session, eglise_id: int, jour: date, data: dict) -> List[jourj.Etape]:
    """Étapes du déroulé telles qu'affichées en direct (nom du responsable résolu depuis le planning si absent)."""
    affectations = services.affectations_du_jour(db, eglise_id, jour)
    out = []
    for e in data.get("etapes", []):
        personne = e.get("personne") or ""
        if not personne and e.get("role_id"):
            personne = affectations.get(int(e["role_id"]), "")
        out.append(jourj.Etape(titre=e.get("titre") or "", personne=personne,
                                duree=int(e.get("duree") or 0), debut=e.get("debut") or ""))
    return out


def _etat_depuis_ligne(ligne: models.DerouleLive) -> jourj.EtatLive:
    etapes = [jourj.Etape(**e) for e in json.loads(ligne.etapes)]
    reel = [
        jourj.EtapeReelle(
            debut=datetime.fromisoformat(r["debut"]) if r.get("debut") else None,
            fin=datetime.fromisoformat(r["fin"]) if r.get("fin") else None,
            pause_ms=r.get("pause_ms", 0),
        )
        for r in (json.loads(ligne.reel) if ligne.reel else [])
    ]
    return jourj.EtatLive(etapes=etapes, index_courant=ligne.index_courant, demarre_a=_aware(ligne.demarre_a),
                          termine_a=_aware(ligne.termine_a), en_pause_depuis=_aware(ligne.en_pause_depuis), reel=reel)


def _ecrire_etat(ligne: models.DerouleLive, etat: jourj.EtatLive) -> None:
    ligne.index_courant = etat.index_courant
    ligne.demarre_a = _naif(etat.demarre_a)
    ligne.termine_a = _naif(etat.termine_a)
    ligne.en_pause_depuis = _naif(etat.en_pause_depuis)
    ligne.reel = json.dumps([
        {"debut": r.debut.isoformat() if r.debut else None, "fin": r.fin.isoformat() if r.fin else None,
         "pause_ms": r.pause_ms}
        for r in etat.reel
    ])
    ligne.version = (ligne.version or 0) + 1


def _etat_jour_j(request: Request, db: Session, eglise: models.Eglise, jour: date) -> dict:
    """État public (JSON) du direct pour une date : étapes, position, chrono, retard."""
    requete = db.query(models.DerouleLive).filter(models.DerouleLive.eglise_id == eglise.id,
                                                   models.DerouleLive.date == jour)
    if engine.dialect.name != "sqlite":
        requete = requete.with_for_update()
    ligne = requete.first()
    maintenant = datetime.now(timezone.utc)

    if ligne:
        etat = _etat_depuis_ligne(ligne)
    else:
        deroule = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id,
                                                   models.Deroule.date == jour).first()
        if not deroule:
            return {"ok": False, "erreur": "Pas de déroulé enregistré pour cette date."}
        try:
            data = json.loads(deroule.data)
        except (TypeError, ValueError):
            data = {}
        etat = jourj.EtatLive(etapes=_etapes_live(db, eglise.id, jour, data))

    courant = etat.etapes[etat.index_courant] if etat.index_courant is not None else None
    reelle = etat.reel[etat.index_courant] if etat.index_courant is not None else None
    restant = jourj.restant_ms(courant, reelle, maintenant, etat.en_pause_depuis) if courant else None
    retard = jourj.retard_ms(etat, jour, maintenant)
    return {
        "ok": True,
        "version": ligne.version if ligne else 0,
        "serveur_maintenant": maintenant.isoformat(),
        "demarre": etat.demarre_a is not None,
        "termine": etat.termine,
        "en_pause": etat.en_pause,
        "index_courant": etat.index_courant,
        "retard_ms": retard,
        "restant_ms": restant,
        "etapes": [
            {
                "titre": e.titre, "personne": e.personne, "duree": e.duree,
                "debut_reel": r.debut.isoformat() if r.debut else None,
                "fin_reel": r.fin.isoformat() if r.fin else None,
            }
            for e, r in zip(etat.etapes, etat.reel)
        ],
    }


@app.get("/deroule/jour-j", response_class=HTMLResponse)
def get_jour_j(request: Request, date_str: Optional[str] = None, db: Session = Depends(get_db),
               eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else services.prochain_dimanche()
    except ValueError:
        jour = services.prochain_dimanche()
    deroule = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id, models.Deroule.date == jour).first()
    return render(request, "deroule_live.html", current_eglise=eglise, jour=jour, a_un_deroule=deroule is not None)


@app.get("/deroule/jour-j/etat")
def get_jour_j_etat(request: Request, date_str: str, db: Session = Depends(get_db),
                    eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"ok": False, "erreur": "Date invalide."}, status_code=400)
    return JSONResponse(_etat_jour_j(request, db, eglise, jour))


@app.post("/deroule/jour-j/action")
async def post_jour_j_action(request: Request, db: Session = Depends(get_db),
                             eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        corps = await request.json()
        jour = datetime.strptime(str(corps.get("date")), "%Y-%m-%d").date()
        action = str(corps.get("action"))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "erreur": "Données invalides."}, status_code=400)
    if action not in ("suivant", "stop", "reprendre", "recommencer"):
        return JSONResponse({"ok": False, "erreur": "Action inconnue."}, status_code=400)

    requete = db.query(models.DerouleLive).filter(models.DerouleLive.eglise_id == eglise.id,
                                                   models.DerouleLive.date == jour)
    if engine.dialect.name != "sqlite":
        requete = requete.with_for_update()
    ligne = requete.first()
    maintenant = datetime.now(timezone.utc)

    if not ligne:
        deroule = db.query(models.Deroule).filter(models.Deroule.eglise_id == eglise.id,
                                                   models.Deroule.date == jour).first()
        if not deroule:
            return JSONResponse({"ok": False, "erreur": "Pas de déroulé enregistré pour cette date."}, status_code=400)
        try:
            data = json.loads(deroule.data)
        except (TypeError, ValueError):
            data = {}
        ligne = models.DerouleLive(eglise_id=eglise.id, date=jour, version=0)
        etapes_fig = _etapes_live(db, eglise.id, jour, data)
        ligne.etapes = json.dumps([{"titre": e.titre, "personne": e.personne, "duree": e.duree, "debut": e.debut}
                                    for e in etapes_fig], ensure_ascii=False)
        etat = jourj.EtatLive(etapes=etapes_fig)
        db.add(ligne)
    else:
        etat = _etat_depuis_ligne(ligne)

    if action == "suivant":
        jourj.avancer(etat, maintenant)
    elif action == "stop":
        jourj.stop(etat, maintenant)
    elif action == "reprendre":
        jourj.reprendre(etat, maintenant)
    elif action == "recommencer":
        jourj.recommencer(etat)

    _ecrire_etat(ligne, etat)
    db.commit()
    return JSONResponse(_etat_jour_j(request, db, eglise, jour))


@app.get("/planning/exporter/pdf")
def export_pdf(annee: int, mois: int, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    planning = services.construire_planning(db, eglise.id, annee, mois)
    return Response(
        content=planning_pdf(eglise.nom_eglise, planning,
                             config.COULEURS.get(eglise.couleur or organisation_de(eglise)["couleur"], config.COULEURS[config.COULEUR_DEFAUT]),
                             services.prochain_dimanche(), organisation_de(eglise)["cle"]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nom_fichier(eglise.nom_eglise, planning, "pdf")}"'},
    )


@app.get("/planning/exporter/excel")
def export_excel(annee: int, mois: int, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    planning = services.construire_planning(db, eglise.id, annee, mois)
    return Response(
        content=planning_csv(planning), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nom_fichier(eglise.nom_eglise, planning, "csv")}"'},
    )


# ---------------------------------------------------------------------------
# Suivi des cultes : pointage et statistiques
# ---------------------------------------------------------------------------
def _dernier_dimanche(jour: Optional[date] = None) -> date:
    jour = jour or date.today()
    return jour - timedelta(days=(jour.weekday() + 1) % 7)


def _periode_demandee(type: Optional[str], annee: Optional[int], n: Optional[int]) -> "stats.Periode":
    type = type if type in stats.PERIODES else "mois"
    if not annee or not (2000 <= annee <= 2100) or not n:
        return stats.periode_contenant(type, _dernier_dimanche())
    return stats.periode(type, annee, n)


def _entier(valeur: Optional[str]) -> Optional[int]:
    try:
        v = int(str(valeur).strip())
        return v if 0 <= v <= 100000 else None
    except (TypeError, ValueError):
        return None


def _filet_import_sheet(request: Request, db: Session, eglise: models.Eglise) -> None:
    """Filet de sécurité du webhook : au plus toutes les 10 min, récupère les modifications du Sheet faites sur
    les cultes du mois en cours et du mois précédent. Ne bloque jamais l'affichage des stats."""
    client = None
    etat = db.query(models.SyncEtat).filter(models.SyncEtat.eglise_id == eglise.id).first()
    if etat and etat.dernier_import and datetime.utcnow() - etat.dernier_import < timedelta(minutes=10):
        return
    client = sheets_sync.client_depuis_env()
    if client is None:
        return
    aujourd_hui = date.today()
    precedent = (aujourd_hui.replace(day=1) - timedelta(days=1))
    jours = [d for a, m in ((precedent.year, precedent.month), (aujourd_hui.year, aujourd_hui.month))
             for d in services.dimanches_du_mois(a, m) if d <= aujourd_hui]
    try:
        r = sheets_sync.importer_auto(db, client, eglise.id, jours=jours)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        log.warning("Filet d'import du Sheet : %s", exc)
        return
    if r.importes:
        flash(request, f"{len(r.importes)} culte(s) mis à jour depuis le Google Sheet.")
    if r.conflits:
        flash(request, f"{len(r.conflits)} culte(s) modifié(s) à la fois sur le site et dans le Sheet : "
                       "à trancher dans « Récupérer depuis le Google Sheet ».", "attention")


@app.get("/stats", response_class=HTMLResponse)
def get_stats(request: Request, type: Optional[str] = None, annee: Optional[int] = None, n: Optional[int] = None,
              db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    _filet_import_sheet(request, db, eglise)
    p = _periode_demandee(type, annee, n)
    b = stats.bilan(db, eglise.id, p)
    recents = (db.query(models.Culte.date).filter(models.Culte.eglise_id == eglise.id)
               .order_by(models.Culte.date.desc()).limit(8).all())
    pointes = {c.date for c in b.cultes}
    a_pointer, jour = [], p.debut + timedelta(days=(6 - p.debut.weekday()) % 7)
    while jour <= min(p.fin, date.today()):
        if jour not in pointes:
            a_pointer.append(jour)
        jour += timedelta(days=7)
    nb_stars = db.query(func.count(models.Star.id)).filter(models.Star.eglise_id == eglise.id).scalar() or 0
    liste_deps = stats.assurer_departements(db, eglise.id)
    nb_deps = len(liste_deps)
    # Radar : tous les départements, dans l'ordre de la liste (forme stable d'une période à l'autre)
    comptes = {stats.cle_nom(nom): (nom, n) for nom, n in b.par_departement}
    radar_items = [(d.nom, comptes.pop(stats.cle_nom(d.nom), (d.nom, 0))[1]) for d in liste_deps]
    radar_items += list(comptes.values())  # départements historiques retirés de la liste
    return render(request, "stats.html", current_eglise=eglise, b=b, p=p, PERIODES=stats.PERIODES, a_pointer=a_pointer,
                  nb_stars=nb_stars, nb_deps=nb_deps, radar_items=radar_items,
                  precedente=stats.periode_voisine(p, -1), suivante=stats.periode_voisine(p, 1),
                  dernier=_dernier_dimanche(), recents=[r[0] for r in recents])


def _exporter_vers_sheet(request: Request, db: Session, client, eglise_id: int, prefixe: str = "") -> None:
    r = sheets_sync.synchroniser_plage(db, client, eglise_id)
    message = prefixe + f"{len(r.ok)} culte(s) écrit(s) dans le Sheet, {len(r.deja_rempli)} déjà rempli(s) à la main (non modifiés)"
    if r.erreurs:
        message += f", {len(r.erreurs)} erreur(s)"
    flash(request, message + ".", "attention" if r.erreurs else "succes")


@app.post("/stats/sheet/synchroniser")
def post_synchroniser_sheet(request: Request, db: Session = Depends(get_db),
                            eglise: models.Eglise = Depends(get_current_eglise)):
    """D'abord récupérer ce qui a été saisi à la main dans le Sheet (avec aperçu), ensuite seulement exporter."""
    client = sheets_sync.client_depuis_env()
    if client is None:
        flash(request, "Synchronisation Google Sheet non configurée.", "attention")
        return redirect("/stats")
    try:
        items = sheets_sync.preparer_import(db, client, eglise.id)
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Lecture du Google Sheet impossible : {exc}", "attention")
        return redirect("/stats")
    # Blocs identiques au site : on les marque comme connus, sans rien modifier.
    sheets_sync.appliquer_import(db, eglise.id, [i for i in items if i.action == "identique"])
    if any(i.action != "identique" for i in items):
        return redirect("/stats/sheet/recuperer")
    _exporter_vers_sheet(request, db, client, eglise.id)
    return redirect("/stats")


@app.get("/stats/sheet/recuperer", response_class=HTMLResponse)
def get_recuperer_sheet(request: Request, db: Session = Depends(get_db),
                        eglise: models.Eglise = Depends(get_current_eglise)):
    """Aperçu de l'import Sheet -> site : rien n'est modifié tant que ce n'est pas confirmé."""
    client = sheets_sync.client_depuis_env()
    if client is None:
        flash(request, "Synchronisation Google Sheet non configurée.", "attention")
        return redirect("/stats")
    try:
        items = [i for i in sheets_sync.preparer_import(db, client, eglise.id) if i.action != "identique"]
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Lecture du Google Sheet impossible : {exc}", "attention")
        return redirect("/stats")
    return render(request, "sheet_import.html", current_eglise=eglise, items=items)


@app.post("/stats/sheet/recuperer/confirmer")
async def post_confirmer_recuperation(request: Request, db: Session = Depends(get_db),
                                      eglise: models.Eglise = Depends(get_current_eglise)):
    client = sheets_sync.client_depuis_env()
    if client is None:
        flash(request, "Synchronisation Google Sheet non configurée.", "attention")
        return redirect("/stats")
    form = await request.form()
    cles = [str(c) for c in form.getlist("cle")]
    try:
        # On relit le Sheet : c'est son état actuel qui est importé, pas celui de l'aperçu affiché plus tôt.
        items = sheets_sync.preparer_import(db, client, eglise.id)
        n = sheets_sync.appliquer_import(db, eglise.id, items, cles)
        # Conflits non cochés : c'est la version du site qui est gardée et renvoyée vers le Sheet.
        sheets_sync.garder_version_site(db, eglise.id, [i for i in items if i.conflit and i.cle not in cles])
    except Exception as exc:  # noqa: BLE001
        flash(request, f"Import depuis le Google Sheet impossible : {exc}", "attention")
        return redirect("/stats")
    _exporter_vers_sheet(request, db, client, eglise.id, prefixe=f"{n} culte(s) récupéré(s) depuis le Sheet. ")
    return redirect("/stats")


def _eglise_du_sheet(db: Session) -> Optional[models.Eglise]:
    """L'église dont le pointage est relié au Google Sheet : SHEET_EGLISE_ID si défini, sinon celle qui a le
    plus de cultes pointés (une seule église utilise la synchro aujourd'hui)."""
    eglise_id = os.getenv("SHEET_EGLISE_ID")
    if eglise_id and eglise_id.isdigit():
        return db.query(models.Eglise).filter(models.Eglise.id == int(eglise_id)).first()
    ligne = (db.query(models.Culte.eglise_id, func.count(models.Culte.id))
             .group_by(models.Culte.eglise_id).order_by(func.count(models.Culte.id).desc()).first())
    return db.query(models.Eglise).filter(models.Eglise.id == ligne[0]).first() if ligne else None


@app.post("/api/sheet/modification", include_in_schema=False)
async def webhook_sheet(request: Request, db: Session = Depends(get_db)):
    """Appelé par le script Apps Script du Google Sheet à chaque modification d'un onglet mensuel."""
    import hmac
    attendu = os.getenv("SHEET_WEBHOOK_SECRET", "")
    recu = request.headers.get("X-Sheet-Secret", "")
    if not attendu or not hmac.compare_digest(attendu.encode(), recu.encode()):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        corps = await request.json()
        onglet, ligne = str(corps.get("onglet", "")), int(corps.get("ligne", 0))
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "erreur": "Données invalides."}, status_code=400)
    client = sheets_sync.client_depuis_env()
    eglise = _eglise_du_sheet(db)
    if client is None or eglise is None:
        return JSONResponse({"ok": False, "erreur": "Synchro non configurée."}, status_code=503)
    try:
        jour = sheets_sync.jour_depuis_cellule(client, onglet, ligne)
        if jour is None or jour < sheets_sync.DEPUIS:
            return {"ok": True, "ignore": True}
        r = sheets_sync.importer_auto(db, client, eglise.id, jours=[jour])
    except Exception as exc:  # noqa: BLE001
        log.warning("Webhook Sheet : %s", exc)
        return JSONResponse({"ok": False, "erreur": "Lecture du Sheet impossible."}, status_code=502)
    return {"ok": True, "jour": jour.isoformat(), "importe": bool(r.importes), "conflit": bool(r.conflits)}


@app.get("/stats/historique", response_class=HTMLResponse)
def get_historique(request: Request, db: Session = Depends(get_db),
                   eglise: models.Eglise = Depends(get_current_eglise)):
    versions = (db.query(models.CulteHistorique).filter(models.CulteHistorique.eglise_id == eglise.id)
                .order_by(models.CulteHistorique.cree_le.desc()).limit(200).all())
    lignes = []
    for v in versions:
        try:
            contenu = json.loads(v.contenu)
        except ValueError:
            contenu = {}
        lignes.append({"v": v, "services": contenu.get("services", []), "effectifs": contenu.get("effectifs", {})})
    conflits = (db.query(models.SheetSync, models.Culte).join(models.Culte, models.Culte.id == models.SheetSync.culte_id)
                .filter(models.Culte.eglise_id == eglise.id, models.SheetSync.statut.in_(["conflit", "deja_rempli"]))
                .order_by(models.Culte.date).all())
    return render(request, "historique.html", current_eglise=eglise, lignes=lignes, conflits=conflits)


@app.post("/stats/historique/{historique_id}/restaurer")
def post_restaurer(request: Request, historique_id: int, db: Session = Depends(get_db),
                   eglise: models.Eglise = Depends(get_current_eglise)):
    culte = sheets_sync.restaurer_version(db, eglise.id, historique_id)
    if culte is None:
        flash(request, "Version introuvable.", "attention")
        return redirect("/stats/historique")
    flash(request, f"Version du {_format_date(culte.date, 'jour')} {culte.date.year} restaurée dans le site "
                   "(la version remplacée reste dans l'historique).")
    r = sheets_sync.synchroniser_culte(db, sheets_sync.client_depuis_env(), culte)
    if r.statut in ("erreur", "deja_rempli") and r.message:
        flash(request, r.message, "attention")
    return redirect("/stats/historique")


@app.get("/stats/culte", response_class=HTMLResponse)
def get_pointage(request: Request, date_str: Optional[str] = None, db: Session = Depends(get_db),
                 eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else _dernier_dimanche()
    except ValueError:
        jour = _dernier_dimanche()
    culte = stats.culte_du_jour(db, eglise.id, jour)
    if culte:
        lignes = [{"departement": s.departement, "prevu": s.prevu, "servi": s.servi} for s in culte.services]
    else:
        lignes = stats.lignes_proposees(db, eglise.id, jour)
    deps = stats.assurer_departements(db, eglise.id)
    noms_deps = [d.nom for d in deps]
    for l in lignes:  # départements historiques supprimés ou renommés depuis
        if l["departement"] not in noms_deps:
            noms_deps.append(l["departement"])
    liste_stars = [x.nom for x in stats.stars(db, eglise.id)]
    membres = liste_stars or sorted({nom_complet(m) for m in services.membres_ordonnes(db, eglise.id)}, key=str.lower)
    groupes: dict = {}
    for l in lignes:
        groupes.setdefault(l["departement"], []).append(l)
    return render(request, "pointage.html", current_eglise=eglise, jour=jour, culte=culte, groupes=list(groupes.items()),
                  departements=noms_deps, membres=membres, a_des_stars=bool(liste_stars),
                  precedent=jour - timedelta(days=7), suivant=jour + timedelta(days=7),
                  futur=jour > date.today())


@app.post("/stats/culte")
async def post_pointage(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    form = await request.form()
    try:
        jour = datetime.strptime(str(form.get("date")), "%Y-%m-%d").date()
    except ValueError:
        return redirect("/stats")
    deps, prevus, servis = form.getlist("departement"), form.getlist("prevu"), form.getlist("servi")
    lignes = list(zip(deps, prevus, servis))[:300]
    culte = stats.enregistrer_culte(db, eglise.id, jour, _entier(form.get("hommes")), _entier(form.get("femmes")), lignes,
                                     _entier(form.get("ados")), _entier(form.get("enfants")))
    flash(request, f"Culte du {_format_date(jour, 'jour')} {jour.year} enregistré : {len(culte.services)} service(s).")
    resultat = sheets_sync.synchroniser_culte(db, sheets_sync.client_depuis_env(), culte)
    if resultat.statut == "erreur":
        flash(request, "Enregistré. Le Google Sheet n'a pas pu être mis à jour, réessayez depuis Stats.", "attention")
    elif resultat.statut == "deja_rempli" and resultat.message:
        flash(request, "Enregistré sur le site. " + resultat.message
              + " Les deux versions sont gardées : comparez-les dans « Récupérer depuis le Google Sheet ».", "attention")
    return redirect(f"/stats?type=mois&annee={jour.year}&n={jour.month}")


@app.post("/stats/culte/supprimer")
def supprimer_pointage(request: Request, date_str: str = Form(...), db: Session = Depends(get_db),
                       eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        jour = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return redirect("/stats")
    culte = stats.culte_du_jour(db, eglise.id, jour)
    if culte:
        db.delete(culte)
        db.commit()
        flash(request, f"Pointage du {_format_date(jour, 'jour')} {jour.year} supprimé.")
    return redirect(f"/stats?type=mois&annee={jour.year}&n={jour.month}")


@app.get("/stats/departements", response_class=HTMLResponse)
def get_departements(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    return render(request, "departements.html", current_eglise=eglise,
                  departements=stats.assurer_departements(db, eglise.id))


@app.post("/stats/departements")
def post_departement(request: Request, nom: str = Form(...), db: Session = Depends(get_db),
                     eglise: models.Eglise = Depends(get_current_eglise)):
    nom = stats.nettoyer(nom, 60)
    existants = stats.departements(db, eglise.id)
    if nom and all(stats.cle_nom(d.nom) != stats.cle_nom(nom) for d in existants):
        db.add(models.Departement(eglise_id=eglise.id, nom=nom, ordre=len(existants)))
        db.commit()
        flash(request, f"Département « {nom} » ajouté.")
    elif nom:
        flash(request, f"Le département « {nom} » existe déjà.", "attention")
    return redirect("/stats/departements")


@app.post("/stats/departements/{dep_id}/renommer")
def renommer_departement(dep_id: int, request: Request, nom: str = Form(...), db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    dep = possede(db, models.Departement, dep_id, eglise.id)
    nom = stats.nettoyer(nom, 60)
    if dep and nom and nom != dep.nom:
        db.query(models.Service).filter(models.Service.eglise_id == eglise.id,
                                        models.Service.departement == dep.nom).update({"departement": nom})
        dep.nom = nom
        db.commit()
        flash(request, f"Département renommé en « {nom} ».")
    return redirect("/stats/departements")


@app.post("/stats/departements/{dep_id}/deplacer")
def deplacer_departement(dep_id: int, request: Request, sens: int = Form(...), db: Session = Depends(get_db),
                         eglise: models.Eglise = Depends(get_current_eglise)):
    liste = stats.departements(db, eglise.id)
    idx = next((i for i, d in enumerate(liste) if d.id == dep_id), None)
    j = idx + (1 if sens > 0 else -1) if idx is not None else None
    if idx is not None and 0 <= j < len(liste):
        liste[idx], liste[j] = liste[j], liste[idx]
        for i, d in enumerate(liste):
            d.ordre = i
        db.commit()
    return redirect("/stats/departements")


@app.post("/stats/departements/{dep_id}/supprimer")
def supprimer_departement(dep_id: int, request: Request, db: Session = Depends(get_db),
                          eglise: models.Eglise = Depends(get_current_eglise)):
    dep = possede(db, models.Departement, dep_id, eglise.id)
    if dep:
        db.delete(dep)
        db.commit()
        flash(request, f"Département « {dep.nom} » retiré de la liste. Son historique reste dans les statistiques.")
    return redirect("/stats/departements")


@app.get("/stats/export/pdf")
def export_stats_pdf(type: Optional[str] = None, annee: Optional[int] = None, n: Optional[int] = None,
                     db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    p = _periode_demandee(type, annee, n)
    b = stats.bilan(db, eglise.id, p)
    couleur = config.COULEURS.get(eglise.couleur or organisation_de(eglise)["couleur"], config.COULEURS[config.COULEUR_DEFAUT])
    base = nom_fichier(eglise.nom_eglise, services.construire_planning(db, eglise.id, p.annee, p.mois[0][1]), "pdf")
    suffixe = {"mois": f"{p.mois[0][1]:02d}_{p.annee}", "trimestre": f"T{p.n}_{p.annee}",
               "semestre": f"S{p.n}_{p.annee}", "annee": str(p.annee)}[p.type]
    fichier = base.replace("planning_", "stats_", 1).rsplit("_", 2)[0] + f"_{suffixe}.pdf"
    return Response(content=stats_pdf(eglise.nom_eglise, b, couleur, organisation_de(eglise)["cle"]),
                    media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fichier}"'})


# ---------------------------------------------------------------------------
# STAR : serviteurs suivis dans les stats (liste distincte des membres)
# ---------------------------------------------------------------------------
@app.get("/stars", response_class=HTMLResponse)
def get_stars(request: Request, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    liste = stats.stars(db, eglise.id)
    membres = {stats.cle_nom(nom_complet(m)) for m in services.membres_ordonnes(db, eglise.id)}
    # nombre de services de chaque STAR sur les 12 derniers mois
    depuis = date.today() - timedelta(days=365)
    compte: dict = defaultdict(int)
    for (servi,) in (db.query(models.Service.servi).join(models.Culte)
                     .filter(models.Service.eglise_id == eglise.id, models.Culte.date >= depuis).all()):
        if servi:
            compte[stats.cle_nom(servi)] += 1
    return render(request, "stars.html", current_eglise=eglise, stars=liste, membres_cles=membres,
                  compte=compte, nb_membres=len(membres))


@app.post("/stars")
def post_stars(request: Request, noms: str = Form(...), db: Session = Depends(get_db),
               eglise: models.Eglise = Depends(get_current_eglise)):
    lignes = [l for l in re.split(r"[\n,;]+", noms or "") if l.strip()][:300]
    n = stats.ajouter_stars(db, eglise.id, lignes)
    if n:
        flash(request, f"{n} STAR ajouté{'s' if n > 1 else ''}.")
    else:
        flash(request, "Aucun nouveau nom : ces personnes sont déjà dans la liste.", "attention")
    return redirect("/stars")


@app.post("/stars/importer-membres")
def importer_membres_stars(request: Request, db: Session = Depends(get_db),
                           eglise: models.Eglise = Depends(get_current_eglise)):
    n = stats.ajouter_stars(db, eglise.id, [nom_complet(m) for m in services.membres_ordonnes(db, eglise.id)])
    flash(request, f"{n} membre{'s' if n > 1 else ''} ajouté{'s' if n > 1 else ''} aux STAR." if n
          else "Tous les membres sont déjà dans la liste des STAR.", "succes" if n else "attention")
    return redirect("/stars")


@app.post("/stars/{star_id}/renommer")
def renommer_star(star_id: int, request: Request, nom: str = Form(...), db: Session = Depends(get_db),
                  eglise: models.Eglise = Depends(get_current_eglise)):
    star = possede(db, models.Star, star_id, eglise.id)
    nom = stats.nettoyer(nom, 60)
    if not star or not nom or nom == star.nom:
        return redirect("/stars")
    ancien = stats.cle_nom(star.nom)
    if any(stats.cle_nom(x.nom) == stats.cle_nom(nom) and x.id != star.id for x in stats.stars(db, eglise.id)):
        flash(request, f"« {nom} » est déjà dans la liste.", "attention")
        return redirect("/stars")
    # l'historique des stats suit le nouveau nom
    for sv in db.query(models.Service).filter(models.Service.eglise_id == eglise.id).all():
        if stats.cle_nom(sv.prevu) == ancien:
            sv.prevu = nom
        if stats.cle_nom(sv.servi) == ancien:
            sv.servi = nom
    star.nom = nom
    db.commit()
    flash(request, f"STAR renommé en « {nom} ».")
    return redirect("/stars")


@app.post("/stars/{star_id}/supprimer")
def supprimer_star(star_id: int, request: Request, db: Session = Depends(get_db),
                   eglise: models.Eglise = Depends(get_current_eglise)):
    star = possede(db, models.Star, star_id, eglise.id)
    if star:
        db.delete(star)
        db.commit()
        flash(request, f"« {star.nom} » retiré des STAR. Ses services passés restent dans les statistiques.")
    return redirect("/stars")


@app.get("/plus", response_class=HTMLResponse)
def get_plus(request: Request, eglise: models.Eglise = Depends(get_current_eglise)):
    return render(request, "plus.html", current_eglise=eglise)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
