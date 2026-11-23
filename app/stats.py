"""Suivi des cultes : pointage des services et statistiques par période."""
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, selectinload

from . import models
from .services import MOIS_FR, bornes_mois, construire_planning

PERIODES = {
    "mois": ("Mensuel", 12),
    "trimestre": ("Trimestriel", 4),
    "semestre": ("Semestriel", 2),
    "annee": ("Annuel", 1),
}

DEPARTEMENTS_PAR_DEFAUT = [
    "Prodige Musique", "Accueil et Intégration", "Réseaux S", "Photo", "Projection", "Sono",
    "Prière des STAR", "Modération", "Sainte Cène et Offrandes", "Prédication", "Coordination", "Musiciens",
]


def cle_nom(texte: str) -> str:
    """Clé de comparaison : sans accents, espaces ni casse (« Berger AYITÉ » == « berger ayite »)."""
    t = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode().lower()
    t = t.replace("&", " et ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def nettoyer(texte: Optional[str], limite: int = 80) -> str:
    return re.sub(r"\s+", " ", (texte or "")).strip()[:limite]


# ---------------------------------------------------------------------------
# Périodes
# ---------------------------------------------------------------------------
@dataclass
class Periode:
    type: str
    annee: int
    n: int
    debut: date
    fin: date
    libelle: str
    mois: List[Tuple[int, int]]  # (année, mois) couverts


def periode(type_: str, annee: int, n: int) -> Periode:
    if type_ not in PERIODES:
        type_ = "mois"
    nb = PERIODES[type_][1]
    n = min(max(1, n), nb)
    taille = 12 // nb
    premier = (n - 1) * taille + 1
    mois = [(annee, m) for m in range(premier, premier + taille)]
    debut = bornes_mois(annee, mois[0][1])[0]
    fin = bornes_mois(annee, mois[-1][1])[1]
    if type_ == "mois":
        libelle = f"{MOIS_FR[premier]} {annee}"
    elif type_ == "trimestre":
        libelle = f"{n}{'er' if n == 1 else 'e'} trimestre {annee}"
    elif type_ == "semestre":
        libelle = f"{n}{'er' if n == 1 else 'nd'} semestre {annee}"
    else:
        libelle = f"Année {annee}"
    return Periode(type_, annee, n, debut, fin, libelle, mois)


def periode_contenant(type_: str, jour: date) -> Periode:
    nb = PERIODES.get(type_, PERIODES["mois"])[1]
    return periode(type_, jour.year, (jour.month - 1) // (12 // nb) + 1)


def periode_voisine(p: Periode, sens: int) -> Periode:
    nb = PERIODES[p.type][1]
    n, annee = p.n + sens, p.annee
    if n < 1:
        n, annee = nb, annee - 1
    elif n > nb:
        n, annee = 1, annee + 1
    return periode(p.type, annee, n)


# ---------------------------------------------------------------------------
# Départements
# ---------------------------------------------------------------------------
def departements(db: Session, eglise_id: int) -> List[models.Departement]:
    return (db.query(models.Departement).filter(models.Departement.eglise_id == eglise_id)
            .order_by(models.Departement.ordre, models.Departement.id).all())


def assurer_departements(db: Session, eglise_id: int) -> List[models.Departement]:
    liste = departements(db, eglise_id)
    if not liste:
        for i, nom in enumerate(DEPARTEMENTS_PAR_DEFAUT):
            db.add(models.Departement(eglise_id=eglise_id, nom=nom, ordre=i))
        db.commit()
        liste = departements(db, eglise_id)
    return liste


# ---------------------------------------------------------------------------
# STAR (serviteurs suivis dans les stats)
# ---------------------------------------------------------------------------
def stars(db: Session, eglise_id: int) -> List[models.Star]:
    return sorted(db.query(models.Star).filter(models.Star.eglise_id == eglise_id).all(), key=lambda s: cle_nom(s.nom))


def ajouter_stars(db: Session, eglise_id: int, noms: List[str]) -> int:
    existants = {cle_nom(s.nom) for s in stars(db, eglise_id)}
    n = 0
    for nom in noms:
        nom = nettoyer(nom, 60)
        k = cle_nom(nom)
        if k and k not in existants:
            db.add(models.Star(eglise_id=eglise_id, nom=nom))
            existants.add(k)
            n += 1
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Pointage d'un culte
# ---------------------------------------------------------------------------
def culte_du_jour(db: Session, eglise_id: int, jour: date) -> Optional[models.Culte]:
    return (db.query(models.Culte).options(selectinload(models.Culte.services))
            .filter(models.Culte.eglise_id == eglise_id, models.Culte.date == jour).first())


def _correspond(dep: str, role: str) -> bool:
    a, b = cle_nom(dep), cle_nom(role)
    return bool(a and b) and (a == b or a.startswith(b + " ") or b.startswith(a + " "))


def lignes_proposees(db: Session, eglise_id: int, jour: date) -> List[dict]:
    """Pré-remplissage d'un culte pas encore pointé.

    Les départements qui correspondent à un rôle du planning reprennent les personnes prévues
    ce dimanche ; les autres reprennent le nombre de lignes du dernier culte pointé.
    """
    deps = assurer_departements(db, eglise_id)
    planning = construire_planning(db, eglise_id, jour.year, jour.month)
    prevus: Dict[str, List[str]] = {}
    for ligne in planning.lignes:
        for cell in ligne.cellules:
            if cell.date == jour and cell.noms:
                prevus[ligne.role.nom] = cell.noms

    dernier = (db.query(models.Culte).options(selectinload(models.Culte.services))
               .filter(models.Culte.eglise_id == eglise_id, models.Culte.date < jour)
               .order_by(models.Culte.date.desc()).first())
    nb_precedent = Counter(cle_nom(s.departement) for s in (dernier.services if dernier else []))

    lignes = []
    for d in deps:
        noms = next((n for role, n in prevus.items() if _correspond(d.nom, role)), None)
        if noms:
            lignes += [{"departement": d.nom, "prevu": n, "servi": n} for n in noms]
        else:
            lignes += [{"departement": d.nom, "prevu": "", "servi": ""}
                       for _ in range(max(1, nb_precedent.get(cle_nom(d.nom), 1)))]
    return lignes


def enregistrer_culte(db: Session, eglise_id: int, jour: date, hommes: Optional[int], femmes: Optional[int],
                      lignes: List[Tuple[str, str, str]], ados: Optional[int] = None,
                      enfants: Optional[int] = None) -> models.Culte:
    culte = culte_du_jour(db, eglise_id, jour)
    if not culte:
        culte = models.Culte(eglise_id=eglise_id, date=jour)
        db.add(culte)
    culte.hommes, culte.femmes = hommes, femmes
    culte.ados, culte.enfants = ados, enfants
    from datetime import datetime
    culte.updated_at = datetime.utcnow()
    culte.services.clear()
    pos = 0
    for dep, prevu, servi in lignes:
        dep, prevu, servi = nettoyer(dep, 60), nettoyer(prevu), nettoyer(servi)
        if not dep or not (prevu or servi):
            continue
        culte.services.append(models.Service(eglise_id=eglise_id, departement=dep, prevu=prevu, servi=servi, position=pos))
        pos += 1
    db.commit()
    return culte


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------
@dataclass
class Compteurs:
    prevus: int = 0
    presents: int = 0
    absences: int = 0
    services: int = 0

    @property
    def taux(self) -> Optional[int]:
        return round(100 * self.presents / self.prevus) if self.prevus else None


@dataclass
class LigneCulte:
    date: date
    hommes: Optional[int]
    femmes: Optional[int]
    compteurs: Compteurs
    ados: Optional[int] = None
    enfants: Optional[int] = None

    @property
    def effectif(self) -> Optional[int]:
        if self.hommes is None and self.femmes is None and self.ados is None and self.enfants is None:
            return None
        return (self.hommes or 0) + (self.femmes or 0) + (self.ados or 0) + (self.enfants or 0)


@dataclass
class LigneMois:
    annee: int
    mois: int
    cultes: int
    compteurs: Compteurs
    effectif_moyen: Optional[int]


@dataclass
class Bilan:
    periode: Periode
    cultes: List[LigneCulte]
    total: Compteurs
    par_departement: List[Tuple[str, int]]
    effectues: List[Tuple[str, int]]
    manques: List[Tuple[str, int]]
    par_mois: List[LigneMois]
    absences_detail: List[Tuple[date, str, str, str]] = field(default_factory=list)  # date, département, prévu, servi
    sans_service: List[str] = field(default_factory=list)  # STAR qui n'ont pas servi sur la période
    hors_liste: List[str] = field(default_factory=list)  # personnes ayant servi mais absentes de la liste des STAR

    @property
    def vide(self) -> bool:
        return not self.cultes

    @property
    def effectif_moyen(self) -> Optional[int]:
        valeurs = [c.effectif for c in self.cultes if c.effectif is not None]
        return round(sum(valeurs) / len(valeurs)) if valeurs else None

    @property
    def hommes_moyen(self) -> Optional[int]:
        v = [c.hommes for c in self.cultes if c.hommes is not None]
        return round(sum(v) / len(v)) if v else None

    @property
    def femmes_moyen(self) -> Optional[int]:
        v = [c.femmes for c in self.cultes if c.femmes is not None]
        return round(sum(v) / len(v)) if v else None

    @property
    def ados_moyen(self) -> Optional[int]:
        v = [c.ados for c in self.cultes if c.ados is not None]
        return round(sum(v) / len(v)) if v else None

    @property
    def enfants_moyen(self) -> Optional[int]:
        v = [c.enfants for c in self.cultes if c.enfants is not None]
        return round(sum(v) / len(v)) if v else None


def _compter(services, compteurs: Compteurs):
    for s in services:
        if s.prevu:
            compteurs.prevus += 1
            if cle_nom(s.servi) == cle_nom(s.prevu):
                compteurs.presents += 1
            else:
                compteurs.absences += 1
        if s.servi:
            compteurs.services += 1


def _classement(compteur: Counter, graphies: Dict[str, Counter]) -> List[Tuple[str, int]]:
    out = [(graphies[k].most_common(1)[0][0], n) for k, n in compteur.items() if n]
    return sorted(out, key=lambda x: (-x[1], cle_nom(x[0])))


def bilan(db: Session, eglise_id: int, p: Periode) -> Bilan:
    cultes = (db.query(models.Culte).options(selectinload(models.Culte.services))
              .filter(models.Culte.eglise_id == eglise_id, models.Culte.date >= p.debut, models.Culte.date <= p.fin)
              .order_by(models.Culte.date).all())
    total = Compteurs()
    lignes: List[LigneCulte] = []
    par_dep, effectues, manques = Counter(), Counter(), Counter()
    graphies: Dict[str, Counter] = defaultdict(Counter)
    graphies_dep: Dict[str, Counter] = defaultdict(Counter)
    absences_detail = []
    par_mois_c: Dict[Tuple[int, int], List[LigneCulte]] = defaultdict(list)

    for c in cultes:
        cpt = Compteurs()
        _compter(c.services, cpt)
        _compter(c.services, total)
        lc = LigneCulte(c.date, c.hommes, c.femmes, cpt, c.ados, c.enfants)
        lignes.append(lc)
        par_mois_c[(c.date.year, c.date.month)].append(lc)
        for s in c.services:
            if s.servi:
                k = cle_nom(s.departement); graphies_dep[k][s.departement] += 1; par_dep[k] += 1
                k = cle_nom(s.servi); graphies[k][s.servi] += 1; effectues[k] += 1
            if s.prevu and cle_nom(s.servi) != cle_nom(s.prevu):
                k = cle_nom(s.prevu); graphies[k][s.prevu] += 1; manques[k] += 1
                absences_detail.append((c.date, s.departement, s.prevu, s.servi))

    par_mois = []
    for (a, m) in p.mois:
        lm = par_mois_c.get((a, m), [])
        cpt = Compteurs()
        for x in lm:
            cpt.prevus += x.compteurs.prevus; cpt.presents += x.compteurs.presents
            cpt.absences += x.compteurs.absences; cpt.services += x.compteurs.services
        eff = [x.effectif for x in lm if x.effectif is not None]
        par_mois.append(LigneMois(a, m, len(lm), cpt, round(sum(eff) / len(eff)) if eff else None))

    liste = stars(db, eglise_id)
    cles_stars = {cle_nom(x.nom): x.nom for x in liste}
    # Les STAR de la liste s'affichent avec leur orthographe de référence
    for k, nom in cles_stars.items():
        if k in graphies:
            graphies[k] = Counter({nom: 10 ** 6})
    sans_service = [nom for k, nom in cles_stars.items() if not effectues.get(k)] if liste else []
    hors_liste = [graphies[k].most_common(1)[0][0] for k in effectues if k not in cles_stars] if liste else []
    return Bilan(p, lignes, total, _classement(par_dep, graphies_dep), _classement(effectues, graphies),
                 _classement(manques, graphies), par_mois, absences_detail,
                 sans_service, sorted(hors_liste, key=cle_nom))


# ---------------------------------------------------------------------------
# Diagramme radar (toile polygonale) pour la répartition par département
# ---------------------------------------------------------------------------
def _couper(nom: str, max_car: int = 14) -> List[str]:
    """Coupe un nom long en deux lignes au plus, sur une espace."""
    if len(nom) <= max_car:
        return [nom]
    mots, l1 = nom.split(), ""
    while mots and len((l1 + " " + mots[0]).strip()) <= max_car:
        l1 = (l1 + " " + mots.pop(0)).strip()
    if not l1:
        l1 = mots.pop(0)
    return [l1, " ".join(mots)] if mots else [l1]


def radar(items: List[Tuple[str, int]], largeur: int = 620, hauteur: int = 480, rayon: int = 150,
          niveaux: int = 4) -> Optional[dict]:
    """Géométrie SVG d'un radar : grille, axes, polygone des valeurs et étiquettes."""
    import math
    if len(items) < 3:
        return None
    cx, cy = largeur / 2, hauteur / 2 + 4
    n = len(items)
    vmax = max(v for _, v in items) or 1

    def point(i, r):
        a = -math.pi / 2 + 2 * math.pi * i / n
        return cx + r * math.cos(a), cy + r * math.sin(a)

    def poly(r):
        return " ".join(f"{x:.1f},{y:.1f}" for x, y in (point(i, r) for i in range(n)))

    grille = [poly(rayon * k / niveaux) for k in range(1, niveaux + 1)]
    axes = [point(i, rayon) for i in range(n)]
    valeurs = [point(i, rayon * v / vmax) for i, (_, v) in enumerate(items)]
    etiquettes = []
    for i, (nom, v) in enumerate(items):
        x, y = point(i, rayon + 22)
        dx = x - cx
        ancre = "middle" if abs(dx) < 12 else ("start" if dx > 0 else "end")
        dy = 4 if abs(y - cy) < rayon * 0.5 else (12 if y > cy else -2)
        lignes = _couper(nom)
        if len(lignes) > 1 and y < cy - rayon * 0.5:
            dy -= 16  # en haut : le bloc de deux lignes remonte
        elif len(lignes) > 1 and abs(y - cy) < rayon * 0.5:
            dy -= 8
        etiquettes.append({"lignes": lignes, "valeur": v, "x": round(x, 1), "y": round(y + dy, 1), "ancre": ancre})
    return {
        "largeur": largeur, "hauteur": hauteur, "cx": cx, "cy": cy,
        "grille": grille, "axes": axes,
        "polygone": " ".join(f"{x:.1f},{y:.1f}" for x, y in valeurs), "points": valeurs,
        "etiquettes": etiquettes, "vmax": vmax,
    }
