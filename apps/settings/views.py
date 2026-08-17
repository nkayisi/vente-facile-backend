"""
Views for settings module.
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from decimal import Decimal

from apps.core.mixins import TenantQuerysetMixin
from apps.core.api_permissions import IsTenantMember, HasActiveSubscription, HasPermission
from apps.contacts.models import Customer

from .models import (
    Currency, OrganizationCurrency, LoyaltyProgram, LoyaltyReward,
    CustomerLoyalty, LoyaltyTransaction, OrganizationSettings
)
from .serializers import (
    CurrencySerializer, OrganizationCurrencySerializer,
    OrganizationCurrencyCreateSerializer, OrganizationCurrencyUpdateSerializer,
    LoyaltyProgramSerializer, LoyaltyProgramCreateUpdateSerializer,
    LoyaltyRewardSerializer, LoyaltyRewardCreateSerializer,
    CustomerLoyaltySerializer, LoyaltyTransactionSerializer,
    OrganizationSettingsSerializer,
    AdjustLoyaltyPointsSerializer, RedeemPointsSerializer,
    UpdateExchangeRateSerializer, CurrencyConversionSerializer
)
from .services import CurrencyService


class CurrencyViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for listing available currencies.
    Read-only, currencies are managed by admins.
    """
    queryset = Currency.objects.filter(is_active=True)
    serializer_class = CurrencySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None


class OrganizationCurrencyViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing organization currencies.
    """
    queryset = OrganizationCurrency.objects.all()
    serializer_class = OrganizationCurrencySerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    pagination_class = None
    
    action_permissions = {
        'list': 'settings.view',
        'retrieve': 'settings.view',
        'create': 'settings.manage',
        'update': 'settings.manage',
        'partial_update': 'settings.manage',
        'destroy': 'settings.manage',
        'set_primary': 'settings.manage',
        'update_rate': 'settings.manage',
        'convert': 'settings.view',
    }
    
    def get_serializer_class(self):
        if self.action == 'create':
            return OrganizationCurrencyCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return OrganizationCurrencyUpdateSerializer
        elif self.action == 'update_rate':
            return UpdateExchangeRateSerializer
        elif self.action == 'convert':
            return CurrencyConversionSerializer
        return OrganizationCurrencySerializer
    
    # Toute mutation d'une devise invalide les caches 5 min de CurrencyService,
    # sur lesquels s'appuie la résolution serveur des taux (caisse, dépenses).
    def perform_create(self, serializer):
        super().perform_create(serializer)
        CurrencyService.invalidate_cache(self.get_organization())

    def perform_update(self, serializer):
        super().perform_update(serializer)
        CurrencyService.invalidate_cache(self.get_organization())

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.is_primary:
            return Response(
                {'error': "Impossible de supprimer la devise principale."},
                status=status.HTTP_400_BAD_REQUEST
            )
        response = super().destroy(request, *args, **kwargs)
        CurrencyService.invalidate_cache(self.get_organization())
        return response
    
    @action(detail=True, methods=['post'])
    def set_primary(self, request, pk=None):
        """Définir une devise comme devise principale."""
        instance = self.get_object()
        org = self.get_organization()
        
        # Bloquer le changement de devise principale après la création
        existing_primary = OrganizationCurrency.objects.filter(
            organization=org,
            is_primary=True
        ).first()
        
        if existing_primary and existing_primary.id != instance.id:
            return Response(
                {
                    'error': "La devise par défaut ne peut pas être modifiée après la création de l'établissement. "
                             "Vous pouvez ajouter d'autres devises secondaires, mais la devise principale reste fixe."
                },
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with transaction.atomic():
            # Désactiver l'ancienne devise principale (si aucune n'existe encore)
            OrganizationCurrency.objects.filter(
                organization=org,
                is_primary=True
            ).update(is_primary=False)
            
            # Activer la nouvelle
            instance.is_primary = True
            instance.exchange_rate = Decimal('1.000000')
            instance.save()
            
            # Synchroniser Organization.currency
            from apps.organizations.models import Organization
            Organization.objects.filter(id=org.id).update(
                currency=instance.currency.code
            )

        # Sans cela, les caches 5 min de CurrencyService continueraient de servir
        # l'ancienne devise principale aux résolutions de taux (caisse, dépenses).
        CurrencyService.invalidate_cache(org)

        serializer = self.get_serializer(instance)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def update_rate(self, request, pk=None):
        """Mettre à jour le taux de change d'une devise."""
        instance = self.get_object()
        
        if instance.is_primary:
            return Response(
                {'error': "Le taux de change de la devise principale est toujours 1."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        serializer = UpdateExchangeRateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        instance.exchange_rate = serializer.validated_data['exchange_rate']
        instance.last_rate_update = timezone.now()
        instance.save()

        # Le nouveau taux doit être visible immédiatement par les résolutions
        # serveur (dépenses, mouvements de caisse), pas dans 5 minutes.
        CurrencyService.invalidate_cache(self.get_organization())

        return Response(OrganizationCurrencySerializer(instance).data)
    
    @action(detail=False, methods=['post'])
    def convert(self, request):
        """Convertir un montant d'une devise à une autre."""
        org = self.get_organization()
        serializer = CurrencyConversionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        amount = serializer.validated_data['amount']
        from_code = serializer.validated_data['from_currency']
        to_code = serializer.validated_data['to_currency']
        
        # Vérifier que les deux devises sont configurées et actives.
        currencies = OrganizationCurrency.objects.filter(
            organization=org,
            currency__code__in=[from_code, to_code],
            is_active=True,
        ).values_list('currency__code', flat=True)
        if from_code not in currencies or to_code not in currencies:
            return Response(
                {'error': "Devise non configurée pour cet établissement."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Conversion via le service centralisé - convention UNIQUE partagée par
        # tout l'app : exchange_rate = unités de devise principale pour 1 unité de
        # cette devise. Vers la principale on multiplie, depuis la principale on
        # divise (ex. USD→CDF = ×taux, CDF→USD = ÷taux).
        from .services import CurrencyService
        result = CurrencyService.convert(amount, from_code, to_code, org)

        return Response({
            'amount': amount,
            'from_currency': from_code,
            'to_currency': to_code,
            'converted_amount': round(result['converted_amount'], 2),
            'exchange_rate': round(result['exchange_rate'], 6),
        })


class LoyaltyProgramViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing loyalty program.
    Each organization has at most one loyalty program.
    """
    queryset = LoyaltyProgram.objects.all()
    serializer_class = LoyaltyProgramSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': '*',  # Accessible à tous les membres de l'organisation
        'retrieve': 'settings.view',
        'create': 'settings.manage',
        'update': 'settings.manage',
        'partial_update': 'settings.manage',
        'destroy': 'settings.manage',
        'toggle': 'settings.manage',
        'calculate_points': 'settings.view',
    }
    
    def get_serializer_class(self):
        if self.action in ['create', 'update', 'partial_update']:
            return LoyaltyProgramCreateUpdateSerializer
        return LoyaltyProgramSerializer
    
    def list(self, request, *args, **kwargs):
        """Retourne le programme de fidélité de l'organisation (ou null)."""
        org = self.get_organization()
        try:
            program = LoyaltyProgram.objects.get(organization=org)
            serializer = self.get_serializer(program)
            return Response(serializer.data)
        except LoyaltyProgram.DoesNotExist:
            # `Response(None)` produit un corps VIDE : le JSONRenderer de DRF
            # renvoie b'' quand data vaut None. Tout client qui parse du JSON
            # casse dessus. On émet un `null` JSON explicite.
            return JsonResponse(None, safe=False)

    def perform_create(self, serializer):
        # L'organisation vient de l'en-tête X-Organization-ID (tenant de la
        # requête), jamais de user.active_organization : un utilisateur membre
        # de plusieurs organisations créerait sinon le programme dans la
        # mauvaise.
        serializer.save(organization=self.get_organization())

    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Activer/désactiver le programme de fidélité."""
        instance = self.get_object()
        instance.is_active = not instance.is_active
        instance.save()
        
        return Response({
            'is_active': instance.is_active,
            'message': f"Programme de fidélité {'activé' if instance.is_active else 'désactivé'}."
        })
    
    @action(detail=True, methods=['post'])
    def calculate_points(self, request, pk=None):
        """Calculer les points pour un montant donné."""
        instance = self.get_object()
        amount = Decimal(request.data.get('amount', 0))
        points = instance.calculate_points(amount)
        
        return Response({
            'amount': amount,
            'points': points,
            'is_active': instance.is_active
        })


class LoyaltyRewardViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing loyalty rewards.
    """
    queryset = LoyaltyReward.objects.all()
    serializer_class = LoyaltyRewardSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'settings.view',
        'retrieve': 'settings.view',
        'create': 'settings.manage',
        'update': 'settings.manage',
        'partial_update': 'settings.manage',
        'destroy': 'settings.manage',
    }
    
    def get_serializer_class(self):
        if self.action == 'create':
            return LoyaltyRewardCreateSerializer
        return LoyaltyRewardSerializer
    
    def perform_create(self, serializer):
        org = self.get_organization()
        try:
            program = LoyaltyProgram.objects.get(organization=org)
        except LoyaltyProgram.DoesNotExist:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Aucun programme de fidélité configuré.")
        
        serializer.save(organization=org, loyalty_program=program)


class CustomerLoyaltyViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing customer loyalty accounts.
    """
    queryset = CustomerLoyalty.objects.all()
    serializer_class = CustomerLoyaltySerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    # Le seul usage réel est « le compte de CE client », interrogé avec
    # `?customer=<uuid>` : une enveloppe de pagination n'apporte rien et faisait
    # échouer la lecture côté POS, qui attendait un tableau.
    pagination_class = None

    action_permissions = {
        'list': 'customers.view',
        'retrieve': 'customers.view',
        'adjust_points': 'settings.manage',
        'redeem': 'sales.create',
        'transactions': 'customers.view',
    }
    
    def get_queryset(self):
        qs = super().get_queryset()
        
        # Filtrer par client
        customer_id = self.request.query_params.get('customer')
        if customer_id:
            qs = qs.filter(customer_id=customer_id)
        
        return qs.select_related('customer')
    
    @action(detail=False, methods=['post'])
    def adjust_points(self, request):
        """Ajuster manuellement les points d'un client."""
        serializer = AdjustLoyaltyPointsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        org = self.get_organization()
        customer_id = serializer.validated_data['customer_id']
        points = serializer.validated_data['points']
        description = serializer.validated_data.get('description', '')
        
        try:
            customer = Customer.objects.get(id=customer_id, organization=org)
        except Customer.DoesNotExist:
            return Response(
                {'error': "Client non trouvé."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Obtenir ou créer le compte de fidélité
        loyalty, created = CustomerLoyalty.objects.get_or_create(
            organization=org,
            customer=customer
        )
        
        with transaction.atomic():
            if points > 0:
                loyalty.add_points(points)
                trans_type = LoyaltyTransaction.TransactionType.BONUS
            else:
                # `redeem_points` verrouille la ligne et lève si le solde est
                # insuffisant. L'ancien code faisait une lecture-modification-
                # écriture non atomique et n'incrémentait jamais
                # `total_points_redeemed`.
                try:
                    loyalty.redeem_points(abs(points))
                except ValueError:
                    return Response(
                        {'error': "Points insuffisants pour cet ajustement."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                trans_type = LoyaltyTransaction.TransactionType.ADJUST
            
            # Créer la transaction
            LoyaltyTransaction.objects.create(
                organization=org,
                customer_loyalty=loyalty,
                transaction_type=trans_type,
                points=points,
                balance_after=loyalty.current_points,
                description=description or f"Ajustement manuel de {points:+d} points",
                created_by=request.user
            )
        
        return Response(CustomerLoyaltySerializer(loyalty).data)
    
    @action(detail=True, methods=['post'])
    def redeem(self, request, pk=None):
        """Utiliser des points pour une récompense."""
        loyalty = self.get_object()
        serializer = RedeemPointsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        org = self.get_organization()
        reward_id = serializer.validated_data.get('reward_id')
        points = serializer.validated_data.get('points')
        
        if reward_id:
            try:
                reward = LoyaltyReward.objects.get(
                    id=reward_id,
                    organization=org,
                    is_active=True
                )
                points = reward.points_required
            except LoyaltyReward.DoesNotExist:
                return Response(
                    {'error': "Récompense non trouvée."},
                    status=status.HTTP_404_NOT_FOUND
                )
        else:
            reward = None
        
        if points > loyalty.current_points:
            return Response(
                {'error': f"Points insuffisants. Disponible: {loyalty.current_points}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Le programme doit être ACTIF : sans ce filtre, on pouvait consommer
        # des points sur un programme désactivé, alors que le chemin de vente
        # l'interdit déjà.
        program = LoyaltyProgram.objects.filter(
            organization=org, is_active=True,
        ).first()
        if program is None:
            return Response(
                {'error': "Aucun programme de fidélité actif."},
                status=status.HTTP_400_BAD_REQUEST
            )
        if points < program.min_points_to_redeem:
            return Response(
                {'error': f"Minimum requis: {program.min_points_to_redeem} points."},
                status=status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            loyalty.redeem_points(points)
            
            LoyaltyTransaction.objects.create(
                organization=org,
                customer_loyalty=loyalty,
                transaction_type=LoyaltyTransaction.TransactionType.REDEEM,
                points=-points,
                balance_after=loyalty.current_points,
                reward=reward,
                description=reward.name if reward else f"Utilisation de {points} points",
                created_by=request.user
            )
        
        return Response({
            'success': True,
            'points_redeemed': points,
            'current_points': loyalty.current_points,
            'reward': LoyaltyRewardSerializer(reward).data if reward else None
        })
    
    @action(detail=True, methods=['get'])
    def transactions(self, request, pk=None):
        """Historique des transactions de fidélité d'un client."""
        loyalty = self.get_object()
        transactions = LoyaltyTransaction.objects.filter(
            customer_loyalty=loyalty
        ).order_by('-created_at')[:50]
        
        serializer = LoyaltyTransactionSerializer(transactions, many=True)
        return Response(serializer.data)


class OrganizationSettingsViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """
    ViewSet for managing organization settings.
    Singleton per organization.
    """
    queryset = OrganizationSettings.objects.all()
    serializer_class = OrganizationSettingsSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'settings.view',
        'retrieve': 'settings.view',
        'create': 'settings.manage',
        'update': 'settings.manage',
        'partial_update': 'settings.manage',
    }
    
    def list(self, request, *args, **kwargs):
        """Retourne les paramètres de l'organisation (ou crée par défaut)."""
        org = self.get_organization()
        settings_obj, created = OrganizationSettings.objects.get_or_create(
            organization=org
        )
        serializer = self.get_serializer(settings_obj)
        return Response(serializer.data)
    
    def create(self, request, *args, **kwargs):
        """Crée ou met à jour les paramètres."""
        org = self.get_organization()
        settings_obj, created = OrganizationSettings.objects.get_or_create(
            organization=org
        )
        
        serializer = self.get_serializer(settings_obj, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        return Response(serializer.data)
