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
        self.onglets_crees = []
        self.calculs_maj = []

    def ajouter_onglet(self, onglet, blocs, effectifs):
        self.onglets[onglet] = {"blocs": blocs, "effectifs": effectifs}

    def onglet_existe(self, onglet):
        return onglet in self.onglets

    def lire_bloc(self, onglet, numero_culte):
        return self.onglets.get(onglet, {}).get("blocs", {}).get(numero_culte)

    def ecrire_bloc(self, onglet, bloc, lignes):
        if len(lignes) > bloc.nb_lignes:
            manquantes = len(lignes) - bloc.nb_lignes
            point_insertion = bloc.ligne_fin  # une vraie insertDimension décale tout ce qui suit ce point
            bloc.valeurs += [("", "", "")] * manquantes
            bloc.ligne_fin += manquantes
            for autre in self.onglets[onglet]["blocs"].values():
                if autre is not bloc and autre.ligne_debut > point_insertion:
                    autre.ligne_debut += manquantes
                    autre.ligne_fin += manquantes
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

    def lister_onglets_mensuels(self):
        return [o for o in self.onglets if ss.onglet_vers_date(o)]

    def creer_onglet_mensuel(self, nouveau, depuis):
        source = self.onglets[depuis]
        blocs = {
            numero: ss.BlocCulte(ligne_debut=b.ligne_debut, ligne_fin=b.ligne_fin, valeurs=[("", "", "")] * b.nb_lignes)
            for numero, b in source["blocs"].items()
        }
        effectifs = {numero: ss.Effectifs(ligne=e.ligne) for numero, e in source["effectifs"].items()}
        self.onglets[nouveau] = {"blocs": blocs, "effectifs": effectifs}
        self.onglets_crees.append((nouveau, depuis))

    def maj_calculs(self, nouveau, depuis):
        self.calculs_maj.append((nouveau, depuis))


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


def test_onglet_manquant_est_cree_par_duplication_du_dernier(db):
    culte = _culte(db, date(2027, 1, 24), [("Accueil", "Marie", "Marie")])  # mois sans onglet encore
    client, numero_nov = _client_avec_bloc(nb_lignes=1)  # crée l'onglet "Novembre 2026"
    r = ss.synchroniser_culte(db, client, culte)
    nouvel_onglet = ss.nom_onglet(2027, 1)
    assert r.statut == "ok"
    assert "Nouvel onglet" in r.message
    assert client.onglets_crees == [(nouvel_onglet, ONGLET)]
    assert client.calculs_maj == [(nouvel_onglet, ONGLET)]
    assert nouvel_onglet in client.onglets
    # L'onglet source n'a pas été touché.
    assert client.onglets[ONGLET]["blocs"][numero_nov].valeurs == [("", "", "")]


def test_onglet_existant_n_est_jamais_recree(db):
    culte = _culte(db, JOUR, [("Accueil", "Marie", "Marie")])
    client, _ = _client_avec_bloc(nb_lignes=1)
    ss.synchroniser_culte(db, client, culte)
    assert client.onglets_crees == []


def test_aucun_onglet_mensuel_a_dupliquer_est_une_erreur(db):
    culte = _culte(db, date(2027, 1, 24), [("Accueil", "Marie", "Marie")])
    client = FakeSheetsClient()  # aucun onglet mensuel connu
    r = ss.synchroniser_culte(db, client, culte)
    assert r.statut == "erreur"


def test_inserer_onglet_dans_formule():
    formule = "=QUERY({'Juillet 2026'!A2:D; 'Septembre 2026'!A2:D; 'Décembre 2026'!A2:D})"
    nouvelle = ss.inserer_onglet_dans_formule(formule, "Janvier 2027", "Décembre 2026")
    assert nouvelle == "=QUERY({'Juillet 2026'!A2:D; 'Septembre 2026'!A2:D; 'Décembre 2026'!A2:D; 'Janvier 2027'!A2:D})"


def test_inserer_onglet_dans_formule_sans_motif_ne_change_rien():
    formule = "=SOMME(A1:A10)"
    assert ss.inserer_onglet_dans_formule(formule, "Janvier 2027", "Décembre 2026") == formule


