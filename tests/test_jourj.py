from datetime import date, datetime, timedelta

from app import jourj
from app.jourj import Etape, EtapeReelle, EtatLive, PARIS


def _etapes():
    return [
        Etape(titre="Louange", personne="Prodiges Musique", duree=20, debut="10:00"),
        Etape(titre="Prédication", personne="Pasteur", duree=60),
        Etape(titre="Clôture", personne="", duree=10),
    ]


def _dt(h, m, s=0):
    return datetime(2026, 11, 22, h, m, s, tzinfo=PARIS)


def test_heures_prevues_enchaine_les_durees():
    debuts = jourj.heures_prevues(date(2026, 11, 22), _etapes())
    assert debuts[0] == _dt(10, 0)
    assert debuts[1] == _dt(10, 20)
    assert debuts[2] == _dt(11, 20)


def test_heure_fixe_relance_le_curseur():
    etapes = _etapes()
    etapes[1].debut = "10:45"  # repart plus tard que la fin prévue de l'étape 0
    debuts = jourj.heures_prevues(date(2026, 11, 22), etapes)
    assert debuts[1] == _dt(10, 45)
    assert debuts[2] == _dt(11, 45)


def test_demarrage():
    etat = EtatLive(etapes=_etapes())
    assert etat.index_courant is None and etat.demarre_a is None
    jourj.avancer(etat, _dt(10, 0))
    assert etat.index_courant == 0
    assert etat.demarre_a == _dt(10, 0)
    assert etat.reel[0].debut == _dt(10, 0)


def test_suivant_cloture_l_etape_precedente():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))
    jourj.avancer(etat, _dt(10, 22))
    assert etat.reel[0].fin == _dt(10, 22)
    assert etat.index_courant == 1
    assert etat.reel[1].debut == _dt(10, 22)


def test_dernier_element_termine_le_direct():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))
    jourj.avancer(etat, _dt(10, 20))
    jourj.avancer(etat, _dt(11, 20))
    assert etat.index_courant is not None
    jourj.avancer(etat, _dt(11, 30))
    assert etat.index_courant is None
    assert etat.termine_a == _dt(11, 30)
    assert etat.termine


def test_pause_ne_compte_pas_dans_le_temps_ecoule():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))
    jourj.stop(etat, _dt(10, 5))  # 5 min écoulées
    assert etat.en_pause
    jourj.reprendre(etat, _dt(10, 15))  # 10 min de pause, non comptées
    assert not etat.en_pause
    # il reste 20 - 5 = 15 min sur l'étape, malgré les 15 min d'horloge passées
    restant = jourj.restant_ms(etat.etapes[0], etat.reel[0], _dt(10, 15))
    assert restant == 15 * 60_000


def test_restant_negatif_en_depassement():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))
    restant = jourj.restant_ms(etat.etapes[0], etat.reel[0], _dt(10, 25))  # étape de 20 min
    assert restant == -5 * 60_000
    assert jourj.clock(restant) == "5:00"


def test_retard_cumule():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))  # à l'heure
    jourj.avancer(etat, _dt(10, 20))  # étape 1 commence à l'heure prévue
    jourj.stop(etat, _dt(10, 20))
    # on démarre l'étape 1 en retard (ex. via un décalage simulé après reprise)
    etat.reel[1].debut = _dt(10, 26)
    retard = jourj.retard_ms(etat, date(2026, 11, 22), _dt(10, 26))
    assert retard == 6 * 60_000


def test_avance_quand_etape_en_cours_depasse_moins_que_prevu():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 10))  # démarre 10 min en retard (étape prévue à 10:00)
    retard = jourj.retard_ms(etat, date(2026, 11, 22), _dt(10, 10))
    assert retard == 10 * 60_000


def test_recommencer_efface_tout():
    etat = EtatLive(etapes=_etapes())
    jourj.avancer(etat, _dt(10, 0))
    jourj.avancer(etat, _dt(10, 20))
    jourj.recommencer(etat)
    assert etat.index_courant is None
    assert etat.demarre_a is None
    assert etat.termine_a is None
    assert etat.en_pause_depuis is None
    assert all(r.debut is None and r.fin is None and r.pause_ms == 0 for r in etat.reel)


def test_stop_sans_direct_en_cours_est_sans_effet():
    etat = EtatLive(etapes=_etapes())
    jourj.stop(etat, _dt(10, 0))
    assert not etat.en_pause
