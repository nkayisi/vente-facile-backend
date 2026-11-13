# ---------- Base image ----------
FROM python:3.12-slim

# ---------- Environment ----------
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ---------- Workdir ----------
WORKDIR /app

# ---------- System dependencies ----------
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# ---------- Python dependencies ----------
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Copy project ----------
COPY . .

# ---------- Create needed directories ----------
RUN mkdir -p /app/media /app/staticfiles

# ---------- Entrypoint ----------
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ---------- Expose ----------
EXPOSE 8001

ENTRYPOINT ["/entrypoint.sh"]

# Le mode par défaut. `entrypoint.sh` lit son premier argument : `web` migre et
# lance Gunicorn, `celery …` attend que le schéma soit à jour puis exécute la
# commande, tout le reste est exécuté tel quel. Un `command:` de compose
# remplace cette valeur, et c'est désormais ainsi que démarrent les workers.
CMD ["web"]