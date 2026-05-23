from fastapi import FastAPI, Request, Form, Depends, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date
import calendar
import random
from fpdf import FPDF

from app import models
from app.database import engine, get_db

# Création des tables dans la base de données au démarrage
models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="API Planning EJP",
    description="API pour l'application de planning EJP",
    version="1.0.0"
)

templates = Jinja2Templates(directory="app/templates")

# Dictionnaire de référence STRICT pour l'ordre, les rôles affichés et les quantités générées
BESOINS_ROLES = {
    "Prière des STAR": 1,
    "Modération": 1,
    "Témoignage": 1,
    "Sainte Cène & Offrandes": 1,
    "Prédication ou Exhortation": 1
}

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="home.html")

@app.get("/membres", response_class=HTMLResponse)
def get_membres(request: Request, db: Session = Depends(get_db)):
    membres = db.query(models.Membre).all()
    # On n'affiche que les rôles qui sont dans BESOINS_ROLES pour nettoyer l'interface de "test"
    roles_db = db.query(models.Role).filter(models.Role.nom.in_(BESOINS_ROLES.keys())).all()
    # On les trie explicitement pour que l'affichage des checkbox respecte aussi l'ordre officiel
    roles = sorted(roles_db, key=lambda r: list(BESOINS_ROLES.keys()).index(r.nom)) if roles_db else []
    
    return templates.TemplateResponse(request=request, name="membres.html", context={"membres": membres, "roles": roles})

@app.post("/membres", response_class=HTMLResponse)
async def post_membre(
    request: Request, 
    nom: str = Form(...), 
    prenom: Optional[str] = Form(None), 
    roles: List[int] = Form(default=[]),
    db: Session = Depends(get_db)
):
    # Si le prénom est vide (chaîne vide), on le force à None
    if prenom is not None and not prenom.strip():
        prenom = None
        
    nouveau_membre = models.Membre(nom=nom, prenom=prenom)
    
    if roles:
        roles_associes = db.query(models.Role).filter(models.Role.id.in_(roles)).all()
        nouveau_membre.roles = roles_associes
        
    db.add(nouveau_membre)
    db.commit()
    db.refresh(nouveau_membre)
    
    membres_list = db.query(models.Membre).all()
    roles_db = db.query(models.Role).filter(models.Role.nom.in_(BESOINS_ROLES.keys())).all()
    roles_list = sorted(roles_db, key=lambda r: list(BESOINS_ROLES.keys()).index(r.nom)) if roles_db else []
    
    return templates.TemplateResponse(request=request, name="membres.html", context={"membres": membres_list, "roles": roles_list})

@app.get("/membres/{membre_id}/modifier", response_class=HTMLResponse)
def get_modifier_membre(membre_id: int, request: Request, db: Session = Depends(get_db)):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id).first()
    if not membre:
        return RedirectResponse(url="/membres", status_code=303)
    roles_db = db.query(models.Role).filter(models.Role.nom.in_(BESOINS_ROLES.keys())).all()
    roles = sorted(roles_db, key=lambda r: list(BESOINS_ROLES.keys()).index(r.nom)) if roles_db else []
    return templates.TemplateResponse(request=request, name="modifier_membre.html", context={"membre": membre, "roles": roles})

@app.post("/membres/{membre_id}/modifier", response_class=RedirectResponse)
async def post_modifier_membre(
    membre_id: int,
    request: Request,
    nom: str = Form(...),
    prenom: Optional[str] = Form(None),
    roles: List[int] = Form(default=[]),
    db: Session = Depends(get_db)
):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id).first()
    if not membre:
        return RedirectResponse(url="/membres", status_code=303)
        
    if prenom is not None and not prenom.strip():
        prenom = None
        
    membre.nom = nom
    membre.prenom = prenom
    
    membre.roles = []
    if roles:
        roles_associes = db.query(models.Role).filter(models.Role.id.in_(roles)).all()
        membre.roles = roles_associes
        
    db.commit()
    return RedirectResponse(url="/membres", status_code=303)

@app.get("/indisponibilites", response_class=HTMLResponse)
def get_indisponibilites(request: Request, db: Session = Depends(get_db)):
    membres = db.query(models.Membre).all()
    roles = db.query(models.Role).filter(models.Role.nom.in_(BESOINS_ROLES.keys())).all()
    indisponibilites = db.query(models.Indisponibilite).order_by(models.Indisponibilite.date.desc()).all()
    return templates.TemplateResponse(request=request, name="indisponibilites.html", context={"request": request, "membres": membres, "roles": roles, "indisponibilites": indisponibilites})

