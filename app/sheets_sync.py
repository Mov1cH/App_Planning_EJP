"""Synchronisation du pointage vers le Google Sheet « Suivi de Culte EJP ».

Règle n°1, absolue : on n'écrase **jamais** ce que Morel a pu remplir à la
main dans le Sheet. On ajoute ce qui manque ; un bloc ou une cellule déjà
remplis ne sont réécrits que si leur contenu est exactement celui que le
site y avait écrit la dernière fois (son empreinte, voir ``SheetSync``).

Ce module est volontairement coupé en deux :
  * la logique (quoi écrire, et si on a le droit) est pure et testée sans
    réseau, via ``SheetsClient`` (interface) et ``FakeSheetsClient`` (tests) ;
  * ``GoogleSheetsClient`` est la seule classe qui parle vraiment à l'API
    Google Sheets (REST, via ``google-auth`` + ``requests``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from . import models
from .stats import cle_nom

log = logging.getLogger("sheets_sync")

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DEPUIS = date(2026, 11, 1)  # la synchro ne touche jamais septembre / octobre
MOIS_FR_MAJ = [
    "", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
]


def nom_onglet(annee: int, mois: int) -> str:
    return f"{MOIS_FR_MAJ[mois]} {annee}"


def numero_culte_dans_mois(jour: date) -> int:
    """Rang du dimanche dans son mois (1 à 5) — c'est le numéro affiché en colonne A du Sheet."""
    from .services import dimanches_du_mois
    return dimanches_du_mois(jour.year, jour.month).index(jour) + 1


def numero_de_cellule(contenu) -> Optional[int]:
    """Numéro du culte en colonne A : « 4 », 4 ou « 4 · 22/11 » -> 4."""
    m = re.match(r"\s*(\d+)", str(contenu or ""))
    return int(m.group(1)) if m else None


def libelle_culte(annee: int, mois: int, numero: int) -> str:
    """Ce qu'on écrit en colonne A : « 4 · 22/11 » (rang du dimanche dans le mois et sa date)."""
    from .services import dimanches_du_mois
    dimanches = dimanches_du_mois(annee, mois)
    if numero <= len(dimanches):
        d = dimanches[numero - 1]
        return f"{numero} · {d.day:02d}/{d.month:02d}"
    return str(numero)


def onglet_vers_date(onglet: str) -> Optional[Tuple[int, int]]:
    """« Décembre 2026 » -> (2026, 12), sinon None (onglet qui n'est pas un mois, ex. Data_Calc)."""
    try:
        mois_nom, annee_s = onglet.rsplit(" ", 1)
        return int(annee_s), MOIS_FR_MAJ.index(mois_nom)
    except (ValueError, IndexError):
        return None


def dernier_onglet_mensuel(onglets: List[str]) -> Optional[str]:
    """Le plus récent des onglets mensuels connus (pour servir de modèle à dupliquer)."""
    dates = [(onglet_vers_date(o), o) for o in onglets]
    dates = [(d, o) for d, o in dates if d]
    return max(dates, key=lambda x: x[0])[1] if dates else None


def inserer_onglet_dans_formule(formule: str, nouvel_onglet: str, dernier_onglet: str) -> str:
    """Ajoute le nouvel onglet dans une formule QUERY({'A'!plage; 'B'!plage; ...}), juste après le dernier onglet connu.

    Ne modifie rien si le motif n'est pas trouvé (formule inattendue) : mieux vaut laisser Morel compléter à la main
    que de corrompre une formule qu'on ne comprend pas.
    """
    motif = re.compile(r"'" + re.escape(dernier_onglet) + r"'!([A-Z]+\d*:[A-Z]+\d*)")
    m = motif.search(formule)
    if not m:
        return formule
    plage = m.group(1)
    return formule[:m.end()] + f"; '{nouvel_onglet}'!{plage}" + formule[m.end():]


# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------
@dataclass
class BlocCulte:
    """Bloc de lignes d'un culte dans l'onglet du mois (colonnes A à D)."""
    ligne_debut: int  # 1-indexé, première ligne de données (après l'en-tête)
    ligne_fin: int
    valeurs: List[Tuple[str, str, str]]  # (département, prévu, servi) actuellement dans le Sheet, par ligne

    @property
    def vide(self) -> bool:
        return all(not (b or c or d) for b, c, d in self.valeurs)

    @property
    def nb_lignes(self) -> int:
        return self.ligne_fin - self.ligne_debut + 1


@dataclass
class Effectifs:
    ligne: int
    hommes: Optional[str] = None
    femmes: Optional[str] = None
    ados: Optional[str] = None
    enfants: Optional[str] = None
    nouveaux: Optional[str] = None
    nouveaux_noms: Optional[str] = None


@dataclass
class ResultatCulte:
    culte_id: int
    statut: str  # "ok" | "deja_rempli" | "erreur" | "desactive"
    message: str = ""


