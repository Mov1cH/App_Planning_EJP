# Planning EJP

Application web de planification des services du culte : membres, rôles, indisponibilités
et génération automatique d'un planning mensuel équitable (export PDF / Excel).

Stack : FastAPI · SQLAlchemy · Jinja2 · CSS maison (aucun framework front) · SQLite en local, Postgres en production.

## Lancer en local

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # puis remplir SECRET_KEY (et ADMIN_EMAIL / ADMIN_PASSWORD si base vide)
python -m uvicorn main:app --reload
# → http://localhost:8000   (téléphone sur le même Wi-Fi : --host 0.0.0.0)
```

La base `planning_ejp.db` (SQLite) est créée/mise à jour automatiquement au démarrage.

## Structure

```
main.py              routes (pages et formulaires)
app/config.py        configuration (variables d'environnement)
app/security.py      mots de passe, jetons, anti-bruteforce
app/services.py      dimanches, matrice du planning, algorithme de génération, migrations
app/exports.py       PDF et CSV
app/templates/       pages HTML (Jinja2)
static/              CSS, JS, logo, icônes PWA
```

## Déployer (Render + base Postgres gratuite)

1. **Base de données** : créer un projet gratuit sur [Supabase](https://supabase.com) (ou Neon),
   puis copier l'URL de connexion « Session pooler » (`postgresql://…`).
2. **Reprendre les données actuelles** (facultatif) :
   `python migrer_vers_postgres.py "postgresql://…"`
3. **Code sur GitHub** : pousser ce dossier dans un dépôt (le `.gitignore` exclut la base locale et `.env`).
4. **Render** → *New* → *Blueprint* → choisir le dépôt : `render.yaml` configure tout.
   Renseigner ensuite dans *Environment* : `DATABASE_URL`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
   `PUBLIC_URL` et, pour le mot de passe oublié, `EMAIL_ADDRESS` / `EMAIL_PASSWORD`
   (Gmail : « mot de passe d'application »).

`SECRET_KEY` est générée automatiquement par Render. Le compte admin n'est créé que s'il n'existe pas ;
son mot de passe n'est jamais écrasé au redémarrage.

> ⚠️ Sur Render gratuit, le disque est effacé à chaque redéploiement : n'utilisez **pas** SQLite en production.

## Sécurité

- Aucun secret dans le code : tout passe par les variables d'environnement.
- Cookies de session signés, `HttpOnly`, `SameSite=Lax`, `Secure` en production.
- Formulaires venant d'un autre site refusés (contrôle de l'origine).
- Limitation des tentatives de connexion et d'envoi d'e-mails.
- Liens de réinitialisation signés, valables 1 h et à usage unique.
- Chaque église ne voit et ne modifie que ses propres données.
