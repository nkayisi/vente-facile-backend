"""
URLs pour l'app Sales.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    RegisterViewSet, RegisterSessionViewSet, PaymentMethodViewSet,
    SaleViewSet, SaleReturnViewSet, QuotationViewSet
)

router = DefaultRouter()
router.register(r'registers', RegisterViewSet, basename='register')
router.register(r'register-sessions', RegisterSessionViewSet, basename='register-session')
router.register(r'payment-methods', PaymentMethodViewSet, basename='payment-method')
router.register(r'sales', SaleViewSet, basename='sale')
router.register(r'sale-returns', SaleReturnViewSet, basename='sale-return')
router.register(r'quotations', QuotationViewSet, basename='quotation')

app_name = 'sales'

urlpatterns = [
    path('', include(router.urls)),
]
