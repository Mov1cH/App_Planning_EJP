from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Table, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base

# Table de liaison pour la relation Many-to-Many entre Membres et Roles
membre_role_association = Table(
    "membre_role",
    Base.metadata,
    Column("membre_id", Integer, ForeignKey("membres.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True)
)

class Eglise(Base):
    __tablename__ = "eglises"

    id = Column(Integer, primary_key=True, index=True)
    nom_eglise = Column(String, index=True)
    email = Column(String, unique=True, index=True)
    mot_de_passe_hash = Column(String)
    is_admin = Column(Boolean, default=False)
    is_verified = Column(Boolean, default=False)
    verification_code = Column(String, nullable=True)
    reset_token = Column(String, nullable=True)
    couleur = Column(String, nullable=True, default="emeraude")
    organisation = Column(String, nullable=True, default="ejp")  # "ejp" ou "icc"

    membres = relationship("Membre", back_populates="eglise")
    indisponibilites = relationship("Indisponibilite", back_populates="eglise")
    plannings = relationship("Planning", back_populates="eglise")
    roles = relationship("Role", back_populates="eglise")

class Membre(Base):
    __tablename__ = "membres"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    prenom = Column(String, nullable=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"))

    eglise = relationship("Eglise", back_populates="membres")
    roles = relationship("Role", secondary=membre_role_association, back_populates="membres")
    indisponibilites = relationship("Indisponibilite", back_populates="membre")
    plannings = relationship("Planning", back_populates="membre")

class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True) # ex: Modération, Prédication
    quota = Column(Integer, default=1)
    ordre = Column(Integer, default=0)
    eglise_id = Column(Integer, ForeignKey("eglises.id"))

    eglise = relationship("Eglise", back_populates="roles")
    membres = relationship("Membre", secondary=membre_role_association, back_populates="roles")
    besoins = relationship("BesoinDimanche", back_populates="role")
    plannings = relationship("Planning", back_populates="role")

class Indisponibilite(Base):
    __tablename__ = "indisponibilites"

    id = Column(Integer, primary_key=True, index=True)
    membre_id = Column(Integer, ForeignKey("membres.id"))
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    date = Column(Date, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"))

    eglise = relationship("Eglise", back_populates="indisponibilites")
    membre = relationship("Membre", back_populates="indisponibilites")
    role = relationship("Role")

class BesoinDimanche(Base):
    __tablename__ = "besoins_dimanche"

    id = Column(Integer, primary_key=True, index=True)
    date_dimanche = Column(Date, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"))
    nombre_requis = Column(Integer, default=1)

    role = relationship("Role", back_populates="besoins")

class Planning(Base):
    __tablename__ = "plannings"

    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, index=True)
    membre_id = Column(Integer, ForeignKey("membres.id"))
    role_id = Column(Integer, ForeignKey("roles.id"))
    eglise_id = Column(Integer, ForeignKey("eglises.id"))

    eglise = relationship("Eglise", back_populates="plannings")
    membre = relationship("Membre", back_populates="plannings")
    role = relationship("Role", back_populates="plannings")


class Deroule(Base):
    """Déroulé du culte (programme horaire) d'un dimanche, exportable en image."""
    __tablename__ = "deroules"
    __table_args__ = (UniqueConstraint("eglise_id", "date", name="uq_deroule_eglise_date"),)

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    date = Column(Date, index=True)
    data = Column(Text)  # JSON : titre, sous-titre, couleur, étapes, photo
    updated_at = Column(DateTime)


class DerouleLive(Base):
    """État du direct (mode jour J) d'un déroulé — partagé par tous les écrans."""
    __tablename__ = "deroules_live"
    __table_args__ = (UniqueConstraint("eglise_id", "date", name="uq_deroule_live_eglise_date"),)

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    date = Column(Date, index=True)
    etapes = Column(Text)  # JSON : copie figée des étapes au démarrage
    index_courant = Column(Integer, nullable=True)
    demarre_a = Column(DateTime, nullable=True)
    termine_a = Column(DateTime, nullable=True)
    en_pause_depuis = Column(DateTime, nullable=True)
    reel = Column(Text)  # JSON : par étape {debut, fin, pause_ms}
    version = Column(Integer, default=0)


class PropositionPlanning(Base):
    """Nouvelle combinaison proposée pour un mois, en attente de validation."""
    __tablename__ = "propositions_planning"
    __table_args__ = (UniqueConstraint("eglise_id", "annee", "mois", name="uq_proposition_eglise_mois"),)

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    annee = Column(Integer)
    mois = Column(Integer)
    data = Column(Text)  # JSON : [[date ISO, role_id, membre_id], ...]
    created_at = Column(DateTime)


# ---------------------------------------------------------------------------
# Suivi des cultes (statistiques)
# ---------------------------------------------------------------------------
class Departement(Base):
    """Département suivi dans les statistiques (liste propre, indépendante des rôles)."""
    __tablename__ = "departements"

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    nom = Column(String)
    ordre = Column(Integer, default=0)


class Culte(Base):
    """Un culte pointé : effectif présent et services rendus."""
    __tablename__ = "cultes"
    __table_args__ = (UniqueConstraint("eglise_id", "date", name="uq_culte_eglise_date"),)

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    date = Column(Date, index=True)
    hommes = Column(Integer, nullable=True)
    femmes = Column(Integer, nullable=True)
    ados = Column(Integer, nullable=True)
    enfants = Column(Integer, nullable=True)
    updated_at = Column(DateTime)

    services = relationship("Service", back_populates="culte", cascade="all, delete-orphan",
                            order_by="Service.position")


class Service(Base):
    """Une ligne du pointage : département, personne prévue, personne ayant servi."""
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    culte_id = Column(Integer, ForeignKey("cultes.id"), index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    departement = Column(String)  # nom conservé tel quel pour l'historique
    prevu = Column(String, default="")
    servi = Column(String, default="")
    position = Column(Integer, default=0)

    culte = relationship("Culte", back_populates="services")


class Star(Base):
    """Serviteur (STAR) suivi dans les statistiques — liste distincte des membres du planning."""
    __tablename__ = "stars"
    __table_args__ = (UniqueConstraint("eglise_id", "nom", name="uq_star_eglise_nom"),)

    id = Column(Integer, primary_key=True, index=True)
    eglise_id = Column(Integer, ForeignKey("eglises.id"), index=True)
    nom = Column(String)
