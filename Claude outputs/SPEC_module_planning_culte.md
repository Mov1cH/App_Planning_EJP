# Module "Planning du culte" — spécification pour intégration dans l'outil de gestion d'église

## Contexte

Ce module existe déjà en version autonome ("App_Planning_EJP", FastAPI + SQLAlchemy + Jinja2 + SQLite/Postgres). Il doit être **repris et intégré comme un outil parmi d'autres** dans une plateforme de gestion d'église plus large, destinée en particulier au serviteur responsable de la **coordination**, pour préparer le culte à l'avance (rôles, disponibilités, planning du mois, déroulé du jour, statistiques de présence).

Ce document décrit le **comportement fonctionnel** à reproduire, pas le code existant ligne à ligne. Adapte l'implémentation aux conventions techniques du site cible (son framework, sa base de données, son système d'auth, son design).

## Objectif du module

Permettre à un coordinateur de :
1. Définir les **rôles** nécessaires à un culte (ex : Modération, Prédication, Sainte Cène, Prière des STAR, Témoignage) et combien de personnes chaque rôle requiert.
2. Lister les **membres/serviteurs** et leur attribuer un ou plusieurs rôles qu'ils sont habilités à tenir.
3. Enregistrer les **indisponibilités** des membres (ponctuelles, par date, éventuellement limitées à un rôle précis).
4. **Générer automatiquement** un planning mensuel (un dimanche = une ligne, un rôle par colonne) qui répartit équitablement la charge entre les membres habilités, en respectant les indisponibilités.
5. **Ajuster manuellement** le planning généré (remplacer un nom par un autre dans une case).
6. Préparer le **déroulé détaillé** d'un culte donné (programme horaire : étapes, intitulés, durée, responsable).
7. Exporter le planning et les déroulés (PDF / Excel / image) pour affichage ou partage (ex. WhatsApp).
8. Suivre des **statistiques de présence** (pointage) : effectif homme/femme par culte, qui était prévu vs qui a réellement servi, par département.

## Modèle de données (à adapter au schéma existant du site)

Toutes les entités ci-dessous sont **rattachées à une église/organisation** (multi-tenant : chaque église ne voit que ses propres données). Si le site cible a déjà une notion d'organisation/église/tenant, relier ce module à cette entité existante plutôt que d'en recréer une.

- **Membre** : nom, prénom, rôles habilités (relation many-to-many avec Rôle).
- **Rôle** : nom (ex. "Modération"), quota (nombre de personnes requises par occurrence), ordre d'affichage.
- **Indisponibilité** : membre, date, rôle optionnel (si vide = indisponible pour tous les rôles ce jour-là).
- **Planning** (affectation validée) : date (dimanche), rôle, membre.
- **PropositionPlanning** : combinaison alternative calculée pour un mois donné, en attente de validation par le coordinateur (pas encore écrite dans le planning officiel).
- **Déroulé** : par date de culte — titre, sous-titre, couleur du thème, photo, liste d'étapes (intitulé, horaire/durée, responsable).
- **Département** (pour les stats) : liste de départements suivis (ex. Louange, Accueil, Sécurité…), indépendante des rôles du planning.
- **Culte (pointage)** : date, nombre d'hommes présents, nombre de femmes présentes.
- **Service (ligne de pointage)** : rattaché à un Culte + un Département, avec "personne prévue" et "personne ayant effectivement servi".
- **Star** (serviteur suivi en stats) : liste de noms suivis dans le pointage, distincte de la liste des Membres du planning (permet de suivre des gens qui ne sont pas forcément dans le planning officiel).

## Fonctionnalités détaillées

### 1. Gestion des rôles
- CRUD rôle : nom, quota (défaut 1), ordre.
- Suppression d'un rôle : détacher proprement les membres et affectations liées (pas de suppression en cascade destructrice des membres).
- Rôles par défaut suggérés à la création d'une église : Prière des STAR, Modération, Sainte Cène & Offrandes, Prédication ou Exhortation, Témoignage (liste modifiable, juste un pré-remplissage).

### 2. Gestion des membres
- CRUD membre : nom, prénom.
- Association à un ou plusieurs rôles via cases à cocher / sélection multiple.

### 3. Indisponibilités
- CRUD indisponibilité : membre + date + rôle (optionnel).
- Actions de nettoyage en masse : "supprimer les indisponibilités passées", "tout supprimer".
- Une indisponibilité sans rôle s'applique à tous les rôles ce jour-là ; avec un rôle, elle ne bloque que ce rôle précis (le membre reste disponible sur ses autres rôles ce même jour).

