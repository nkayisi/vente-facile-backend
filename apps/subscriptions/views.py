"""
ViewSets pour le module Abonnements.
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response

from apps.core.api_permissions import IsTenantMember, IsTenantOwner

from .models import Plan, Subscription, SubscriptionPayment, Invoice
from .serializers import (
    PlanSerializer,
    PublicPlanSerializer,
    SubscriptionSerializer,
    SubscriptionStatusSerializer,
    ActivateSubscriptionSerializer,
    SubscriptionPaymentSerializer,
    InvoiceSerializer,
)
from .services import SubscriptionService


class PublicPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Plans visibles publiquement sur la landing page.
    Aucune authentification requise.
    """

    def get_queryset(self):
        return (
            Plan.objects.filter(is_active=True)
            .select_related("currency")
            .order_by("sort_order", "price_monthly")
        )
    serializer_class = PublicPlanSerializer
    permission_classes = [AllowAny]
    pagination_class = None


class PlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Liste des plans d'abonnement disponibles.
    Accessible à tous les utilisateurs authentifiés.
    """

    def get_queryset(self):
        return Plan.objects.filter(is_active=True).select_related("currency")
    serializer_class = PlanSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None


class SubscriptionViewSet(viewsets.GenericViewSet):
    """
    Gestion de l'abonnement de l'organisation courante.
    """
    permission_classes = [IsAuthenticated, IsTenantMember]

    def _get_organization(self, request):
        org_id = request.headers.get('X-Organization-ID')
        if not org_id:
            return None
        from apps.organizations.models import Organization
        try:
            return Organization.objects.get(id=org_id, is_deleted=False)
        except Organization.DoesNotExist:
            return None

    @action(detail=False, methods=['get'])
    def status(self, request):
        """
        Retourne le statut complet de l'abonnement de l'organisation.
        Endpoint: GET /api/v1/subscriptions/status/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        sub_status = SubscriptionService.get_subscription_status(organization)

        # Sérialiser la réponse
        data = {
            'has_subscription': sub_status['has_subscription'],
            'is_active': sub_status['is_active'],
            'is_blocked': sub_status['is_blocked'],
            'status': sub_status['status'],
            'message': sub_status['message'],
        }

        subscription = sub_status.get('subscription')
        if subscription:
            data['subscription'] = SubscriptionSerializer(subscription).data
            data['plan'] = PlanSerializer(subscription.plan).data
            data['days_remaining'] = sub_status.get('days_remaining', 0)
            data['days_remaining_grace'] = sub_status.get('days_remaining_grace')
        else:
            data['subscription'] = None
            data['plan'] = None

        return Response(data)

    @action(detail=False, methods=['get'])
    def current(self, request):
        """
        Retourne l'abonnement actuel.
        Endpoint: GET /api/v1/subscriptions/current/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        subscription = organization.subscriptions.filter(
            status__in=[
                Subscription.Status.TRIAL,
                Subscription.Status.ACTIVE,
                Subscription.Status.PAST_DUE,
            ]
        ).order_by('-created_at').first()

        if not subscription:
            return Response(
                {'detail': 'Aucun abonnement actif.'},
                status=status.HTTP_404_NOT_FOUND
            )

        return Response(SubscriptionSerializer(subscription).data)

    @action(detail=False, methods=['get'])
    def history(self, request):
        """
        Historique de tous les abonnements de l'organisation.
        Endpoint: GET /api/v1/subscriptions/history/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        subscriptions = organization.subscriptions.all().order_by('-created_at')
        page = self.paginate_queryset(subscriptions)
        if page is not None:
            serializer = SubscriptionSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SubscriptionSerializer(subscriptions, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def activate(self, request):
        """
        Active un abonnement suite à un paiement.
        Endpoint: POST /api/v1/subscriptions/activate/
        
        TEMPORAIREMENT BLOQUÉ pour les utilisateurs normaux tant que
        le système de paiement n'est pas entièrement configuré.
        Seuls les administrateurs plateforme (is_staff) peuvent activer
        un abonnement via l'admin panel.
        """
        if not request.user.is_staff:
            return Response(
                {
                    'detail': (
                        'Le paiement en ligne n\'est pas encore disponible. '
                        'Veuillez contacter l\'administrateur pour activer '
                        'votre abonnement.'
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        # Vérifier que l'utilisateur est owner ou admin
        org_id = request.headers.get('X-Organization-ID')
        if not request.user.is_staff:
            membership = request.user.memberships.filter(
                organization_id=org_id, is_active=True
            ).first()
            if not membership or membership.role != 'owner':
                return Response(
                    {'detail': 'Seul le propriétaire peut gérer l\'abonnement.'},
                    status=status.HTTP_403_FORBIDDEN
                )

        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = ActivateSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        plan = Plan.objects.get(id=serializer.validated_data['plan_id'])

        result = SubscriptionService.process_payment(
            organization=organization,
            plan=plan,
            billing_cycle=serializer.validated_data['billing_cycle'],
            amount=serializer.validated_data['amount'],
            payment_method=serializer.validated_data['payment_method'],
            reference=serializer.validated_data.get('reference', ''),
            paid_by=request.user,
            notes=serializer.validated_data.get('notes', ''),
        )

        return Response({
            'message': 'Abonnement activé avec succès.',
            'subscription': SubscriptionSerializer(result['subscription']).data,
            'invoice': InvoiceSerializer(result['invoice']).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['get'])
    def payments(self, request):
        """
        Historique des paiements d'abonnement.
        Endpoint: GET /api/v1/subscriptions/payments/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        payments = SubscriptionPayment.objects.filter(
            organization=organization
        ).order_by('-created_at')

        page = self.paginate_queryset(payments)
        if page is not None:
            serializer = SubscriptionPaymentSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = SubscriptionPaymentSerializer(payments, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def invoices(self, request):
        """
        Factures d'abonnement.
        Endpoint: GET /api/v1/subscriptions/invoices/
        """
        organization = self._get_organization(request)
        if not organization:
            return Response(
                {'detail': 'Organisation non trouvée.'},
                status=status.HTTP_404_NOT_FOUND
            )

        invoices = Invoice.objects.filter(
            organization=organization
        ).order_by('-issue_date')

        page = self.paginate_queryset(invoices)
        if page is not None:
            serializer = InvoiceSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = InvoiceSerializer(invoices, many=True)
        return Response(serializer.data)
