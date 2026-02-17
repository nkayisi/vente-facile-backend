"""
Serializers pour le module Livre de Caisse.
"""
from rest_framework import serializers
from django.utils import timezone
from .models import IncomeCategory, ExpenseCategory, Expense, CashMovement


# =============================================================================
# INCOME CATEGORY SERIALIZERS
# =============================================================================

class IncomeCategoryListSerializer(serializers.ModelSerializer):
    movement_count = serializers.IntegerField(read_only=True, default=0)
    total_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True, default=0
    )

    class Meta:
        model = IncomeCategory
        fields = [
            'id', 'name', 'code', 'description', 'color', 'icon',
            'is_active', 'movement_count', 'total_amount',
        ]
        read_only_fields = ['id']


class IncomeCategoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncomeCategory
        fields = [
            'name', 'code', 'description', 'color', 'icon', 'is_active',
        ]


class IncomeCategoryDetailSerializer(serializers.ModelSerializer):
    movement_count = serializers.SerializerMethodField()

    class Meta:
        model = IncomeCategory
        fields = [
            'id', 'name', 'code', 'description', 'color', 'icon',
            'is_active', 'movement_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_movement_count(self, obj):
        return obj.cash_movements.filter(
            is_cancelled=False,
            direction='in'
        ).count()


# =============================================================================
# EXPENSE CATEGORY SERIALIZERS
# =============================================================================

class ExpenseCategoryListSerializer(serializers.ModelSerializer):
    expense_count = serializers.IntegerField(read_only=True, default=0)
    total_spent = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True, default=0
    )

    class Meta:
        model = ExpenseCategory
        fields = [
            'id', 'name', 'code', 'description', 'color', 'icon',
            'is_active', 'budget_monthly', 'expense_count', 'total_spent',
        ]
        read_only_fields = ['id']


class ExpenseCategoryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = [
            'name', 'code', 'description', 'color', 'icon',
            'is_active', 'budget_monthly',
        ]


