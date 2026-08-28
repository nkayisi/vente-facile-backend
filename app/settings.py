"""
Django settings for Vente Facile SaaS POS.
Production-ready configuration with environment variables.
"""

import os
from pathlib import Path
from datetime import timedelta
from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================================================
# SECURITY SETTINGS
# =============================================================================

SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())
# Toujours autoriser l'accès local pour les healthchecks Docker internes
# (GET http://127.0.0.1:8001/healthz/ depuis le conteneur), quelle que soit la
# valeur d'ALLOWED_HOSTS fournie en prod.
for _local_host in ('127.0.0.1', 'localhost'):
    if _local_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_local_host)

# En développement, on accepte n'importe quel hôte. L'alternative, qui était en
# place, consistait à lister les IP de la machine dans le .env : elles se
# périmaient à chaque changement de réseau (.128, puis .154, puis .116), et un
# téléphone sur le Wi-Fi de la boutique recevait un 400 DisallowedHost sans
# rapport apparent avec la cause. Sans effet en production, où DEBUG est faux.
if DEBUG:
    ALLOWED_HOSTS = ['*']

if not DEBUG and SECRET_KEY.startswith('django-insecure'):
    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a strong value (env var) when DEBUG=False."
    )

# =============================================================================
# APPLICATION DEFINITION
# =============================================================================

DJANGO_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

THIRD_PARTY_APPS = [
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'guardian',
    'django_filters',
    'corsheaders',
    'drf_spectacular',
    'django_celery_beat',
    'django_celery_results',
]

LOCAL_APPS = [
    'apps.core',
    'apps.organizations',
    'apps.users',
    'apps.products',
    'apps.inventory',
    'apps.sales',
    'apps.purchases',
    'apps.contacts',
    'apps.subscriptions',
    'apps.notifications',
    'apps.reports',
    'apps.cashbook',
    'apps.settings',
    'apps.platform_admin',
    'apps.sync',
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# =============================================================================
# MIDDLEWARE
# =============================================================================

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    "whitenoise.middleware.WhiteNoiseMiddleware",
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.TenantMiddleware',
    'apps.core.middleware.OrganizationHeaderMiddleware',
    'apps.core.middleware.SubscriptionMiddleware',
]

ROOT_URLCONF = 'app.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'app.wsgi.application'

# =============================================================================
# DATABASE - PostgreSQL for production
# =============================================================================

DB_ENGINE = config('DB_ENGINE', default='django.db.backends.postgresql')

DATABASES = {
    'default': {
        'ENGINE': DB_ENGINE,
        'NAME': config('DB_NAME', default='vente_facile'),
    }
}

if DB_ENGINE == 'django.db.backends.postgresql':
    DATABASES['default'].update({
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default='postgres'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
        'CONN_MAX_AGE': 60,
        # Vérifie qu'une connexion persistante réutilisée est toujours vivante
        # (évite les erreurs après un idle / une coupure réseau côté Postgres).
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'connect_timeout': 10,
            'options': '-c statement_timeout=30000',  # 30s max par requête

        },
    })
elif DB_ENGINE == 'django.db.backends.sqlite3':
    pass



STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
# =============================================================================
# CUSTOM USER MODEL
# =============================================================================

AUTH_USER_MODEL = 'users.User'

# =============================================================================
# AUTHENTICATION BACKENDS (django-guardian)
# =============================================================================

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'guardian.backends.ObjectPermissionBackend',
]

ANONYMOUS_USER_NAME = None

# =============================================================================
# PASSWORD VALIDATION
# =============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# =============================================================================
# INTERNATIONALIZATION
# =============================================================================

LANGUAGE_CODE = 'fr-fr'
TIME_ZONE = 'Africa/Kinshasa'
USE_I18N = True
USE_TZ = True

# =============================================================================
# STATIC & MEDIA FILES
# =============================================================================

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# =============================================================================
# DEFAULT PRIMARY KEY
# =============================================================================

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# =============================================================================
# DJANGO REST FRAMEWORK
# =============================================================================

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    # Pagination custom : permet au client d'override via ?page_size=N
    # (borné à max_page_size côté StandardResultsSetPagination). Sans cette
    # surcharge, DRF ignore silencieusement page_size envoyé par le client.
    'DEFAULT_PAGINATION_CLASS': 'apps.core.pagination.StandardResultsSetPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ] if not DEBUG else [],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        # Throttle scoped sur les endpoints publics, actif même en DEBUG via
        # ScopedRateThrottle déclaré explicitement par la view (le throttle
        # par scope reste appliqué tant que la classe est attachée à l'action,
        # indépendamment de DEFAULT_THROTTLE_CLASSES).
        'moko_callback': '60/minute',
        # Reveil d'un terminal enrole. Le jeton d'appareil est un secret de
        # 256 bits, il ne se devine pas : la limite protege contre le rejeu en
        # rafale, pas contre la force brute, et cinq essais par minute couvrent
        # largement un usage legitime.
        'device_session': '5/min',
    },
    # Handler custom : garantit une réponse JSON structurée (jamais de 500 HTML)
    # et logue/remonte les exceptions non gérées. Voir apps/core/exception_handler.py
    'EXCEPTION_HANDLER': 'apps.core.exception_handler.api_exception_handler',
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
    ],
}

# =============================================================================
# JWT SETTINGS
# =============================================================================

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=30),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN': True,
    
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    
    'AUTH_HEADER_TYPES': ('Bearer',),
    'AUTH_HEADER_NAME': 'HTTP_AUTHORIZATION',
    'USER_ID_FIELD': 'id',
    'USER_ID_CLAIM': 'user_id',
    
    'AUTH_TOKEN_CLASSES': ('rest_framework_simplejwt.tokens.AccessToken',),
    'TOKEN_TYPE_CLAIM': 'token_type',
}

