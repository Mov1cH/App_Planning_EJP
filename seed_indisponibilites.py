from app.database import SessionLocal
from app import models
from datetime import datetime

def seed_indisponibilites():
    db = SessionLocal()
    
    print("=== PEUPLEMENT DES INDISPONIBILITÉS (JUIN 2026) ===")
    # On demande l'identifiant de l'église (ID ou email pour plus de flexibilité)
    entree = input("Entrez l'ID de l'église (ou son email) : ")
    
    if entree.isdigit():
        eglise = db.query(models.Eglise).filter(models.Eglise.id == int(entree)).first()
    else:
        eglise = db.query(models.Eglise).filter(models.Eglise.email == entree).first()
        
    if not eglise:
        print(f"[ERREUR] Aucune église n'a été trouvée avec la valeur '{entree}'.")
        db.close()
        return
        
    print(f"\n[INFO] Ajout des indisponibilités pour : {eglise.nom_eglise} (ID: {eglise.id})\n")
    
    # Liste exacte des indisponibilités
    donnees = [
        # Dimanche 07/06/2026
        ("07/06/2026", "Grace", None),
        ("07/06/2026", "Laetitia", "Modération"),
        ("07/06/2026", "Liesse", None),
        ("07/06/2026", "Esther", None),
        ("07/06/2026", "Débora", None),
        ("07/06/2026", "Projection", None),
        ("07/06/2026", "Morel", None),
        ("07/06/2026", "Armel", None),
        ("07/06/2026", "Priscille", None),
        
        # Dimanche 14/06/2026
        ("14/06/2026", "Priscille", "Modération"),
        ("14/06/2026", "Priscille", None),
        ("14/06/2026", "Tahira", None),
        ("14/06/2026", "Liesse", None),
        ("14/06/2026", "Berger", None),
        ("14/06/2026", "Projection", None),
        
        # Dimanche 21/06/2026
        ("21/06/2026", "Grace", None),
        ("21/06/2026", "Berger", None),
        
        # Dimanche 28/06/2026
        ("28/06/2026", "Grace", None),
        ("28/06/2026", "Liesse", None),
        ("28/06/2026", "Berger", None),
        ("28/06/2026", "Projection", None),
    ]
    
    # Mise en cache des rôles pour une recherche rapide
    roles_cache = {r.nom: r.id for r in db.query(models.Role).filter(models.Role.eglise_id == eglise.id).all()}
    
    # Helper pour trouver ou créer un membre
    def get_or_create_membre(nom_membre):
        membre = db.query(models.Membre).filter(models.Membre.nom == nom_membre, models.Membre.eglise_id == eglise.id).first()
        if not membre:
            membre = models.Membre(nom=nom_membre, eglise_id=eglise.id)
            db.add(membre)
            db.flush() # Applique temporairement en base pour récupérer l'ID autogénéré
            print(f"  -> [NOUVEAU] Création du membre '{nom_membre}' car absent de la base.")
        return membre
        
    for date_str, nom_membre, nom_role in donnees:
        # 1. Convertir la chaîne en datetime.date
        date_obj = datetime.strptime(date_str, "%d/%m/%Y").date()
        
        # 2. Récupérer l'ID du membre (créé si inexistant comme demandé pour Projection et Berger)
        membre = get_or_create_membre(nom_membre)
        
        # 3. Définir le role_id ('Modération' ou None)
        role_id = roles_cache.get(nom_role) if nom_role else None
        
        # 4. Insérer l'indisponibilité
        indispo = models.Indisponibilite(
            membre_id=membre.id,
            date=date_obj,
            role_id=role_id,
            eglise_id=eglise.id
        )
        db.add(indispo)
        print(f"[SUCCÈS] Indisponibilité ajoutée : {nom_membre} | {date_str} | {nom_role or 'Toutes activités'}")
            
    # 5. Valider avec commit une seule fois à la fin pour plus de performance
    db.commit()
    db.close()
    print("\n[TERMINÉ] Toutes les indisponibilités ont été enregistrées avec succès !")

if __name__ == "__main__":
    seed_indisponibilites()