def test_extension_d_un_bloc_ne_touche_pas_le_bloc_suivant(db):
    """Un culte avec plus de lignes que son bloc insère des lignes à la fin du bloc (étend la fusion de
    la colonne A) ; le bloc du culte suivant dans le même onglet doit juste être décalé, pas écrasé."""
    client = FakeSheetsClient()
    numero = ss.numero_culte_dans_mois(JOUR)
    bloc1 = ss.BlocCulte(ligne_debut=2, ligne_fin=4, valeurs=[("", "", "")] * 3)
    bloc2 = ss.BlocCulte(ligne_debut=5, ligne_fin=7, valeurs=[("Sono", "Paul", "Paul"), ("", "", ""), ("", "", "")])
    client.ajouter_onglet(ONGLET, {numero: bloc1, numero + 1: bloc2},
                          {numero: ss.Effectifs(ligne=3), numero + 1: ss.Effectifs(ligne=4)})

    culte = _culte(db, JOUR, [("Accueil", "A", "A"), ("Modération", "B", "B"),
                              ("Prédication", "C", "C"), ("Sainte Cène", "D", "D")])  # 4 lignes > les 3 du bloc
    r = ss.synchroniser_culte(db, client, culte)

    assert r.statut == "ok"
    assert bloc1.nb_lignes == 4
    assert bloc1.ligne_debut == 2 and bloc1.ligne_fin == 5
    # Le bloc 2 est décalé d'une ligne, son contenu est intact.
    assert bloc2.ligne_debut == 6 and bloc2.ligne_fin == 8
    assert bloc2.valeurs == [("Sono", "Paul", "Paul"), ("", "", ""), ("", "", "")]


# ---------------------------------------------------------------------------
# Import Sheet -> site (saisies manuelles dans le Sheet)
# ---------------------------------------------------------------------------
def _eglise(db):
    eglise = models.Eglise(nom_eglise="Import", email="import@test.local", mot_de_passe_hash="x")
    db.add(eglise)
    db.commit()
    return eglise


def _client_import(valeurs, effectifs=None, nb_lignes=4):
    client = FakeSheetsClient()
    numero = ss.numero_culte_dans_mois(JOUR)
    valeurs = list(valeurs) + [("", "", "")] * (nb_lignes - len(valeurs))
    bloc = ss.BlocCulte(ligne_debut=53, ligne_fin=53 + nb_lignes - 1, valeurs=valeurs)
    client.ajouter_onglet(ONGLET, {numero: bloc}, {numero: effectifs or ss.Effectifs(ligne=2 + numero)})
    return client, numero


def test_import_bloc_manuel_cree_le_culte(db):
    eglise = _eglise(db)
    client, _ = _client_import([("Accueil", "Anne", "Anne"), ("Projection", "", "Jeremie")],
                               ss.Effectifs(ligne=6, hommes="14", femmes="19", ados="3", enfants=""))
    items = ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))
    assert [(i.jour, i.action) for i in items] == [(JOUR, "creer")]
    assert stats_culte(db, eglise.id) is None  # aperçu : rien n'est écrit

    assert ss.appliquer_import(db, eglise.id, items) == 1
    culte = stats_culte(db, eglise.id)
    assert [(s.departement, s.prevu, s.servi) for s in culte.services] == [("Accueil", "Anne", "Anne"), ("Projection", "", "Jeremie")]
    assert (culte.hommes, culte.femmes, culte.ados, culte.enfants) == (14, 19, 3, None)


def test_bloc_ecrit_par_le_site_non_reimporte(db):
    eglise = _eglise(db)
    culte = models.Culte(eglise_id=eglise.id, date=JOUR)
    db.add(culte)
    db.commit()
    db.add(models.Service(culte_id=culte.id, eglise_id=eglise.id, departement="Accueil", prevu="Anne", servi="Anne", position=0))
    db.commit()
    db.refresh(culte)
    client, numero = _client_import([], nb_lignes=3)
    assert ss.synchroniser_culte(db, client, culte).statut == "ok"  # le site écrit le bloc
    assert _changements(ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))) == []


def test_conflit_apercu_sans_ecriture_puis_confirmation(db):
    eglise = _eglise(db)
    from app import stats
    stats.enregistrer_culte(db, eglise.id, JOUR, 10, 10, [("Accueil", "Anne", "Anne")])
    client, _ = _client_import([("Accueil", "Anne", "Armel")])  # corrigé à la main dans le Sheet
    items = ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))
    assert [i.action for i in items] == ["remplacer"]
    assert items[0].avant_lignes == [("Accueil", "Anne", "Anne")]
    assert stats_culte(db, eglise.id).services[0].servi == "Anne"  # pas encore confirmé

    ss.appliquer_import(db, eglise.id, items, cles=[])  # rien de coché
    assert stats_culte(db, eglise.id).services[0].servi == "Anne"

    ss.appliquer_import(db, eglise.id, items, cles=[JOUR.isoformat()])
    culte = stats_culte(db, eglise.id)
    assert culte.services[0].servi == "Armel"
    assert (culte.hommes, culte.femmes) == (10, 10)  # cases vides du Sheet : valeurs du site conservées


def test_apres_import_l_export_ne_reecrit_pas_le_bloc(db):
    eglise = _eglise(db)
    client, numero = _client_import([("Accueil", "Anne", "Anne")])
    items = ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))
    ss.appliquer_import(db, eglise.id, items)
    r = ss.synchroniser_culte(db, client, stats_culte(db, eglise.id))
    assert r.statut == "ok"
    assert client.ecritures_bloc == []  # déjà à jour, aucune écriture
    assert _changements(ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))) == []


