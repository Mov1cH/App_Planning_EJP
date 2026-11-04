"""Logique métier : dimanches, matrice du planning, génération automatique, initialisation."""
import calendar
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Set

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session, joinedload

from . import config, models
from .database import engine
from .security import hash_password

log = logging.getLogger("planning")

MOIS_FR = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]
MOIS_COURTS = ["", "janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
JOURS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

ROLES_PAR_DEFAUT = [
    "Prière des STAR",
    "Modération",
    "Sainte Cène & Offrandes",
    "Prédication ou Exhortation",
    "Témoignage",
]


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
def bornes_mois(annee: int, mois: int):
    return date(annee, mois, 1), date(annee, mois, calendar.monthrange(annee, mois)[1])


def dimanches_du_mois(annee: int, mois: int) -> List[date]:
    return [
        date(annee, mois, semaine[calendar.SUNDAY])
        for semaine in calendar.monthcalendar(annee, mois)
        if semaine[calendar.SUNDAY] != 0
    ]


def prochain_dimanche(depuis: Optional[date] = None) -> date:
    depuis = depuis or date.today()
    return depuis + timedelta(days=(6 - depuis.weekday()) % 7)


def mois_voisins(annee: int, mois: int):
    prec = (annee - 1, 12) if mois == 1 else (annee, mois - 1)
    suiv = (annee + 1, 1) if mois == 12 else (annee, mois + 1)
    return prec, suiv


def nom_complet(membre: models.Membre) -> str:
    return f"{membre.prenom} {membre.nom}" if membre.prenom else membre.nom


# ---------------------------------------------------------------------------
# Requêtes communes
# ---------------------------------------------------------------------------
def roles_ordonnes(db: Session, eglise_id: int) -> List[models.Role]:
    return (
        db.query(models.Role)
        .filter(models.Role.eglise_id == eglise_id)
        .order_by(models.Role.ordre.asc(), models.Role.id.asc())
        .all()
    )


def membres_ordonnes(db: Session, eglise_id: int) -> List[models.Membre]:
    return (
        db.query(models.Membre)
        .options(joinedload(models.Membre.roles))
        .filter(models.Membre.eglise_id == eglise_id)
        .order_by(models.Membre.nom.asc(), models.Membre.prenom.asc())
        .all()
    )


# ---------------------------------------------------------------------------
# Matrice d'affichage
# ---------------------------------------------------------------------------
@dataclass
class Cellule:
    date: date
    noms: List[str] = field(default_factory=list)
    quota: int = 1

    @property
    def manque(self) -> int:
        return max(0, self.quota - len(self.noms))


@dataclass
class Ligne:
    role: models.Role
    cellules: List[Cellule]


@dataclass
class PlanningMois:
    annee: int
    mois: int
    dimanches: List[date]
    lignes: List[Ligne]
    nb_affectations: int

    @property
    def est_vide(self) -> bool:
        return self.nb_affectations == 0

    @property
    def places_totales(self) -> int:
        return sum(c.quota for l in self.lignes for c in l.cellules)

    @property
    def places_manquantes(self) -> int:
        return sum(c.manque for l in self.lignes for c in l.cellules)

    @property
    def taux_couverture(self) -> int:
        total = self.places_totales
        return 100 if total == 0 else round(100 * (total - self.places_manquantes) / total)

    def par_dimanche(self) -> Dict[date, List[Cellule]]:
        """Vue « par dimanche » (utilisée pour l'affichage mobile)."""
        out: Dict[date, List] = {d: [] for d in self.dimanches}
        for ligne in self.lignes:
            for cell in ligne.cellules:
                out[cell.date].append((ligne.role, cell))
        return out


def construire_planning(db: Session, eglise_id: int, annee: int, mois: int) -> PlanningMois:
    debut, fin = bornes_mois(annee, mois)
    dimanches = dimanches_du_mois(annee, mois)
    roles = roles_ordonnes(db, eglise_id)

    affectations = (
        db.query(models.Planning)
        .options(joinedload(models.Planning.membre))
        .filter(
            models.Planning.eglise_id == eglise_id,
            models.Planning.date >= debut,
            models.Planning.date <= fin,
        )
        .all()
    )
    noms: Dict[tuple, List[str]] = defaultdict(list)
    for p in affectations:
        if p.membre is not None:
            noms[(p.role_id, p.date)].append(nom_complet(p.membre))

    lignes = [
        Ligne(
            role=r,
            cellules=[Cellule(date=d, noms=sorted(noms.get((r.id, d), [])), quota=r.quota or 1) for d in dimanches],
        )
        for r in roles
    ]
    return PlanningMois(annee, mois, dimanches, lignes, nb_affectations=len(affectations))


# ---------------------------------------------------------------------------
# Génération automatique
# ---------------------------------------------------------------------------
def generer_planning(db: Session, eglise_id: int, annee: int, mois: int) -> int:
    """(Re)génère le planning d'un mois. Retourne le nombre de places non pourvues.

    Règles :
      * une personne ne sert qu'à un seul rôle par dimanche ;
      * les indisponibilités (générales ou par rôle) sont respectées ;
      * priorité à ceux qui ont le moins servi ce mois-ci, puis à ceux qui n'ont
        pas servi dans ce rôle depuis le plus longtemps, puis globalement.
    """
    debut, fin = bornes_mois(annee, mois)

    db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise_id,
        models.Planning.date >= debut,
        models.Planning.date <= fin,
    ).delete(synchronize_session=False)

    historique = (
        db.query(models.Planning)
        .filter(models.Planning.eglise_id == eglise_id, models.Planning.date < debut)
        .all()
    )
    dernier_global: Dict[int, date] = {}
    dernier_role: Dict[tuple, date] = {}
    for p in historique:
        if p.date > dernier_global.get(p.membre_id, date.min):
            dernier_global[p.membre_id] = p.date
        cle = (p.membre_id, p.role_id)
        if p.date > dernier_role.get(cle, date.min):
            dernier_role[cle] = p.date

    indispos = (
        db.query(models.Indisponibilite)
        .filter(
            models.Indisponibilite.eglise_id == eglise_id,
            models.Indisponibilite.date >= debut,
            models.Indisponibilite.date <= fin,
        )
        .all()
    )
    indispo_generale: Dict[date, Set[int]] = defaultdict(set)
    indispo_role: Dict[tuple, Set[int]] = defaultdict(set)
    for i in indispos:
        if i.role_id is None:
            indispo_generale[i.date].add(i.membre_id)
        else:
            indispo_role[(i.date, i.role_id)].add(i.membre_id)

    roles = roles_ordonnes(db, eglise_id)
    membres = membres_ordonnes(db, eglise_id)
    membres_par_role: Dict[int, List[models.Membre]] = defaultdict(list)
    for m in membres:
        for r in m.roles:
            membres_par_role[r.id].append(m)

    compteur_mois: Dict[int, int] = defaultdict(int)
    manquants = 0

    for dimanche in dimanches_du_mois(annee, mois):
        deja_pris: Set[int] = set()
        for role in roles:
            exclus = deja_pris | indispo_generale[dimanche] | indispo_role[(dimanche, role.id)]
            candidats = [m for m in membres_par_role[role.id] if m.id not in exclus]
            random.shuffle(candidats)  # départage aléatoire à égalité
            candidats.sort(
                key=lambda m: (
                    compteur_mois[m.id],
                    dernier_role.get((m.id, role.id), date.min),
                    dernier_global.get(m.id, date.min),
                )
            )
            quota = role.quota or 1
            choisis = candidats[:quota]
            manquants += quota - len(choisis)
            for m in choisis:
                db.add(models.Planning(date=dimanche, membre_id=m.id, role_id=role.id, eglise_id=eglise_id))
                deja_pris.add(m.id)
                dernier_global[m.id] = dimanche
                dernier_role[(m.id, role.id)] = dimanche
                compteur_mois[m.id] += 1

    db.commit()
    return manquants


