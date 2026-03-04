# Utiliser Python 3.12
FROM python:3.12-slim

# Variables d'environnement
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Installer dépendances système
RUN apt-get update && apt-get install -y build-essential libpq-dev

# Créer un dossier de travail
WORKDIR /app

# Copier requirements.txt
COPY requirements.txt .

# Installer les packages Python
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copier le code
COPY . .

# Exposer le port
EXPOSE 8001

# Commande par défaut (sera overridée par docker-compose.yml)
CMD ["gunicorn", "app.wsgi:application", "--bind", "0.0.0.0:8001", "--workers", "3"]