def test_import_nom_avec_espace_final_rapproche(db):
    eglise = _eglise(db)
    from app import stats
    stats.ajouter_stars(db, eglise.id, ["Débora"])
    client, _ = _client_import([("Prédication", "Débora ", "débora ")])
    items = ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))
    ss.appliquer_import(db, eglise.id, items)
    s = stats_culte(db, eglise.id).services[0]
    assert (s.prevu, s.servi) == ("Débora", "Débora")
    assert [x.nom for x in stats.stars(db, eglise.id)] == ["Débora"]  # pas de doublon


def test_effectifs_corriges_a_la_main_apres_export_sont_importes(db):
    eglise = _eglise(db)
    from app import stats
    culte = stats.enregistrer_culte(db, eglise.id, JOUR, 10, 12, [("Accueil", "Anne", "Anne")])
    client, numero = _client_import([], nb_lignes=3)
    ss.synchroniser_culte(db, client, culte)
    client.onglets[ONGLET]["effectifs"][numero].hommes = "15"  # corrigé dans le Sheet
    items = ss.preparer_import(db, client, eglise.id, jusqu_a=date(2026, 11, 30))
    assert [i.action for i in items] == ["remplacer"]
    assert items[0].lignes == [("Accueil", "Anne", "Anne")]  # les services du site sont gardés
    ss.appliquer_import(db, eglise.id, items)
    assert stats_culte(db, eglise.id).hommes == 15


def stats_culte(db, eglise_id):
    from app import stats
    db.expire_all()
    return stats.culte_du_jour(db, eglise_id, JOUR)


def _changements(items):
    return [i for i in items if i.action != "identique"]


# ---------------------------------------------------------------------------
# Miroir site ⇄ Sheet : dernière modification gagne, rien n'est jamais perdu
# ---------------------------------------------------------------------------
def _historique(db, eglise_id):
    import json
    db.expire_all()
    return [(h.source, json.loads(h.contenu)) for h in
            db.query(models.CulteHistorique).filter(models.CulteHistorique.eglise_id == eglise_id)
            .order_by(models.CulteHistorique.id)]


def _site_puis_sheet(db):
    """Le site pointe le culte puis l'écrit dans le Sheet : les deux sont synchronisés."""
    from app import stats
    eglise = _eglise(db)
    culte = stats.enregistrer_culte(db, eglise.id, JOUR, 10, 12, [("Accueil", "Anne", "Anne"), ("Sono", "Franck", "Franck")])
    client, numero = _client_import([], nb_lignes=4)
    assert ss.synchroniser_culte(db, client, culte).statut == "ok"
    return eglise, client, numero


def test_modif_du_sheet_appliquee_automatiquement_et_ancienne_version_gardee(db):
    eglise, client, numero = _site_puis_sheet(db)
    bloc = client.onglets[ONGLET]["blocs"][numero]
    bloc.valeurs[1] = ("Sono", "Franck", "Gael")  # corrigé dans le Sheet
    r = ss.importer_auto(db, client, eglise.id, jours=[JOUR])
    assert r.importes == [JOUR] and r.conflits == []
    assert [s.servi for s in stats_culte(db, eglise.id).services] == ["Anne", "Gael"]
    h = _historique(db, eglise.id)
    assert h[-1][0] == "site" and ["Sono", "Franck", "Franck"] in h[-1][1]["services"]


def test_modif_du_site_apres_synchro_remplace_le_sheet_et_garde_sa_version(db):
    from app import stats
    eglise, client, numero = _site_puis_sheet(db)
    culte = stats.enregistrer_culte(db, eglise.id, JOUR, 10, 12, [("Accueil", "Anne", "Armel"), ("Sono", "Franck", "Franck")])
    assert ss.synchroniser_culte(db, client, culte).statut == "ok"
    assert client.onglets[ONGLET]["blocs"][numero].valeurs[0] == ("Accueil", "Anne", "Armel")
    h = _historique(db, eglise.id)
    assert h[-1][0] == "sheet" and ["Accueil", "Anne", "Anne"] in h[-1][1]["services"]


