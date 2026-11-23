# admin@ejp-stetienne.fr    identifiant administrateur pour l'application déployée en local
# admin123                  mot de passe administrateur pour l'application déployée en local
# python -m uvicorn main:app --reload # pour lancer le serveur en mode développement avec rechargement automatique
# python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload # pour le rendre accessible sur le réseau local (ex: téléphone)
#  http://localhost:8000 # lien pour accéder à l'application dans le navigateur
#  http://127.0.0.1:8000 # lien pour accéder à l'application dans le navigateur
#https://planning-ejp-ste.onrender.com # lien pour accéder à l'application déployée sur Render
# https://planning-ejp-ste.onrender.com/setup-admin?email=movichlemec@gmail.com&password=admin123&secret=maclesecrete2024 # lien pour initialiser la base de données et créer un compte administrateur (à supprimer après utilisation pour des raisons de sécurité)

from fastapi import FastAPI, Request, Form, Depends, Response, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date, datetime
import calendar
import random
import os
import re
import secrets
import csv
import io
import asyncio  # Nécessaire pour l'envoi de mail non-bloquant
from fpdf import FPDF
from passlib.context import CryptContext
from starlette.middleware.sessions import SessionMiddleware
import smtplib
from email.mime.text import MIMEText

from app import models
from app.database import engine, get_db

# Création des tables dans la base de données au démarrage
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="API Planning EJP SaaS",
    description="Plateforme multi-locataire pour l'application de planning EJP",
    version="2.0.0"
)

# Montage du dossier static pour servir le logo et le manifest PWA
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Configuration de la session (cookies)
app.add_middleware(SessionMiddleware, secret_key="super_secret_key_change_me_in_production")

templates = Jinja2Templates(directory="app/templates")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# --- CONFIGURATION SMTP SÉCURISÉE ET DYNAMIQUE ---
# Lit d'abord les variables configurées sur Render. Si absentes, utilise vos valeurs par défaut.
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465  # Utilisation du port SSL (plus stable sur Render)
SMTP_USER = os.environ.get("EMAIL_ADDRESS", "movichlemec@gmail.com")
SMTP_PASSWORD = os.environ.get("EMAIL_PASSWORD", "oeihorxxtpirnpzm")

def execution_envoi_verification(destinataire: str, code: str):
    """Fonction synchrone exécutée dans un thread séparé pour ne pas figer l'application."""
    try:
        msg = MIMEText(f"Bonjour,\n\nVotre code de vérification à 6 chiffres est : {code}\n\nMerci de vous être inscrit sur Planning EJP.")
        msg['Subject'] = 'Vérification de votre compte Planning EJP'
        msg['From'] = f"Planning EJP <{SMTP_USER}>"
        msg['To'] = destinataire

        # Connexion en SSL direct sur le port 465 avec un timeout de 8 secondes
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=8)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"[SMTP] Email de vérification envoyé avec succès à {destinataire}")
    except Exception as e:
        print(f"[SMTP] Erreur lors de l'envoi de l'email : {e}")

def execution_envoi_reinitialisation(destinataire: str, lien: str):
    """Fonction synchrone exécutée dans un thread séparé pour le mot de passe oublié."""
    try:
        msg = MIMEText(f"Bonjour,\n\nVous avez demandé la réinitialisation de votre mot de passe sur Planning EJP.\n\nCliquez sur ce lien sécurisé pour en créer un nouveau : {lien}\n\nSi vous n'êtes pas à l'origine de cette demande, vous pouvez simplement ignorer cet email.")
        msg['Subject'] = 'Réinitialisation de votre mot de passe - Planning EJP'
        msg['From'] = f"Planning EJP <{SMTP_USER}>"
        msg['To'] = destinataire

        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=8)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"[SMTP] Email de réinitialisation envoyé avec succès à {destinataire}")
    except Exception as e:
        print(f"[SMTP] Erreur lors de l'envoi de l'email de réinitialisation : {e}")

# Fonctions asynchrones appelées par les routes (Zéro chargement infini)
async def envoyer_email_verification(destinataire: str, code: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, execution_envoi_verification, destinataire, code)

async def envoyer_email_reinitialisation(destinataire: str, lien: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, execution_envoi_reinitialisation, destinataire, lien)


class NotAuthenticatedException(Exception):
    pass

