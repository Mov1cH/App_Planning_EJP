from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, sheets_sync as ss


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _culte(db, jour, services, hommes=None, femmes=None, ados=None, enfants=None):
    eglise = models.Eglise(nom_eglise="Test", email=f"{jour.isoformat()}@test.local", mot_de_passe_hash="x")
    db.add(eglise)
    db.commit()
    culte = models.Culte(eglise_id=eglise.id, date=jour, hommes=hommes, femmes=femmes, ados=ados, enfants=enfants)
    db.add(culte)
    db.commit()
    for i, (dep, prevu, servi) in enumerate(services):
        db.add(models.Service(culte_id=culte.id, eglise_id=eglise.id, departement=dep, prevu=prevu, servi=servi, position=i))
    db.commit()
    db.refresh(culte)
    return culte


class FakeSheetsClient(ss.SheetsClient):
    def __init__(self):
        self.onglets = {}
        self.noms = []
        self.ecritures_bloc = []
        self.ecritures_effectifs = []

    def ajouter_onglet(self, onglet, blocs, effectifs):
        self.onglets[onglet] = {"blocs": blocs, "effectifs": effectifs}

    def onglet_existe(self, onglet):
        return onglet in self.onglets

    def lire_bloc(self, onglet, numero_culte):
        return self.onglets.get(onglet, {}).get("blocs", {}).get(numero_culte)

    def ecrire_bloc(self, onglet, bloc, lignes):
        if len(lignes) > bloc.nb_lignes:
            manquantes = len(lignes) - bloc.nb_lignes
            bloc.valeurs += [("", "", "")] * manquantes
            bloc.ligne_fin += manquantes
        bloc.valeurs = list(lignes) + [("", "", "")] * (bloc.nb_lignes - len(lignes))
        self.ecritures_bloc.append((onglet, list(lignes)))

    def lire_effectifs(self, onglet, numero_culte):
        return self.onglets[onglet]["effectifs"][numero_culte]

    def ecrire_effectifs(self, onglet, numero_culte, valeurs):
        eff = self.onglets[onglet]["effectifs"][numero_culte]
        for k, v in valeurs.items():
            setattr(eff, k, v)
        self.ecritures_effectifs.append((onglet, numero_culte, dict(valeurs)))

    def noms_connus(self):
        return list(self.noms)

    def ajouter_nom(self, nom):
        self.noms.append(nom)


JOUR = date(2026, 11, 22)  # un dimanche de novembre 2026, 4e du mois
ONGLET = ss.nom_onglet(JOUR.year, JOUR.month)


def _client_avec_bloc(nb_lignes=3, valeurs=None, effectifs=None):
    client = FakeSheetsClient()
    bloc = ss.BlocCulte(ligne_debut=10, ligne_fin=10 + nb_lignes - 1,
                        valeurs=valeurs or [("", "", "")] * nb_lignes)
    numero = ss.numero_culte_dans_mois(JOUR)
    client.ajouter_onglet(ONGLET, {numero: bloc}, {numero: effectifs or ss.Effectifs(ligne=2 + numero)})
    return client, numero


def test_bloc_vide_est_ecrit(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie", "Marie"), ("Sono", "Paul", "")])
    client, numero = _client_avec_bloc(nb_lignes=3)
    r = ss.synchroniser_culte(db, client, culte)
    assert r.statut == "ok"
    assert len(client.ecritures_bloc) == 1
    bloc = client.onglets[ONGLET]["blocs"][numero]
    assert bloc.valeurs[0] == ("Accueil", "Marie", "Marie")


def test_bloc_rempli_a_la_main_non_touche(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie", "Marie")])
    valeurs_main = [("Accueil", "Jean", "Jean"), ("", "", ""), ("", "", "")]
    client, numero = _client_avec_bloc(nb_lignes=3, valeurs=valeurs_main)
    r = ss.synchroniser_culte(db, client, culte)
    assert r.statut == "deja_rempli"
    assert client.ecritures_bloc == []
    bloc = client.onglets[ONGLET]["blocs"][numero]
    assert bloc.valeurs == valeurs_main  # inchangé


def test_bloc_ecrit_par_le_site_puis_pointage_corrige_est_reecrit(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie", "Marie")])
    client, numero = _client_avec_bloc(nb_lignes=3)
    r1 = ss.synchroniser_culte(db, client, culte)
    assert r1.statut == "ok"

    # Pointage corrigé après coup : "Marie" absente, remplacée par "Julie".
    culte.services[0].servi = "Julie"
    db.commit()

    r2 = ss.synchroniser_culte(db, client, culte)
    assert r2.statut == "ok"
    assert len(client.ecritures_bloc) == 2
    bloc = client.onglets[ONGLET]["blocs"][numero]
    assert bloc.valeurs[0] == ("Accueil", "Marie", "Julie")


def test_effectif_deja_saisi_est_conserve(db):
    culte = _culte(db, JOUR, [], hommes=40, femmes=35, ados=5, enfants=10)
    numero = ss.numero_culte_dans_mois(JOUR)
    eff = ss.Effectifs(ligne=2 + numero, hommes="38")  # déjà rempli à la main dans le Sheet
    client, _ = _client_avec_bloc(nb_lignes=0, effectifs=eff)
    ss.synchroniser_culte(db, client, culte)
    # Hommes conservé (déjà rempli) ; les autres cellules vides sont complétées.
    assert client.ecritures_effectifs == [(ONGLET, numero, {"femmes": 35, "ados": 5, "enfants": 10})]
    assert client.onglets[ONGLET]["effectifs"][numero].hommes == "38"


def test_nom_avec_espace_final_est_rapproche(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie ange", "Marie ange")])  # sans l'espace final du Sheet
    client, numero = _client_avec_bloc(nb_lignes=1)
    client.noms = ["Marie ange "]  # orthographe de référence, avec l'espace final
    ss.synchroniser_culte(db, client, culte)
    bloc = client.onglets[ONGLET]["blocs"][numero]
    assert bloc.valeurs[0] == ("Accueil", "Marie ange ", "Marie ange ")
    assert "Marie ange" not in client.noms or client.noms == ["Marie ange "]  # pas de doublon ajouté


def test_nom_absent_de_la_liste_est_ajoute(db):
    culte = _culte(db, JOUR, [("Accueil", "Nouvelle Personne", "Nouvelle Personne")])
    client, _ = _client_avec_bloc(nb_lignes=1)
    ss.synchroniser_culte(db, client, culte)
    assert "Nouvelle Personne" in client.noms


def test_client_desactive_ne_fait_rien(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie", "Marie")])
    r = ss.synchroniser_culte(db, None, culte)
    assert r.statut == "desactive"


def test_culte_avant_novembre_2026_est_ignore(db):
    culte = _culte(db, date(2026, 10, 25), [("Accueil", "Marie", "Marie")])
    client, _ = _client_avec_bloc(nb_lignes=1)
    r = ss.synchroniser_culte(db, client, culte)
    assert r.statut == "deja_rempli"
    assert client.ecritures_bloc == []
