"""
Serializers for settings module.
"""
from rest_framework import serializers
from decimal import Decimal
from drf_spectacular.utils import extend_schema_field
from apps.core.bulk import bulk_update_rows
from .models import (
    MAX_REDEMPTION_PERCENT_CEILING,
    Currency, OrganizationCurrency, LoyaltyProgram,
    CustomerLoyalty, LoyaltyTransaction, OrganizationSettings
)


class CurrencySerializer(serializers.ModelSerializer):
    """Serializer for Currency model."""
    
    class Meta:
        model = Currency
        fields = ['id', 'code', 'name', 'symbol', 'decimal_places', 'is_active']
        read_only_fields = ['id']


class OrganizationCurrencySerializer(serializers.ModelSerializer):
    """Serializer for OrganizationCurrency model."""
    currency_code = serializers.CharField(source='currency.code', read_only=True)
    currency_name = serializers.CharField(source='currency.name', read_only=True)
    currency_symbol = serializers.CharField(source='currency.symbol', read_only=True)
    currency_decimal_places = serializers.IntegerField(
        source='currency.decimal_places', read_only=True
    )

    class Meta:
        model = OrganizationCurrency
        fields = [
            'id', 'currency', 'currency_code', 'currency_name', 'currency_symbol',
            'currency_decimal_places',
            'is_primary', 'exchange_rate', 'is_active', 'last_rate_update'
        ]
        read_only_fields = ['id', 'last_rate_update']


class OrganizationCurrencyCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating OrganizationCurrency."""
    
    class Meta:
        model = OrganizationCurrency
        fields = ['currency', 'is_primary', 'exchange_rate', 'is_active']
    
    def validate(self, attrs):
        organization = self.context['request'].user.active_organization
        currency = attrs.get('currency')
        
        # Vérifier si cette devise existe déjà pour l'organisation
        if OrganizationCurrency.objects.filter(
            organization=organization,
            currency=currency
        ).exists():
            raise serializers.ValidationError({
                'currency': "Cette devise est déjà configurée pour votre établissement."
            })
        
        return attrs
    
    def create(self, validated_data):
        organization = self.context['request'].user.active_organization
        validated_data['organization'] = organization
        
        # Si c'est la devise principale, désactiver les autres devises principales
        if validated_data.get('is_primary'):
            # `bulk_update_rows` : rétrograder l'ancienne devise principale par
            # un `update()` nu laissait sa ligne avec l'horodatage d'avant, donc
            # INVISIBLE au tirage. Le terminal gardait DEUX devises principales,
            # et c'est le taux de change qui décide de chaque montant au comptoir.
            bulk_update_rows(
                OrganizationCurrency.objects.filter(
                    organization=organization,
                    is_primary=True,
                ),
                is_primary=False,
            )
        
        return super().create(validated_data)


class OrganizationCurrencyUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating OrganizationCurrency."""
    
    class Meta:
        model = OrganizationCurrency
        fields = ['exchange_rate', 'is_active', 'is_primary']
    
    def update(self, instance, validated_data):
        organization = instance.organization
        
        # Si on définit cette devise comme principale
        if validated_data.get('is_primary') and not instance.is_primary:
            # `bulk_update_rows` : rétrograder l'ancienne devise principale par
            # un `update()` nu laissait sa ligne avec l'horodatage d'avant, donc
            # INVISIBLE au tirage. Le terminal gardait DEUX devises principales,
            # et c'est le taux de change qui décide de chaque montant au comptoir.
            bulk_update_rows(
                OrganizationCurrency.objects.filter(
                    organization=organization,
                    is_primary=True,
                ),
                is_primary=False,
            )
        
        return super().update(instance, validated_data)


