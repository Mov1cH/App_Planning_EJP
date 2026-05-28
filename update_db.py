from app.database import engine
from sqlalchemy import text

def update_database():
    print("Démarrage de la mise à jour de la base de données...")
    requetes = [
        "ALTER TABLE eglises ADD COLUMN is_admin BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE eglises ADD COLUMN is_verified BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE eglises ADD COLUMN verification_code VARCHAR;",
        "ALTER TABLE eglises ADD COLUMN reset_token VARCHAR;"
    ]
    
    with engine.connect() as conn:
        for req in requetes:
            try:
                conn.execute(text(req))
                conn.commit()
                print(f"[SUCCÈS] Colonne ajoutée : {req.split('ADD COLUMN ')[1].split(' ')[0]}")
            except Exception as e:
                print(f"[INFO] Colonne déjà existante ou ignorée : {req.split('ADD COLUMN ')[1].split(' ')[0]}")
                conn.rollback()  # Réinitialiser la transaction en cas d'erreur

if __name__ == "__main__":
    update_database()