@app.exception_handler(NotAuthenticatedException)
async def auth_exception_handler(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(url="/login", status_code=303)

# --- DÉPENDANCE D'AUTHENTIFICATION ---
def get_current_eglise(request: Request, db: Session = Depends(get_db)):
    eglise_id = request.session.get("eglise_id")
    if not eglise_id:
        raise NotAuthenticatedException()
    eglise = db.query(models.Eglise).filter(models.Eglise.id == eglise_id).first()
    if not eglise:
        request.session.clear()
        raise NotAuthenticatedException()
    return eglise

# --- ROUTE D'INITIALISATION SECRÈTE (POUR RENDER GRATUIT) ---
@app.get("/setup-admin")
def setup_admin(email: str, password: str, secret: str, db: Session = Depends(get_db)):
    if secret != "maclesecrete2024":
        return {"erreur": "Code secret invalide"}
        
    from sqlalchemy import text
    requetes = [
        "ALTER TABLE eglises ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE eglises ADD COLUMN is_verified BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE eglises ADD COLUMN verification_code VARCHAR;",
        "ALTER TABLE eglises ADD COLUMN reset_token VARCHAR;",
        "ALTER TABLE roles ADD COLUMN ordre INTEGER DEFAULT 0;"
    ]
    with engine.connect() as conn:
        for req in requetes:
            try:
                conn.execute(text(req))
                conn.commit()
            except Exception:
                conn.rollback()

    existant = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if existant:
        existant.is_admin = True
        existant.mot_de_passe_hash = pwd_context.hash(password)
        existant.is_verified = True
        db.commit()
        return {"statut": "succès", "message": f"Le compte existant {email} a été mis à jour et est maintenant Administrateur."}
    else:
        admin = models.Eglise(
            nom_eglise="Admin Global",
            email=email,
            mot_de_passe_hash=pwd_context.hash(password),
            is_admin=True,
            is_verified=True
        )
        db.add(admin)
        db.commit()
        return {"statut": "succès", "message": f"Le compte administrateur {email} a été créé avec succès."}

# --- ROUTES D'AUTHENTIFICATION ---

@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if request.session.get("eglise_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login", response_class=HTMLResponse)
def post_login(
    request: Request, 
    email: str = Form(...), 
    mot_de_passe: str = Form(...),
    db: Session = Depends(get_db)
):
    eglise = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if not eglise or not pwd_context.verify(mot_de_passe, eglise.mot_de_passe_hash):
        return templates.TemplateResponse(request=request, name="login.html", context={"erreur": "Email ou mot de passe incorrect"})
    
    # Connexion réussie
    request.session["eglise_id"] = eglise.id
    return RedirectResponse(url="/", status_code=303)

@app.get("/register", response_class=HTMLResponse)
def get_register(request: Request):
    return templates.TemplateResponse(request=request, name="register.html")

@app.post("/register", response_class=HTMLResponse)
async def post_register(
    request: Request,
    nom_eglise: str = Form(...),
    email: str = Form(...),
    mot_de_passe: str = Form(...),
    db: Session = Depends(get_db)
):
    email_regex = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    if not email_regex.match(email):
        return templates.TemplateResponse(request=request, name="register.html", context={"erreur": "Veuillez entrer une adresse email valide."})

    existant = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if existant:
        return templates.TemplateResponse(request=request, name="register.html", context={"erreur": "Cet email est déjà utilisé."})
    
    nouvelle_eglise = models.Eglise(
        nom_eglise=nom_eglise,
        email=email,
        mot_de_passe_hash=pwd_context.hash(mot_de_passe),
        is_verified=True,
        verification_code=None
    )
    db.add(nouvelle_eglise)
    db.commit()
    db.refresh(nouvelle_eglise)
    
    roles_par_defaut = [
        'Prière des STAR',
        'Modération',
        'Sainte Cène & Offrandes',
        'Prédication ou Exhortation',
        'Témoignage'
    ]
    
    for nom_role in roles_par_defaut:
        nouveau_role = models.Role(nom=nom_role, quota=1, eglise_id=nouvelle_eglise.id)
        db.add(nouveau_role)
        
    db.commit()

    return templates.TemplateResponse(request=request, name="login.html", context={"succes": "Votre compte a été créé avec succès. Vous pouvez maintenant vous connecter !"})

@app.get("/verifier_email", response_class=HTMLResponse)
def get_verifier_email(request: Request, email: str):
    return templates.TemplateResponse(request=request, name="verify.html", context={"email": email})

@app.post("/verifier_email", response_class=HTMLResponse)
def post_verifier_email(
    request: Request,
    email: str = Form(...),
    code: str = Form(...),
    db: Session = Depends(get_db)
):
    eglise = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if not eglise:
        return templates.TemplateResponse(request=request, name="verify.html", context={"email": email, "erreur": "Compte introuvable."})
    
    if eglise.verification_code != code:
        return templates.TemplateResponse(request=request, name="verify.html", context={"email": email, "erreur": "Le code de vérification est incorrect."})
        
    eglise.is_verified = True
    eglise.verification_code = None
    db.commit()
    
    return templates.TemplateResponse(request=request, name="login.html", context={"succes": "Votre email a été vérifié avec succès. Vous pouvez maintenant vous connecter !"})

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/mot-de-passe-oublie", response_class=HTMLResponse)
def get_mot_de_passe_oublie(request: Request):
    return templates.TemplateResponse(request=request, name="forgot_password.html")

@app.post("/mot-de-passe-oublie", response_class=HTMLResponse)
async def post_mot_de_passe_oublie(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    eglise = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if eglise:
        token = secrets.token_urlsafe(32)
        eglise.reset_token = token
        db.commit()
        lien = str(request.base_url) + f"reinitialiser-mot-de-passe?token={token}"
        await envoyer_email_reinitialisation(email, lien)
    
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"succes": "Si cette adresse est associée à un compte, un email contenant les instructions de réinitialisation vous a été envoyé."})

