"""
URLs pour l'app Inventory.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    WarehouseViewSet, StockLocationViewSet, StockViewSet,
    StockBatchViewSet, StockMovementViewSet,
    StockTransferViewSet, StockAdjustmentViewSet
)

router = DefaultRouter()
router.register(r'warehouses', WarehouseViewSet, basename='warehouse')
router.register(r'stock-locations', StockLocationViewSet, basename='stock-location')
router.register(r'stocks', StockViewSet, basename='stock')
router.register(r'stock-batches', StockBatchViewSet, basename='stock-batch')
router.register(r'stock-movements', StockMovementViewSet, basename='stock-movement')
router.register(r'stock-transfers', StockTransferViewSet, basename='stock-transfer')
router.register(r'stock-adjustments', StockAdjustmentViewSet, basename='stock-adjustment')

app_name = 'inventory'

urlpatterns = [
    path('', include(router.urls)),
]
