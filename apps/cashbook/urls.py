"""
URLs pour le module Livre de Caisse.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    IncomeCategoryViewSet,
    ExpenseCategoryViewSet,
    ExpenseViewSet,
    CashMovementViewSet,
)

router = DefaultRouter()
router.register(r'income-categories', IncomeCategoryViewSet, basename='income-category')
router.register(r'expense-categories', ExpenseCategoryViewSet, basename='expense-category')
router.register(r'expenses', ExpenseViewSet, basename='expense')
router.register(r'cash-movements', CashMovementViewSet, basename='cash-movement')

app_name = 'cashbook'

urlpatterns = [
    path('', include(router.urls)),
]