@app.post("/indisponibilites", response_class=HTMLResponse)
async def post_indisponibilite(
    request: Request, 
    membre_id: int = Form(...), 
    date_indispo: str = Form(...),
    role_id: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    from datetime import datetime
    
    # Gérer la valeur vide de role_id renvoyée par le formulaire HTML
    role_id_val = int(role_id) if role_id and role_id.strip() else None
    
    # Gérer la sélection multiple (Flatpickr renvoie les dates séparées par des virgules)
    dates = [d.strip() for d in date_indispo.split(',') if d.strip()]
    
    for date_str in dates:
        try:
            d_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
            nouvelle_indispo = models.Indisponibilite(
                membre_id=membre_id, 
                date=d_obj,
                role_id=role_id_val
            )
            db.add(nouvelle_indispo)
        except ValueError:
            pass
            
    db.commit()
    
    membres_list = db.query(models.Membre).all()
    roles_list = db.query(models.Role).filter(models.Role.nom.in_(BESOINS_ROLES.keys())).all()
    indisponibilites_list = db.query(models.Indisponibilite).order_by(models.Indisponibilite.date.desc()).all()
    
    return templates.TemplateResponse(request=request, name="indisponibilites.html", context={"request": request, "membres": membres_list, "roles": roles_list, "indisponibilites": indisponibilites_list})

@app.get("/generateur", response_class=HTMLResponse)
def get_generateur(request: Request, annee: Optional[int] = None, mois: Optional[int] = None, db: Session = Depends(get_db)):
    try:
        from datetime import datetime
        annee_courante = datetime.now().year
        mois_courant = datetime.now().month
        
        if annee is None:
            annee = annee_courante
        if mois is None:
            mois = mois_courant

        # Trouver les plannings du mois
        plannings = db.query(models.Planning).filter(
            models.Planning.date >= date(annee, mois, 1),
            models.Planning.date <= date(annee, mois, calendar.monthrange(annee, mois)[1])
        ).all()

        dimanches = []
        cal = calendar.monthcalendar(annee, mois)
        for semaine in cal:
            if semaine[calendar.SUNDAY] != 0:
                dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
                
        # Préparer le dictionnaire matriciel dans l'ordre strict de BESOINS_ROLES
        matrice = {}
        for nom_role in BESOINS_ROLES.keys():
            matrice[nom_role] = {d: [] for d in dimanches}
            
        for planning in plannings:
            # On ignore les plannings liés à des rôles qui ne sont plus dans BESOINS_ROLES (ex: 'test')
            if planning.role.nom in matrice and planning.date in matrice[planning.role.nom]:
                matrice[planning.role.nom][planning.date].append(f"{planning.membre.nom} {planning.membre.prenom}")

        return templates.TemplateResponse(request=request, name="generateur.html", context={
            "request": request, 
            "annee_courante": annee_courante,
            "annee_selectionnee": annee,
            "mois_selectionne": mois,
            "dimanches": dimanches,
            "matrice": matrice
        })
    except Exception as e:
        import traceback
        return HTMLResponse(content=f"<pre>{traceback.format_exc()}</pre>", status_code=500)

@app.post("/generer_planning", response_class=RedirectResponse)
async def post_generer_planning(
    request: Request,
    annee: int = Form(...),
    mois: int = Form(...),
    db: Session = Depends(get_db)
):
    try:
        # 0. Vider le planning existant pour ce mois précis avant de générer
        date_debut = date(annee, mois, 1)
        date_fin = date(annee, mois, calendar.monthrange(annee, mois)[1])
        db.query(models.Planning).filter(
            models.Planning.date >= date_debut,
            models.Planning.date <= date_fin
        ).delete()
        db.commit()

        # 1. Calculer l'historique des services pour maximiser la rotation
        historique = db.query(models.Planning).filter(models.Planning.date < date_debut).all()
        
        # Trackers pour équilibrer au maximum la répartition
        dernier_service_global = {}
        dernier_service_role = {}
        compteur_services_mois = {} # Nombre de services attribués dans le mois généré
        
        for p in historique:
            # Historique global (dernier service peu importe le rôle)
            if p.membre_id not in dernier_service_global or p.date > dernier_service_global[p.membre_id]:
                dernier_service_global[p.membre_id] = p.date
            
            # Historique spécifique au rôle
            key = (p.membre_id, p.role_id)
            if key not in dernier_service_role or p.date > dernier_service_role[key]:
                dernier_service_role[key] = p.date
        
        # a. Trouver tous les dimanches du mois
        dimanches = []
        cal = calendar.monthcalendar(annee, mois)
        for semaine in cal:
            if semaine[calendar.SUNDAY] != 0:
                dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
                
        # b. Pour chaque dimanche
        for dimanche in dimanches:
            date_str = dimanche.strftime('%Y-%m-%d')
            membres_exclus_ce_jour = set()

            # Trouver les indisponibilités pour ce dimanche
            toutes_indispos = db.query(models.Indisponibilite).all()
            indispos_du_jour = [i for i in toutes_indispos if str(i.date) == date_str]
            
            # c. Itérer sur chaque rôle défini, en suivant l'ordre strict
            for nom_role, quantite in BESOINS_ROLES.items():
                role = db.query(models.Role).filter(models.Role.nom == nom_role).first()
                if not role:
                    continue # Ignore si le rôle n'existe pas en base
                
                membres_exclus_pour_ce_role = set(membres_exclus_ce_jour)
                for indispo in indispos_du_jour:
                    if indispo.role_id is None or indispo.role_id == role.id:
                        membres_exclus_pour_ce_role.add(indispo.membre_id)
                
                # Récupère la liste de tous les membres habilités pour ce role
                membres_role = db.query(models.Membre).filter(models.Membre.roles.contains(role)).all()
                
                # Filtre-la strictement : garde uniquement les candidats non exclus
                candidats = [m for m in membres_role if m.id not in membres_exclus_pour_ce_role]
                
                if candidats:
                    # Mélanger d'abord pour garantir l'aléatoire en cas d'égalité
                    random.shuffle(candidats)
                    
                    # Tri multi-critères très strict pour réduire la récurrence :
                    candidats.sort(key=lambda m: (
                        compteur_services_mois.get(m.id, 0),
                        dernier_service_role.get((m.id, role.id), date.min),
                        dernier_service_global.get(m.id, date.min)
                    ))
                    
                    # On limite strictement au nombre de personnes requises pour ce rôle (ex: 3 pour Prière, 1 pour Modération)
                    choisis = candidats[:quantite]
                    
                    for membre_choisi in choisis:
                        nouveau_planning = models.Planning(
                            date=dimanche,
                            membre_id=membre_choisi.id,
                            role_id=role.id
                        )
                        db.add(nouveau_planning)
                        # ÉTAPE CRUCIALE : Exclure immédiatement le membre pour le reste du dimanche
                        membres_exclus_ce_jour.add(membre_choisi.id)
                        
                        # ÉTAPE DE ROTATION : Mettre à jour les trackers
                        dernier_service_global[membre_choisi.id] = dimanche
                        dernier_service_role[(membre_choisi.id, role.id)] = dimanche
                        compteur_services_mois[membre_choisi.id] = compteur_services_mois.get(membre_choisi.id, 0) + 1
                        
        db.commit()
        # e. Rediriger vers GET /generateur avec paramètres
        return RedirectResponse(url=f"/generateur?annee={annee}&mois={mois}", status_code=303)
    except Exception as e:
        import traceback
        return HTMLResponse(content=f"<pre>{traceback.format_exc()}</pre>", status_code=500)

@app.post("/membres/{membre_id}/supprimer", response_class=RedirectResponse)
async def supprimer_membre(membre_id: int, db: Session = Depends(get_db)):
    membre = db.query(models.Membre).filter(models.Membre.id == membre_id).first()
    if membre:
        # Supprimer d'abord les entrées liées pour éviter les erreurs d'intégrité
        db.execute(models.membre_role_association.delete().where(models.membre_role_association.c.membre_id == membre_id))
        db.query(models.Indisponibilite).filter(models.Indisponibilite.membre_id == membre_id).delete()
        db.query(models.Planning).filter(models.Planning.membre_id == membre_id).delete()
        
        # Supprimer le membre
        db.delete(membre)
        db.commit()
    return RedirectResponse(url="/membres", status_code=303)

@app.post("/indisponibilites/{indispo_id}/supprimer", response_class=RedirectResponse)
async def supprimer_indispo(indispo_id: int, db: Session = Depends(get_db)):
    indispo = db.query(models.Indisponibilite).filter(models.Indisponibilite.id == indispo_id).first()
    if indispo:
        db.delete(indispo)
        db.commit()
    return RedirectResponse(url="/indisponibilites", status_code=303)

@app.post("/planning/reinitialiser", response_class=RedirectResponse)
async def reinitialiser_planning(db: Session = Depends(get_db)):
    db.query(models.Planning).delete()
    db.commit()
    return RedirectResponse(url="/generateur", status_code=303)

@app.get("/planning/exporter/pdf")
async def export_planning_pdf(annee: int, mois: int, db: Session = Depends(get_db)):
    date_debut = date(annee, mois, 1)
    date_fin = date(annee, mois, calendar.monthrange(annee, mois)[1])
    plannings = db.query(models.Planning).filter(
        models.Planning.date >= date_debut,
        models.Planning.date <= date_fin
    ).all()

    dimanches = []
    cal = calendar.monthcalendar(annee, mois)
    for semaine in cal:
        if semaine[calendar.SUNDAY] != 0:
            dimanches.append(date(annee, mois, semaine[calendar.SUNDAY]))
            
    # Construire la matrice dans l'ordre strict de BESOINS_ROLES
    matrice = {}
    for nom_role in BESOINS_ROLES.keys():
        matrice[nom_role] = {d: [] for d in dimanches}
        
    for planning in plannings:
        if planning.role.nom in matrice and planning.date in matrice[planning.role.nom]:
            matrice[planning.role.nom][planning.date].append(f"{planning.membre.nom} {planning.membre.prenom or ''}".strip())

    pdf = FPDF(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    
    # Ajout du logo en haut à gauche
    try:
        pdf.image('logo.png', x=10, y=10, w=30)
    except Exception:
        pass # Ignore l'erreur si le fichier n'existe pas encore
        
    pdf.set_font("Arial", 'B', 16)
    
    # Ajustement de la position X/Y du titre pour éviter le logo
    pdf.set_xy(50, 15)
    pdf.cell(0, 10, f"Planning Culte EJP - {mois:02d}/{annee}", ln=True, align='L')
    pdf.ln(15)
    
    pdf.set_font("Arial", 'B', 10)
    col_width_role = 50
    usable_width = 277
    col_width_dimanche = (usable_width - col_width_role) / len(dimanches) if dimanches else 40
    
    # En-têtes
    pdf.set_fill_color(41, 128, 185) # Bleu
    pdf.set_text_color(255, 255, 255) # Blanc
    pdf.cell(col_width_role, 10, "Activité / Rôle", border=1, fill=True, align='C')
    for d in dimanches:
        pdf.cell(col_width_dimanche, 10, d.strftime('%d/%m/%Y'), border=1, fill=True, align='C')
    pdf.ln()
    
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Arial", '', 10)
    
    # Remplissage respectant l'ordre exact
    for nom_role in BESOINS_ROLES.keys():
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
            pdf.cell(col_width_role, 10, "Activité / Rôle", border=1, fill=True, align='C')
            for d in dimanches:
                pdf.cell(col_width_dimanche, 10, d.strftime('%d/%m/%Y'), border=1, fill=True, align='C')
            pdf.ln()
            pdf.set_font("Arial", '', 10)
            pdf.set_text_color(0, 0, 0)
            x_start = pdf.get_x()
            y_start = pdf.get_y()
            
        # Role
        pdf.rect(x_start, y_start, col_width_role, row_height)
        pdf.set_xy(x_start, y_start + (row_height - 8)/2)
        pdf.multi_cell(col_width_role, 8, nom_role, border=0, align='C')
        
        # Dimanches
        for i, names in enumerate(names_per_dimanche):
            current_x = x_start + col_width_role + i * col_width_dimanche
            pdf.rect(current_x, y_start, col_width_dimanche, row_height)
            
            num_lines = names.count('\n') + 1
            top_padding = (row_height - (num_lines * 8)) / 2
            
            pdf.set_xy(current_x, y_start + top_padding)
            pdf.multi_cell(col_width_dimanche, 8, names, border=0, align='C')
            
        pdf.set_xy(x_start, y_start + row_height)

    return Response(content=bytes(pdf.output()), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=planning_culte_ejp_{mois:02d}_{annee}.pdf"})
