"""Copie toutes les données de la base SQLite locale vers une base Postgres.

Usage :
    python migrer_vers_postgres.py "postgresql://user:motdepasse@hote:5432/postgres"

La base cible doit être vide (elle est créée si besoin). Les identifiants sont conservés.
"""
import sys

from sqlalchemy import create_engine, func, select, text

from app import models
from app.config import BASE_DIR

TABLES = ["eglises", "roles", "membres", "membre_role", "indisponibilites", "besoins_dimanche", "plannings"]


def main(cible: str) -> None:
    if cible.startswith("postgres://"):
        cible = cible.replace("postgres://", "postgresql://", 1)
    source = create_engine(f"sqlite:///{BASE_DIR / 'planning_ejp.db'}")
    dest = create_engine(cible)

    models.Base.metadata.create_all(bind=dest)
    meta = models.Base.metadata

    with source.connect() as src, dest.begin() as dst:
        for nom in TABLES:
            table = meta.tables[nom]
            if dst.execute(select(func.count()).select_from(table)).scalar():
                sys.exit(f"La table « {nom} » de la base cible n'est pas vide : migration annulée.")
            colonnes_src = {row[1] for row in src.execute(text(f"PRAGMA table_info({nom})"))}
            cols = [c for c in table.c if c.name in colonnes_src]
            lignes = [dict(r._mapping) for r in src.execute(select(*cols))] if cols else []
            if lignes:
                dst.execute(table.insert(), lignes)
            print(f"{nom:<20} {len(lignes):>5} ligne(s)")

        # Recaler les séquences d'identifiants Postgres
        for nom in TABLES if dest.dialect.name == "postgresql" else []:
            if "id" in meta.tables[nom].c:
                dst.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{nom}', 'id'), COALESCE((SELECT MAX(id) FROM {nom}), 0) + 1, false)"
                ))
    print("Migration terminée.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