@app.get("/reinitialiser-mot-de-passe", response_class=HTMLResponse)
def get_reinitialiser_mot_de_passe(request: Request, token: str, db: Session = Depends(get_db)):
    eglise = db.query(models.Eglise).filter(models.Eglise.reset_token == token).first()
    if not eglise:
        return templates.TemplateResponse(request=request, name="login.html", context={"erreur": "Ce lien de réinitialisation est invalide ou a expiré."})
    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token})

@app.post("/reinitialiser-mot-de-passe", response_class=HTMLResponse)
def post_reinitialiser_mot_de_passe(
    request: Request, 
    token: str = Form(...), 
    nouveau_mot_de_passe: str = Form(...),
    db: Session = Depends(get_db)
):
    eglise = db.query(models.Eglise).filter(models.Eglise.reset_token == token).first()
    if not eglise:
        return templates.TemplateResponse(request=request, name="login.html", context={"erreur": "Ce lien de réinitialisation est invalide ou a expiré."})
        
    eglise.mot_de_passe_hash = pwd_context.hash(nouveau_mot_de_passe)
    eglise.reset_token = None
    db.commit()
    
    return templates.TemplateResponse(request=request, name="login.html", context={"succes": "Votre mot de passe a été réinitialisé avec succès. Vous pouvez maintenant vous connecter."})

# --- ROUTES PROTÉGÉES (MÉTIER) ---

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request, current_eglise: models.Eglise = Depends(get_current_eglise)):
    return templates.TemplateResponse(request=request, name="home.html", context={"current_eglise": current_eglise})

# --- GESTION DU PROFIL ---
@app.get("/profil", response_class=HTMLResponse)
def get_profil(request: Request, current_eglise: models.Eglise = Depends(get_current_eglise)):
    return templates.TemplateResponse(request=request, name="profil.html", context={"current_eglise": current_eglise})

