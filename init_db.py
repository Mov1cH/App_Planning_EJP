"""Initialise la base : crée les tables, ajoute les colonnes manquantes et,
si ADMIN_EMAIL / ADMIN_PASSWORD sont définis, crée le compte administrateur.

Usage : python init_db.py   (aussi exécuté automatiquement au démarrage de l'app)
Ne modifie jamais le mot de passe d'un compte existant.
"""
from app.database import SessionLocal
from app import services

if __name__ == "__main__":
    services.migrer_schema()
    with SessionLocal() as db:
        services.assurer_admin(db)
    print("Base de données prête.")