def test_conflit_rien_n_est_ecrase_d_aucun_cote(db):
    from app import stats
    import time
    eglise, client, numero = _site_puis_sheet(db)
    bloc = client.onglets[ONGLET]["blocs"][numero]
    bloc.valeurs[0] = ("Accueil", "Anne", "Colombe")  # modifié dans le Sheet…
    time.sleep(1.1)
    culte = stats.enregistrer_culte(db, eglise.id, JOUR, 10, 12, [("Accueil", "Anne", "Liza"), ("Sono", "Franck", "Franck")])  # … et sur le site
    r = ss.importer_auto(db, client, eglise.id, jours=[JOUR])
    assert r.conflits == [JOUR] and r.importes == []
    assert stats_culte(db, eglise.id).services[0].servi == "Liza"  # site intact
    assert ss.synchroniser_culte(db, client, culte).statut == "deja_rempli"
    assert client.onglets[ONGLET]["blocs"][numero].valeurs[0] == ("Accueil", "Anne", "Colombe")  # Sheet intact


def test_bloc_vide_dans_le_sheet_n_efface_pas_le_site(db):
    eglise, client, numero = _site_puis_sheet(db)
    bloc = client.onglets[ONGLET]["blocs"][numero]
    bloc.valeurs = [("", "", "")] * bloc.nb_lignes
    ss.importer_auto(db, client, eglise.id, jours=[JOUR])
    assert len(stats_culte(db, eglise.id).services) == 2


def test_case_d_effectif_videe_dans_le_sheet_n_efface_pas_le_site(db):
    eglise, client, numero = _site_puis_sheet(db)
    client.onglets[ONGLET]["effectifs"][numero].hommes = None
    ss.importer_auto(db, client, eglise.id, jours=[JOUR])
    assert stats_culte(db, eglise.id).hommes == 10


def test_effectif_modifie_sur_le_site_remplace_celui_du_sheet(db):
    from app import stats
    eglise, client, numero = _site_puis_sheet(db)
    culte = stats.enregistrer_culte(db, eglise.id, JOUR, 11, 12, [("Accueil", "Anne", "Anne"), ("Sono", "Franck", "Franck")])
    ss.synchroniser_culte(db, client, culte)
    assert client.onglets[ONGLET]["effectifs"][numero].hommes == 11


def test_restaurer_une_version(db):
    eglise, client, numero = _site_puis_sheet(db)
    client.onglets[ONGLET]["blocs"][numero].valeurs[1] = ("Sono", "Franck", "Gael")
    ss.importer_auto(db, client, eglise.id, jours=[JOUR])
    h = db.query(models.CulteHistorique).filter(models.CulteHistorique.source == "site").first()
    culte = ss.restaurer_version(db, eglise.id, h.id)
    assert [s.servi for s in culte.services] == ["Anne", "Franck"]
    # La version remplacée par la restauration est elle-même gardée.
    assert any(["Sono", "Franck", "Gael"] in c["services"] for _, c in _historique(db, eglise.id))


def test_septembre_jamais_synchronise(db):
    from app import stats
    eglise = _eglise(db)
    culte = stats.enregistrer_culte(db, eglise.id, date(2026, 9, 13), 14, 19, [("Accueil", "Anne", "Anne")])
    client, _ = _client_import([])
    assert ss.synchroniser_culte(db, client, culte).statut == "deja_rempli"
    assert client.ecritures_bloc == []
    assert ss.importer_auto(db, client, eglise.id, jours=[date(2026, 9, 13)]).importes == []


def test_jour_depuis_cellule(db):
    client, numero = _client_import([("Accueil", "Anne", "Anne")])  # bloc lignes 53-56, culte 4
    assert ss.jour_depuis_cellule(client, ONGLET, 54) == JOUR
    assert ss.jour_depuis_cellule(client, ONGLET, 6) == JOUR  # ligne d'effectifs du culte 4
    assert ss.jour_depuis_cellule(client, "Data_Calc", 54) is None


def test_webhook_refuse_sans_secret(monkeypatch):
    import os
    os.environ.setdefault("SECRET_KEY", "test")
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("SHEET_WEBHOOK_SECRET", "bon-secret")
    c = TestClient(main.app)
    assert c.post("/api/sheet/modification", json={"onglet": ONGLET, "ligne": 54}).status_code == 401
    assert c.post("/api/sheet/modification", json={"onglet": ONGLET, "ligne": 54},
                  headers={"X-Sheet-Secret": "mauvais"}).status_code == 401
    monkeypatch.delenv("SHEET_WEBHOOK_SECRET")
    assert c.post("/api/sheet/modification", json={"onglet": ONGLET, "ligne": 54},
                  headers={"X-Sheet-Secret": ""}).status_code == 401


def test_numero_et_date_en_colonne_a():
    assert ss.numero_de_cellule("4 · 22/11") == 4
    assert ss.numero_de_cellule(4) == 4 and ss.numero_de_cellule("4") == 4
    assert ss.numero_de_cellule("") is None
    assert ss.libelle_culte(2026, 11, 4) == "4 · 22/11"
    assert ss.libelle_culte(2026, 11, 5) == "5 · 29/11"
    assert ss.libelle_culte(2027, 2, 5) == "5"  # février 2027 n'a que 4 dimanches