@dataclass
class ResultatPlage:
    ok: List[date] = field(default_factory=list)
    deja_rempli: List[date] = field(default_factory=list)
    erreurs: List[Tuple[date, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Interface client (implémentée par GoogleSheetsClient et FakeSheetsClient)
# ---------------------------------------------------------------------------
class SheetsClient(ABC):
    @abstractmethod
    def onglet_existe(self, onglet: str) -> bool: ...

    @abstractmethod
    def lire_bloc(self, onglet: str, numero_culte: int) -> Optional[BlocCulte]: ...

    @abstractmethod
    def ecrire_bloc(self, onglet: str, bloc: BlocCulte, lignes: List[Tuple[str, str, str]]) -> None:
        """Écrit les colonnes B:D du bloc. Si ``lignes`` dépasse le bloc, l'étend d'abord (lignes héritées)."""

    @abstractmethod
    def lire_effectifs(self, onglet: str, numero_culte: int) -> Effectifs: ...

    @abstractmethod
    def ecrire_effectifs(self, onglet: str, numero_culte: int, valeurs: Dict[str, int]) -> None:
        """N'écrit que les cellules vides parmi hommes/femmes/ados/enfants."""

    @abstractmethod
    def noms_connus(self) -> List[str]:
        """Listes!B — l'orthographe de référence (peut contenir des espaces finaux)."""

    @abstractmethod
    def ajouter_nom(self, nom: str) -> None: ...

    @abstractmethod
    def lister_onglets_mensuels(self) -> List[str]:
        """Les onglets du Sheet qui sont des mois (« Décembre 2026 »…), pour trouver lequel dupliquer."""

    @abstractmethod
    def creer_onglet_mensuel(self, nouveau: str, depuis: str) -> None:
        """Duplique l'onglet ``depuis``, le renomme ``nouveau``, vide les valeurs (A2:D, O3:R7 et T3:U7 ; la colonne S de formules est gardée ; fusions et
        validations conservées) et remet 1 à 5 en colonne A. Ne touche jamais un onglet déjà existant."""

    @abstractmethod
    def maj_calculs(self, nouveau: str, depuis: str) -> None:
        """Ajoute ``nouveau`` dans les formules QUERY de Data_Calc qui listaient déjà ``depuis``."""


# ---------------------------------------------------------------------------
# Rapprochement département / personne avec l'orthographe du Sheet
# ---------------------------------------------------------------------------
def _rapprocher(valeur: str, reference: List[str]) -> str:
    """Retourne l'orthographe de ``reference`` qui correspond à ``valeur`` (cle_nom), sinon ``valeur`` telle quelle."""
    if not valeur:
        return valeur
    cle = cle_nom(valeur)
    for ref in reference:
        if cle_nom(ref) == cle:
            return ref
    return valeur


def construire_lignes(culte: models.Culte, noms_connus: List[str]) -> List[Tuple[str, str, str]]:
    """Les lignes [département, prévu, servi] du culte, orthographe rapprochée de Listes!B pour les personnes."""
    lignes = []
    for s in culte.services:
        prevu = _rapprocher(s.prevu, noms_connus)
        servi = _rapprocher(s.servi, noms_connus)
        lignes.append((s.departement, prevu, servi))
    return lignes


def noms_manquants(lignes: List[Tuple[str, str, str]], noms_connus: List[str]) -> List[str]:
    """Personnes citées dans ``lignes`` qui n'ont pas déjà une entrée dans Listes!B (par cle_nom)."""
    connues = {cle_nom(n) for n in noms_connus}
    manquants, vus = [], set()
    for _, prevu, servi in lignes:
        for nom in (prevu, servi):
            if nom and cle_nom(nom) not in connues and cle_nom(nom) not in vus:
                manquants.append(nom)
                vus.add(cle_nom(nom))
    return manquants


# ---------------------------------------------------------------------------
# Empreinte : ce que le site a écrit la dernière fois dans le bloc B:D
# ---------------------------------------------------------------------------
def empreinte_bloc(valeurs: List[Tuple[str, str, str]]) -> str:
    brut = json.dumps(valeurs, ensure_ascii=False)
    return hashlib.sha256(brut.encode()).hexdigest()


def _bloc_attendu(lignes: List[Tuple[str, str, str]], nb_lignes: int) -> List[Tuple[str, str, str]]:
    """Les lignes qu'on écrirait, complétées de lignes vides jusqu'à la taille du bloc."""
    lignes = lignes[:nb_lignes]
    return lignes + [("", "", "")] * (nb_lignes - len(lignes))


# ---------------------------------------------------------------------------
# Onglet du mois : le créer s'il manque (section 3.4), sans jamais toucher un onglet existant
# ---------------------------------------------------------------------------
def assurer_onglet(client: SheetsClient, annee: int, mois: int) -> Tuple[str, Optional[str]]:
    onglet = nom_onglet(annee, mois)
    if client.onglet_existe(onglet):
        return onglet, None
    dernier = dernier_onglet_mensuel(client.lister_onglets_mensuels())
    if not dernier:
        raise RuntimeError("Aucun onglet mensuel existant à dupliquer dans le Sheet.")
    client.creer_onglet_mensuel(onglet, dernier)
    client.maj_calculs(onglet, dernier)
    message = (
        f"Nouvel onglet « {onglet} » créé à partir de « {dernier} ». "
        "Les graphiques du Tableau de bord annuel et la ligne « Mois / Services » de Data_Calc "
        "ne se mettent pas à jour tout seuls : à vérifier à la main."
    )
    return onglet, message


# ---------------------------------------------------------------------------
# Synchronisation d'un culte
# ---------------------------------------------------------------------------
def synchroniser_culte(db: Session, client: Optional[SheetsClient], culte: models.Culte) -> ResultatCulte:
    """Site -> Sheet pour un culte.

    Miroir : le Sheet est mis à jour avec la version du site, MAIS seulement là où le Sheet n'a pas été
    modifié à la main depuis la dernière synchro (sinon c'est une modification du Sheet pas encore
    récupérée : on n'écrase rien, on signale). Toute version du Sheet remplacée est d'abord gardée dans
    l'historique. Le site ne vide jamais un bloc du Sheet.
    """
    if client is None:
        return ResultatCulte(culte.id, "desactive")
    if culte.date < DEPUIS:
        return ResultatCulte(culte.id, "deja_rempli", "Avant le 1er novembre 2026 : non synchronisé.")

    try:
        onglet, message_creation = assurer_onglet(client, culte.date.year, culte.date.month)

        numero = numero_culte_dans_mois(culte.date)
        bloc = client.lire_bloc(onglet, numero)
        if bloc is None:
            return ResultatCulte(culte.id, "erreur", f"Bloc du culte {numero} introuvable dans « {onglet} ».")

        noms_connus = client.noms_connus()
        lignes = construire_lignes(culte, noms_connus)
        for nom in noms_manquants(lignes, noms_connus):
            client.ajouter_nom(nom)
            noms_connus.append(nom)
        lignes = construire_lignes(culte, noms_connus)  # ré-applique avec les noms venant d'être ajoutés

        sync = db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()
        eff_sync = _effectifs_sync(sync)
        actuel = [tuple(v) for v in bloc.valeurs]
        eff_sheet = client.lire_effectifs(onglet, numero)
        eff_sheet_int = {k: _valeur_sheet(k, getattr(eff_sheet, k, None)) for k in EFFECTIFS}

        # Le Sheet est « connu » s'il est vide ou s'il contient exactement ce qui a été synchronisé la dernière fois.
        bloc_connu = bloc.vide or bool(sync and sync.empreinte and empreinte_bloc(actuel) == sync.empreinte)
        attendu = _bloc_attendu(lignes, bloc.nb_lignes)
        ecrire = bloc_connu and bool(lignes) and attendu != actuel
        conflit_bloc = not bloc_connu and _normaliser([l for l in actuel if any(l)]) != _normaliser(lignes)

        a_ecrire_eff: Dict[str, int] = {}
        conflit_eff = []
        for k in EFFECTIFS:
            v_site = getattr(culte, k)
            v_sheet = eff_sheet_int[k]
            if v_site is None or v_site == v_sheet:
                continue
            if v_sheet is None or (k in eff_sync and eff_sync[k] == v_sheet):
                a_ecrire_eff[k] = v_site  # case vide, ou valeur venant d'une synchro précédente
            else:
                conflit_eff.append(k)  # corrigée à la main dans le Sheet : on n'y touche pas

        # Ce qui va être remplacé dans le Sheet part d'abord dans l'historique.
        if (ecrire and not bloc.vide) or any(eff_sheet_int[k] is not None for k in a_ecrire_eff):
            historiser(db, culte.eglise_id, culte.date, "sheet", "remplacée par une modification du site",
                       [l for l in actuel if any(l)], eff_sheet_int)
            db.commit()  # l'ancienne version est en sécurité AVANT d'écrire dans le Sheet
        if ecrire:
            client.ecrire_bloc(onglet, bloc, lignes)
            actuel = _bloc_attendu(lignes, bloc.nb_lignes)
        if a_ecrire_eff:
            client.ecrire_effectifs(onglet, numero, a_ecrire_eff)
            eff_sheet_int.update(a_ecrire_eff)

        if not sync:
            sync = models.SheetSync(culte_id=culte.id)
            db.add(sync)
        sync.ecrit_le = datetime.utcnow()
        # On ne mémorise comme « synchronisé » que ce qui est identique des deux côtés : une saisie manuelle
        # du Sheet pas encore récupérée reste ainsi détectable par l'import.
        if bloc_connu or not conflit_bloc:
            sync.empreinte = empreinte_bloc(actuel)
        sync.effectifs = json.dumps({k: eff_sheet_int[k] for k in EFFECTIFS
                                     if eff_sheet_int[k] is not None and eff_sheet_int[k] == getattr(culte, k)})
        if conflit_bloc or conflit_eff:
            sync.statut = "deja_rempli"
            sync.message = (f"Culte du {culte.date.day:02d}/{culte.date.month:02d} : modifié à la main dans le Sheet "
                            "et pas encore récupéré, le Sheet n'a pas été écrasé.")
        else:
            sync.statut, sync.message = "ok", ""
        if message_creation:
            sync.message = (message_creation + " " + (sync.message or "")).strip()
        db.commit()
        return ResultatCulte(culte.id, sync.statut, sync.message or "")
    except Exception as exc:  # noqa: BLE001 — on ne doit jamais faire échouer le pointage pour ça
        db.rollback()
        log.warning("Synchro Sheet échouée pour le culte %s : %s", culte.id, exc)
        return ResultatCulte(culte.id, "erreur", str(exc))


def _non_vide(valeur) -> bool:
    return valeur not in (None, "", 0)


def synchroniser_plage(db: Session, client: Optional[SheetsClient], eglise_id: int, depuis: date = DEPUIS) -> ResultatPlage:
    """Pour le bouton « Synchroniser le Google Sheet » : passe sur tous les cultes depuis ``depuis``."""
    resultat = ResultatPlage()
    if client is None:
        return resultat
    cultes = (
        db.query(models.Culte)
        .filter(models.Culte.eglise_id == eglise_id, models.Culte.date >= depuis)
        .order_by(models.Culte.date)
        .all()
    )
    for culte in cultes:
        r = synchroniser_culte(db, client, culte)
        if r.statut == "ok":
            resultat.ok.append(culte.date)
        elif r.statut == "deja_rempli":
            resultat.deja_rempli.append(culte.date)
        elif r.statut == "erreur":
            resultat.erreurs.append((culte.date, r.message))
    return resultat


# ---------------------------------------------------------------------------
# Import Sheet -> site : récupérer ce que Morel a saisi à la main dans le Sheet
# ---------------------------------------------------------------------------
# Un bloc rempli dont le contenu n'est PAS celui que le site y a écrit (empreinte SheetSync absente ou
# différente) est une saisie manuelle : le Sheet fait foi. Rien n'est modifié dans le site sans
# confirmation : ``preparer_import`` ne fait que lire et calculer un aperçu, ``appliquer_import`` écrit.

EFFECTIFS = ("hommes", "femmes", "ados", "enfants", "nouveaux", "nouveaux_noms")
# Colonnes du tableau d'effectifs dans chaque onglet mensuel (ligne = 2 + n° du culte).
# S = « Total » du culte (formule du Sheet) : jamais écrit par le site.
COLONNES_EFFECTIFS = {"hommes": "O", "femmes": "P", "ados": "Q", "enfants": "R", "nouveaux": "T", "nouveaux_noms": "U"}


@dataclass
class ImportCulte:
    jour: date
    onglet: str
    numero: int
    action: str  # "creer" | "remplacer" | "identique"
    lignes: List[Tuple[str, str, str]]  # ce qui sera enregistré dans le site (après)
    effectifs: Dict[str, Optional[int]]  # après
    avant_lignes: List[Tuple[str, str, str]] = field(default_factory=list)
    avant_effectifs: Dict[str, Optional[int]] = field(default_factory=dict)
    empreinte: str = ""  # empreinte du bloc tel qu'il est dans le Sheet
    effectifs_sheet: Dict[str, Optional[int]] = field(default_factory=dict)
    conflit: bool = False  # le site ET le Sheet ont changé depuis la dernière synchro

    @property
    def cle(self) -> str:
        return self.jour.isoformat()


def _valeur_sheet(cle: str, valeur):
    """Valeur d'une case d'effectif : texte nettoyé pour les prénoms des nouveaux venus, entier sinon."""
    if cle == "nouveaux_noms":
        texte = re.sub(r"\s+", " ", str(valeur or "")).strip()
        return texte or None
    return _entier_sheet(valeur)


def _entier_sheet(valeur) -> Optional[int]:
    """« 14 », 14, « 14,0 » -> 14 ; vide ou illisible -> None."""
    if valeur in (None, ""):
        return None
    try:
        return int(float(str(valeur).replace(",", ".").replace("\u202f", "").replace(" ", "")))
    except ValueError:
        return None


def _effectifs_sync(sync) -> Dict[str, int]:
    try:
        return {k: (v if k == "nouveaux_noms" else int(v)) for k, v in json.loads(sync.effectifs).items()} \
            if sync and sync.effectifs else {}
    except (TypeError, ValueError):
        return {}


def _normaliser(lignes) -> List[Tuple[str, str, str]]:
    return [(cle_nom(d), cle_nom(p), cle_nom(s)) for d, p, s in lignes]


def _mois_entre(debut: date, fin: date) -> List[Tuple[int, int]]:
    mois, (a, m) = [], (debut.year, debut.month)
    while (a, m) <= (fin.year, fin.month):
        mois.append((a, m))
        a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return mois


def historiser(db: Session, eglise_id: int, jour: date, source: str, raison: str,
               services: List[Tuple[str, str, str]], effectifs: Dict[str, Optional[int]]) -> None:
    """Met de côté une version d'un culte avant qu'elle soit remplacée. Ne garde rien si elle est vide."""
    if not services and all(v is None for v in effectifs.values()):
        return
    db.add(models.CulteHistorique(
        eglise_id=eglise_id, date=jour, source=source, raison=raison, cree_le=datetime.utcnow(),
        contenu=json.dumps({"services": [list(l) for l in services],
                            "effectifs": {k: effectifs.get(k) for k in EFFECTIFS}}, ensure_ascii=False),
    ))


def _site_modifie_depuis_sync(culte, sync) -> bool:
    """Le site a-t-il été modifié depuis la dernière synchro (ou jamais synchronisé alors qu'il a du contenu) ?"""
    if culte is None:
        return False
    if sync is None or sync.ecrit_le is None:
        return bool(culte.services) or any(getattr(culte, k) is not None for k in EFFECTIFS)
    return bool(culte.updated_at and (culte.updated_at - sync.ecrit_le).total_seconds() > 1)


def preparer_import(db: Session, client: Optional[SheetsClient], eglise_id: int,
                    depuis: date = DEPUIS, jusqu_a: Optional[date] = None,
                    jours: Optional[List[date]] = None) -> List[ImportCulte]:
    """Ce qui a changé dans le Sheet depuis la dernière synchro, comparé au site. N'écrit RIEN."""
    from .services import dimanches_du_mois
    from . import stats

    if client is None:
        return []
    jusqu_a = jusqu_a or date.today()
    if jours:
        mois_a_lire = sorted({(j.year, j.month) for j in jours})
    else:
        mois_a_lire = _mois_entre(depuis, jusqu_a)
    noms_site = [x.nom for x in stats.stars(db, eglise_id)]
    deps_site = [d.nom for d in stats.departements(db, eglise_id)]
    resultat: List[ImportCulte] = []

    for annee, mois in mois_a_lire:
        onglet = nom_onglet(annee, mois)
        if not client.onglet_existe(onglet):
            continue  # jamais de création d'onglet à l'import
        for numero, jour in enumerate(dimanches_du_mois(annee, mois), start=1):
            if jour < depuis or (jours is None and jour > jusqu_a) or (jours is not None and jour not in jours):
                continue
            bloc = client.lire_bloc(onglet, numero)
            if bloc is None:
                continue
            try:
                eff_sheet = client.lire_effectifs(onglet, numero)
            except (KeyError, LookupError):
                eff_sheet = None
            effectifs_sheet = {k: _valeur_sheet(k, getattr(eff_sheet, k, None)) for k in EFFECTIFS}
            if bloc.vide and all(v is None for v in effectifs_sheet.values()):
                continue  # rien de saisi dans le Sheet pour ce culte

            culte = stats.culte_du_jour(db, eglise_id, jour)
            sync = (db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()
                    if culte is not None else None)
            empreinte = empreinte_bloc([tuple(v) for v in bloc.valeurs])
            eff_sync = _effectifs_sync(sync)

            avant_lignes = [(x.departement, x.prevu, x.servi) for x in culte.services] if culte else []
            avant_eff = {k: getattr(culte, k) for k in EFFECTIFS} if culte else {k: None for k in EFFECTIFS}

            lignes_sheet = [
                (_rapprocher(stats.nettoyer(d, 60), deps_site),
                 _rapprocher(stats.nettoyer(p), noms_site),
                 _rapprocher(stats.nettoyer(sv), noms_site))
                for d, p, sv in bloc.valeurs if (d or p or sv)
            ]
            lignes_sheet = [l for l in lignes_sheet if l[0] and (l[1] or l[2])]

            # Lignes : celles du Sheet si son bloc a changé depuis la dernière synchro. Un bloc vidé dans le
            # Sheet n'efface jamais les services du site.
            bloc_change = bool(lignes_sheet) and not (sync and sync.empreinte == empreinte)
            lignes = lignes_sheet if bloc_change else list(avant_lignes)
            # Effectifs : une valeur du Sheet ne compte que si elle a été changée dans le Sheet
            # (différente de ce qui a été synchronisé). Une case vide n'efface jamais une valeur du site.
            apres_eff = dict(avant_eff)
            for k in EFFECTIFS:
                v = effectifs_sheet[k]
                if v is not None and v != avant_eff[k] and eff_sync.get(k) != v:
                    apres_eff[k] = v

            if culte is None:
                action = "creer"
            elif _normaliser(lignes) == _normaliser(avant_lignes) and apres_eff == avant_eff:
                action = "identique"
            else:
                action = "remplacer"
            conflit = action == "remplacer" and _site_modifie_depuis_sync(culte, sync)
            resultat.append(ImportCulte(jour, onglet, numero, action, lignes, apres_eff, avant_lignes, avant_eff,
                                        empreinte, effectifs_sheet, conflit))
    return resultat


def appliquer_import(db: Session, eglise_id: int, items: List[ImportCulte], cles: Optional[List[str]] = None,
                     raison: str = "remplacée par une modification du Sheet") -> int:
    """Écrit dans le site les cultes donnés (seulement ceux de ``cles`` si fourni). Retourne le nombre modifié.

    La version du site remplacée est d'abord gardée dans l'historique.
    """
    from . import stats

    n = 0
    nouveaux_noms: List[str] = []
    for it in items:
        if cles is not None and it.cle not in cles:
            continue
        if it.action != "identique":
            if it.action == "remplacer":
                historiser(db, eglise_id, it.jour, "site", raison, it.avant_lignes, it.avant_effectifs)
                db.commit()  # l'ancienne version est en sécurité AVANT de la remplacer
            culte = stats.enregistrer_culte(
                db, eglise_id, it.jour, it.effectifs["hommes"], it.effectifs["femmes"], it.lignes,
                it.effectifs["ados"], it.effectifs["enfants"], it.effectifs["nouveaux"], it.effectifs["nouveaux_noms"],
            )
            n += 1
        else:
            culte = stats.culte_du_jour(db, eglise_id, it.jour)
        nouveaux_noms += [nom for _, p, s in it.lignes for nom in (p, s) if nom]
        # Le bloc et les effectifs du Sheet sont désormais « connus » : ils ne seront plus vus comme manuels.
        sync = db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()
        if not sync:
            sync = models.SheetSync(culte_id=culte.id)
            db.add(sync)
        sync.empreinte = it.empreinte
        sync.effectifs = json.dumps({k: v for k, v in it.effectifs_sheet.items()
                                     if v is not None and v == getattr(culte, k)})
        sync.ecrit_le = datetime.utcnow()
        sync.statut = "importe"
        sync.message = f"Récupéré depuis le Sheet ({it.onglet}, culte {it.numero})."
        db.commit()
    if nouveaux_noms:
        stats.ajouter_stars(db, eglise_id, nouveaux_noms)
    return n


@dataclass
class ResultatImport:
    importes: List[date] = field(default_factory=list)
    conflits: List[date] = field(default_factory=list)


def importer_auto(db: Session, client: Optional[SheetsClient], eglise_id: int,
                  jours: Optional[List[date]] = None, depuis: date = DEPUIS) -> ResultatImport:
    """Récupération automatique (webhook du Sheet, filet de sécurité) : applique sans confirmation tout ce qui a
    changé dans le Sheet, SAUF les conflits (site et Sheet modifiés tous les deux), laissés à la page d'aperçu."""
    r = ResultatImport()
    if client is None:
        return r
    items = preparer_import(db, client, eglise_id, depuis=depuis, jours=jours)
    sans_conflit = [i for i in items if not i.conflit]
    appliquer_import(db, eglise_id, sans_conflit)
    r.importes = [i.jour for i in sans_conflit if i.action != "identique"]
    for i in items:
        if i.conflit:
            r.conflits.append(i.jour)
            from . import stats
            culte = stats.culte_du_jour(db, eglise_id, i.jour)
            sync = db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()
            if not sync:
                sync = models.SheetSync(culte_id=culte.id)
                db.add(sync)
            sync.statut = "conflit"
            sync.message = (f"Culte du {i.jour.day:02d}/{i.jour.month:02d} : modifié à la fois sur le site et dans "
                            "le Sheet. À trancher dans « Récupérer depuis le Google Sheet ».")
            db.commit()
    etat = db.query(models.SyncEtat).filter(models.SyncEtat.eglise_id == eglise_id).first()
    if not etat:
        etat = models.SyncEtat(eglise_id=eglise_id)
        db.add(etat)
    etat.dernier_import = datetime.utcnow()
    db.commit()
    return r


def garder_version_site(db: Session, eglise_id: int, items: List[ImportCulte]) -> None:
    """Conflit tranché en faveur du site : la version actuelle du Sheet est marquée comme « connue », pour que
    l'export suivant la remplace par celle du site (en la gardant d'abord dans l'historique)."""
    from . import stats

    for it in items:
        culte = stats.culte_du_jour(db, eglise_id, it.jour)
        if culte is None:
            continue
        sync = db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()
        if not sync:
            sync = models.SheetSync(culte_id=culte.id)
            db.add(sync)
        sync.empreinte = it.empreinte
        sync.effectifs = json.dumps({k: v for k, v in it.effectifs_sheet.items() if v is not None})
        sync.statut, sync.message = "ok", ""
    db.commit()


def restaurer_version(db: Session, eglise_id: int, historique_id: int) -> Optional[models.Culte]:
    """Remet dans le site une version de l'historique. La version actuelle part elle-même dans l'historique."""
    from . import stats

    h = db.query(models.CulteHistorique).filter(models.CulteHistorique.id == historique_id,
                                                 models.CulteHistorique.eglise_id == eglise_id).first()
    if not h:
        return None
    contenu = json.loads(h.contenu)
    actuel = stats.culte_du_jour(db, eglise_id, h.date)
    if actuel:
        historiser(db, eglise_id, h.date, "site", "remplacée par une restauration",
                   [(x.departement, x.prevu, x.servi) for x in actuel.services],
                   {k: getattr(actuel, k) for k in EFFECTIFS})
        db.commit()
    eff = contenu.get("effectifs", {})
    return stats.enregistrer_culte(db, eglise_id, h.date, eff.get("hommes"), eff.get("femmes"),
                                   [tuple(l) for l in contenu.get("services", [])], eff.get("ados"), eff.get("enfants"),
                                   eff.get("nouveaux"), eff.get("nouveaux_noms"))


def jour_depuis_cellule(client: SheetsClient, onglet: str, ligne: int) -> Optional[date]:
    """Le culte concerné par une cellule modifiée dans le Sheet (bloc A:D, ou ligne d'effectifs 3 à 7)."""
    from .services import dimanches_du_mois

    am = onglet_vers_date(onglet)
    if not am:
        return None
    dimanches = dimanches_du_mois(*am)
    for numero in range(1, len(dimanches) + 1):
        bloc = client.lire_bloc(onglet, numero)
        if bloc and bloc.ligne_debut <= ligne <= bloc.ligne_fin:
            return dimanches[numero - 1]
    if 3 <= ligne <= 2 + len(dimanches):
        return dimanches[ligne - 3]
    return None


# ---------------------------------------------------------------------------
# Client réel (REST, google-auth + requests)
# ---------------------------------------------------------------------------
def client_depuis_env() -> Optional["GoogleSheetsClient"]:
    """Construit le client depuis les variables d'environnement, ou None si la synchro est désactivée."""
    cle_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not cle_json or not sheet_id:
        return None
    try:
        return GoogleSheetsClient(cle_json, sheet_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Impossible d'initialiser le client Google Sheets : %s", exc)
        return None


class GoogleSheetsClient(SheetsClient):
    """Implémentation réelle : appels REST à Google Sheets, authentifiés par compte de service."""

    def __init__(self, cle_service_json: str, sheet_id: str, timeout: float = 8.0):
        import requests
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account

        self._requests = requests
        self._cache: dict = {}
        self._timeout = timeout
        self._sheet_id = sheet_id
        info = json.loads(cle_service_json)
        self._creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self._creds.refresh(GoogleRequest())

    def _headers(self) -> dict:
        if not self._creds.valid:
            from google.auth.transport.requests import Request as GoogleRequest
            self._creds.refresh(GoogleRequest())
        return {"Authorization": f"Bearer {self._creds.token}"}

    def _get(self, url: str, **params):
        # Petit cache de lecture, vidé à chaque écriture : évite de relire 10 fois le même onglet.
        cle = (url, tuple(sorted(params.items())))
        if cle not in self._cache:
            r = self._requests.get(url, headers=self._headers(), params=params, timeout=self._timeout)
            r.raise_for_status()
            self._cache[cle] = r.json()
        return self._cache[cle]

    def _post(self, url: str, body: dict):
        self._cache.clear()
        r = self._requests.post(url, headers=self._headers(), json=body, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def _metadonnees(self) -> dict:
        return self._get(f"{SHEETS_API}/{self._sheet_id}", fields="sheets(properties,merges)")

    def _feuille(self, onglet: str) -> Optional[dict]:
        for f in self._metadonnees().get("sheets", []):
            if f["properties"]["title"] == onglet:
                return f
        return None

    def onglet_existe(self, onglet: str) -> bool:
        return self._feuille(onglet) is not None

    def _fusions_a(self, feuille: dict) -> List[Tuple[int, int]]:
        """Cases fusionnées de la colonne A (numéro des dimanches), en lignes 1-indexées (début, fin incluse)."""
        sheet_id = feuille["properties"]["sheetId"]
        return sorted((m["startRowIndex"] + 1, m["endRowIndex"]) for m in feuille.get("merges", [])
                      if m["sheetId"] == sheet_id and m["startColumnIndex"] == 0 and m["endColumnIndex"] == 1
                      and m["startRowIndex"] >= 1)

    def lire_bloc(self, onglet: str, numero_culte: int) -> Optional[BlocCulte]:
        feuille = self._feuille(onglet)
        if not feuille:
            return None
        valeurs_a = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'{onglet}'!A1:D500").get("values", [])
        fusions = self._fusions_a(feuille)
        for i, (debut, fin_fusion) in enumerate(fusions):
            contenu = valeurs_a[debut - 1][0] if debut - 1 < len(valeurs_a) and valeurs_a[debut - 1] else ""
            if numero_de_cellule(contenu) != numero_culte:
                continue
            # Le bloc va jusqu'au dimanche suivant : une ligne ajoutée juste sous la case fusionnée (sans l'agrandir)
            # appartient quand même à ce dimanche.
            fin = fusions[i + 1][0] - 1 if i + 1 < len(fusions) else fin_fusion
            lignes = []
            for r in range(debut - 1, fin):
                row = valeurs_a[r] if r < len(valeurs_a) else []
                lignes.append((row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else "", row[3] if len(row) > 3 else ""))
            return BlocCulte(ligne_debut=debut, ligne_fin=fin, valeurs=lignes)
        return None

    def ecrire_bloc(self, onglet: str, bloc: BlocCulte, lignes: List[Tuple[str, str, str]]) -> None:
        feuille = self._feuille(onglet)
        sheet_id = feuille["properties"]["sheetId"]
        requetes = []
        if len(lignes) > bloc.nb_lignes:
            manquantes = len(lignes) - bloc.nb_lignes
            requetes.append({"insertDimension": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": bloc.ligne_fin - 1, "endIndex": bloc.ligne_fin - 1 + manquantes},
                "inheritFromBefore": True,  # insérées AVANT la dernière ligne : à l'intérieur du bloc
            }})
            bloc.ligne_fin += manquantes
        # La case du numéro couvre toujours tout le bloc (aucune ligne « hors dimanche »).
        zone_a = {"sheetId": sheet_id, "startRowIndex": bloc.ligne_debut - 1, "endRowIndex": bloc.ligne_fin,
                  "startColumnIndex": 0, "endColumnIndex": 1}
        requetes += [{"unmergeCells": {"range": zone_a}}, {"mergeCells": {"range": zone_a, "mergeType": "MERGE_ALL"}}]
        self._post(f"{SHEETS_API}/{self._sheet_id}:batchUpdate", {"requests": requetes})
        valeurs = [[dep, prevu, servi] for dep, prevu, servi in lignes]
        self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate", {
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"'{onglet}'!B{bloc.ligne_debut}:D{bloc.ligne_debut + len(valeurs) - 1}", "values": valeurs}],
        })

    def lire_effectifs(self, onglet: str, numero_culte: int) -> Effectifs:
        ligne = 2 + numero_culte
        valeurs = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'{onglet}'!O{ligne}:U{ligne}").get("values", [[]])
        row = valeurs[0] if valeurs else []
        get = lambda i: row[i] if len(row) > i else None  # noqa: E731
        return Effectifs(ligne=ligne, hommes=get(0), femmes=get(1), ados=get(2), enfants=get(3),
                         nouveaux=get(5), nouveaux_noms=get(6))  # get(4) = colonne S, total calculé par le Sheet

    def ecrire_effectifs(self, onglet: str, numero_culte: int, valeurs: Dict[str, int]) -> None:
        ligne = 2 + numero_culte
        data = [{"range": f"'{onglet}'!{COLONNES_EFFECTIFS[cle]}{ligne}", "values": [[v]]} for cle, v in valeurs.items()]
        if data:
            self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate",
                       {"valueInputOption": "USER_ENTERED", "data": data})

    def noms_connus(self) -> List[str]:
        valeurs = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'Listes'!B2:B1000").get("values", [])
        return [v[0] for v in valeurs if v]

    def ajouter_nom(self, nom: str) -> None:
        # values.append évite de devoir connaître la dernière ligne occupée de la colonne.
        self._cache.clear()
        r = self._requests.post(
            f"{SHEETS_API}/{self._sheet_id}/values/'Listes'!B1:B1000:append",
            headers=self._headers(), timeout=self._timeout,
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": [[nom]]},
        )
        r.raise_for_status()

    def lister_onglets_mensuels(self) -> List[str]:
        return [f["properties"]["title"] for f in self._metadonnees().get("sheets", [])
                if onglet_vers_date(f["properties"]["title"])]

    def creer_onglet_mensuel(self, nouveau: str, depuis: str) -> None:
        feuille = self._feuille(depuis)
        if feuille is None:
            raise RuntimeError(f"Onglet modèle « {depuis} » introuvable.")
        reponse = self._post(f"{SHEETS_API}/{self._sheet_id}:batchUpdate", {"requests": [{
            "duplicateSheet": {
                "sourceSheetId": feuille["properties"]["sheetId"],
                "insertSheetIndex": feuille["properties"]["index"] + 1,
                "newSheetName": nouveau,
            }
        }]})
        nouveau_sheet_id = reponse["replies"][0]["duplicateSheet"]["properties"]["sheetId"]

        # Fusions et validations sont conservées par duplicateSheet ; seules les valeurs sont vidées.
        self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchClear",
                   {"ranges": [f"'{nouveau}'!A2:D500", f"'{nouveau}'!O3:R7", f"'{nouveau}'!T3:U7"]})

        nouvelle_feuille = self._feuille(nouveau)
        lignes_a = sorted(
            m["startRowIndex"] + 1 for m in nouvelle_feuille.get("merges", [])
            if m["sheetId"] == nouveau_sheet_id and m["startColumnIndex"] == 0 and m["endColumnIndex"] == 1
        )
        if lignes_a:
            annee, mois = onglet_vers_date(nouveau)
            self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate", {
                "valueInputOption": "RAW",
                "data": [{"range": f"'{nouveau}'!A{ligne}", "values": [[libelle_culte(annee, mois, i + 1)]]}
                         for i, ligne in enumerate(lignes_a)],
            })

    def maj_calculs(self, nouveau: str, depuis: str) -> None:
        """Best-effort : insère ``nouveau`` dans les formules QUERY de Data_Calc (colonnes A, D, G) qui citaient
        déjà ``depuis``. La ligne « Mois / Services » (J/K) n'a pas de format connu : signalée à Morel (voir
        le message retourné par ``assurer_onglet``), pas touchée ici pour ne pas corrompre une formule mal comprise.
        """
        for colonne in ("A", "D", "G"):
            cellule = f"'Data_Calc'!{colonne}1"
            valeurs = self._get(f"{SHEETS_API}/{self._sheet_id}/values/{cellule}", valueRenderOption="FORMULA").get("values", [])
            formule = valeurs[0][0] if valeurs and valeurs[0] else ""
            if not formule:
                continue
            nouvelle = inserer_onglet_dans_formule(formule, nouveau, depuis)
            if nouvelle != formule:
                self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate", {
                    "valueInputOption": "USER_ENTERED", "data": [{"range": cellule, "values": [[nouvelle]]}],
                })
