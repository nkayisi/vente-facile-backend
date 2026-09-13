"""
URL configuration for Vente Facile SaaS POS.
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import HttpResponse
from rest_framework_simplejwt.views import (
    TokenRefreshView,
    TokenBlacklistView,
)
from apps.users.views import CustomTokenObtainPairView
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
    SpectacularRedocView,
)

api_v1_patterns = [
    # Authentication JWT
    path('auth/token/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/token/blacklist/', TokenBlacklistView.as_view(), name='token_blacklist'),
    
    # Apps URLs
    path('', include('apps.organizations.urls')),
    path('', include('apps.users.urls')),
    path('', include('apps.products.urls')),
    path('', include('apps.inventory.urls')),
    path('', include('apps.sales.urls')),
    path('', include('apps.purchases.urls')),
    path('', include('apps.contacts.urls')),
    path('', include('apps.cashbook.urls')),
    path('reports/', include('apps.reports.urls')),
    path('settings/', include('apps.settings.urls')),
    path('', include('apps.subscriptions.urls')),
    path('platform-admin/', include('apps.platform_admin.urls')),
    
    # Sync API : /sync/pull/ et /sync/operations/ pour vf-marchand, plus
    # l'ancien /sync/ conserve pour l'application heritee.
    path('', include('apps.sync.urls')),
]

urlpatterns = [
    # Liveness léger pour les healthchecks Docker/orchestrateur.
    # Hors /api/v1/ : non filtré par Tenant/Subscription middlewares. Ne touche
    # pas la base (vérifie seulement que le process gunicorn répond).
    path('healthz/', lambda request: HttpResponse('ok'), name='healthz'),

    # Admin
    path('admin/', admin.site.urls),

    # API v1
    path('api/v1/', include(api_v1_patterns)),
    
    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

# ┌──────────────────────────────────────────────────────────────────────────┐
# │ LES PHOTOS DE PRODUITS N'ÉTAIENT SERVIES QU'EN DÉVELOPPEMENT.            │
# │                                                                          │
# │ `static()` ne branche sa route que sous `DEBUG`, et WhiteNoise ne sert   │
# │ que les fichiers STATIQUES. En production, toute photo répondait donc    │
# │ 404 : la fiche affichait son repli « colis », et le marchand concluait   │
# │ que sa photo n'avait pas été enregistrée.                                │
# │                                                                          │
# │ Servie par Django, une photo occupe un fil de Gunicorn (2 workers × 4    │
# │ threads) le temps de son transfert : c'est le compromis assumé d'un      │
# │ déploiement sur disque local, et c'est pourquoi le stockage objet reste  │
# │ le chemin recommandé - dès qu'`AWS_STORAGE_BUCKET_NAME` est posé, les    │
# │ URL pointent ailleurs et cette route n'est plus empruntée.               │
# │                                                                          │
# │ WHITENOISE EST LE MAUVAIS OUTIL ICI, et c'est ce qui a été essayé en     │
# │ premier : il indexe les fichiers AU DÉMARRAGE. Une photo envoyée après   │
# │ aurait répondu 404 jusqu'au prochain redémarrage - une panne             │
# │ intermittente, donc invisible en recette et impossible à reproduire.     │
# └──────────────────────────────────────────────────────────────────────────┘
if not getattr(settings, 'USE_S3_MEDIA', False):
    from django.views.static import serve as _serve_media
    from django.urls import re_path as _re_path

    urlpatterns += [
        _re_path(
            r'^media/(?P<path>.*)$',
            _serve_media,
            {'document_root': settings.MEDIA_ROOT},
        ),
    ]
