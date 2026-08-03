#!/bin/sh
# Fail-loud : toute commande qui échoue (notamment `migrate`) stoppe le
# démarrage au lieu de lancer Gunicorn sur une base non migrée (ce qui produit
# des 500 silencieux du type « column ... does not exist »).
set -e

echo "🔎 Waiting for database..."

# Attente PostgreSQL (variables venant de docker-compose)
while ! nc -z $DB_HOST $DB_PORT; do
  sleep 1
done

echo "✅ Database is up!"

echo "📦 Applying migrations..."
python manage.py migrate --noinput

echo "📁 Collecting static files..."
python manage.py collectstatic --noinput

echo "🚀 Starting Gunicorn..."

# Calibré pour un VPS 2 vCPU où tout cohabite (Postgres, Redis, Celery) :
# 2 workers (≈ 1 par cœur) en gthread + 4 threads/worker pour absorber l'attente
# I/O (DB/Redis) sans saturer le CPU. --max-requests recycle les workers pour
# éviter l'accumulation mémoire. Surchargeable via les variables d'env GUNICORN_*.
exec gunicorn app.wsgi:application \
    --bind 0.0.0.0:8001 \
    --workers ${GUNICORN_WORKERS:-2} \
    --worker-class gthread \
    --threads ${GUNICORN_THREADS:-4} \
    --timeout 60 \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --log-level info