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
        """Duplique l'onglet ``depuis``, le renomme ``nouveau``, vide les valeurs (A2:D et O3:R7, fusions et
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

        effectifs = {"hommes": culte.hommes, "femmes": culte.femmes, "ados": culte.ados, "enfants": culte.enfants}
        effectifs = {k: v for k, v in effectifs.items() if v is not None}

        precedente = db.query(models.SheetSync).filter(models.SheetSync.culte_id == culte.id).first()

        # Le bloc n'est réécrit que s'il est vide, ou si son contenu actuel est exactement celui que le site
        # y avait écrit la dernière fois (empreinte identique) : personne ne l'a touché à la main depuis.
        bloc_ecrit = bloc.vide or (
            bool(precedente and precedente.empreinte) and empreinte_bloc(bloc.valeurs) == precedente.empreinte
        )
        if bloc_ecrit:
            client.ecrire_bloc(onglet, bloc, lignes)

        eff_sheet = client.lire_effectifs(onglet, numero)
        a_ecrire_eff = {cle: v for cle, v in effectifs.items() if not _non_vide(getattr(eff_sheet, cle))}
        if a_ecrire_eff:
            client.ecrire_effectifs(onglet, numero, a_ecrire_eff)

        if not precedente:
            precedente = models.SheetSync(culte_id=culte.id)
            db.add(precedente)
        precedente.ecrit_le = datetime.utcnow()
        if bloc_ecrit:
            precedente.empreinte = empreinte_bloc(_bloc_attendu(lignes, bloc.nb_lignes))
            precedente.statut, precedente.message = "ok", (message_creation or "")
        else:
            precedente.statut = "deja_rempli"
            precedente.message = f"Culte du {culte.date.day:02d}/{culte.date.month:02d} : déjà rempli dans le Sheet, non modifié."
            if message_creation:
                precedente.message = message_creation + " " + precedente.message
        db.commit()
        return ResultatCulte(culte.id, precedente.statut, precedente.message)
    except Exception as exc:  # noqa: BLE001 — on ne doit jamais faire échouer le pointage pour ça
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
        r = self._requests.get(url, headers=self._headers(), params=params, timeout=self._timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, url: str, body: dict):
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

    def lire_bloc(self, onglet: str, numero_culte: int) -> Optional[BlocCulte]:
        feuille = self._feuille(onglet)
        if not feuille:
            return None
        sheet_id = feuille["properties"]["sheetId"]
        valeurs_a = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'{onglet}'!A1:D500").get("values", [])
        for m in feuille.get("merges", []):
            if m["sheetId"] != sheet_id or m["startColumnIndex"] != 0 or m["endColumnIndex"] != 1:
                continue
            ligne0 = m["startRowIndex"]
            contenu = valeurs_a[ligne0][0] if ligne0 < len(valeurs_a) and valeurs_a[ligne0] else ""
            if str(contenu).strip() == str(numero_culte):
                debut, fin = m["startRowIndex"] + 1, m["endRowIndex"]  # 1-indexé, fin inclusif
                lignes = []
                for i in range(debut - 1, fin):
                    row = valeurs_a[i] if i < len(valeurs_a) else []
                    lignes.append((
                        row[1] if len(row) > 1 else "", row[2] if len(row) > 2 else "", row[3] if len(row) > 3 else ""
                    ))
                return BlocCulte(ligne_debut=debut, ligne_fin=fin, valeurs=lignes)
        return None

    def ecrire_bloc(self, onglet: str, bloc: BlocCulte, lignes: List[Tuple[str, str, str]]) -> None:
        if len(lignes) > bloc.nb_lignes:
            feuille = self._feuille(onglet)
            sheet_id = feuille["properties"]["sheetId"]
            manquantes = len(lignes) - bloc.nb_lignes
            self._post(f"{SHEETS_API}/{self._sheet_id}:batchUpdate", {"requests": [{
                "insertDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS",
                              "startIndex": bloc.ligne_fin, "endIndex": bloc.ligne_fin + manquantes},
                    "inheritFromBefore": True,
                }
            }]})
            bloc.ligne_fin += manquantes
        valeurs = [[dep, prevu, servi] for dep, prevu, servi in lignes]
        self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate", {
            "valueInputOption": "USER_ENTERED",
            "data": [{"range": f"'{onglet}'!B{bloc.ligne_debut}:D{bloc.ligne_debut + len(valeurs) - 1}", "values": valeurs}],
        })

    def lire_effectifs(self, onglet: str, numero_culte: int) -> Effectifs:
        ligne = 2 + numero_culte
        valeurs = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'{onglet}'!O{ligne}:R{ligne}").get("values", [[]])
        row = valeurs[0] if valeurs else []
        get = lambda i: row[i] if len(row) > i else None  # noqa: E731
        return Effectifs(ligne=ligne, hommes=get(0), femmes=get(1), ados=get(2), enfants=get(3))

    def ecrire_effectifs(self, onglet: str, numero_culte: int, valeurs: Dict[str, int]) -> None:
        ligne = 2 + numero_culte
        colonnes = {"hommes": "O", "femmes": "P", "ados": "Q", "enfants": "R"}
        data = [{"range": f"'{onglet}'!{colonnes[cle]}{ligne}", "values": [[v]]} for cle, v in valeurs.items()]
        if data:
            self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate",
                       {"valueInputOption": "USER_ENTERED", "data": data})

    def noms_connus(self) -> List[str]:
        valeurs = self._get(f"{SHEETS_API}/{self._sheet_id}/values/'Listes'!B2:B1000").get("values", [])
        return [v[0] for v in valeurs if v]

    def ajouter_nom(self, nom: str) -> None:
        # values.append évite de devoir connaître la dernière ligne occupée de la colonne.
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
                   {"ranges": [f"'{nouveau}'!A2:D500", f"'{nouveau}'!O3:R7"]})

        nouvelle_feuille = self._feuille(nouveau)
        lignes_a = sorted(
            m["startRowIndex"] + 1 for m in nouvelle_feuille.get("merges", [])
            if m["sheetId"] == nouveau_sheet_id and m["startColumnIndex"] == 0 and m["endColumnIndex"] == 1
        )
        if lignes_a:
            self._post(f"{SHEETS_API}/{self._sheet_id}/values:batchUpdate", {
                "valueInputOption": "USER_ENTERED",
                "data": [{"range": f"'{nouveau}'!A{ligne}", "values": [[i + 1]]} for i, ligne in enumerate(lignes_a)],
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
