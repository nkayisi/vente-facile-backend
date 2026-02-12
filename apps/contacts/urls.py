"""
URLs pour l'app Contacts.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_nested import routers

from .views import (
    CustomerViewSet,
    SupplierViewSet, SupplierProductViewSet
)

router = DefaultRouter()
router.register(r'customers', CustomerViewSet, basename='customer')
router.register(r'suppliers', SupplierViewSet, basename='supplier')

# Nested routes pour les produits fournisseur
suppliers_router = routers.NestedDefaultRouter(router, r'suppliers', lookup='supplier')
suppliers_router.register(r'products', SupplierProductViewSet, basename='supplier-products')

app_name = 'contacts'

urlpatterns = [
    path('', include(router.urls)),
    path('', include(suppliers_router.urls)),
]
