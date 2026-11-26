# Évolutions du site Planning EJP — cahier d'instructions

> Document de travail pour l'agent de code (VS Code). À lire en entier avant de commencer.
> Rédigé le 23/11/2026 à partir du code de ce dossier (identique à la production Vercel `planning-ejp`)
> et du projet « Gestion d'église » (`../Gestion d'eglise/gestion-eglise`), dont le mode jour J est à reproduire à l'identique.

## 0. Règles générales

- **Stack existante, à respecter** : FastAPI + SQLAlchemy + Jinja2, CSS maison (`static/css/app.css`), JS vanilla (`static/js/app.js`), aucun framework front. SQLite en local (`planning_ejp.db`), Postgres Supabase en production (`DATABASE_URL`).
- **Migrations** : pas d'Alembic. Les nouvelles tables sont créées par `models.Base.metadata.create_all` et les nouvelles colonnes s'ajoutent dans le dictionnaire `_COLONNES_AJOUTEES` de `app/services.py` (fonction `migrer_schema()`). Toujours passer par là, jamais de `DROP`.
- **Le dépôt git local a des modifications non commitées** (`git status`) qui sont **déjà en production**. Commencer par un commit « état production 23/11 » avant toute modification, puis un commit par chantier.
- **Pas de remote GitHub** à jour : le déploiement se fait avec la CLI Vercel (`vercel --prod`, projet déjà lié via `.vercel/`). **Ne jamais déployer sans l'accord explicite de Morel.** Tester d'abord en local (`python -m uvicorn main:app --reload`).
- **Secrets** : jamais dans le code ni dans git. Variables d'environnement uniquement (`.env` en local, Vercel en production).
- Textes de l'interface en français, ton simple, mobile d'abord (la majorité des usages se fait sur iPhone).
- Ajouter `docs/` à `.vercelignore` (ce dossier ne doit pas partir en production).

Ordre conseillé : **1 → 2 → 3**, chacun testé en local avant de passer au suivant.

---

## 1. Pointage : ajouter Ados et Enfants

Aujourd'hui `Culte` a `hommes` et `femmes` (formulaire `app/templates/pointage.html`, route `POST /stats/culte` dans `main.py`, `stats.enregistrer_culte()`).

À faire :
1. `app/models.py` → `Culte` : ajouter `ados = Column(Integer, nullable=True)` et `enfants = Column(Integer, nullable=True)`.
2. `app/services.py` → `_COLONNES_AJOUTEES["cultes"] = {"ados": "INTEGER", "enfants": "INTEGER"}`.
3. `pointage.html` : deux champs « Ados » et « Enfants » dans la section « Effectif du culte », même style que Hommes / Femmes ; le **Total** additionne les 4 (JS `effectif()` du template).
4. `main.py` `post_pointage` + `stats.enregistrer_culte(...)` : lire et enregistrer `ados`, `enfants` (via `_entier`).
5. `app/stats.py` : `LigneCulte.effectif` = somme des 4 (None si les 4 sont vides) ; ajouter `ados_moyen` / `enfants_moyen` à côté de `hommes_moyen` / `femmes_moyen`.
6. `app/templates/stats.html` et `app/exports.py` (PDF) : afficher Ados et Enfants partout où Hommes / Femmes apparaissent.

---

## 2. Mode jour J (chronomètre du déroulé)

### 2.1 Ce que veut Morel
- **Reproduire le mode jour J de « Gestion d'église », à l'identique** : même écran, même mise en page, mêmes textes, même comportement. Il ne veut **pas** d'un autre design (l'ancien prototype noir « horloge à volets » est abandonné, ne pas s'en inspirer).
- Le projet de référence est dans ce même ordinateur : `../Gestion d'eglise/gestion-eglise` (Next.js + Tailwind). **Lire ses fichiers et transposer fidèlement** en Jinja2 + CSS maison + JS vanilla (pas de Tailwind dans ce projet : réécrire les classes en CSS dans `static/css/app.css`, section « Jour J »).
- Deux ajouts seulement par rapport à Gestion d'église :
  1. En dépassement, le chrono affiche **`−1:30`** (signe moins) au lieu de `+1:30`.
  2. Un bouton secondaire **« Stop »** (pause) / **« Reprendre »**, dans le style du bouton « Recommencer » (bouton discret), sous le bouton principal « Passer au suivant ». Le temps en pause ne compte pas.

