"""Logique métier : dimanches, matrice du planning, génération automatique, initialisation."""
import calendar
import logging
import random
from collections import defaultdict
from dataclasses import dataclass, field
import json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

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


def construire_planning(db: Session, eglise_id: int, annee: int, mois: int,
                        proposition: Optional[List[Tuple[date, int, int]]] = None) -> PlanningMois:
    debut, fin = bornes_mois(annee, mois)
    dimanches = dimanches_du_mois(annee, mois)
    roles = roles_ordonnes(db, eglise_id)

    if proposition is not None:
        membres = {m.id: m for m in membres_ordonnes(db, eglise_id)}
        noms_p: Dict[tuple, List[str]] = defaultdict(list)
        for jour, role_id, membre_id in proposition:
            if membre_id in membres:
                noms_p[(role_id, jour)].append(nom_complet(membres[membre_id]))
        lignes = [
            Ligne(role=r, cellules=[Cellule(date=d, noms=sorted(noms_p.get((r.id, d), [])), quota=r.quota or 1)
                                    for d in dimanches])
            for r in roles
        ]
        return PlanningMois(annee, mois, dimanches, lignes, nb_affectations=len(proposition))

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
Affectation = Tuple[date, int, int]  # (dimanche, role_id, membre_id)


def calculer_affectations(db: Session, eglise_id: int, annee: int, mois: int,
                          varier: bool = False) -> Tuple[List[Affectation], int]:
    """Calcule une combinaison pour le mois, sans rien enregistrer.

    Règles :
      * une personne ne sert qu'à un seul rôle par dimanche ;
      * les indisponibilités (générales ou par rôle) sont respectées ;
      * priorité à ceux qui ont le moins servi ce mois-ci ; puis, par défaut, à ceux
        qui n'ont pas servi dans ce rôle depuis le plus longtemps.
    Avec ``varier``, l'équité du mois est conservée mais l'ordre entre personnes
    à égalité est tiré au hasard, ce qui donne une combinaison différente.
    """
    debut, fin = bornes_mois(annee, mois)

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
    resultat: List[Affectation] = []

    for dimanche in dimanches_du_mois(annee, mois):
        deja_pris: Set[int] = set()
        for role in roles:
            exclus = deja_pris | indispo_generale[dimanche] | indispo_role[(dimanche, role.id)]
            candidats = [m for m in membres_par_role[role.id] if m.id not in exclus]
            random.shuffle(candidats)  # départage aléatoire à égalité
            if varier:
                candidats.sort(key=lambda m: compteur_mois[m.id])
            else:
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
                resultat.append((dimanche, role.id, m.id))
                deja_pris.add(m.id)
                dernier_global[m.id] = dimanche
                dernier_role[(m.id, role.id)] = dimanche
                compteur_mois[m.id] += 1

    return resultat, manquants


def _remplacer_mois(db: Session, eglise_id: int, annee: int, mois: int, affectations: List[Affectation]) -> None:
    debut, fin = bornes_mois(annee, mois)
    db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise_id,
        models.Planning.date >= debut,
        models.Planning.date <= fin,
    ).delete(synchronize_session=False)
    for jour, role_id, membre_id in affectations:
        db.add(models.Planning(date=jour, membre_id=membre_id, role_id=role_id, eglise_id=eglise_id))


def generer_planning(db: Session, eglise_id: int, annee: int, mois: int) -> int:
    """Génère et enregistre directement le planning d'un mois (mois encore vide)."""
    affectations, manquants = calculer_affectations(db, eglise_id, annee, mois)
    _remplacer_mois(db, eglise_id, annee, mois, affectations)
    supprimer_proposition(db, eglise_id, annee, mois)
    db.commit()
    return manquants


# ---------------------------------------------------------------------------
# Propositions : une autre combinaison, enregistrée seulement si elle est validée
# ---------------------------------------------------------------------------
def affectations_actuelles(db: Session, eglise_id: int, annee: int, mois: int) -> List[Affectation]:
    debut, fin = bornes_mois(annee, mois)
    lignes = db.query(models.Planning).filter(
        models.Planning.eglise_id == eglise_id, models.Planning.date >= debut, models.Planning.date <= fin,
    ).all()
    return [(p.date, p.role_id, p.membre_id) for p in lignes if p.membre_id is not None]


