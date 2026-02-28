"""
Serializers pour le module Abonnements.
"""
from rest_framework import serializers
from .models import Plan, PlanFeature, Subscription, SubscriptionPayment, Invoice, InvoiceItem


class PlanFeatureSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanFeature
        fields = ['id', 'name', 'code', 'description', 'is_enabled', 'limit_value']


class PlanSerializer(serializers.ModelSerializer):
    plan_features = PlanFeatureSerializer(many=True, read_only=True)

    class Meta:
        model = Plan
        fields = [
            'id', 'name', 'code', 'description',
            'price_monthly', 'price_yearly', 'currency',
            'max_users', 'max_branches', 'max_products',
            'max_monthly_transactions', 'storage_limit_mb',
            'features', 'is_active', 'is_featured',
            'trial_days', 'sort_order',
            'plan_features',
        ]


class SubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    plan_code = serializers.CharField(source='plan.code', read_only=True)
    days_remaining = serializers.IntegerField(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_trial = serializers.BooleanField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    billing_cycle_display = serializers.CharField(source='get_billing_cycle_display', read_only=True)

    class Meta:
        model = Subscription
        fields = [
            'id', 'plan', 'plan_name', 'plan_code',
            'status', 'status_display',
            'billing_cycle', 'billing_cycle_display',
            'price', 'currency',
            'trial_start', 'trial_end',
            'current_period_start', 'current_period_end',
            'cancelled_at', 'cancel_at_period_end',
            'days_remaining', 'is_active', 'is_trial',
            'created_at',
        ]


class SubscriptionStatusSerializer(serializers.Serializer):
    """Serializer pour la réponse du statut d'abonnement."""
    has_subscription = serializers.BooleanField()
    is_active = serializers.BooleanField()
    is_blocked = serializers.BooleanField()
    status = serializers.CharField()
    message = serializers.CharField(allow_null=True)
    days_remaining = serializers.IntegerField(required=False)
    days_remaining_grace = serializers.IntegerField(required=False)
    subscription = SubscriptionSerializer(allow_null=True)
    plan = PlanSerializer(required=False, allow_null=True)


class ActivateSubscriptionSerializer(serializers.Serializer):
    """Serializer pour l'activation d'un abonnement par paiement."""
    plan_id = serializers.UUIDField()
    billing_cycle = serializers.ChoiceField(choices=Plan.BillingCycle.choices)
    payment_method = serializers.ChoiceField(choices=SubscriptionPayment.PaymentMethod.choices)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    reference = serializers.CharField(required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_plan_id(self, value):
        try:
            plan = Plan.objects.get(id=value, is_active=True)
        except Plan.DoesNotExist:
            raise serializers.ValidationError("Plan introuvable ou inactif.")
        return value


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    payment_method_display = serializers.CharField(source='get_payment_method_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SubscriptionPayment
        fields = [
            'id', 'amount', 'currency',
            'payment_method', 'payment_method_display',
            'status', 'status_display',
            'reference', 'paid_at', 'notes',
            'created_by', 'created_by_name',
            'created_at',
        ]

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.full_name
        return None


class InvoiceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceItem
        fields = ['id', 'description', 'quantity', 'unit_price', 'total']


class InvoiceSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_number', 'status', 'status_display',
            'subtotal', 'tax_amount', 'discount_amount', 'total', 'currency',
            'issue_date', 'due_date', 'paid_date',
            'period_start', 'period_end',
            'notes', 'items',
            'created_at',
        ]
