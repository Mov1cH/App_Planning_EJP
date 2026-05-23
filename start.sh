#!/bin/bash
# Initialiser la base de données (création des tables et des rôles par défaut)
python init_db.py

# Démarrer l'application avec Gunicorn et Uvicorn
exec gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
