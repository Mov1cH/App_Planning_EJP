from app.database import SessionLocal
from app import models
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def create_admin():
    print("=== Création d'un compte Super Administrateur ===")
    email = input("Entrez votre email administrateur : ")
    password = input("Entrez un mot de passe sécurisé : ")
    nom_eglise = input("Nom de votre instance (ex: Admin Global) : ")

    db = SessionLocal()
    
    existant = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if existant:
        existant.is_admin = True
        existant.mot_de_passe_hash = pwd_context.hash(password)
        print(f"\n[SUCCÈS] Le compte existant '{email}' a été mis à niveau en Administrateur.")
    else:
        admin = models.Eglise(
            nom_eglise=nom_eglise,
            email=email,
            mot_de_passe_hash=pwd_context.hash(password),
            is_admin=True
        )
        db.add(admin)
        print(f"\n[SUCCÈS] Le compte administrateur '{email}' a été créé avec succès !")
        
    db.commit()
    db.close()

if __name__ == "__main__":
    create_admin()