@app.post("/profil/modifier", response_class=HTMLResponse)
def post_modifier_profil(
    request: Request,
    nom_eglise: str = Form(...),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    current_eglise.nom_eglise = nom_eglise
    db.commit()
    return templates.TemplateResponse(request=request, name="profil.html", context={"current_eglise": current_eglise, "succes": "Le nom de l'église a été mis à jour avec succès."})

@app.post("/profil/mot-de-passe", response_class=HTMLResponse)
def post_modifier_mot_de_passe(
    request: Request,
    mot_de_passe_actuel: str = Form(...),
    nouveau_mot_de_passe: str = Form(...),
    confirmation_mot_de_passe: str = Form(...),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    if not pwd_context.verify(mot_de_passe_actuel, current_eglise.mot_de_passe_hash):
        return templates.TemplateResponse(request=request, name="profil.html", context={"current_eglise": current_eglise, "erreur": "Le mot de passe actuel est incorrect."})
        
    if nouveau_mot_de_passe != confirmation_mot_de_passe:
        return templates.TemplateResponse(request=request, name="profil.html", context={"current_eglise": current_eglise, "erreur": "Les nouveaux mots de passe ne correspondent pas."})
        
    current_eglise.mot_de_passe_hash = pwd_context.hash(nouveau_mot_de_passe)
    db.commit()
    return templates.TemplateResponse(request=request, name="profil.html", context={"current_eglise": current_eglise, "succes": "Votre mot de passe a été mis à jour avec succès."})

# --- GESTION DYNAMIQUES DES RÔLES ---
@app.get("/roles", response_class=HTMLResponse)
def get_roles(request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
    return templates.TemplateResponse(request=request, name="roles.html", context={"roles": roles, "current_eglise": current_eglise})

@app.post("/roles", response_class=HTMLResponse)
def post_role(
    request: Request,
    nom: str = Form(...),
    quota: int = Form(1),
    ordre: int = Form(0),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    nouveau_role = models.Role(nom=nom, quota=quota, ordre=ordre, eglise_id=current_eglise.id)
    db.add(nouveau_role)
    db.commit()
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
    return templates.TemplateResponse(request=request, name="roles.html", context={"roles": roles, "current_eglise": current_eglise})

@app.post("/roles/{role_id}/supprimer", response_class=RedirectResponse)
def supprimer_role(role_id: int, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    role = db.query(models.Role).filter(models.Role.id == role_id, models.Role.eglise_id == current_eglise.id).first()
    if role:
        db.query(models.Indisponibilite).filter(models.Indisponibilite.role_id == role_id).delete()
        db.query(models.Planning).filter(models.Planning.role_id == role_id).delete()
        db.delete(role)
        db.commit()
    return RedirectResponse(url="/roles", status_code=303)

@app.get("/roles/{role_id}/modifier", response_class=HTMLResponse)
def get_modifier_role(role_id: int, request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    role = db.query(models.Role).filter(models.Role.id == role_id, models.Role.eglise_id == current_eglise.id).first()
    if not role:
        return RedirectResponse(url="/roles", status_code=303)
    return templates.TemplateResponse(request=request, name="modifier_role.html", context={"role": role, "current_eglise": current_eglise})

@app.post("/roles/{role_id}/modifier", response_class=RedirectResponse)
def post_modifier_role(
    role_id: int,
    request: Request,
    nom: str = Form(...),
    quota: int = Form(1),
    ordre: int = Form(0),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    role = db.query(models.Role).filter(models.Role.id == role_id, models.Role.eglise_id == current_eglise.id).first()
    if not role:
        return RedirectResponse(url="/roles", status_code=303)
        
    role.nom = nom
    role.quota = quota
    role.ordre = ordre
    db.commit()
    return RedirectResponse(url="/roles", status_code=303)


# --- IMPORTATION AUTO DES MEMBRES ---
@app.get("/import-membres-auto")
def import_membres_auto(db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    donnees_membres = {
        "Armel": ["Prière des STAR"],
        "Franck": ["Prière des STAR", "Modération", "Sainte Cène & Offrandes"],
        "Tily": ["Prière des STAR", "Modération", "Sainte Cène & Offrandes"],
        "Priscille": ["Prière des STAR", "Modération"],
        "Laetitia": ["Prière des STAR", "Modération"],
        "Orlyne": ["Prière des STAR", "Modération"],
        "Liesse": ["Prière des STAR", "Modération"],
        "Danielle": ["Prière des STAR", "Modération"],
        "Morel": ["Prière des STAR"],
        "Chancelvie": ["Prière des STAR"],
        "Tahira": ["Modération", "Sainte Cène & Offrandes"],
        "Esther": ["Modération", "Sainte Cène & Offrandes"],
        "Jérémie": ["Modération"],
        "Grace": ["Sainte Cène & Offrandes", "Prédication ou Exhortation"],
        "Débora": ["Prédication ou Exhortation"]
    }
    
    roles_db = {r.nom: r for r in db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()}
    
    for nom, roles_noms in donnees_membres.items():
        membre = db.query(models.Membre).filter(models.Membre.nom == nom, models.Membre.eglise_id == current_eglise.id).first()
        if not membre:
            membre = models.Membre(nom=nom, eglise_id=current_eglise.id)
            db.add(membre)
            
        membre.roles = [roles_db[role_nom] for role_nom in roles_noms if role_nom in roles_db]
        
    db.commit()
    return RedirectResponse(url="/membres", status_code=303)

# --- IMPORTATION AUTO DES INDISPONIBILITÉS ---
@app.get("/import-indispos-auto")
def import_indispos_auto(db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    donnees = [
        ("07/06/2026", "Grace", None),
        ("07/06/2026", "Laetitia", "Modération"),
        ("07/06/2026", "Liesse", None),
        ("07/06/2026", "Esther", None),
        ("07/06/2026", "Débora", None),
        ("07/06/2026", "Projection", None),
        ("07/06/2026", "Morel", None),
        ("07/06/2026", "Armel", None),
        ("07/06/2026", "Priscille", None),
        ("14/06/2026", "Priscille", "Modération"),
        ("14/06/2026", "Priscille", None),
        ("14/06/2026", "Tahira", None),
        ("14/06/2026", "Liesse", None),
        ("14/06/2026", "Berger", None),
        ("14/06/2026", "Projection", None),
        ("21/06/2026", "Grace", None),
        ("21/06/2026", "Berger", None),
        ("28/06/2026", "Grace", None),
        ("28/06/2026", "Liesse", None),
        ("28/06/2026", "Berger", None),
        ("28/06/2026", "Projection", None),
    ]
    
    roles_cache = {r.nom: r.id for r in db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()}
    
    def get_or_create_membre(nom_membre):
        membre = db.query(models.Membre).filter(models.Membre.nom == nom_membre, models.Membre.eglise_id == current_eglise.id).first()
        if not membre:
            membre = models.Membre(nom=nom_membre, eglise_id=current_eglise.id)
            db.add(membre)
            db.flush()
        return membre
        
    for date_str, nom_membre, nom_role in donnees:
        date_obj = datetime.strptime(date_str, "%d/%m/%Y").date()
        membre = get_or_create_membre(nom_membre)
        role_id = roles_cache.get(nom_role) if nom_role else None
        
        indispo = models.Indisponibilite(membre_id=membre.id, date=date_obj, role_id=role_id, eglise_id=current_eglise.id)
        db.add(indispo)
            
    db.commit()
    return RedirectResponse(url="/indisponibilites", status_code=303)

# --- GESTION DES MEMBRES ---
@app.get("/membres", response_class=HTMLResponse)
def get_membres(request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    membres = db.query(models.Membre).filter(models.Membre.eglise_id == current_eglise.id).all()
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    return templates.TemplateResponse(request=request, name="membres.html", context={"membres": membres, "roles": roles, "current_eglise": current_eglise})

@app.post("/membres", response_class=HTMLResponse)
async def post_membre(
    request: Request, 
    nom: str = Form(...), 
    prenom: Optional[str] = Form(None), 
    roles: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    if prenom is not None and not prenom.strip():
        prenom = None
        
    nouveau_membre = models.Membre(nom=nom, prenom=prenom, eglise_id=current_eglise.id)
    
    if roles:
        roles_associes = db.query(models.Role).filter(models.Role.id.in_(roles), models.Role.eglise_id == current_eglise.id).all()
        nouveau_membre.roles = roles_associes
        
    db.add(nouveau_membre)
    db.commit()
    
    membres_list = db.query(models.Membre).filter(models.Membre.eglise_id == current_eglise.id).all()
    roles_list = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    
    return templates.TemplateResponse(request=request, name="membres.html", context={"membres": membres_list, "roles": roles_list, "current_eglise": current_eglise})

@app.get("/membres/{membre_id}/modifier", response_class=HTMLResponse)
def get_modifier_membre(membre_id: int, request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id, models.Membre.eglise_id == current_eglise.id).first()
    if not membre:
        return RedirectResponse(url="/membres", status_code=303)
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    return templates.TemplateResponse(request=request, name="modifier_membre.html", context={"membre": membre, "roles": roles, "current_eglise": current_eglise})

@app.post("/membres/{membre_id}/modifier", response_class=RedirectResponse)
async def post_modifier_membre(
    membre_id: int,
    request: Request,
    nom: str = Form(...),
    prenom: Optional[str] = Form(None),
    roles: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id, models.Membre.eglise_id == current_eglise.id).first()
    if not membre:
        return RedirectResponse(url="/membres", status_code=303)
        
    if prenom is not None and not prenom.strip():
        prenom = None
        
    membre.nom = nom
    membre.prenom = prenom
    
    membre.roles = []
    if roles:
        roles_associes = db.query(models.Role).filter(models.Role.id.in_(roles), models.Role.eglise_id == current_eglise.id).all()
        membre.roles = roles_associes
        
    db.commit()
    return RedirectResponse(url="/membres", status_code=303)


# --- GESTION DES INDISPONIBILITÉS ---
@app.get("/indisponibilites", response_class=HTMLResponse)
def get_indisponibilites(request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    membres = db.query(models.Membre).filter(models.Membre.eglise_id == current_eglise.id).all()
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    indisponibilites = db.query(models.Indisponibilite).filter(models.Indisponibilite.eglise_id == current_eglise.id).order_by(models.Indisponibilite.date.desc()).all()
    return templates.TemplateResponse(request=request, name="indisponibilites.html", context={"membres": membres, "roles": roles, "indisponibilites": indisponibilites, "current_eglise": current_eglise})

@app.post("/indisponibilites", response_class=HTMLResponse)
async def post_indisponibilite(
    request: Request, 
    membre_id: int = Form(...), 
    date_indispo: str = Form(...),
    role_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id, models.Membre.eglise_id == current_eglise.id).first()
    if not membre:
        return RedirectResponse(url="/indisponibilites", status_code=303)

    dates = [d.strip() for d in date_indispo.split(',') if d.strip()]
    
    for date_str in dates:
        try:
            d_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            if not role_ids: # Si aucune activité n'est sélectionnée, c'est une indisponibilité générale
                nouvelle_indispo = models.Indisponibilite(membre_id=membre_id, date=d_obj, role_id=None, eglise_id=current_eglise.id)
                db.add(nouvelle_indispo)
            else: # Sinon, on crée une indisponibilité pour chaque activité sélectionnée
                for role_id in role_ids:
                    nouvelle_indispo = models.Indisponibilite(
                        membre_id=membre_id, 
                        date=d_obj,
                        role_id=role_id,
                        eglise_id=current_eglise.id
                    )
                    db.add(nouvelle_indispo)
        except ValueError:
            pass
            
    db.commit()
    
    membres_list = db.query(models.Membre).filter(models.Membre.eglise_id == current_eglise.id).all()
    roles_list = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    indisponibilites_list = db.query(models.Indisponibilite).filter(models.Indisponibilite.eglise_id == current_eglise.id).order_by(models.Indisponibilite.date.desc()).all()
    
    return templates.TemplateResponse(request=request, name="indisponibilites.html", context={"membres": membres_list, "roles": roles_list, "indisponibilites": indisponibilites_list, "current_eglise": current_eglise})

@app.get("/indisponibilites/{indispo_id}/modifier", response_class=HTMLResponse)
def get_modifier_indisponibilite(indispo_id: int, request: Request, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    indispo = db.query(models.Indisponibilite).filter(models.Indisponibilite.id == indispo_id, models.Indisponibilite.eglise_id == current_eglise.id).first()
    if not indispo:
        return RedirectResponse(url="/indisponibilites", status_code=303)
    membres = db.query(models.Membre).filter(models.Membre.eglise_id == current_eglise.id).all()
    roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).all()
    return templates.TemplateResponse(request=request, name="modifier_indisponibilite.html", context={"indispo": indispo, "membres": membres, "roles": roles, "current_eglise": current_eglise})

@app.post("/indisponibilites/{indispo_id}/modifier", response_class=RedirectResponse)
def post_modifier_indisponibilite(
    indispo_id: int,
    membre_id: int = Form(...),
    date_indispo: str = Form(...),
    role_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    indispo = db.query(models.Indisponibilite).filter(models.Indisponibilite.id == indispo_id, models.Indisponibilite.eglise_id == current_eglise.id).first()
    if not indispo:
        return RedirectResponse(url="/indisponibilites", status_code=303)

    indispo.membre_id = membre_id
    indispo.date = datetime.strptime(date_indispo, '%Y-%m-%d').date()
    indispo.role_id = int(role_id) if role_id and role_id.strip() else None

    db.commit()
    return RedirectResponse(url="/indisponibilites", status_code=303)


# --- GÉNÉRATEUR MATRICE ET PLANNING ---
@app.get("/generateur", response_class=HTMLResponse)
def get_generateur(request: Request, annee: Optional[int] = None, mois: Optional[int] = None, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    try:
        annee_courante = datetime.now().year
        mois_courant = datetime.now().month
        
        if annee is None:
            annee = annee_courante
        if mois is None:
            mois = mois_courant

        plannings = db.query(models.Planning).filter(
            models.Planning.eglise_id == current_eglise.id,
            models.Planning.date >= date(annee, mois, 1),
            models.Planning.date <= date(annee, mois, calendar.monthrange(annee, mois)[1])
        ).all()

        dimanches = []
        cal = calendar.monthcalendar(annee, mois)
        for semaine in cal:
            if semaine[calendar.SUNDAY] != 0:
                dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
                
        roles_eglise = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
        
        matrice = {}
        for r in roles_eglise:
            matrice[r.id] = {'nom': r.nom, 'dates': {d: [] for d in dimanches}}
            
        for planning in plannings:
            if planning.role.id in matrice and planning.date in matrice[planning.role.id]['dates']:
                nom_complet = f"{planning.membre.prenom} {planning.membre.nom}" if planning.membre.prenom else planning.membre.nom
                matrice[planning.role.id]['dates'][planning.date].append(nom_complet)

        return templates.TemplateResponse(request=request, name="generateur.html", context={
            "annee_courante": annee_courante,
            "annee_selectionnee": annee,
            "mois_selectionne": mois,
            "dimanches": dimanches,
            "matrice": matrice,
            "current_eglise": current_eglise
        })
    except Exception as e:
        import traceback
        return HTMLResponse(content=f"<pre>{traceback.format_exc()}</pre>", status_code=500)

@app.post("/generer_planning", response_class=RedirectResponse)
async def post_generer_planning(
    request: Request,
    annee: int = Form(...),
    mois: int = Form(...),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    try:
        date_debut = date(annee, mois, 1)
        date_fin = date(annee, mois, calendar.monthrange(annee, mois)[1])
        db.query(models.Planning).filter(
            models.Planning.eglise_id == current_eglise.id,
            models.Planning.date >= date_debut,
            models.Planning.date <= date_fin
        ).delete()
        db.commit()

        historique = db.query(models.Planning).filter(
            models.Planning.eglise_id == current_eglise.id,
            models.Planning.date < date_debut
        ).all()
        
        dernier_service_global = {}
        dernier_service_role = {}
        compteur_services_mois = {}
        
        for p in historique:
            if p.membre_id not in dernier_service_global or p.date > dernier_service_global[p.membre_id]:
                dernier_service_global[p.membre_id] = p.date
            key = (p.membre_id, p.role_id)
            if key not in dernier_service_role or p.date > dernier_service_role[key]:
                dernier_service_role[key] = p.date
        
        dimanches = []
        cal = calendar.monthcalendar(annee, mois)
        for semaine in cal:
            if semaine[calendar.SUNDAY] != 0:
                dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
                
        for dimanche in dimanches:
            date_str = dimanche.strftime('%Y-%m-%d')
            membres_exclus_ce_jour = set()

            toutes_indispos = db.query(models.Indisponibilite).filter(models.Indisponibilite.eglise_id == current_eglise.id).all()
            indispos_du_jour = [i for i in toutes_indispos if str(i.date) == date_str]
            
            roles = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
            
            for role in roles:
                quantite = role.quota
                
                membres_exclus_pour_ce_role = set(membres_exclus_ce_jour)
                for indispo in indispos_du_jour:
                    if indispo.role_id is None or indispo.role_id == role.id:
                        membres_exclus_pour_ce_role.add(indispo.membre_id)
                
                membres_role = db.query(models.Membre).filter(
                    models.Membre.eglise_id == current_eglise.id,
                    models.Membre.roles.contains(role)
                ).all()
                
                candidats = [m for m in membres_role if m.id not in membres_exclus_pour_ce_role]
                
                if candidats:
                    random.shuffle(candidats)
                    candidats.sort(key=lambda m: (
                        compteur_services_mois.get(m.id, 0),
                        dernier_service_role.get((m.id, role.id), date.min),
                        dernier_service_global.get(m.id, date.min)
                    ))
                    
                    choisis = candidats[:quantite]
                    
                    for membre_choisi in choisis:
                        nouveau_planning = models.Planning(
                            date=dimanche,
                            membre_id=membre_choisi.id,
                            role_id=role.id,
                            eglise_id=current_eglise.id
                        )
                        db.add(nouveau_planning)
                        membres_exclus_ce_jour.add(membre_choisi.id)
                        
                        dernier_service_global[membre_choisi.id] = dimanche
                        dernier_service_role[(membre_choisi.id, role.id)] = dimanche
                        compteur_services_mois[membre_choisi.id] = compteur_services_mois.get(membre_choisi.id, 0) + 1
                        
        db.commit()
        return RedirectResponse(url=f"/generateur?annee={annee}&mois={mois}", status_code=303)
    except Exception as e:
        import traceback
        return HTMLResponse(content=f"<pre>{traceback.format_exc()}</pre>", status_code=500)

@app.get("/planning/modifier", response_class=HTMLResponse)
def get_modifier_planning(
    request: Request,
    date_str: str,
    role_id: int,
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    try:
        planning_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Format de date invalide.")

    role = db.query(models.Role).filter(models.Role.id == role_id, models.Role.eglise_id == current_eglise.id).first()
    if not role:
        raise HTTPException(status_code=404, detail="Rôle non trouvé.")

    membres_assignes_ids = {p.membre_id for p in db.query(models.Planning).filter(models.Planning.date == planning_date, models.Planning.role_id == role_id, models.Planning.eglise_id == current_eglise.id).all()}
    membres_eligibles = db.query(models.Membre).filter(models.Membre.roles.contains(role), models.Membre.eglise_id == current_eglise.id).all()

    return templates.TemplateResponse(request=request, name="modifier_planning.html", context={
        "current_eglise": current_eglise,
        "role": role,
        "planning_date": planning_date,
        "membres_eligibles": membres_eligibles,
        "membres_assignes_ids": membres_assignes_ids
    })

@app.post("/planning/modifier", response_class=RedirectResponse)
def post_modifier_planning(
    request: Request,
    date_str: str = Form(...),
    role_id: int = Form(...),
    membres_ids: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
    current_eglise: models.Eglise = Depends(get_current_eglise)
):
    planning_date = datetime.strptime(date_str, '%Y-%m-%d').date()

    # Supprimer les anciennes assignations pour ce jour et ce rôle
    db.query(models.Planning).filter(models.Planning.date == planning_date, models.Planning.role_id == role_id, models.Planning.eglise_id == current_eglise.id).delete()

    # Ajouter les nouvelles assignations
    for membre_id in membres_ids:
        db.add(models.Planning(date=planning_date, role_id=role_id, membre_id=membre_id, eglise_id=current_eglise.id))
    
    db.commit()
    return RedirectResponse(url=f"/generateur?annee={planning_date.year}&mois={planning_date.month}", status_code=303)

@app.post("/membres/{membre_id}/supprimer", response_class=RedirectResponse)
async def supprimer_membre(membre_id: int, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id, models.Membre.eglise_id == current_eglise.id).first()
    if membre:
        db.execute(models.membre_role_association.delete().where(models.membre_role_association.c.membre_id == membre_id))
        db.query(models.Indisponibilite).filter(models.Indisponibilite.membre_id == membre_id).delete()
        db.query(models.Planning).filter(models.Planning.membre_id == membre_id).delete()
        db.delete(membre)
        db.commit()
    return RedirectResponse(url="/membres", status_code=303)

@app.post("/indisponibilites/{indispo_id}/supprimer", response_class=RedirectResponse)
async def supprimer_indispo(indispo_id: int, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    indispo = db.query(models.Indisponibilite).filter(models.Indisponibilite.id == indispo_id, models.Indisponibilite.eglise_id == current_eglise.id).first()
    if indispo:
        db.delete(indispo)
        db.commit()
    return RedirectResponse(url="/indisponibilites", status_code=303)

@app.post("/indisponibilites/supprimer-tout", response_class=RedirectResponse)
async def supprimer_toutes_les_indisponibilites(db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    """Supprime toutes les indisponibilités pour l'église actuelle."""
    db.query(models.Indisponibilite).filter(models.Indisponibilite.eglise_id == current_eglise.id).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/indisponibilites", status_code=303)

@app.post("/planning/reinitialiser", response_class=RedirectResponse)
async def reinitialiser_planning(db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    db.query(models.Planning).filter(models.Planning.eglise_id == current_eglise.id).delete()
    db.commit()
    return RedirectResponse(url="/generateur", status_code=303)


# --- EXPORTATION PDF NETTOYÉE ---
@app.get("/planning/exporter/pdf")
async def export_planning_pdf(annee: int, mois: int, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    date_debut = date(annee, mois, 1)
    date_fin = date(annee, mois, calendar.monthrange(annee, mois)[1])
    plannings = db.query(models.Planning).filter(
        models.Planning.eglise_id == current_eglise.id,
        models.Planning.date >= date_debut,
        models.Planning.date <= date_fin
    ).all()

    dimanches = []
    cal = calendar.monthcalendar(annee, mois)
    for semaine in cal:
        if semaine[calendar.SUNDAY] != 0:
            dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
            
    roles_eglise = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
    
    matrice = {}
    for role in roles_eglise:
        matrice[role.nom] = {d: [] for d in dimanches}
        
    for planning in plannings:
        if planning.role.nom in matrice and planning.date in matrice[planning.role.nom]:
            nom_complet = f"{planning.membre.prenom} {planning.membre.nom}" if planning.membre.prenom else planning.membre.nom
            matrice[planning.role.nom][planning.date].append(nom_complet)

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    
    try:
        pdf.image('logo.png', x=10, y=10, w=30)
    except Exception:
        pass
        
    pdf.set_font("Arial", 'B', 16)
    pdf.set_xy(50, 15)
    pdf.cell(0, 10, f"Planning Culte - {current_eglise.nom_eglise} - {mois:02d}/{annee}", ln=True, align='L')
    pdf.ln(15)
    
    pdf.set_font("Arial", 'B', 10)
    col_width_role = 50
    usable_width = 277
    col_width_dimanche = (usable_width - col_width_role) / len(dimanches) if dimanches else 40
    
    pdf.set_fill_color(41, 128, 185)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(col_width_role, 10, "Activite / Role", border=1, fill=True, align='C')
    for d in dimanches:
        pdf.cell(col_width_dimanche, 10, d.strftime('%d/%m/%Y'), border=1, fill=True, align='C')
    pdf.ln()
    
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", '', 10)
    
    for role in roles_eglise:
        nom_role = role.nom
        names_per_dimanche = []
        max_lines = 1
        for d in dimanches:
            people = matrice[nom_role][d]
            if not people:
                names_per_dimanche.append("-")
            else:
                names_per_dimanche.append("\n".join(people))
                if len(people) > max_lines:
                    max_lines = len(people)
        
        row_height = max(10, 8 * max_lines)
        x_start = pdf.get_x()
        y_start = pdf.get_y()
        
        if y_start + row_height > 180:
            pdf.add_page()
            y_start = pdf.get_y()
            pdf.set_font("Arial", 'B', 10)
            pdf.set_fill_color(41, 128, 185)
            pdf.set_text_color(255, 255, 255)
            pdf.cell(col_width_role, 10, "Activite / Role", border=1, fill=True, align='C')
            for d in dimanches:
                pdf.cell(col_width_dimanche, 10, d.strftime('%d/%m/%Y'), border=1, fill=True, align='C')
            pdf.ln()
            pdf.set_font("Arial", '', 10)
            pdf.set_text_color(0, 0, 0)
            x_start = pdf.get_x()
            y_start = pdf.get_y()
            
        pdf.rect(x_start, y_start, col_width_role, row_height)
        pdf.set_xy(x_start, y_start + (row_height - 8)/2)
        pdf.multi_cell(col_width_role, 8, nom_role, border=0, align='C')
        
        for i, names in enumerate(names_per_dimanche):
            current_x = x_start + col_width_role + i * col_width_dimanche
            pdf.rect(current_x, y_start, col_width_dimanche, row_height)
            
            num_lines = names.count('\n') + 1
            top_padding = (row_height - (num_lines * 8)) / 2
            
            pdf.set_xy(current_x, y_start + top_padding)
            pdf.multi_cell(col_width_dimanche, 8, names, border=0, align='C')
            
        pdf.set_xy(x_start, y_start + row_height)
        
    pdf_out = pdf.output(dest='S')
    pdf_bytes = pdf_out.encode('latin-1') if isinstance(pdf_out, str) else bytes(pdf_out)
    
    nom_fichier = f"planning_{current_eglise.nom_eglise.replace(' ', '_').lower()}_{str(mois).zfill(2)}_{annee}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{nom_fichier}"'})

# --- EXPORTATION EXCEL / CSV ---
@app.get("/planning/exporter/excel")
async def export_planning_excel(annee: int, mois: int, db: Session = Depends(get_db), current_eglise: models.Eglise = Depends(get_current_eglise)):
    date_debut = date(annee, mois, 1)
    date_fin = date(annee, mois, calendar.monthrange(annee, mois)[1])
    plannings = db.query(models.Planning).filter(
        models.Planning.eglise_id == current_eglise.id,
        models.Planning.date >= date_debut,
        models.Planning.date <= date_fin
    ).all()

    dimanches = []
    cal = calendar.monthcalendar(annee, mois)
    for semaine in cal:
        if semaine[calendar.SUNDAY] != 0:
            dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
            
    roles_eglise = db.query(models.Role).filter(models.Role.eglise_id == current_eglise.id).order_by(models.Role.ordre.asc(), models.Role.id.asc()).all()
    
    matrice = {}
    for role in roles_eglise:
        matrice[role.nom] = {d: [] for d in dimanches}
        
    for planning in plannings:
        if planning.role.nom in matrice and planning.date in matrice[planning.role.nom]:
            nom_complet = f"{planning.membre.prenom} {planning.membre.nom}" if planning.membre.prenom else planning.membre.nom
            matrice[planning.role.nom][planning.date].append(nom_complet)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';') 
    
    en_tete = ["Activite / Role"] + [d.strftime('%d/%m/%Y') for d in dimanches]
    writer.writerow(en_tete)
    
    for role in roles_eglise:
        ligne = [role.nom]
        for d in dimanches:
            personnes = matrice[role.nom][d]
            ligne.append(", ".join(personnes) if personnes else "-")
        writer.writerow(ligne)
        
    nom_fichier = f"planning_{current_eglise.nom_eglise.replace(' ', '_').lower()}_{str(mois).zfill(2)}_{annee}.csv"
    csv_bytes = output.getvalue().encode('utf-8-sig') 
    
    return Response(content=csv_bytes, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{nom_fichier}"'})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)