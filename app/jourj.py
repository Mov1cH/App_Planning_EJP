"""Jour J : minuteur du déroulé en direct.

Logique pure (aucun accès DB, aucune dépendance HTTP), calquée sur
``lib/runsheet/timing.ts`` et ``lib/runsheet/actions.ts`` du projet de
référence « Gestion d'église ». ``main.py`` se charge de lire/écrire
``models.DerouleLive`` et d'appeler ces fonctions.

Deux ajouts voulus par Morel par rapport au modèle de référence :
  * en dépassement, le temps restant est négatif (``-1:30``) plutôt que ``+1:30`` ;
  * une action ``stop`` / ``reprendre`` (pause) : le temps en pause ne compte pas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

PARIS = ZoneInfo("Europe/Paris")


@dataclass
class Etape:
    titre: str
    personne: str
    duree: int  # minutes
    debut: str = ""  # "HH:MM" si heure fixe ; vide = enchaîne sur la précédente


@dataclass
class EtapeReelle:
    debut: Optional[datetime] = None
    fin: Optional[datetime] = None
    pause_ms: int = 0


@dataclass
class EtatLive:
    etapes: List[Etape]
    index_courant: Optional[int] = None
    demarre_a: Optional[datetime] = None
    termine_a: Optional[datetime] = None
    en_pause_depuis: Optional[datetime] = None
    reel: List[EtapeReelle] = field(default_factory=list)

    def __post_init__(self):
        while len(self.reel) < len(self.etapes):
            self.reel.append(EtapeReelle())

    @property
    def en_pause(self) -> bool:
        return self.en_pause_depuis is not None

    @property
    def termine(self) -> bool:
        return self.demarre_a is not None and self.index_courant is None


# ---------------------------------------------------------------------------
# Heures prévues
# ---------------------------------------------------------------------------
def heures_prevues(jour: date, etapes: List[Etape]) -> List[datetime]:
    """Début prévu (Europe/Paris) de chaque étape.

    Les durées s'enchaînent ; une étape à heure fixe (``debut`` rempli) relance
    le curseur sur cette heure ce jour-là, les étapes suivantes repartent de là.
    """
    defaut = next((e.debut for e in etapes if e.debut), "00:00")
    curseur = datetime.combine(jour, datetime.strptime(defaut, "%H:%M").time(), tzinfo=PARIS)
    debuts: List[datetime] = []
    for e in etapes:
        if e.debut:
            curseur = datetime.combine(jour, datetime.strptime(e.debut, "%H:%M").time(), tzinfo=PARIS)
        debuts.append(curseur)
        curseur += timedelta(minutes=e.duree)
    return debuts


# ---------------------------------------------------------------------------
# Actions : démarrer / suivant / stop / reprendre / recommencer
# ---------------------------------------------------------------------------
def avancer(etat: EtatLive, maintenant: datetime) -> EtatLive:
    """Ferme l'étape en cours (s'il y en a une) et démarre la suivante.

    Démarre le direct si rien n'a encore commencé ; une fois la dernière étape
    close, le direct est terminé (``index_courant`` devient ``None``).
    """
    if etat.index_courant is not None:
        etat.reel[etat.index_courant].fin = maintenant
        suivant = etat.index_courant + 1
    else:
        suivant = 0
    etat.demarre_a = etat.demarre_a or maintenant
    etat.en_pause_depuis = None
    if suivant < len(etat.etapes):
        etat.index_courant = suivant
        etat.reel[suivant] = EtapeReelle(debut=maintenant)
    else:
        etat.index_courant = None
        etat.termine_a = maintenant
    return etat


def stop(etat: EtatLive, maintenant: datetime) -> EtatLive:
    """Met le chrono en pause ; sans effet si déjà en pause ou si rien n'est en cours."""
    if etat.index_courant is not None and not etat.en_pause:
        etat.en_pause_depuis = maintenant
    return etat


def reprendre(etat: EtatLive, maintenant: datetime) -> EtatLive:
    """Sort de pause : le temps passé en pause est retiré du temps écoulé de l'étape."""
    if etat.en_pause_depuis is not None and etat.index_courant is not None:
        pause = maintenant - etat.en_pause_depuis
        etat.reel[etat.index_courant].pause_ms += int(pause.total_seconds() * 1000)
        etat.en_pause_depuis = None
    return etat


def recommencer(etat: EtatLive) -> EtatLive:
    """Remet le jour J à zéro : plus d'étape en cours, heures réelles effacées."""
    etat.index_courant = None
    etat.demarre_a = None
    etat.termine_a = None
    etat.en_pause_depuis = None
    etat.reel = [EtapeReelle() for _ in etat.etapes]
    return etat


# ---------------------------------------------------------------------------
# Calculs d'affichage
# ---------------------------------------------------------------------------
def ecoule_ms(reelle: EtapeReelle, maintenant: datetime, en_pause_depuis: Optional[datetime] = None) -> int:
    """Temps effectivement passé sur l'étape, pause déduite. Le temps de pause en cours ne compte pas."""
    if reelle.debut is None:
        return 0
    borne = reelle.fin or en_pause_depuis or maintenant
    return int((borne - reelle.debut).total_seconds() * 1000) - reelle.pause_ms


def restant_ms(etape: Etape, reelle: EtapeReelle, maintenant: datetime, en_pause_depuis: Optional[datetime] = None) -> int:
    """Temps restant sur l'étape en cours (négatif = dépassement)."""
    return etape.duree * 60_000 - ecoule_ms(reelle, maintenant, en_pause_depuis)


def retard_ms(etat: EtatLive, jour: date, maintenant: datetime) -> Optional[int]:
    """Retard cumulé = début réel de l'étape en cours − son début prévu (+ dépassement en cours)."""
    if etat.index_courant is None:
        return None
    reelle = etat.reel[etat.index_courant]
    if reelle.debut is None:
        return None
    prevu = heures_prevues(jour, etat.etapes)[etat.index_courant]
    retard_depart = int((reelle.debut - prevu).total_seconds() * 1000)
    depassement_en_cours = max(0, -restant_ms(etat.etapes[etat.index_courant], reelle, maintenant, etat.en_pause_depuis))
    return retard_depart + depassement_en_cours


def clock(ms: int) -> str:
    """Compte à rebours lisible : « 9:46 », « 1:02:05 » au-delà d'une heure."""
    s = abs(round(ms / 1000))
    mm, ss = divmod(s % 3600, 60)
    if s >= 3600:
        return f"{s // 3600}:{mm:02d}:{ss:02d}"
    return f"{mm}:{ss:02d}"


def humaine(minutes: float) -> str:
    """Durée lisible : « 12 min », « 1 h 05 », « 2 j 11 h »."""
    m = abs(round(minutes))
    if m < 60:
        return f"{m} min"
    if m < 1440:
        return f"{m // 60} h {m % 60:02d}"
    return f"{m // 1440} j {(m % 1440) // 60} h"
