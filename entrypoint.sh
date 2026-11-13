#!/bin/sh
# Fail-loud : toute commande qui échoue (notamment `migrate`) stoppe le
# démarrage au lieu de lancer Gunicorn sur une base non migrée (ce qui produit
# des 500 silencieux du type « column ... does not exist »).
set -e

# ┌──────────────────────────────────────────────────────────────────────────┐
# │ CE SCRIPT IGNORAIT LA COMMANDE QU'ON LUI PASSAIT.                        │
# │                                                                          │
# │ `ENTRYPOINT ["/entrypoint.sh"]` reçoit la `command:` du compose en       │
# │ arguments, et l'ancien corps ne les lisait pas : il migrait puis lançait │
# │ Gunicorn quoi qu'on lui demande. Les services `celery_worker` et         │
# │ `celery_beat` de `docker-compose.yml` démarraient donc chacun UN SERVEUR │
# │ WEB DE PLUS, sur le même port, et aucun worker ni ordonnanceur : plus de │
# │ relance Moko, plus d'alerte de stock, plus de purge des journaux - sans  │
# │ une erreur, puisque chaque conteneur était « en bonne santé ».           │
# │                                                                          │
# │ Quatre modes, selon le premier argument :                                │
# │   web       (défaut)  migre, rassemble les statiques, lance Gunicorn     │
# │   gunicorn …          migre, rassemble, puis exécute CETTE commande      │
# │   celery …            attend que les migrations soient APPLIQUÉES, puis  │
# │                       exécute la commande                                │
# │   tout le reste       attend la base, puis exécute la commande, sans     │
# │                       migrer (`python manage.py migrate --plan`, un      │
# │                       shell, une commande de réparation…)                │
# │                                                                          │
# │ ⚠ SEUL LE SERVICE WEB MIGRE. Deux conteneurs qui migrent ensemble se     │
# │   disputent la table `django_migrations` ; et un worker qui démarre sur  │
# │   un schéma en retard lit des colonnes qui n'existent pas encore, puis   │
# │   meurt à sa première tâche. D'où l'attente de `celery`.                 │
# │                                                                          │
# │ ⚠ `gunicorn …` MIGRE AUSSI, et ce n'est pas une redondance : un          │
# │   déploiement qui passe sa propre ligne Gunicorn au conteneur web ne doit│
# │   pas, du jour au lendemain, démarrer sans migrer. C'est ce que l'ancien │
# │   script garantissait en ignorant la commande ; on garde la garantie,    │
# │   sans le défaut.                                                        │
# └──────────────────────────────────────────────────────────────────────────┘

attendre_la_base() {
  echo "🔎 Waiting for database..."
  # Attente PostgreSQL (variables venant de docker-compose)
  while ! nc -z "$DB_HOST" "$DB_PORT"; do
    sleep 1
  done
  echo "✅ Database is up!"
}

migrer_et_rassembler() {
  echo "📦 Applying migrations..."
  python manage.py migrate --noinput

  echo "📁 Collecting static files..."
  python manage.py collectstatic --noinput
}

# Attend que le service web ait appliqué TOUTES les migrations.
#
# `migrate --check` sort en erreur tant qu'une migration reste à appliquer
# (ou que la base ne répond pas) : on boucle dessus, sans jamais migrer soi-même.
# Chaque essai charge Django, d'où l'intervalle de cinq secondes.
#
# ⚠ AU-DELÀ DU DÉLAI, ON ÉCHOUE, ON NE DÉMARRE PAS QUAND MÊME. Un worker sur un
#   schéma en retard est pire qu'un worker arrêté : il prend les tâches, puis
#   lève sur chacune. La politique de redémarrage du conteneur réessaiera.
attendre_les_migrations() {
  delai="${MIGRATIONS_WAIT_TIMEOUT:-600}"
  ecoule=0
  echo "⏳ Waiting for migrations to be applied by the web service..."
  until python manage.py migrate --check >/dev/null 2>&1; do
    if [ "$ecoule" -ge "$delai" ]; then
      echo "❌ Migrations still pending after ${delai}s: refusing to start." >&2
      python manage.py migrate --plan >&2 || true
      exit 1
    fi
    sleep 5
    ecoule=$((ecoule + 5))
  done
  echo "✅ Schema is up to date."
}

lancer_gunicorn() {
  echo "🚀 Starting Gunicorn..."
  # Calibré pour un VPS 2 vCPU où tout cohabite (Postgres, Redis, Celery) :
  # 2 workers (≈ 1 par cœur) en gthread + 4 threads/worker pour absorber l'attente
  # I/O (DB/Redis) sans saturer le CPU. --max-requests recycle les workers pour
  # éviter l'accumulation mémoire. Surchargeable via les variables d'env GUNICORN_*.
  exec gunicorn app.wsgi:application \
      --bind 0.0.0.0:8001 \
      --workers "${GUNICORN_WORKERS:-2}" \
      --worker-class gthread \
      --threads "${GUNICORN_THREADS:-4}" \
      --timeout 60 \
      --graceful-timeout 30 \
      --max-requests 1000 \
      --max-requests-jitter 100 \
      --log-level info
}

mode="${1:-web}"

case "$mode" in
  web)
    attendre_la_base
    migrer_et_rassembler
    lancer_gunicorn
    ;;
  gunicorn)
    attendre_la_base
    migrer_et_rassembler
    echo "🚀 Starting: $*"
    exec "$@"
    ;;
  celery)
    attendre_la_base
    attendre_les_migrations
    echo "🚀 Starting: $*"
    exec "$@"
    ;;
  *)
    attendre_la_base
    exec "$@"
    ;;
esac
