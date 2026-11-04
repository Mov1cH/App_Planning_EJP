"""Planning EJP — application de planification des services du culte.

Lancer en local :
    python -m uvicorn main:app --reload
    (ou, accessible depuis un téléphone du même réseau : --host 0.0.0.0 --port 8000)
"""
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app import config, models, services
from app.database import SessionLocal, get_db
from app.emails import envoyer_email_reinitialisation
from app.exports import nom_fichier, planning_csv, planning_pdf
from app.security import (
    email_limiter,
    hash_password,
    login_limiter,
    new_reset_token,
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


templates.env.globals.update(
    icon=icon,
    get_flashes=_pop_flashes,
    MOIS_FR=MOIS_FR,
    MOIS_COURTS=MOIS_COURTS,
    JOURS_FR=JOURS_FR,
    nom_complet=nom_complet,
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
    if not eglise:
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


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if request.session.get("eglise_id"):
        return redirect("/")
    return render(request, "login.html")


@app.post("/login", response_class=HTMLResponse)
def post_login(request: Request, email: str = Form(...), mot_de_passe: str = Form(...), db: Session = Depends(get_db)):
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
    request.session["eglise_id"] = eglise.id
    return redirect("/")


@app.get("/register", response_class=HTMLResponse)
def get_register(request: Request):
    if not config.ALLOW_REGISTRATION:
        return redirect("/login")
    return render(request, "register.html")


@app.post("/register", response_class=HTMLResponse)
def post_register(
    request: Request,
    nom_eglise: str = Form(...),
    email: str = Form(...),
    mot_de_passe: str = Form(...),
    db: Session = Depends(get_db),
):
    if not config.ALLOW_REGISTRATION:
        return redirect("/login")
    nom_eglise, email = nettoyer(nom_eglise), nettoyer(email).lower()
    ctx = {"nom_eglise": nom_eglise, "email": email}

    if not nom_eglise:
        return render(request, "register.html", status_code=400, erreur="Indiquez le nom de l'église.", **ctx)
    if "@" not in email or "." not in email.split("@")[-1]:
        return render(request, "register.html", status_code=400, erreur="Veuillez entrer une adresse email valide.", **ctx)
    if len(mot_de_passe) < config.MIN_PASSWORD_LENGTH:
        return render(request, "register.html", status_code=400,
                      erreur=f"Le mot de passe doit contenir au moins {config.MIN_PASSWORD_LENGTH} caractères.", **ctx)
    if trouver_par_email(db, email):
        return render(request, "register.html", status_code=400, erreur="Cet email est déjà utilisé.", **ctx)

    eglise = models.Eglise(nom_eglise=nom_eglise, email=email, mot_de_passe_hash=hash_password(mot_de_passe), is_verified=True)
    db.add(eglise)
    db.flush()
    services.creer_roles_par_defaut(db, eglise.id)
    db.commit()

    request.session.clear()
    request.session["eglise_id"] = eglise.id
    flash(request, "Bienvenue ! Votre espace est prêt. Commencez par ajouter vos membres.")
    return redirect("/membres")


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
        .limit(6)
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
        indispos=a_venir, stats=stats,
    )


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
    precedent, suivant = services.mois_voisins(annee, mois)
    return render(request, "generateur.html", current_eglise=eglise, planning=planning,
                  precedent=precedent, suivant=suivant, aujourd_hui=date.today(),
                  prochain=services.prochain_dimanche())


@app.post("/generer_planning")
def post_generer_planning(request: Request, annee: int = Form(...), mois: int = Form(...),
                          db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    manquants = services.generer_planning(db, eglise.id, annee, mois)
    if manquants:
        flash(request, f"Planning de {MOIS_FR[mois].lower()} généré. {manquants} place(s) n'ont pas pu être pourvues "
                       f"(pas assez de membres disponibles).", "attention")
    else:
        flash(request, f"Planning de {MOIS_FR[mois].lower()} {annee} généré : toutes les places sont pourvues.")
    return redirect(f"/generateur?annee={annee}&mois={mois}")


@app.post("/planning/reinitialiser")
def reinitialiser_planning(request: Request, annee: int = Form(...), mois: int = Form(...),
                           db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    debut, fin = services.bornes_mois(annee, mois)
    db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise.id, models.Planning.date >= debut, models.Planning.date <= fin
    ).delete(synchronize_session=False)
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


@app.get("/planning/exporter/pdf")
def export_pdf(annee: int, mois: int, db: Session = Depends(get_db), eglise: models.Eglise = Depends(get_current_eglise)):
    annee, mois = _mois_valide(annee, mois)
    planning = services.construire_planning(db, eglise.id, annee, mois)
    return Response(
        content=planning_pdf(eglise.nom_eglise, planning), media_type="application/pdf",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
