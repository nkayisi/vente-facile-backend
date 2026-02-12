"""
URLs pour l'app Products.
"""
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_nested import routers

from .views import (
    CategoryViewSet, BrandViewSet, UnitViewSet,
    ProductViewSet, ProductImageViewSet, ProductVariantViewSet,
    PriceListViewSet
)

# Router principal
router = DefaultRouter()
router.register(r'categories', CategoryViewSet, basename='category')
router.register(r'brands', BrandViewSet, basename='brand')
router.register(r'units', UnitViewSet, basename='unit')
router.register(r'products', ProductViewSet, basename='product')
router.register(r'price-lists', PriceListViewSet, basename='price-list')

# Router nested pour les images et variantes de produits
products_router = routers.NestedDefaultRouter(router, r'products', lookup='product')
products_router.register(r'images', ProductImageViewSet, basename='product-images')
products_router.register(r'variants', ProductVariantViewSet, basename='product-variants')

app_name = 'products'

urlpatterns = [
    path('', include(router.urls)),
    path('', include(products_router.urls)),
]
