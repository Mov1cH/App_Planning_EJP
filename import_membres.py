from app.database import SessionLocal
from app import models

def import_membres():
    db = SessionLocal()
    
    # On demande l'email pour s'assurer d'importer sur le bon compte
    email_compte = input("Entrez l'adresse email de votre compte pour importer les membres : ")
    eglise = db.query(models.Eglise).filter(models.Eglise.email == email_compte).first()
    
    if not eglise:
        print(f"Aucun compte n'a été trouvé avec l'email '{email_compte}'.")
        db.close()
        return
        
    print(f"Importation des membres pour l'instance : {eglise.nom_eglise}")

    donnees_membres = {
        "Armel": ["Prière des STAR"],
        "Franck": ["Prière des STAR", "Modération", "Sainte Cène & Offrandes"],
        "Tily": ["Prière des STAR", "Modération", "Sainte Cène & Offrandes"],
        "Priscille": ["Prière des STAR", "Modération"],
        "Laetitia": ["Prière des STAR", "Modération"],
        "Orlyne": ["Prière des STAR", "Modération"],
        "Liesse": ["Prière des STAR", "Modération"],
        "Danielle": ["Prière des STAR", "Modération"],
        "Morel": ["Prière des STAR"],
        "Chancelvie": ["Prière des STAR"],
        "Tahira": ["Modération", "Sainte Cène & Offrandes"],
        "Esther": ["Modération", "Sainte Cène & Offrandes"],
        "Jérémie": ["Modération"],
        "Grace": ["Sainte Cène & Offrandes", "Prédication ou Exhortation"],
        "Débora": ["Prédication ou Exhortation"]
    }

    # On récupère tous les rôles de cette église pour les lier facilement
    roles_db = {r.nom: r for r in db.query(models.Role).filter(models.Role.eglise_id == eglise.id).all()}

    for nom, roles_noms in donnees_membres.items():
        membre = db.query(models.Membre).filter(models.Membre.nom == nom, models.Membre.eglise_id == eglise.id).first()
        if not membre:
            membre = models.Membre(nom=nom, eglise_id=eglise.id)
            db.add(membre)
            
        # Association des rôles
        membre.roles = [roles_db[role_nom] for role_nom in roles_noms if role_nom in roles_db]
        print(f"[SUCCÈS] Membre {nom} ajouté/mis à jour avec ses rôles.")

    db.commit()
    db.close()
    print("\nImportation terminée avec succès ! Vous pouvez vérifier sur l'interface.")

if __name__ == "__main__":
    import_membres()