class ExpenseCategoryDetailSerializer(serializers.ModelSerializer):
    expense_count = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseCategory
        fields = [
            'id', 'name', 'code', 'description', 'color', 'icon',
            'is_active', 'budget_monthly', 'expense_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_expense_count(self, obj):
        return obj.expenses.filter(
            status__in=['approved', 'paid']
        ).count()


# =============================================================================
# EXPENSE SERIALIZERS
# =============================================================================

class ExpenseListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_color = serializers.CharField(source='category.color', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    payment_method_name = serializers.CharField(
        source='payment_method.name', read_only=True, default=None
    )

    class Meta:
        model = Expense
        fields = [
            'id', 'reference', 'category', 'category_name', 'category_color',
            'description', 'amount', 'status', 'beneficiary',
            'payment_method', 'payment_method_name', 'payment_reference',
            'expense_date', 'due_date', 'paid_date',
            'is_recurring', 'recurrence_period',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name',
            'created_at',
        ]
        read_only_fields = ['id', 'reference', 'created_by', 'approved_by', 'created_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.email
        return None

    def get_approved_by_name(self, obj):
        if obj.approved_by:
            return f"{obj.approved_by.first_name} {obj.approved_by.last_name}".strip() or obj.approved_by.email
        return None


class ExpenseCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = [
            'category', 'description', 'amount', 'beneficiary',
            'payment_method', 'payment_reference',
            'expense_date', 'due_date',
            'is_recurring', 'recurrence_period',
            'notes',
        ]

    def validate(self, data):
        if data.get('is_recurring') and not data.get('recurrence_period'):
            raise serializers.ValidationError({
                'recurrence_period': "La période de récurrence est requise pour une dépense récurrente."
            })
        return data


class ExpenseDetailSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_color = serializers.CharField(source='category.color', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    payment_method_name = serializers.CharField(
        source='payment_method.name', read_only=True, default=None
    )
    cash_movements = serializers.SerializerMethodField()

    class Meta:
        model = Expense
        fields = [
            'id', 'reference', 'category', 'category_name', 'category_color',
            'description', 'amount', 'status', 'beneficiary',
            'payment_method', 'payment_method_name', 'payment_reference',
            'expense_date', 'due_date', 'paid_date',
            'is_recurring', 'recurrence_period',
            'notes', 'attachment',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name',
            'cash_movements',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'reference', 'created_by', 'approved_by', 'created_at', 'updated_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.email
        return None

    def get_approved_by_name(self, obj):
        if obj.approved_by:
            return f"{obj.approved_by.first_name} {obj.approved_by.last_name}".strip() or obj.approved_by.email
        return None

    def get_cash_movements(self, obj):
        movements = obj.cash_movements.filter(is_cancelled=False)
        return CashMovementListSerializer(movements, many=True).data


class ExpenseUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = [
            'category', 'description', 'amount', 'beneficiary',
            'payment_method', 'payment_reference',
            'expense_date', 'due_date',
            'is_recurring', 'recurrence_period',
            'notes',
        ]


# =============================================================================
# CASH MOVEMENT SERIALIZERS
# =============================================================================

class CashMovementListSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()
    payment_method_name = serializers.CharField(
        source='payment_method.name', read_only=True, default=None
    )
    income_category_name = serializers.CharField(
        source='income_category.name', read_only=True, default=None
    )
    income_category_color = serializers.CharField(
        source='income_category.color', read_only=True, default=None
    )
    expense_category_name = serializers.CharField(
        source='expense_category.name', read_only=True, default=None
    )
    expense_category_color = serializers.CharField(
        source='expense_category.color', read_only=True, default=None
    )
    sale_reference = serializers.CharField(
        source='sale.reference', read_only=True, default=None
    )
    expense_reference = serializers.CharField(
        source='expense.reference', read_only=True, default=None
    )
    customer_name = serializers.CharField(
        source='customer.name', read_only=True, default=None
    )
    supplier_name = serializers.CharField(
        source='supplier.name', read_only=True, default=None
    )
    signed_amount = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    direction_display = serializers.CharField(
        source='get_direction_display', read_only=True
    )
    movement_type_display = serializers.CharField(
        source='get_movement_type_display', read_only=True
    )

    class Meta:
        model = CashMovement
        fields = [
            'id', 'reference', 'direction', 'direction_display',
            'movement_type', 'movement_type_display',
            'amount', 'signed_amount', 'description',
            'payment_method', 'payment_method_name',
            'income_category', 'income_category_name', 'income_category_color',
            'expense_category', 'expense_category_name', 'expense_category_color',
            'sale', 'sale_reference',
            'expense', 'expense_reference',
            'customer', 'customer_name',
            'supplier', 'supplier_name',
            'balance_after', 'movement_date',
            'is_cancelled',
            'created_by', 'created_by_name',
            'created_at',
        ]
        read_only_fields = ['id', 'reference', 'created_by', 'created_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return f"{obj.created_by.first_name} {obj.created_by.last_name}".strip() or obj.created_by.email
        return None


class CashMovementCreateSerializer(serializers.ModelSerializer):
    """
    Serializer pour créer un mouvement de caisse manuel.
    Les mouvements liés aux ventes/dépenses sont créés automatiquement.
    """

    class Meta:
        model = CashMovement
        fields = [
            'direction', 'movement_type', 'amount', 'description',
            'payment_method', 'income_category', 'expense_category',
            'customer', 'supplier',
            'movement_date', 'notes',
        ]

    def validate(self, data):
        direction = data.get('direction')
        movement_type = data.get('movement_type')

        # Valider la cohérence direction / type
        in_types = ['sale', 'supplier_refund', 'debt_collection', 'fund_in', 'other_in']
        out_types = ['sale_return', 'expense', 'purchase', 'fund_out', 'other_out']

        if direction == 'in' and movement_type not in in_types + ['adjustment']:
            raise serializers.ValidationError({
                'movement_type': f"Le type '{movement_type}' n'est pas valide pour une entrée."
            })
        if direction == 'out' and movement_type not in out_types + ['adjustment']:
            raise serializers.ValidationError({
                'movement_type': f"Le type '{movement_type}' n'est pas valide pour une sortie."
            })

        return data


class CashMovementDetailSerializer(CashMovementListSerializer):
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta(CashMovementListSerializer.Meta):
        fields = CashMovementListSerializer.Meta.fields + [
            'notes', 'purchase_order',
            'cancelled_at', 'cancelled_by', 'cancelled_by_name', 'cancel_reason',
            'updated_at',
        ]

    def get_cancelled_by_name(self, obj):
        if obj.cancelled_by:
            return f"{obj.cancelled_by.first_name} {obj.cancelled_by.last_name}".strip() or obj.cancelled_by.email
        return None