### 4. Génération automatique du planning (le cœur du module)
Algorithme d'affectation pour un mois donné (répété pour chaque dimanche du mois, dans l'ordre des rôles) :

1. Pour chaque dimanche, pour chaque rôle (dans l'ordre d'affichage) :
   - Exclure les membres déjà affectés ce même dimanche sur un autre rôle (**une personne = un seul rôle par dimanche**).
   - Exclure les membres indisponibles ce jour-là (indisponibilité générale ou spécifique à ce rôle).
   - Parmi les candidats restants (membres habilités pour ce rôle), trier par :
     1. nombre de fois déjà affecté ce mois-ci (ordre croissant → priorité à qui a le moins servi) ;
     2. date de dernière affectation sur CE rôle (priorité à qui ne l'a pas fait depuis le plus longtemps) ;
     3. date de dernière affectation tous rôles confondus ;
     4. aléatoire pour départager les ex æquo.
   - Affecter les N premiers candidats où N = quota du rôle. S'il manque des candidats, compter une "place non pourvue" et continuer (ne jamais bloquer toute la génération pour un seul trou).
2. L'historique utilisé pour le tri (dernière affectation) remonte sur tous les mois précédents déjà enregistrés, pas seulement le mois en cours — pour une équité qui se maintient dans le temps.
3. **Génération initiale** : si le mois est vide, générer directement et enregistrer.
4. **Proposition alternative** ("varier la combinaison") : si un planning existe déjà pour le mois, calculer une combinaison différente (jusqu'à ~40 tentatives pour obtenir un résultat distinct du planning actuel et de la proposition précédente) sans l'écraser immédiatement — elle est stockée à part et le coordinateur doit explicitement la **valider** (remplace le planning officiel) ou l'**annuler** (la rejette).
5. Possibilité de **réinitialiser** (vider) le planning d'un mois pour le régénérer de zéro.

### 5. Modification manuelle du planning
- Vue tableau : lignes = dimanches du mois, colonnes = rôles (répétées si quota > 1).
- Édition case par case : remplacer le membre affecté par un autre membre habilité pour ce rôle (liste déroulante filtrée par rôle), ou laisser vide.
- Navigation mois précédent / mois suivant.

### 6. Déroulé du culte
- Par dimanche : définir un programme horaire structuré en étapes (ex. "Louange — 15 min — resp. Jean"), avec titre, sous-titre, couleur de thème et photo illustrative facultative.
- Export en image (pour partage rapide, ex. sur un groupe WhatsApp) et consultation en lecture.

### 7. Exports
- Planning du mois : export PDF et Excel (mise en page tableau, une ligne par dimanche).
- Statistiques : export PDF.
- Nom de fichier généré automatiquement à partir du mois/année concerné.

### 8. Statistiques / pointage de présence
- Pointage par culte (date) : nombre d'hommes et de femmes présents.
- Par département (liste configurable, avec ordre et renommage) : qui était prévu, qui a effectivement servi — permet de repérer les écarts planning vs réalité.
- Gestion d'une liste de "STAR" (serviteurs suivis), avec import rapide depuis la liste des membres existants.
- Vue d'ensemble des statistiques + export PDF.

## Points d'intégration avec le site cible

- **Authentification / multi-tenant** : réutiliser le système de compte et d'organisation déjà existant sur le site de gestion d'église plutôt que de recréer un système de login séparé (dans l'original, chaque "Église" est un compte avec son propre email/mot de passe — à remplacer par le rattachement au compte/église déjà présent sur la plateforme cible).
- **Permissions** : ce module doit être accessible au rôle "coordination" (et aux rôles admin/pasteur si pertinent) ; les membres simples n'ont pas besoin d'y accéder en édition.
- **Navigation** : l'intégrer comme une section/outil dans le menu existant de l'outil de gestion (pas une appli séparée).
- **Design** : reprendre la charte graphique du site cible plutôt que celle de l'app d'origine.
- Les notions de "Membre" et "Rôle" de ce module peuvent potentiellement être fusionnées avec des entités équivalentes déjà existantes sur le site (ex. si le site a déjà une liste de membres d'église ou de groupes de service) — à évaluer au moment de l'implémentation pour éviter les doublons de données.

## Ce qui n'est volontairement pas dans le scope

- Pas de notifications automatiques (SMS/email) aux serviteurs planifiés — sauf si le site cible le souhaite en v2.
- Pas de gestion de plusieurs cultes dans la même semaine (l'original est pensé "un dimanche = un créneau") — à étendre si le site cible a plusieurs services par semaine.
