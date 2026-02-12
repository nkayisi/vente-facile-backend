"""
URLs pour l'app Purchases.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    PurchaseOrderViewSet, GoodsReceiptViewSet,
    SupplierPaymentViewSet, PurchaseReturnViewSet
)

router = DefaultRouter()
router.register(r'purchase-orders', PurchaseOrderViewSet, basename='purchase-order')
router.register(r'goods-receipts', GoodsReceiptViewSet, basename='goods-receipt')
router.register(r'supplier-payments', SupplierPaymentViewSet, basename='supplier-payment')
router.register(r'purchase-returns', PurchaseReturnViewSet, basename='purchase-return')

app_name = 'purchases'

urlpatterns = [
    path('', include(router.urls)),
]