def _ligne_proposition(db: Session, eglise_id: int, annee: int, mois: int) -> Optional[models.PropositionPlanning]:
    return db.query(models.PropositionPlanning).filter(
        models.PropositionPlanning.eglise_id == eglise_id,
        models.PropositionPlanning.annee == annee,
        models.PropositionPlanning.mois == mois,
    ).first()


def lire_proposition(db: Session, eglise_id: int, annee: int, mois: int) -> Optional[List[Affectation]]:
    ligne = _ligne_proposition(db, eglise_id, annee, mois)
    if not ligne:
        return None
    try:
        return [(date.fromisoformat(j), int(r), int(m)) for j, r, m in json.loads(ligne.data)]
    except (TypeError, ValueError):
        return None


def supprimer_proposition(db: Session, eglise_id: int, annee: int, mois: int) -> None:
    db.query(models.PropositionPlanning).filter(
        models.PropositionPlanning.eglise_id == eglise_id,
        models.PropositionPlanning.annee == annee,
        models.PropositionPlanning.mois == mois,
    ).delete(synchronize_session=False)


def proposer_planning(db: Session, eglise_id: int, annee: int, mois: int, essais: int = 40) -> Tuple[int, bool]:
    """Calcule une combinaison différente du planning validé (et de la proposition en cours).

    Retourne (places non pourvues, nouvelle combinaison trouvée).
    """
    a_eviter = [set(affectations_actuelles(db, eglise_id, annee, mois))]
    precedente = lire_proposition(db, eglise_id, annee, mois)
    if precedente is not None:
        a_eviter.append(set(precedente))

    meilleure, manquants_meilleure, nouvelle = None, 0, False
    for _ in range(essais):
        affectations, manquants = calculer_affectations(db, eglise_id, annee, mois, varier=True)
        if all(set(affectations) != e for e in a_eviter):
            meilleure, manquants_meilleure, nouvelle = affectations, manquants, True
            break
        if meilleure is None:
            meilleure, manquants_meilleure = affectations, manquants

    ligne = _ligne_proposition(db, eglise_id, annee, mois)
    if not ligne:
        ligne = models.PropositionPlanning(eglise_id=eglise_id, annee=annee, mois=mois)
        db.add(ligne)
    ligne.data = json.dumps([[j.isoformat(), r, m] for j, r, m in meilleure])
    ligne.created_at = datetime.utcnow()
    db.commit()
    return manquants_meilleure, nouvelle


def valider_proposition(db: Session, eglise_id: int, annee: int, mois: int) -> bool:
    proposition = lire_proposition(db, eglise_id, annee, mois)
    if proposition is None:
        return False
    membres = {m.id for m in membres_ordonnes(db, eglise_id)}
    roles = {r.id for r in roles_ordonnes(db, eglise_id)}
    propres = [a for a in proposition if a[1] in roles and a[2] in membres]
    _remplacer_mois(db, eglise_id, annee, mois, propres)
    supprimer_proposition(db, eglise_id, annee, mois)
    db.commit()
    return True


