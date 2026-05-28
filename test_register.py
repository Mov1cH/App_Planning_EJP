from app import models
from app.database import SessionLocal
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

db = SessionLocal()
email = "test@test.com"
nom_eglise = "Test Church"
mot_de_passe = "password123"

try:
    existant = db.query(models.Eglise).filter(models.Eglise.email == email).first()
    if existant:
        print("Email exists")
    else:
        nouvelle_eglise = models.Eglise(
            nom_eglise=nom_eglise,
            email=email,
            mot_de_passe_hash=pwd_context.hash(mot_de_passe)
        )
        db.add(nouvelle_eglise)
        db.commit()
        db.refresh(nouvelle_eglise)
        print("Eglise created with ID:", nouvelle_eglise.id)
except Exception as e:
    import traceback
    traceback.print_exc()