# ---------------------------------------------------------------------------
# Initialisation / migrations légères
# ---------------------------------------------------------------------------
_COLONNES_AJOUTEES = {
    "eglises": {
        "is_admin": "BOOLEAN DEFAULT FALSE",
        "is_verified": "BOOLEAN DEFAULT FALSE",
        "verification_code": "VARCHAR",
        "reset_token": "VARCHAR",
    },
    "roles": {"ordre": "INTEGER DEFAULT 0"},
    "membres": {"prenom": "VARCHAR"},
}


def migrer_schema() -> None:
    """Crée les tables et ajoute les colonnes manquantes des anciennes versions."""
    models.Base.metadata.create_all(bind=engine)
    inspecteur = inspect(engine)
    with engine.begin() as conn:
        for table, colonnes in _COLONNES_AJOUTEES.items():
            if not inspecteur.has_table(table):
                continue
            existantes = {c["name"] for c in inspecteur.get_columns(table)}
            for nom, ddl in colonnes.items():
                if nom not in existantes:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {nom} {ddl}"))
                    log.info("Colonne ajoutée : %s.%s", table, nom)


def creer_roles_par_defaut(db: Session, eglise_id: int) -> None:
    for ordre, nom in enumerate(ROLES_PAR_DEFAUT):
        db.add(models.Role(nom=nom, quota=1, ordre=ordre, eglise_id=eglise_id))


def assurer_admin(db: Session) -> None:
    """Crée le compte administrateur défini par ADMIN_EMAIL/ADMIN_PASSWORD s'il n'existe pas.

    Ne modifie JAMAIS le mot de passe d'un compte existant.
    """
    if not (config.ADMIN_EMAIL and config.ADMIN_PASSWORD):
        return
    existant = db.query(models.Eglise).filter(models.Eglise.email == config.ADMIN_EMAIL).first()
    if existant:
        if not existant.is_admin:
            existant.is_admin = True
            db.commit()
        return
    admin = models.Eglise(
        nom_eglise=config.ADMIN_CHURCH_NAME,
        email=config.ADMIN_EMAIL,
        mot_de_passe_hash=hash_password(config.ADMIN_PASSWORD),
        is_admin=True,
        is_verified=True,
    )
    db.add(admin)
    db.flush()
    creer_roles_par_defaut(db, admin.id)
    db.commit()
    log.info("Compte administrateur créé : %s", config.ADMIN_EMAIL)
