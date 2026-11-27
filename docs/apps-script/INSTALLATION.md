# Installer la synchro automatique Sheet → site

À faire **une fois le site déployé** (le script doit pouvoir joindre l'adresse en ligne du site), d'abord sur la
**copie de test** du Sheet, puis sur le vrai Sheet quand tout est validé.

1. Ouvrir le Google Sheet → menu **Extensions → Apps Script**.
2. Renommer le projet « Synchro planning ». Remplacer tout le contenu de `Code.gs` par celui de `synchro.gs`
   (ce dossier), puis **Enregistrer** (icône disquette).
3. **Paramètres du projet** (roue dentée à gauche) → **Propriétés du script** → **Ajouter une propriété** :
   - `SHEET_WEBHOOK_SECRET` = la même valeur que la variable `SHEET_WEBHOOK_SECRET` du site
     (dans le `.env` du projet en local, et dans les variables d'environnement Vercel en production) ;
   - `SITE_URL` = `https://planning-ejp.vercel.app`.
4. **Déclencheurs** (réveil à gauche) → **Ajouter un déclencheur** :
   - fonction : `surModification` ;
   - source de l'événement : **À partir d'une feuille de calcul** ;
   - type d'événement : **Lors de la modification** ;
   - notifications d'échec : quotidiennes.
   Enregistrer, puis accepter l'autorisation Google (compte du propriétaire du Sheet ; si Google affiche
   « Application non validée » : Paramètres avancés → Accéder à Synchro planning).
5. Vérifier : sélectionner la fonction `testerConnexion` → **Exécuter** → le journal doit afficher
   `Réponse du site : 200`. Un `401` veut dire que le secret ne correspond pas.
6. Essai réel : modifier une case d'un culte de novembre dans le Sheet, attendre quelques secondes, recharger
   la page Stats du site : la modification doit y être. L'ancienne version est dans **Stats → Historique de synchro**.

Le script ne réagit qu'aux onglets mensuels (« Novembre 2026 »…), colonnes A à D et cases d'effectifs O3:U7 (dont les nouveaux venus en T et U).
Les écritures faites par le site dans le Sheet ne le déclenchent pas (pas de boucle).
