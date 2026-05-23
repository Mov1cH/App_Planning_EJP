import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Récupération de l'URL de la base de données depuis les variables d'environnement (Render)
# Sinon, on utilise SQLite en local
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./planning_ejp.db")

# SQLAlchemy 1.4+ exige "postgresql://" au lieu de "postgres://"
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

# connect_args={"check_same_thread": False} n'est nécessaire que pour SQLite
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(SQLALCHEMY_DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# Dépendance pour obtenir la session de base de données dans les routes
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