### 2.2 Comment c'est fait dans « Gestion d'église » (modèle à suivre)
Fichiers de référence : `components/runsheet/live-view.tsx`, `lib/runsheet/timing.ts`, `lib/runsheet/actions.ts`, `supabase/migrations/20261110000020_run_sheet_live.sql`, `app/(app)/[churchSlug]/cultes/[serviceId]/deroule/live/page.tsx`.
- **L'état du direct est en base, pas dans le navigateur** : étape en cours (`current_item_id`), début du direct (`live_started_at`), et pour chaque étape `actual_started_at` / `actual_ended_at`. Conséquence : **tous les écrans voient la même chose** (régie, téléphone de l'orateur, écran de scène) et un rechargement ne perd rien.
- Un seul bouton pilote : Démarrer → Suivant → Terminer (fonction SQL `advance_run_sheet` : clôt l'étape en cours, démarre la suivante à `now()`).
- **Temps restant** = `début réel de l'étape + durée − maintenant` (négatif = dépassement).
- **Retard cumulé** = `début réel de l'étape en cours − début prévu` (« En retard de 6 min » / « En avance de 2 min » / « À l'heure »). Une étape à heure fixe repart de son heure prévue.
- **Recommencer** (essai, fausse manœuvre) : efface étape en cours et heures réelles, **avec confirmation** (tout le monde voit le direct).
- Écran du direct en `position: fixed; inset: 0` par-dessus le menu de l'app.
- Une **entrée « Jour J »** très visible apparaît sur le tableau de bord et sur le déroulé, de 2 h avant le culte à 30 min après la fin.
- Différences voulues par Morel : la pause (Stop) et le signe « − » en dépassement (voir 2.1).

### 2.3 Données
Le déroulé est stocké dans `models.Deroule` (une ligne par église et par date, colonne `data` = JSON avec `etapes: [{debut: "HH:MM", duree: min, titre, role_id, personne}, ...]`, cf. `services.deroule_par_defaut` et `GET/POST /deroule` dans `main.py`).

Nouvelle table `DerouleLive` (fichier `app/models.py`) :
| colonne | type | rôle |
|---|---|---|
| id | Integer PK | |
| eglise_id | FK eglises | |
| date | Date | UniqueConstraint(eglise_id, date) |
| etapes | Text (JSON) | **copie figée** des étapes au démarrage (titre, personne, debut, duree) pour qu'une modif du déroulé pendant le culte ne casse pas le direct |
| index_courant | Integer, nullable | `None` = pas commencé ou terminé |
| demarre_a | DateTime (UTC), nullable | début du direct |
| termine_a | DateTime (UTC), nullable | |
| en_pause_depuis | DateTime (UTC), nullable | non nul = chrono arrêté (Stop) |
| reel | Text (JSON) | par étape : `{debut, fin, pause_ms}` (ISO UTC) |
| version | Integer | +1 à chaque action (sert au rafraîchissement des écrans) |

Calculs (dans un nouveau module `app/jourj.py`, testable sans HTTP) :
- `ecoule_ms(etape)` = `(fin ou maintenant ou en_pause_depuis) − debut − pause_ms`.
- `restant_ms` = `duree × 60 000 − ecoule_ms` (peut être négatif).
- Début prévu d'une étape = son `debut` « HH:MM » du déroulé (heure de Paris, `Europe/Paris`) ; à défaut, fin prévue de la précédente.
- `retard_ms` = début réel de l'étape en cours − début prévu (+ dépassement en cours au-delà de la durée).

### 2.4 Routes (dans `main.py`, protégées par `get_current_eglise` comme le reste)
- `GET /deroule/jour-j?date_str=YYYY-MM-DD` → page HTML du direct (template `deroule_live.html`, qui **n'étend pas** `base.html` ou masque la barre de navigation : écran `fixed inset:0`). Par défaut : dimanche courant / prochain (`services.prochain_dimanche`).
- `GET /deroule/jour-j/etat?date_str=…` → JSON : étapes, index courant, en pause ou non, heures réelles, `version`, et **`serveur_maintenant`** (ISO UTC) pour corriger le décalage d'horloge du téléphone.
- `POST /deroule/jour-j/action` (JSON `{date, action}`) avec `action` ∈ `suivant | stop | reprendre | recommencer` (`suivant` démarre le culte s'il n'a pas commencé). Verrou ligne (`with_for_update()` en Postgres) pour éviter deux « Suivant » simultanés. Répond avec le nouvel état.
- Si aucun déroulé n'est enregistré pour la date : message clair + lien vers `/deroule`.

### 2.5 Synchronisation des écrans
Vercel = fonctions serverless, pas de WebSocket : **polling** de `/deroule/jour-j/etat` toutes les **2 s** (et immédiatement au retour au premier plan, `visibilitychange`). L'horloge tourne localement chaque 250 ms à partir de l'état + décalage serveur. N'afficher le rendu que si `version` a changé ou pour le tic d'horloge.

### 2.6 Écran — copie de `components/runsheet/live-view.tsx` et de `deroule/live/page.tsx`
Structure, de haut en bas, dans un conteneur `position: fixed; inset: 0; z-index: 50; overflow-y: auto; padding: 20px (40px ≥ 768px)` qui recouvre le menu du site, colonne avec un espacement de 24px :
1. **En-tête** : titre « Jour J, 12h40 » (22px, gras) à gauche ; lien « Quitter le jour J » (souligné, 14px) à droite vers `/deroule?date_str=…`.
2. **Bloc « En cours »** : grand aplat de couleur pleine largeur, padding 24px (40px ≥ 768px), texte clair.
   - Couleur de l'aplat : la **couleur principale du thème du site** (celle choisie dans le profil, cf. `config.COULEURS`) à la place du bleu `--poster` de Gestion d'église ; texte `#f8f6f1`. **En dépassement, tout l'aplat passe en rouge** `#c4211a` (texte blanc).
   - Ligne du haut : « En cours » (14px, opacité 0.8) à gauche, **heure actuelle** HH:MM (18px gras, chiffres tabulaires, opacité 0.8) à droite.
   - Titre du moment (30px, 48px ≥ 768px, gras, interligne serré).
   - Détail (18px, opacité 0.8) : type / durée « 10 min » / responsable, séparés par des virgules (équivalent de `detail` dans `deroule/live/page.tsx`).
   - **Chrono** : `font-size: clamp(4rem, 18vw, 11rem)`, gras, `line-height: 1`, `letter-spacing` serré, chiffres tabulaires ; format de `clock()` dans `lib/runsheet/timing.ts` (`9:46`, `1:02:05`) avec `−` devant en dépassement.
   - Barre de progression : hauteur 12px, fond `currentColor` à 20 %, remplissage `currentColor`, transition linéaire 1 s (désactivée si `prefers-reduced-motion`).
   - Avant le début : « Le culte n'a pas commencé. » ; à la fin : « Déroulé terminé. ».
   - Annonce lecteur d'écran (`aria-live="polite"`, masquée) : « Reste 9:46 » / « Dépassé de 1:30 ».
3. **Pastille retard** (si le direct a démarré) : texte 20px gras, padding 8px 16px, alignée à gauche : « Retard : 6 min » sur fond rouge texte blanc si retard > 0, sinon « Avance : 2 min » ou « À l'heure » sur fond gris clair (`#efece4`). Format des durées = `humanMinutes()`.
4. **Frise du déroulé** : une ligne de 16px de haut, un segment par étape, **largeur proportionnelle à sa durée** (`flex-grow: durée`), 2px d'écart ; étapes passées = couleur du thème à 40 %, étape en cours = couleur du thème, à venir = gris `#efece4` ; `title` = nom de l'étape ; `aria-label` « Étape 3 sur 7 ».
5. **« Ensuite »** : bordure gauche 4px couleur du thème, padding gauche 16px : « Ensuite » (14px gris), titre de l'étape suivante (24px gras), son détail (gris).
6. **Bouton principal** pleine largeur, 64px de haut, texte 18px : « Démarrer le culte » → « Passer au suivant » → « Terminer le dernier élément » ; masqué quand c'est terminé.
7. **Stop / Reprendre** (ajout Morel, voir 2.1) : bouton discret 44px.
8. **« Recommencer depuis le début »** : bouton discret (44px, aligné à gauche) ; devient le bouton principal (64px) quand le déroulé est terminé. Au clic, confirmation intégrée à la page (pas de `confirm()`) : « Remettre le jour J à zéro pour tous les écrans ? » + « Oui, recommencer » (rouge) + « Annuler ».
- Messages d'erreur en haut de l'écran (équivalent `FormError`).
- Garder l'écran allumé avec l'API Wake Lock (catch silencieux). En PWA lancée depuis l'écran d'accueil, l'écran est déjà sans barre de navigateur.
- Tous les textes exacts : `messages/fr.json` → section `runSheet` de Gestion d'église.
- Le **Bilan** (prévu / réel / écart) n'existe pas dans Gestion d'église : ne pas l'ajouter.

### 2.7 Points d'entrée
- `deroule.html` : bouton bien visible « Mode jour J » vers `/deroule/jour-j?date_str={{ jour }}`.
- `home.html` (tableau de bord) : le jour du culte, de 2 h avant la première étape à 30 min après la fin prévue, une grande entrée « Jour J — lancer le chrono » (s'inspirer de `components/runsheet/live-entry.tsx`).

### 2.8 Tests
Tests unitaires `pytest` sur `app/jourj.py`, sur le modèle de `lib/runsheet/timing.test.ts` de Gestion d'église : démarrage, suivant, pause/reprise (le temps en pause ne compte pas), dépassement négatif, retard cumulé, étape à heure fixe, dernier moment → terminé, recommencer. Puis test manuel à deux navigateurs ouverts : une action sur l'un doit apparaître sur l'autre en ≤ 2 s.

---

## 3. Synchronisation vers le Google Sheet « Suivi de Culte EJP »

### 3.1 But
Pouvoir consulter les stats même si le site tombe : chaque pointage enregistré sur le site est **aussi écrit dans le Google Sheet**, **au format du Sheet**.

Sheet : `https://docs.google.com/spreadsheets/d/1Vy4o04M5kTbKop58hpBDi4_yyAyC5-l1tn34O851kkw`

### 3.2 ⚠️ Règle n°1 : ne JAMAIS écraser ce qui est déjà dans le Sheet
Morel remplit aussi le Sheet à la main (il l'a fait le 22/11). Donc :
- On **ajoute ce qui manque**, on ne remplace pas.
- Le bloc d'un culte n'est écrit **que s'il est vide** (colonnes B à D vides sur tout le bloc).
- S'il n'est pas vide : on ne le réécrit que si son contenu est **exactement** celui que le site y avait écrit la dernière fois (le site garde une empreinte de ce qu'il a écrit, voir 3.6). Sinon (modifié ou rempli à la main) → on **ne touche à rien** et on le signale sur la page Stats (« Culte du 22/11 : déjà rempli dans le Sheet, non modifié »).
- Effectifs : chaque cellule Hommes / Femmes / Ados / Enfants n'est écrite **que si elle est vide**.
- On n'écrit **jamais** dans les colonnes de formules (E à M) ni dans les en-têtes.
- Synchronisation à partir du **1er novembre 2026** seulement (septembre et octobre restent comme ils sont).

### 3.3 Structure actuelle du Sheet (vérifiée le 23/11 — relire avant de coder, Morel peut l'avoir modifiée)
- Un onglet par mois, nommé `"<Mois> <année>"` en français avec majuscule : `Novembre 2026`, `Décembre 2026`… (onglets existants : Juillet (masqué), Septembre, Octobre, Novembre, Décembre 2026). Autres onglets : `Tableau de Bord Annuel`, `Data_Calc`, `Listes`.
- Ligne 1 = en-têtes. **Saisie en colonnes A à D** :
  - **A** : numéro du culte dans le mois (1, 2, 3, 4, 5) = rang du dimanche dans le mois (22/11/2026 → 4). Cellule **fusionnée verticalement** sur tout le bloc du culte.
  - **B** : Département, **C** : Personne prévue, **D** : Personne ayant servi. Une ligne par service.
  - (L'ancienne colonne « OK » a été supprimée, elle ne servait à rien.)
- Blocs actuels (lignes 1-indexées) dans Novembre / Décembre : culte 1 = 2–17, culte 2 = 19–37, culte 3 = 38–52, culte 4 = 53–67, culte 5 = 68–82. **Ne pas coder ces numéros en dur** : retrouver le bloc d'un culte en lisant les fusions (`merges`) de la colonne A et la cellule A qui contient le numéro.
- Si un culte a plus de lignes que son bloc : insérer des lignes à la fin du bloc (`insertDimension`, `inheritFromBefore: true`) et étendre la fusion de la colonne A.
- **Colonnes E à M = formules** (Total prévu / présent / absences / taux de présence, services par département, services effectués, services manqués). Elles se recalculent seules.
- **Effectifs** : tableau à partir de la colonne N : N3..N7 = numéro du culte (1 à 5), **O = Hommes, P = Femmes, Q = Ados, R = Enfants** (ligne d'en-tête 2), ligne 8 = Total. Culte n → ligne `2 + n`.
- **Listes** : `Listes!A2:A` = départements, `Listes!B2:B` = membres. Les cellules B/C/D ont une validation par liste déroulante sur ces plages.
- **Noms** : écrire **exactement** l'orthographe de `Listes!B` (certains noms ont un **espace final** : « Marie ange », « Débora », « Danielle », « Paul Daniel » — sinon les stats comptent deux personnes). Rapprocher avec `stats.cle_nom()` (sans accents / casse / espaces). Nom absent de la liste → l'ajouter en bas de `Listes!B` puis l'écrire.
- Départements : mêmes noms que sur le site (Prodige Musique, Accueil et Intégration, Réseaux S, Photo, Projection, Sono, Prière des STAR, Modération, Sainte Cène et Offrandes, Prédication, Coordination, Musiciens). Même rapprochement via `cle_nom()`.
- `Data_Calc` et le Tableau de bord agrègent les onglets mensuels avec des formules `QUERY({'Juillet 2026'!A2:D; 'Septembre 2026'!A2:D; ...})` qui listent les mois **en dur**.

### 3.4 Mois sans onglet (à partir de janvier 2027)
Créer l'onglet en **dupliquant** `Décembre 2026` (ou le dernier mois existant), le renommer, vider A2:D (valeurs seulement, garder fusions et validations) et O3:R7, remettre les numéros 1–5 en colonne A. Puis ajouter le nouvel onglet dans les formules `QUERY` de `Data_Calc` (colonnes A, D, G) et la ligne « Mois / Services » (J/K). Les graphiques du Tableau de bord ne sont pas mis à jour automatiquement : afficher un message pour Morel.

### 3.5 Accès Google (à faire par Morel, guider pas à pas)
1. Google Cloud Console → créer un projet → activer **Google Sheets API**.
2. Créer un **compte de service** → onglet Clés → « Ajouter une clé » → JSON.
3. Partager le Sheet avec l'e-mail du compte de service (`…@….iam.gserviceaccount.com`) en **Éditeur**.
4. Variables d'environnement (local `.env` + Vercel, type *sensitive*) :
   - `GOOGLE_SERVICE_ACCOUNT_JSON` = contenu complet du fichier JSON (une ligne)
   - `GOOGLE_SHEET_ID` = `1Vy4o04M5kTbKop58hpBDi4_yyAyC5-l1tn34O851kkw`
   - Si l'une manque, la synchro est simplement désactivée (aucune erreur visible pour l'utilisateur).

### 3.6 Implémentation
- Nouveau module `app/sheets_sync.py`. Dépendances : `google-auth` + `requests` (appels REST `spreadsheets.values.get/batchUpdate`, `spreadsheets.batchUpdate`, `spreadsheets.get` avec `fields=sheets(properties,merges)`), ou `gspread`. Ajouter à `requirements.txt` avec versions épinglées.
- Nouvelle table `SheetSync` : `culte_id` (unique), `empreinte` (hash du bloc A:D + effectifs tels qu'écrits par le site), `ecrit_le`, `statut` (`ok` / `deja_rempli` / `erreur`), `message`.
- Déclenchement : à la fin de `POST /stats/culte`, appel **synchrone** (Vercel peut geler la fonction après la réponse, pas de tâche de fond) avec un **timeout court (~8 s)**, dans un `try/except` : le pointage est **toujours** enregistré en base même si Google échoue ; en cas d'échec, message flash « Enregistré. Le Google Sheet n'a pas pu être mis à jour, réessayez depuis Stats. »
- Page Stats : bouton **« Synchroniser le Google Sheet »** (`POST /stats/sheet/synchroniser`) qui passe sur tous les cultes depuis le 01/11/2026 avec la règle 3.2, puis affiche un compte rendu (écrits / déjà remplis / erreurs).
- Lignes écrites dans l'ordre `Service.position` du site ; une ligne = `[département, prévu, servi]` ; A contient seulement le numéro du culte (déjà présent dans la cellule fusionnée).
- Tests : avec un **faux client** (pas d'appel réseau) : bloc vide → écrit ; bloc rempli à la main → non touché ; bloc écrit par le site puis pointage corrigé → réécrit ; effectif déjà saisi → conservé ; nom avec espace final → bien rapproché.
- **Avant la production** : tester sur une **copie** du Sheet (Fichier → Créer une copie, la partager au compte de service, mettre son ID dans `.env`).

---

## 4. Livraison
1. Tests `pytest` verts, test manuel en local (iPhone sur le même Wi-Fi : `--host 0.0.0.0`).
2. Résumé à Morel : ce qui a changé, ce qu'il doit faire (compte de service Google, variables Vercel).
3. **Déploiement seulement après son accord** : `vercel --prod`, puis vérifier `/healthz`, la page jour J et un pointage test (à supprimer ensuite).

---

## 5. Page Stats du site : même rendu que le « Tableau de Bord Annuel » du Sheet de test

Morel aime le rendu du Sheet de TEST (ID `1AoMpGp4GWjyDmcfRr32GWnbw7TsUky4J9sIdJB_rBzw`, onglet « Tableau de Bord Annuel » + onglets mensuels). **Lire ce Sheet via l'API (compte de service déjà partagé) avant de coder**, et reproduire sa structure sur la page `/stats` (vue « Année » en priorité, puis la vue « Mois »), en gardant le style du site (CSS maison, polices et couleurs du thème).

Structure à reproduire, de haut en bas :
1. **Titre** « Tableau de bord annuel 2026 ».
2. **Bilan annuel** : une rangée de 6 chiffres clés : Services prévus · Services assurés · Absences · Taux de présence · Effectif total · STAR actives.
3. **Top 10 des STAR** : barres horizontales, couleur vert sapin `#2E5D50`, sous-titre « Les 10 personnes qui ont servi le plus souvent depuis juillet. »
4. **Top 10 des absences** : barres horizontales, couleur terre cuite `#AF7C61`, sous-titre « Personnes prévues le plus souvent absentes ou remplacées depuis juillet. »
5. **Services prévus et effectués par mois** : colonnes groupées, clair `#A9C7B8` = prévus, foncé `#2E5D50` = assurés, sous-titre « Clair : services planifiés. Foncé : services réellement assurés. Un mois vide n'est pas encore rempli. »
6. **Une section par mois** (Septembre, Octobre, Novembre, Décembre…), titre en capitales « NOVEMBRE 2026 » :
   - 6 chiffres clés : Services prévus · Services assurés · Absences · Taux de présence · Effectif total · Moyenne par culte ;
   - deux graphiques côte à côte : « <Mois> : services effectués » (colonnes `#5B8B76`, « Nombre de fois où chaque STAR a servi ce mois-ci. ») et « <Mois> : services manqués » ;
   - (vue Mois) « Services par département » et le tableau des effectifs par culte (Hommes, Femmes, Ados, Enfants, Total).
- Graphiques en **SVG ou HTML dessinés côté serveur** (comme le radar existant dans `app/stats.py`), pas de librairie lourde ; lisibles sur iPhone (les barres horizontales s'empilent sous 600 px, étiquettes jamais tronquées de façon illisible).
- Les chiffres viennent de `stats.bilan()` (à compléter si besoin) : le site et le Sheet doivent afficher **les mêmes nombres** pour la même période (vérifier sur novembre).
- L'export PDF des stats suit le même ordre.

---

## 6. Synchronisation automatique dans les deux sens (site ⇄ Sheet)

Nouvelle demande : **ce qui est rempli d'un côté se remplit automatiquement de l'autre**, sans bouton.

### 6.1 Nouvelle règle (remplace la confirmation manuelle de l'import pour les modifications courantes)
- Le site et le Sheet sont des **miroirs** à partir du 1er novembre 2026. Pour un culte donné, **la dernière modification gagne**, d'où qu'elle vienne.
- Site → Sheet : déjà en place à l'enregistrement d'un pointage ; le bloc du culte est maintenant réécrit **même s'il avait été saisi à la main**, à condition que le site ait déjà importé la dernière version du Sheet (empreinte identique). Si le Sheet a changé depuis la dernière synchro et que le site n'a pas encore récupéré ce changement → on importe d'abord le Sheet, puis on affiche un message de conflit au lieu d'écraser (« Le Sheet a été modifié entre-temps : vérifiez le pointage avant d'enregistrer à nouveau »).
- Sheet → site : automatique (6.2). Une case vide dans le Sheet efface la valeur du site seulement si elle avait été remplie par le Sheet ; une ligne de service vidée dans le Sheet est supprimée du site.
- **Historique** : avant chaque écrasement (d'un côté ou de l'autre), enregistrer la version précédente du culte dans une table `CulteHistorique` (date, source « site » ou « sheet », JSON services + effectifs). Page Stats → « Historique de synchro » avec un bouton « Restaurer » par version. Rien ne doit pouvoir être perdu définitivement.
- Septembre et octobre ne sont jamais synchronisés.
- La page d'aperçu « Récupérer depuis le Google Sheet » reste disponible pour le rattrapage manuel.

### 6.2 Sheet → site en temps réel : Google Apps Script
- Dans le Sheet : Extensions → Apps Script, script `Synchro planning` avec une fonction `surModification(e)` liée à un **déclencheur installable « Lors de la modification »** (un déclencheur simple `onEdit` ne peut pas appeler d'URL).
- Elle ne réagit qu'aux onglets mensuels (« Novembre 2026 »…), colonnes A à D (services) et O à R lignes 3 à 7 (effectifs). Elle envoie `POST https://planning-ejp.vercel.app/api/sheet/modification` avec `{onglet, ligne, colonne}` et l'en-tête `X-Sheet-Secret: <SHEET_WEBHOOK_SECRET>` (secret stocké dans les Propriétés du script, jamais dans le code).
- Anti-rafale : `CacheService` pour ne pas envoyer plus d'un appel par bloc toutes les 5 s (l'appel suivant relit tout le bloc, rien n'est perdu), `muteHttpExceptions: true`.
- Les écritures faites par le site via l'API ne déclenchent pas le script : pas de boucle.
- Fournir le code du script dans `docs/apps-script/synchro.gs` + un mode d'emploi pas à pas pour l'installer (Morel le fera faire par Claude dans Chrome).
- Côté site : route `POST /api/sheet/modification` (sans session, protégée par `SHEET_WEBHOOK_SECRET` comparé en temps constant ; 401 sinon), qui retrouve le culte du bloc touché (rang du dimanche → date), relit le bloc et ses effectifs via l'API, et applique l'import automatiquement (règles 6.1 + historique). Réponse rapide (< 8 s), idempotente.
- Filet de sécurité : au chargement de la page Stats, si la dernière synchro Sheet → site date de plus de 10 min, relancer un import automatique des cultes du mois en cours (sans aperçu, avec historique).
- Nouvelle variable d'environnement : `SHEET_WEBHOOK_SECRET` (longue chaîne aléatoire, en local et sur Vercel, type sensitive).

### 6.3 Tests
Faux client + client HTTP de test FastAPI : modification Sheet → site ; modification site → Sheet après import ; conflit (Sheet modifié après la dernière synchro) → pas d'écrasement ; historique créé et restauration ; webhook sans secret → 401 ; septembre/octobre ignorés.
