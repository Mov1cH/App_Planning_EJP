from sqlalchemy import Column, Integer, String, Date, ForeignKey, Table
from sqlalchemy.orm import relationship
from .database import Base

# Table de liaison pour la relation Many-to-Many entre Membres et Roles
membre_role_association = Table(
    "membre_role",
    Base.metadata,
    Column("membre_id", Integer, ForeignKey("membres.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True)
)

class Membre(Base):
    __tablename__ = "membres"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, index=True)
    prenom = Column(String, nullable=True, index=True)

    roles = relationship("Role", secondary=membre_role_association, back_populates="membres")
    indisponibilites = relationship("Indisponibilite", back_populates="membre")
    plannings = relationship("Planning", back_populates="membre")

class Role(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String, unique=True, index=True) # ex: Modération, Prédication

    membres = relationship("Membre", secondary=membre_role_association, back_populates="roles")
    besoins = relationship("BesoinDimanche", back_populates="role")
    plannings = relationship("Planning", back_populates="role")

class Indisponibilite(Base):
    __tablename__ = "indisponibilites"

    id = Column(Integer, primary_key=True, index=True)
    membre_id = Column(Integer, ForeignKey("membres.id"))
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    date = Column(Date, index=True)

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

    membre = relationship("Membre", back_populates="plannings")
    role = relationship("Role", back_populates="plannings")
