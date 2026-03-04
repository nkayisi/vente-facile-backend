#!/bin/sh

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

exec gunicorn app.wsgi:application \
    --bind 0.0.0.0:8001 \
    --workers 3 \
    --worker-class gthread \
    --threads 4 \
    --timeout 60 \
    --log-level info