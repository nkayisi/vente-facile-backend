"""
Serializers for settings module.
"""
from rest_framework import serializers
from decimal import Decimal
from .models import (
    Currency, OrganizationCurrency, LoyaltyProgram, LoyaltyReward,
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
            OrganizationCurrency.objects.filter(
                organization=organization,
                is_primary=True
            ).update(is_primary=False)
        
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
            OrganizationCurrency.objects.filter(
                organization=organization,
                is_primary=True
            ).update(is_primary=False)
        
        return super().update(instance, validated_data)


class LoyaltyProgramSerializer(serializers.ModelSerializer):
    """Serializer for LoyaltyProgram model."""
    points_calculation_type_display = serializers.CharField(
        source='get_points_calculation_type_display',
        read_only=True
    )
    
    class Meta:
        model = LoyaltyProgram
        fields = [
            'id', 'name', 'is_active',
            'points_calculation_type', 'points_calculation_type_display',
            'points_per_unit', 'amount_per_unit',
            'points_percentage', 'point_value',
            'min_points_to_redeem', 'points_expiry_days',
            'only_registered_customers',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class LoyaltyProgramCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating LoyaltyProgram."""
    
    class Meta:
        model = LoyaltyProgram
        fields = [
            'name', 'is_active',
            'points_calculation_type',
            'points_per_unit', 'amount_per_unit',
            'points_percentage', 'point_value',
            'min_points_to_redeem', 'points_expiry_days',
            'only_registered_customers'
        ]
    
    def validate(self, attrs):
        calc_type = attrs.get('points_calculation_type', 
                              getattr(self.instance, 'points_calculation_type', None))
        
        if calc_type == LoyaltyProgram.PointsCalculationType.FIXED_PER_AMOUNT:
            if attrs.get('amount_per_unit', Decimal('0')) <= 0:
                raise serializers.ValidationError({
                    'amount_per_unit': "Le montant par unité doit être supérieur à 0."
                })
        
        return attrs
    
    def create(self, validated_data):
        organization = self.context['request'].user.active_organization
        
        # Vérifier si un programme existe déjà
        if LoyaltyProgram.objects.filter(organization=organization).exists():
            raise serializers.ValidationError(
                "Un programme de fidélité existe déjà pour cet établissement."
            )
        
        validated_data['organization'] = organization
        return super().create(validated_data)


class LoyaltyRewardSerializer(serializers.ModelSerializer):
    """Serializer for LoyaltyReward model."""
    reward_type_display = serializers.CharField(
        source='get_reward_type_display',
        read_only=True
    )
    product_name = serializers.CharField(source='product.name', read_only=True)
    
    class Meta:
        model = LoyaltyReward
        fields = [
            'id', 'loyalty_program', 'name', 'description',
            'reward_type', 'reward_type_display',
            'points_required', 'discount_amount', 'discount_percentage',
            'product', 'product_name', 'is_active',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'loyalty_program', 'created_at', 'updated_at']


class LoyaltyRewardCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating LoyaltyReward."""
    
    class Meta:
        model = LoyaltyReward
        fields = [
            'name', 'description', 'reward_type',
            'points_required', 'discount_amount', 'discount_percentage',
            'product', 'is_active'
        ]
    
    def validate(self, attrs):
        reward_type = attrs.get('reward_type')
        
        if reward_type == LoyaltyReward.RewardType.FREE_PRODUCT:
            if not attrs.get('product'):
                raise serializers.ValidationError({
                    'product': "Un produit est requis pour ce type de récompense."
                })
        
        return attrs


class CustomerLoyaltySerializer(serializers.ModelSerializer):
    """Serializer for CustomerLoyalty model."""
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_phone = serializers.CharField(source='customer.phone', read_only=True)
    
    class Meta:
        model = CustomerLoyalty
        fields = [
            'id', 'customer', 'customer_name', 'customer_phone',
            'total_points_earned', 'total_points_redeemed', 'current_points',
            'tier', 'last_points_earned_at', 'last_points_redeemed_at',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class LoyaltyTransactionSerializer(serializers.ModelSerializer):
    """Serializer for LoyaltyTransaction model."""
    transaction_type_display = serializers.CharField(
        source='get_transaction_type_display',
        read_only=True
    )
    customer_name = serializers.CharField(
        source='customer_loyalty.customer.name',
        read_only=True
    )
    sale_reference = serializers.CharField(source='sale.reference', read_only=True)
    reward_name = serializers.CharField(source='reward.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.full_name', read_only=True)
    
    class Meta:
        model = LoyaltyTransaction
        fields = [
            'id', 'customer_loyalty', 'customer_name',
            'transaction_type', 'transaction_type_display',
            'points', 'balance_after',
            'sale', 'sale_reference',
            'reward', 'reward_name',
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
            'show_loyalty_points_on_receipt',
            'low_stock_threshold',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


# Serializers pour les actions spécifiques

class AdjustLoyaltyPointsSerializer(serializers.Serializer):
    """Serializer for manually adjusting customer loyalty points."""
    customer_id = serializers.UUIDField()
    points = serializers.IntegerField()
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)
    
    def validate_points(self, value):
        if value == 0:
            raise serializers.ValidationError("Le nombre de points ne peut pas être 0.")
        return value


class RedeemPointsSerializer(serializers.Serializer):
    """Serializer for redeeming loyalty points."""
    customer_id = serializers.UUIDField()
    reward_id = serializers.UUIDField(required=False)
    points = serializers.IntegerField(required=False)
    
    def validate(self, attrs):
        if not attrs.get('reward_id') and not attrs.get('points'):
            raise serializers.ValidationError(
                "Vous devez spécifier soit une récompense, soit un nombre de points."
            )
        return attrs


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
