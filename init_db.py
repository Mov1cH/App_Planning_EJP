from app.database import engine, SessionLocal
from app import models

# Créer les tables si elles n'existent pas
models.Base.metadata.create_all(bind=engine)

def init_roles():
    db = SessionLocal()
    roles_to_create = [
        'Prière des STAR',
        'Modération',
        'Sainte Cène & Offrandes',
        'Prédication ou Exhortation',
        'Témoignage'
    ]
    
    for role_name in roles_to_create:
        existing_role = db.query(models.Role).filter(models.Role.nom == role_name).first()
        if not existing_role:
            new_role = models.Role(nom=role_name)
            db.add(new_role)
            print(f"Rôle ajouté : {role_name}")
        else:
            print(f"Rôle déjà existant : {role_name}")
            
    db.commit()
    db.close()
    print("Initialisation des rôles terminée.")

if __name__ == "__main__":
    init_roles()