# =============================================================================
# CORS SETTINGS
# =============================================================================

CORS_ALLOWED_ORIGINS = config(
    'CORS_ALLOWED_ORIGINS',
    default='http://localhost:3005,http://127.0.0.1:3005',
    cast=Csv()
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
    'x-organization-id',
]

# =============================================================================
# DRF SPECTACULAR (API Documentation)
# =============================================================================

SPECTACULAR_SETTINGS = {
    'TITLE': 'Vente Facile API',
    'DESCRIPTION': 'API REST pour le SaaS de gestion commerciale POS',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': r'/api/v1',
}

# =============================================================================
# CELERY CONFIGURATION
# =============================================================================

CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
# Limite douce : lève SoftTimeLimitExceeded (rattrapable) avant la limite dure,
# pour couper une tâche qui traîne (ex : appel Moko lent) sans bloquer un worker.
CELERY_TASK_SOFT_TIME_LIMIT = 120
# VPS 2 cœurs : un worker ne précharge qu'une tâche à la fois (répartition plus
# juste) et se recycle après 200 tâches (garde-fou contre les fuites mémoire).
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

# =============================================================================
# CACHING
# =============================================================================

REDIS_URL = config('REDIS_URL', default='redis://127.0.0.1:6379/1')
TOKEN_CACHE_TTL = config('TOKEN_CACHE_TTL', default=3600, cast=int)
PENDING_META_TTL_SECONDS = config(
    'PENDING_META_TTL_SECONDS',
    default=172800,
    cast=int,
)
POLL_BATCH_SIZE = config('POLL_BATCH_SIZE', default=50, cast=int)

# Pour le développement sans Redis, utiliser le cache local pour default.
# Le cache « moko » utilise Redis si disponible (token partagé web + worker Celery).
if DEBUG:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'unique-snowflake',
        },
        'moko': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'vf_moko',
            'TIMEOUT': TOKEN_CACHE_TTL,
        },
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
        },
        'moko': {
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            'LOCATION': REDIS_URL,
            'KEY_PREFIX': 'vf_moko',
            'TIMEOUT': TOKEN_CACHE_TTL,
        },
    }

# =============================================================================
# EMAIL CONFIGURATION
# =============================================================================

EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='noreply@ventefacile.com')

FRONTEND_URL = config('FRONTEND_URL', default='http://localhost:3005')
PASSWORD_RESET_TIMEOUT = 3600  # 1 heure en secondes

# MOKO / GoFreshPay v2 (abonnements)
MOKO_API_V2_URL = config(
    'MOKO_API_V2_URL',
    default='https://moko.gofreshpay.com',
)
MOKO_API_KEY = config('MOKO_API_KEY', default='')
# URL publique du backend pour le callback (MOKO doit pouvoir joindre cette URL)
PUBLIC_BACKEND_URL = config('PUBLIC_BACKEND_URL', default='http://127.0.0.1:8005')
# Secret partagé inclus en query param de l'URL de callback pour authentifier MOKO.
# Si vide, la vérification est désactivée (à n'utiliser qu'en dev).
MOKO_CALLBACK_SECRET = config('MOKO_CALLBACK_SECRET', default='')


# =============================================================================
# LOGGING
# =============================================================================

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs' / 'django.log',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': config('DJANGO_LOG_LEVEL', default='INFO'),
            'propagate': False,
        },
        'apps': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}

# =============================================================================
# SECURITY SETTINGS (Production)
# =============================================================================

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'

if not DEBUG:
    # HTTPS / cookies
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
    # La sonde Docker interroge Gunicorn en direct sur 127.0.0.1 sans passer par
    # le reverse proxy : elle ne porte donc pas `X-Forwarded-Proto: https` et se
    # faisait rediriger (301) vers une URL TLS que Gunicorn ne sait pas servir.
    # urllib suivait la redirection et envoyait un ClientHello TLS, que Gunicorn
    # rejetait en « Invalid HTTP method » : le conteneur restait `unhealthy`.
    SECURE_REDIRECT_EXEMPT = [r'^healthz/$']
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

    # HSTS
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# =============================================================================
# SENTRY (Error Tracking)
# =============================================================================

SENTRY_DSN = config('SENTRY_DSN', default='')
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.celery import CeleryIntegration
    
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=0.1,
        send_default_pii=False,
        environment=config('ENVIRONMENT', default='development'),
    )

# =============================================================================
# VENTE FACILE CUSTOM SETTINGS
# =============================================================================

VENTE_FACILE = {
    'DEFAULT_CURRENCY': 'CDF',
    'SUPPORTED_CURRENCIES': ['CDF', 'USD'],
    'DEFAULT_TIMEZONE': 'Africa/Kinshasa',
    'MAX_UPLOAD_SIZE_MB': 10,
    'RECEIPT_LOGO_MAX_SIZE': (200, 100),
    'LOW_STOCK_THRESHOLD_DAYS': 7,
    'EXPIRY_WARNING_DAYS': 30,
    # Créances clients : à combien de jours de l'échéance on prévient, et à
    # quel pourcentage de la limite de crédit on alerte.
    'PAYMENT_DUE_WARNING_DAYS': 3,
    'CREDIT_LIMIT_WARNING_PERCENT': 80,
    'SESSION_TIMEOUT_MINUTES': 480,
    'MAX_LOGIN_ATTEMPTS': 5,
    'LOCKOUT_DURATION_MINUTES': 30,
}
