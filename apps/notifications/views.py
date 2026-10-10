"""
Lire ce qu'on a reçu, et les alertes de son périmètre.

┌──────────────────────────────────────────────────────────────────────────────┐
│ IL N'Y AVAIT NI VUE, NI SERIALIZER, NI ROUTE.                               │
│                                                                              │
│ `apps/notifications` portait cinq modèles et aucun moyen de les lire : une   │
│ alerte produite n'était consultable sur AUCUNE surface. C'est l'autre moitié │
│ de « la livraison n'existait pas ».                                          │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.api_permissions import DENY, HasPermission, IsTenantMember
from apps.core.mixins import TenantQuerysetMixin
from apps.notifications.models import Alert, Notification
from apps.notifications.serializers import AlertSerializer, NotificationSerializer
from apps.notifications.services import destinataires


class NotificationViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    Mes notifications, et rien que les miennes.

    ⚠ **AUCUNE PERMISSION DE RUBRIQUE, ET C'EST VOULU.** Ce qu'on lit ici, on
    l'a reçu : le filtre est l'identité, pas un droit. Exiger `notifications.view`
    - qui n'existe pas au catalogue - fermerait sa propre boîte à un caissier,
    alors que le fan-out ne lui adresse QUE ce qu'il a le droit de voir.
    """

    queryset = Notification.objects.all()
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated, IsTenantMember]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['is_read', 'notification_type']
    ordering = ['-created_at']

    def get_queryset(self):
        return super().get_queryset().filter(user=self.request.user)

    @action(detail=False, methods=['get'], url_path='unread-count')
    def unread_count(self, request):
        """Le décompte de la pastille. Une requête, un nombre."""
        return Response({'count': self.get_queryset().filter(is_read=False).count()})

    @action(detail=True, methods=['post'], url_path='mark-read')
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        if not notification.is_read:
            notification.mark_as_read()
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=['post'], url_path='mark-all-read')
    def mark_all_read(self, request):
        """
        ⚠ L'ÉCRITURE EN MASSE PASSE PAR `bulk_update_rows`. Un `update()` nu ne
        touche pas `updated_at`, et une ligne dont l'horodatage ne bouge pas est
        INVISIBLE AU TIRAGE, définitivement et sur tous les terminaux à la fois.
        C'est le défaut de `CustomerBalance`, et le garde-fou de
        `test_bulk_write_visibility` l'interdit par l'AST.
        """
        from django.utils import timezone

        from apps.core.bulk import bulk_update_rows

        maintenant = timezone.now()
        touchees = bulk_update_rows(
            self.get_queryset().filter(is_read=False),
            is_read=True,
            read_at=maintenant,
        )
        return Response({'updated': touchees})


class AlertViewSet(TenantQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    """
    Les alertes de MON périmètre.

    ⚠ **LE PÉRIMÈTRE EST CELUI DE LA LIVRAISON, PAS UN SECOND CALCUL.** Lire la
    liste doit montrer exactement ce qui a été - ou aurait été - adressé : deux
    règles donneraient une alerte reçue en notification et introuvable dans la
    liste, ou l'inverse. `destinataires` est donc la seule main qui décide, ici
    comme là-bas.
    """

    queryset = Alert.objects.all()
    serializer_class = AlertSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['alert_type', 'severity', 'status']
    ordering = ['-created_at']

    action_permissions = {
        'list': 'dashboard.view',
        'retrieve': 'dashboard.view',
        'acknowledge': 'dashboard.view',
        # Résoudre une alerte à la main n'existe pas : elle se résout quand le
        # FAIT disparaît (le rayon se remplit, la facture se règle), et c'est
        # la tâche périodique qui le constate. Un bouton « Résolu » laisserait
        # croire au marchand qu'il a traité ce qu'il a seulement masqué.
        'create': DENY,
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        membership = self._membership()
        if membership is None:
            return queryset.none()

        # ⚠ On filtre EN PYTHON, et c'est assumé. Le périmètre d'une alerte vit
        # dans son `data` JSON, pas dans une colonne : l'exprimer en SQL
        # demanderait une requête par type d'alerte, et c'est exactement la
        # divergence que ce point d'entrée unique existe pour empêcher. La
        # liste est bornée par la pagination, et les alertes actives d'un
        # établissement se comptent en dizaines.
        permises = [
            a.id for a in queryset
            if any(m.id == membership.id for m in destinataires(a))
        ]
        return queryset.filter(id__in=permises)

    def _membership(self):
        from apps.core.api_permissions import _get_membership

        return _get_membership(self.request)

    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """« J'ai vu. » Ce n'est pas « c'est réglé » : le fait est toujours là."""
        from django.utils import timezone

        alerte = self.get_object()
        if alerte.status == Alert.Status.ACTIVE:
            alerte.status = Alert.Status.ACKNOWLEDGED
            alerte.acknowledged_by = request.user
            alerte.acknowledged_at = timezone.now()
            alerte.save(update_fields=[
                'status', 'acknowledged_by', 'acknowledged_at', 'updated_at',
            ])
        return Response(self.get_serializer(alerte).data, status=status.HTTP_200_OK)
