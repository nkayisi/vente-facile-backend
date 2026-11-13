#!/bin/sh
# Répéter les migrations d'une version NEUVE sur une COPIE de la base de production.
#
# ┌──────────────────────────────────────────────────────────────────────────┐
# │ LE 9 NOVEMBRE, `settings/0016` A ÉCHOUÉ EN PRODUCTION, ET LA SUITE DE    │
# │ TESTS ÉTAIT VERTE.                                                       │
# │                                                                          │
# │ Une base de test est vide : un `RunPython` y tourne sur zéro ligne. Seule│
# │ la vraie base, avec ses vrais programmes de fidélité, faisait lever la   │
# │ migration - et elle l'a fait au déploiement, serveur arrêté, personne    │
# │ ne pouvant plus se connecter. Ce script rejoue ce moment-là AVANT, sur   │
# │ une copie, avec l'image qu'on s'apprête à déployer.                      │
# └──────────────────────────────────────────────────────────────────────────┘
#
# Usage, sur le serveur, depuis `backend/` :
#
#   docker build -t vf-backend:candidat .
#   IMAGE=vf-backend:candidat sh scripts/repetition_migrations.sh
#
# Variables (toutes facultatives sauf IMAGE) :
#   IMAGE            l'image NEUVE, construite mais pas encore déployée
#   DB_CONTAINER     le conteneur PostgreSQL         (défaut : vente_facile_db)
#   RESEAU           son réseau Docker                (défaut : le premier du conteneur)
#   DB_HOST_COPIE    le nom de la base sur ce réseau  (défaut : db)
#   ENV_FILE         le fichier d'environnement       (défaut : .env)
#   EXTRA_RUN_ARGS   arguments de plus pour `docker run` (montage, variables…)
#
# Ce que le script fait, dans l'ordre, et ce qu'il NE fait PAS :
#   1. copie la base (`pg_dump`), sans la verrouiller : la production continue ;
#   2. la restaure sous un nom neuf, `vf_repetition_<horodatage>`, sur le MÊME
#      serveur PostgreSQL (prévoir l'espace disque d'une seconde base) ;
#   3. montre les migrations qui vont s'appliquer (`migrate --plan`) ;
#   4. les applique sur la COPIE, avec l'image neuve ;
#   5. lance `verifier_deploiement` sur la copie : administrateurs intacts,
#      tâches planifiées, plus aucune migration en attente ;
#   6. supprime la copie et le fichier de sauvegarde, quoi qu'il arrive.
#
#   Il ne touche JAMAIS la base de production : chaque commande de l'image vise
#   `DB_NAME=<la copie>`. Il ne démarre ni Gunicorn ni Celery : `entrypoint.sh`
#   exécute tel quel tout ce qui n'est ni `web`, ni `gunicorn`, ni `celery`.
#
# ⚠ L'IMAGE DOIT PORTER LE NOUVEL `entrypoint.sh`. L'ancien ignorait la commande
#   et lançait Gunicorn sur la base qu'on lui désignait : ici, sur la copie,
#   le script resterait bloqué au lieu de migrer.
set -eu

# ⚠ AUCUNE APOSTROPHE dans le message de `${IMAGE:?…}` : le `bash` de macOS
#   l'y lit comme une ouverture de chaîne, et tout le script se décale - l'erreur
#   tombe alors quarante lignes plus bas, sur une ligne parfaitement saine.
: "${IMAGE:?IMAGE manque : donnez la version neuve à éprouver (docker build -t vf-backend:candidat .)}"
DB_CONTAINER="${DB_CONTAINER:-vente_facile_db}"
ENV_FILE="${ENV_FILE:-.env}"
DB_HOST_COPIE="${DB_HOST_COPIE:-db}"
EXTRA_RUN_ARGS="${EXTRA_RUN_ARGS:-}"

if [ -z "${RESEAU:-}" ]; then
  # L'identifiant du premier réseau du conteneur : `--network` l'accepte comme
  # un nom, et le gabarit n'a besoin d'aucune variable.
  RESEAU="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{end}}' "$DB_CONTAINER" | awk '{print $1}')"
fi
[ -n "$RESEAU" ] || { echo "❌ Réseau introuvable pour $DB_CONTAINER : passez RESEAU=…" >&2; exit 2; }
[ -f "$ENV_FILE" ] || { echo "❌ $ENV_FILE introuvable : passez ENV_FILE=…" >&2; exit 2; }

PGUSER="$(docker exec "$DB_CONTAINER" printenv POSTGRES_USER)"
SOURCE="$(docker exec "$DB_CONTAINER" printenv POSTGRES_DB)"
COPIE="vf_repetition_$(date +%Y%m%d%H%M%S)"
DUMP="$(mktemp "${TMPDIR:-/tmp}/vf_repetition.XXXXXX")"

nettoyer() {
  docker exec "$DB_CONTAINER" dropdb -U "$PGUSER" --if-exists "$COPIE" >/dev/null 2>&1 || true
  rm -f "$DUMP"
}
trap nettoyer EXIT INT TERM

# ⚠ `DB_NAME` désigne la COPIE, et c'est ce qui rend tout le reste sûr. Les
#   autres variables (Redis, Moko, courriel) viennent du fichier : aucune
#   migration n'enfile de tâche ni n'envoie de message, et
#   `verifier_deploiement` n'écrit rien.
lancer() {
  # shellcheck disable=SC2086 - EXTRA_RUN_ARGS est volontairement découpé.
  docker run --rm --network "$RESEAU" --env-file "$ENV_FILE" \
    -e DB_HOST="$DB_HOST_COPIE" -e DB_PORT=5432 -e DB_NAME="$COPIE" \
    $EXTRA_RUN_ARGS "$IMAGE" "$@"
}

echo "1/5 Copie de « $SOURCE » ($DB_CONTAINER)…"
docker exec "$DB_CONTAINER" pg_dump -U "$PGUSER" -Fc "$SOURCE" > "$DUMP"
echo "    $(du -h "$DUMP" | cut -f1) copiés."

echo "2/5 Restauration dans « $COPIE »…"
docker exec "$DB_CONTAINER" createdb -U "$PGUSER" "$COPIE"
docker exec -i "$DB_CONTAINER" pg_restore -U "$PGUSER" -d "$COPIE" --no-owner --no-privileges < "$DUMP"

echo "3/5 Migrations qui vont s'appliquer :"
lancer python manage.py migrate --plan

echo "4/5 Application sur la copie…"
lancer python manage.py migrate --noinput

echo "5/5 Vérification de la copie migrée…"
lancer python manage.py verifier_deploiement

echo
echo "✅ Répétition réussie : la copie de « $SOURCE » migre et reste conforme."
echo "   Ordre de déploiement : backend d'abord, puis le back-office web."
echo "   Juste après : docker exec <conteneur backend> python manage.py verifier_deploiement"
