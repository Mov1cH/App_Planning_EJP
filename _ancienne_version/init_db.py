from app.database import engine, SessionLocal
from app import models
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Créer les tables si elles n'existent pas
models.Base.metadata.create_all(bind=engine)

def init_db():
    db = SessionLocal()
    
    # 1. Création du compte par défaut pour EJP ST Etienne
    email_defaut = "admin@ejp-stetienne.fr"
    mdp_defaut = "admin123"
    nom_eglise = "EJP ST Etienne"
    
    eglise = db.query(models.Eglise).filter(models.Eglise.email == email_defaut).first()
    if not eglise:
        eglise = models.Eglise(
            nom_eglise=nom_eglise,
            email=email_defaut,
            mot_de_passe_hash=pwd_context.hash(mdp_defaut),
            is_admin=True,
            is_verified=True
        )
        db.add(eglise)
        db.commit()
        db.refresh(eglise)
        print(f"Compte par défaut créé avec succès !\nEmail : {email_defaut}\nMot de passe : {mdp_defaut}")
    else:
        eglise.mot_de_passe_hash = pwd_context.hash(mdp_defaut)
        eglise.is_verified = True
        db.commit()
        print(f"Le compte ({email_defaut}) existait déjà. Son mot de passe a été forcé à : {mdp_defaut}")

    # 2. Initialisation des rôles pour cette église
    roles_to_create = [
        'Prière des STAR',
        'Modération',
        'Sainte Cène & Offrandes',
        'Prédication ou Exhortation',
        'Témoignage'
    ]
    
    for role_name in roles_to_create:
        existing_role = db.query(models.Role).filter(models.Role.nom == role_name, models.Role.eglise_id == eglise.id).first()
        if not existing_role:
            new_role = models.Role(nom=role_name, eglise_id=eglise.id)
            db.add(new_role)
            print(f"Rôle ajouté pour {nom_eglise} : {role_name}")
        else:
            print(f"Rôle déjà existant pour {nom_eglise} : {role_name}")
            
    db.commit()
    db.close()
    print("Initialisation de la base de données terminée.")

if __name__ == "__main__":
    init_db()
