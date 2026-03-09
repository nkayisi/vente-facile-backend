# Redis & Celery - Configuration et Utilisation

## 📋 Vue d'ensemble

Ce projet utilise **Redis** et **Celery** pour gérer les tâches asynchrones :
- **Redis** : Message broker et cache
- **Celery Worker** : Exécute les tâches asynchrones
- **Celery Beat** : Planificateur de tâches périodiques

## 🚀 Démarrage avec Docker

### Lancer tous les services

```bash
docker compose up -d
```

Cela démarre :
- ✅ PostgreSQL (base de données)
- ✅ Redis (message broker)
- ✅ Backend Django (API)
- ✅ Celery Worker (tâches asynchrones)
- ✅ Celery Beat (tâches planifiées)

### Vérifier l'état des services

```bash
docker compose ps
```

### Voir les logs

```bash
# Tous les services
docker compose logs -f

# Celery Worker uniquement
docker compose logs -f celery_worker

# Celery Beat uniquement
docker compose logs -f celery_beat

# Redis uniquement
docker compose logs -f redis
```

## 📦 Services configurés

### 1. Redis
- **Port** : 6379
- **Base 0** : Celery broker
- **Base 1** : Cache Django (production)
- **Volume** : `redis_data` (persistance)

### 2. Celery Worker
- **Commande** : `celery -A app worker --loglevel=info`
- **Rôle** : Exécute les tâches asynchrones
- **Dépendances** : PostgreSQL + Redis

### 3. Celery Beat
- **Commande** : `celery -A app beat --loglevel=info --scheduler django_celery_beat.schedulers:DatabaseScheduler`
- **Rôle** : Planifie et déclenche les tâches périodiques
- **Scheduler** : Base de données (via django-celery-beat)
- **Dépendances** : PostgreSQL + Redis + Backend

## 🔧 Tâches asynchrones disponibles

### Tâches planifiées (Celery Beat)

Configurées dans l'admin Django (`/admin/django_celery_beat/`) :

1. **check_low_stock_alerts**
   - Vérifie les produits avec stock bas
   - Crée des alertes automatiques
   - Recommandé : Toutes les heures

2. **check_expiring_products**
   - Vérifie les produits proches de la date d'expiration
   - Alerte 30 jours avant expiration
   - Recommandé : Une fois par jour

3. **check_subscription_expiry**
   - Vérifie les abonnements qui expirent bientôt
   - Alerte 7 jours avant expiration
   - Recommandé : Une fois par jour

4. **send_daily_sales_report**
   - Envoie un résumé des ventes aux admins
   - Recommandé : Tous les jours à 8h

5. **cleanup_old_notifications**
   - Supprime les notifications lues de plus de 30 jours
   - Recommandé : Une fois par semaine

### Tâches manuelles

```python
from apps.notifications.tasks import send_email_notification

# Envoyer un email pour une notification
send_email_notification.delay(notification_id)
```

## 🛠️ Commandes utiles

### Tester une tâche manuellement

```bash
# Depuis le conteneur backend
docker compose exec backend python manage.py shell

# Dans le shell Python
from apps.notifications.tasks import check_low_stock_alerts
check_low_stock_alerts.delay()
```

### Monitorer Celery

```bash
# Voir les workers actifs
docker compose exec celery_worker celery -A app inspect active

# Voir les tâches planifiées
docker compose exec celery_worker celery -A app inspect scheduled

# Statistiques
docker compose exec celery_worker celery -A app inspect stats
```

### Vider la queue Redis

```bash
docker compose exec redis redis-cli FLUSHDB
```

## 📊 Configuration des tâches périodiques

1. Accéder à l'admin Django : `http://localhost:8001/admin`
2. Aller dans **Periodic Tasks** (django-celery-beat)
3. Cliquer sur **Add Periodic Task**
4. Configurer :
   - **Name** : Nom descriptif
   - **Task** : Sélectionner la tâche (ex: `apps.notifications.tasks.check_low_stock_alerts`)
   - **Interval** : Créer un intervalle (ex: toutes les heures)
   - **Enabled** : Cocher pour activer

## 🔍 Dépannage

### Celery Worker ne démarre pas

```bash
# Vérifier les logs
docker compose logs celery_worker

# Redémarrer le worker
docker compose restart celery_worker
```

### Redis n'est pas accessible

```bash
# Tester la connexion Redis
docker compose exec redis redis-cli ping
# Devrait retourner: PONG

# Vérifier les variables d'environnement
docker compose exec backend env | grep CELERY
```

### Les tâches ne s'exécutent pas

1. Vérifier que Celery Worker est actif :
   ```bash
   docker compose ps celery_worker
   ```

2. Vérifier que Celery Beat est actif :
   ```bash
   docker compose ps celery_beat
   ```

3. Vérifier les tâches planifiées dans l'admin Django

4. Vérifier les logs :
   ```bash
   docker compose logs -f celery_worker celery_beat
   ```

## 🌐 Production

### Variables d'environnement requises

```bash
# Redis
REDIS_URL=redis://your-redis-host:6379/1
CELERY_BROKER_URL=redis://your-redis-host:6379/0

# Database
DB_HOST=your-db-host
DB_NAME=vente_facile
DB_USER=your-db-user
DB_PASSWORD=your-db-password
```

### Recommandations production

1. **Redis** : Utiliser un service managé (Redis Cloud, AWS ElastiCache, etc.)
2. **Celery Worker** : Augmenter le nombre de workers selon la charge
3. **Monitoring** : Utiliser Flower pour monitorer Celery
4. **Logs** : Configurer Sentry pour capturer les erreurs

### Installer Flower (monitoring Celery)

```bash
# Ajouter au requirements.txt
flower>=2.0,<3.0

# Lancer Flower
docker compose exec celery_worker celery -A app flower --port=5555
```

Accéder à Flower : `http://localhost:5555`

## 📝 Notes

- Les tâches sont stockées dans `apps/notifications/tasks.py`
- La configuration Celery est dans `app/celery.py`
- Les settings Celery sont dans `app/settings.py`
- Les tâches périodiques sont gérées via l'admin Django