# ---------------------------------------------------------------------------
# Initialisation / migrations légères
# ---------------------------------------------------------------------------
_COLONNES_AJOUTEES = {
    "eglises": {
        "is_admin": "BOOLEAN DEFAULT FALSE",
        "is_verified": "BOOLEAN DEFAULT FALSE",
        "verification_code": "VARCHAR",
        "reset_token": "VARCHAR",
        "couleur": "VARCHAR",
        "organisation": "VARCHAR",
    },
    "roles": {"ordre": "INTEGER DEFAULT 0"},
    "membres": {"prenom": "VARCHAR"},
    "cultes": {"ados": "INTEGER", "enfants": "INTEGER"},
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
        if inspecteur.has_table("eglises"):
            conn.execute(text("UPDATE eglises SET couleur = 'emeraude' WHERE couleur = 'sapin' OR couleur IS NULL"))


ROLES_PAR_DEFAUT_ICC = [
    "Prière des STAR",
    "Louange et adoration",
    "Annonces",
    "Prédication",
    "Sainte Cène",
    "Offrandes",
    "Accueil des nouveaux",
]


def creer_roles_par_defaut(db: Session, eglise_id: int, organisation: str = "ejp") -> None:
    noms = ROLES_PAR_DEFAUT_ICC if organisation == "icc" else ROLES_PAR_DEFAUT
    for ordre, nom in enumerate(noms):
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
        organisation="ejp",
    )
    db.add(admin)
    db.flush()
    creer_roles_par_defaut(db, admin.id)
    db.commit()
    log.info("Compte administrateur créé : %s", config.ADMIN_EMAIL)


# ---------------------------------------------------------------------------
# Déroulé du culte
# ---------------------------------------------------------------------------
DEROULE_ETAPES_DEFAUT = [
    ("12:40", 10, "Prière des STAR", "prière", ""),
    ("13:00", 10, "Modération", "modération", ""),
    ("", 5, "EJP le Mag", None, "MCAD"),
    ("", 15, "Sainte Cène et offrandes", "cène", ""),
    ("", 20, "Louange et adoration", None, "Prodiges Musique"),
    ("", 60, "Prédication", "prédication", ""),
    ("", 10, "Accueil des nouveaux, annonces et clôture", "prédication", ""),
]


DEROULE_ETAPES_ICC = [
    ("08:30", 30, "Prière des STAR", "prière des star", ""),
    ("", 30, "Louange et adorations", "louange", ""),
    ("", 15, "Prière et annonces", "annonce", ""),
    ("", 20, "Louanges et prière", None, ""),
    ("", 60, "Prédication", "prédication", ""),
    ("", 10, "Sainte Cène", "cène", ""),
    ("", 10, "Offrandes", "offrande", ""),
    ("", 10, "Accueil des nouveaux", "accueil", ""),
    ("", 5, "Prière de clôture", None, ""),
]


def deroule_par_defaut(roles: List[models.Role], organisation: str = "ejp", nom_eglise: str = "") -> dict:
    def role_pour(motcle):
        if not motcle:
            return None
        for r in roles:
            if motcle in r.nom.lower():
                return r.id
        return None

    etapes = DEROULE_ETAPES_ICC if organisation == "icc" else DEROULE_ETAPES_DEFAUT
    base = {
        "modele": organisation if organisation in ("ejp", "icc") else "ejp",
        "photo": "",
        "pied_titre": "",
        "pied_lignes": [],
        "etapes": [
            {"debut": debut, "duree": duree, "titre": titre, "role_id": role_pour(motcle), "personne": personne}
            for debut, duree, titre, motcle, personne in etapes
        ],
    }
    if organisation == "icc":
        base.update({
            "titre": nom_eglise or "ICC Saint-Étienne", "sous_titre": "Culte",
            "couleur": "#0F3D26", "couleur2": "#9E7432",
            "pied_titre": "Famille de disciples en service", "pied_lignes": ["Hommes : ", "Femmes : "],
        })
    else:
        base.update({"titre": "Déroulé " + (nom_eglise or "EJP Saint-Étienne"), "sous_titre": "Culte",
                     "couleur": "#8B5E3C", "couleur2": "#8B5E3C"})
    return base


def affectations_du_jour(db: Session, eglise_id: int, jour: date) -> Dict[int, str]:
    lignes = (
        db.query(models.Planning)
        .options(joinedload(models.Planning.membre))
        .filter(models.Planning.eglise_id == eglise_id, models.Planning.date == jour)
        .all()
    )
    out: Dict[int, List[str]] = defaultdict(list)
    for p in lignes:
        if p.membre:
            out[p.role_id].append(p.membre.prenom or p.membre.nom)
    return {rid: " et ".join(sorted(noms)) for rid, noms in out.items()}