class LoyaltyProgramSerializer(serializers.ModelSerializer):
    """Le programme de fidélité : trois nombres réglables, et c'est tout."""

    # Borne dure du plafond, exposée pour que le POS n'ait pas à la coder en
    # dur de son côté : la valeur vit dans le modèle, pas dans deux endroits.
    max_redemption_percent_ceiling = serializers.SerializerMethodField()

    class Meta:
        model = LoyaltyProgram
        fields = [
            'id', 'name', 'is_active',
            'earn_percent',
            'max_redemption_percent', 'max_redemption_percent_ceiling',
            'only_registered_customers',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    @extend_schema_field(serializers.DecimalField(max_digits=5, decimal_places=2))
    def get_max_redemption_percent_ceiling(self, obj):
        return MAX_REDEMPTION_PERCENT_CEILING


class LoyaltyProgramCreateUpdateSerializer(serializers.ModelSerializer):
    """Création et modification du programme.

    ⚠ IL N'Y A PLUS AUCUN `validate()` CONJOINT, et c'est le signe que la
    simplification a porté : le seul contrôle croisé qui existait opposait
    `amount_per_unit` au mode de calcul, et les deux ont disparu. Les bornes de
    `earn_percent` et de `max_redemption_percent` sont portées par leurs
    validateurs de modèle, donc elles tiennent aussi sur le chemin du journal.
    """

    class Meta:
        model = LoyaltyProgram
        fields = [
            'name', 'is_active',
            'earn_percent',
            'max_redemption_percent',
            'only_registered_customers',
        ]

    def create(self, validated_data):
        # L'organisation est injectée par la vue (perform_create) depuis
        # l'en-tête X-Organization-ID. On ne retombe pas sur
        # user.active_organization : elle ignore le tenant de la requête.
        organization = validated_data.get('organization')
        if organization is None:
            raise serializers.ValidationError(
                "Organisation introuvable pour cette requête."
            )

        # Vérifier si un programme existe déjà
        if LoyaltyProgram.objects.filter(organization=organization).exists():
            raise serializers.ValidationError(
                "Un programme de fidélité existe déjà pour cet établissement."
            )

        return super().create(validated_data)


def money_field(**kwargs):
    """
    Champ de cagnotte, exposé en **chaîne** comme tout montant du projet.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ IL S'APPELAIT `points_field` ET IL FORÇAIT `coerce_to_string=False`.    │
    │                                                                          │
    │ C'était défendable sur des points : « ils se comparent et s'additionnent │
    │ directement à l'écran, et deux décimales tiennent largement dans un      │
    │ nombre JavaScript ». Ce n'est plus défendable sur de l'ARGENT : une       │
    │ cagnotte en CDF atteint sept chiffres, et un panier à sept chiffres perd │
    │ ses unités en virgule flottante - c'est la règle qui tient `Sale.total`  │
    │ et tous les montants de la plateforme en chaîne.                         │
    │                                                                          │
    │ ⚠ LE CHANGEMENT DE TYPE JSON EST SÛR ICI PARCE QUE LE RENOMMAGE FORCE   │
    │   LA VISITE DE CHAQUE LECTEUR : aucun `current_points` ne survit, donc   │
    │   aucun client ne lit encore un nombre là où arrive une chaîne. C'est la │
    │   classe de défaut du `r.toFixed is not a function` du 23 octobre, et    │
    │   `pnpm check:api-decimals` la garde côté web.                           │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    return serializers.DecimalField(max_digits=15, decimal_places=2, **kwargs)


class CustomerLoyaltySerializer(serializers.ModelSerializer):
    """La cagnotte d'un client."""
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    total_earned = money_field(read_only=True)
    total_used = money_field(read_only=True)
    balance = money_field(read_only=True)

    class Meta:
        model = CustomerLoyalty
        fields = [
            'id', 'customer', 'customer_name', 'customer_phone',
            'total_earned', 'total_used', 'balance',
            'tier', 'last_earned_at', 'last_used_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class LoyaltyTransactionSerializer(serializers.ModelSerializer):
    """Une ligne du registre de cagnotte."""
    transaction_type_display = serializers.CharField(
        source='get_transaction_type_display',
        read_only=True
    )
    customer_name = serializers.CharField(
        source='customer_loyalty.customer.name',
        read_only=True
    )
    sale_reference = serializers.CharField(source='sale.reference', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    amount = money_field(read_only=True)
    balance_after = money_field(read_only=True)

    class Meta:
        model = LoyaltyTransaction
        fields = [
            'id', 'customer_loyalty', 'customer_name',
            'transaction_type', 'transaction_type_display',
            'amount', 'balance_after',
            'sale', 'sale_reference',
            'description', 'created_by', 'created_by_name',
            'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class OrganizationSettingsSerializer(serializers.ModelSerializer):
    """Serializer for OrganizationSettings model."""
    
    class Meta:
        model = OrganizationSettings
        fields = [
            'id',
            'receipt_header', 'receipt_footer', 'receipt_paper_width',
            'show_loyalty_on_receipt',
            'low_stock_threshold',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# Serializers pour les actions spécifiques

class AdjustLoyaltyBalanceSerializer(serializers.Serializer):
    """Correction manuelle de la cagnotte d'un client.

    ⚠ `RedeemPointsSerializer` a disparu avec les récompenses : il n'existait
    que pour consommer une `LoyaltyReward`, et la cagnotte se dépense désormais
    au comptoir, sur une facture.
    """
    customer_id = serializers.UUIDField()
    amount = money_field()
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("Le montant de l'ajustement ne peut pas être 0.")
        return value


class UpdateExchangeRateSerializer(serializers.Serializer):
    """Serializer for updating exchange rate."""
    exchange_rate = serializers.DecimalField(
        max_digits=20,
        decimal_places=12,
        min_value=Decimal('0.000000000001')
    )


class CurrencyConversionSerializer(serializers.Serializer):
    """Serializer for currency conversion."""
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)
    from_currency = serializers.CharField(max_length=3)
    to_currency = serializers.CharField(max_length=3)
    converted_amount = serializers.DecimalField(
        max_digits=15,
        decimal_places=2,
        read_only=True
    )
    exchange_rate = serializers.DecimalField(
        max_digits=20,
        decimal_places=12,
        read_only=True
    )
