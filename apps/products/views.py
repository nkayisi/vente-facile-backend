"""
ViewSets DRF pour l'app Products.
Tous les ViewSets héritent de TenantViewSetMixin pour le filtrage multi-tenant.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count

from apps.core.api_mixins import TenantViewSetMixin, BulkActionMixin, AuditMixin
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from .models import Category, Brand, Unit, Product, ProductImage, ProductVariant, PriceList, ProductPrice
from .serializers import (
    CategoryListSerializer, CategoryDetailSerializer, CategoryCreateSerializer,
    BrandSerializer, UnitSerializer,
    ProductListSerializer, ProductDetailSerializer, ProductCreateSerializer, ProductUpdateSerializer,
    ProductImageSerializer, ProductVariantSerializer,
    PriceListSerializer, ProductPriceSerializer,
    ProductBulkUpdateSerializer
)


# =============================================================================
# CATEGORY VIEWSET
# =============================================================================

class CategoryViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des catégories de produits.
    
    Endpoints:
    - GET /categories/ : Liste des catégories
    - POST /categories/ : Créer une catégorie
    - GET /categories/{id}/ : Détail d'une catégorie
    - PUT/PATCH /categories/{id}/ : Modifier une catégorie
    - DELETE /categories/{id}/ : Supprimer une catégorie (soft delete)
    - GET /categories/{id}/products/ : Produits de la catégorie
    - GET /categories/tree/ : Arborescence des catégories
    """
    
    queryset = Category.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'parent']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'sort_order', 'created_at']
    ordering = ['sort_order', 'name']
    
    select_related_fields = ['parent']
    prefetch_related_fields = ['children']
    
    action_permissions = {
        'list': 'categories.view',
        'retrieve': 'categories.view',
        'create': 'categories.create',
        'update': 'categories.edit',
        'partial_update': 'categories.edit',
        'destroy': 'categories.delete',
        'products': 'products.view',
        'tree': 'categories.view',
    }

    def get_serializer_class(self):
        """Retourne le serializer approprié selon l'action."""
        if self.action == 'list':
            return CategoryListSerializer
        elif self.action in ['create']:
            return CategoryCreateSerializer
        return CategoryDetailSerializer

    @action(detail=True, methods=['get'])
    def products(self, request, pk=None):
        """Retourne les produits d'une catégorie."""
        category = self.get_object()
        products = Product.objects.filter(
            organization=category.organization,
            category=category,
            is_deleted=False
        ).select_related('brand', 'unit')
        
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = ProductListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = ProductListSerializer(products, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def tree(self, request):
        """Retourne l'arborescence complète des catégories."""
        organization = self.get_organization()
        
        # Récupérer les catégories racines
        root_categories = Category.objects.filter(
            organization=organization,
            parent__isnull=True,
            is_deleted=False
        ).prefetch_related('children')
        
        def build_tree(category):
            return {
                'id': str(category.id),
                'name': category.name,
                'slug': category.slug,
                'is_active': category.is_active,
                'children': [
                    build_tree(child)
                    for child in category.children.filter(is_deleted=False)
                ]
            }
        
        tree = [build_tree(cat) for cat in root_categories]
        return Response(tree)


# =============================================================================
# BRAND VIEWSET
# =============================================================================

class BrandViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des marques.
    
    Endpoints:
    - GET /brands/ : Liste des marques
    - POST /brands/ : Créer une marque
    - GET /brands/{id}/ : Détail d'une marque
    - PUT/PATCH /brands/{id}/ : Modifier une marque
    - DELETE /brands/{id}/ : Supprimer une marque (soft delete)
    """
    
    queryset = Brand.objects.all()
    serializer_class = BrandSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active']
    search_fields = ['name']
    ordering_fields = ['name', 'created_at']
    ordering = ['name']
    
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
    }


# =============================================================================
# UNIT VIEWSET
# =============================================================================

class UnitViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des unités de mesure.
    
    Endpoints:
    - GET /units/ : Liste des unités
    - POST /units/ : Créer une unité
    - GET /units/{id}/ : Détail d'une unité
    - PUT/PATCH /units/{id}/ : Modifier une unité
    - DELETE /units/{id}/ : Supprimer une unité
    """
    
    queryset = Unit.objects.all()
    serializer_class = UnitSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'symbol']
    ordering = ['name']
    
    select_related_fields = ['base_unit']
    
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
    }


# =============================================================================
# PRODUCT VIEWSET
# =============================================================================

class ProductViewSet(TenantViewSetMixin, AuditMixin, BulkActionMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des produits.
    
    Endpoints:
    - GET /products/ : Liste des produits
    - POST /products/ : Créer un produit
    - GET /products/{id}/ : Détail d'un produit
    - PUT/PATCH /products/{id}/ : Modifier un produit
    - DELETE /products/{id}/ : Supprimer un produit (soft delete)
    - POST /products/bulk-update/ : Mise à jour en masse
    - POST /products/bulk-delete/ : Suppression en masse
    - GET /products/{id}/stock/ : Stock du produit
    - GET /products/low-stock/ : Produits en stock bas
    - GET /products/search-barcode/ : Recherche par code-barres
    """
    
    queryset = Product.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category', 'brand', 'is_active', 'is_featured', 'track_inventory']
    search_fields = ['name', 'sku', 'barcode', 'short_description']
    ordering_fields = ['name', 'sku', 'selling_price', 'created_at']
    ordering = ['name']
    
    select_related_fields = ['category', 'brand', 'unit']
    prefetch_related_fields = ['stocks', 'images', 'variants']
    bulk_update_fields = ['is_active', 'is_featured', 'category', 'brand']
    
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
        'bulk_update': 'products.delete',
        'bulk_delete': 'products.delete',
        'stock': 'stock.view',
        'low_stock': 'stock.view',
        'search_barcode': 'products.view',
    }

    def get_serializer_class(self):
        """Retourne le serializer approprié selon l'action."""
        if self.action == 'list':
            return ProductListSerializer
        elif self.action == 'create':
            return ProductCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return ProductUpdateSerializer
        elif self.action == 'bulk_update':
            return ProductBulkUpdateSerializer
        return ProductDetailSerializer

    def get_queryset(self):
        """
        Surcharge pour ajouter l'annotation du stock total
        et filtrer par disponibilité en stock si demandé.
        """
        queryset = super().get_queryset()
        
        # Annoter avec le stock total
        queryset = queryset.annotate(
            total_stock=Sum('stocks__quantity')
        )
        
        # Filtre in_stock : produits disponibles en stock
        # Les produits qui ne trackent pas l'inventaire sont toujours inclus
        in_stock = self.request.query_params.get('in_stock')
        if in_stock and in_stock.lower() in ('true', '1'):
            queryset = queryset.filter(
                models.Q(track_inventory=False) |
                models.Q(total_stock__gt=0)
            )
        
        return queryset

    @action(detail=True, methods=['get'])
    def stock(self, request, pk=None):
        """Retourne le détail du stock par entrepôt."""
        product = self.get_object()
        stocks = product.stocks.select_related('warehouse', 'location').all()
        
        data = [
            {
                'warehouse_id': str(stock.warehouse_id),
                'warehouse_name': stock.warehouse.name,
                'location': stock.location.name if stock.location else None,
                'quantity': str(stock.quantity),
                'reserved': str(stock.reserved_quantity),
                'available': str(stock.available_quantity),
                'avg_cost': str(stock.avg_cost),
                'last_movement': stock.last_movement_at
            }
            for stock in stocks
        ]
        
        return Response(data)

    @action(detail=False, methods=['get'])
    def low_stock(self, request):
        """Retourne les produits en stock bas avec pagination."""
        organization = self.get_organization()
        
        products = Product.objects.filter(
            organization=organization,
            is_deleted=False,
            is_active=True,
            track_inventory=True
        ).annotate(
            total_stock=Sum('stocks__quantity')
        ).filter(
            total_stock__lte=models.F('reorder_point')
        ).select_related('category', 'brand')
        
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = ProductListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = ProductListSerializer(products, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='search-barcode')
    def search_barcode(self, request):
        """Recherche un produit par code-barres."""
        barcode = request.query_params.get('barcode', None)
        if not barcode:
            return Response(
                {'error': 'Le paramètre barcode est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        organization = self.get_organization()
        
        # Chercher dans les produits
        product = Product.objects.filter(
            organization=organization,
            barcode=barcode,
            is_deleted=False
        ).select_related('category', 'brand', 'unit').first()
        
        if product:
            serializer = ProductDetailSerializer(product)
            return Response(serializer.data)
        
        # Chercher dans les variantes
        variant = ProductVariant.objects.filter(
            organization=organization,
            barcode=barcode,
            is_deleted=False
        ).select_related('product').first()
        
        if variant:
            serializer = ProductDetailSerializer(variant.product)
            data = serializer.data
            data['selected_variant'] = ProductVariantSerializer(variant).data
            return Response(data)
        
        return Response(
            {'error': 'Produit non trouvé'},
            status=status.HTTP_404_NOT_FOUND
        )

    @action(detail=False, methods=['post'], url_path='bulk-update')
    def bulk_update(self, request):
        """Mise à jour en masse de produits."""
        serializer = ProductBulkUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        ids = serializer.validated_data.pop('ids')
        update_data = {k: v for k, v in serializer.validated_data.items() if v is not None}
        
        if not update_data:
            return Response(
                {'error': 'Aucune donnée à mettre à jour'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        count = queryset.update(**update_data)
        
        return Response({'updated': count})

    @action(detail=False, methods=['post'], url_path='bulk-delete')
    def bulk_delete(self, request):
        """Suppression en masse de produits (soft delete)."""
        ids = request.data.get('ids', [])
        if not ids:
            return Response(
                {'error': 'Aucun ID fourni'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        queryset = self.get_queryset().filter(id__in=ids)
        count = 0
        
        for product in queryset:
            product.soft_delete()
            count += 1
        
        return Response({'deleted': count})


# =============================================================================
# PRODUCT IMAGE VIEWSET
# =============================================================================

class ProductImageViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des images produit.
    
    Endpoints:
    - GET /products/{product_id}/images/ : Liste des images
    - POST /products/{product_id}/images/ : Ajouter une image
    - DELETE /products/{product_id}/images/{id}/ : Supprimer une image
    """
    
    queryset = ProductImage.objects.all()
    serializer_class = ProductImageSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
    }
    
    def get_queryset(self):
        queryset = super().get_queryset()
        product_id = self.kwargs.get('product_pk')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        return queryset

    def perform_create(self, serializer):
        product_id = self.kwargs.get('product_pk')
        organization = self.get_organization()
        serializer.save(product_id=product_id, organization=organization)


# =============================================================================
# PRODUCT VARIANT VIEWSET
# =============================================================================

class ProductVariantViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des variantes produit.
    
    Endpoints:
    - GET /products/{product_id}/variants/ : Liste des variantes
    - POST /products/{product_id}/variants/ : Créer une variante
    - PUT/PATCH /products/{product_id}/variants/{id}/ : Modifier une variante
    - DELETE /products/{product_id}/variants/{id}/ : Supprimer une variante
    """
    
    queryset = ProductVariant.objects.all()
    serializer_class = ProductVariantSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
    }
    
    def get_queryset(self):
        queryset = super().get_queryset()
        product_id = self.kwargs.get('product_pk')
        if product_id:
            queryset = queryset.filter(product_id=product_id)
        return queryset

    def perform_create(self, serializer):
        product_id = self.kwargs.get('product_pk')
        organization = self.get_organization()
        serializer.save(product_id=product_id, organization=organization)


# =============================================================================
# PRICE LIST VIEWSET
# =============================================================================

class PriceListViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des listes de prix.
    
    Endpoints:
    - GET /price-lists/ : Liste des listes de prix
    - POST /price-lists/ : Créer une liste de prix
    - GET /price-lists/{id}/ : Détail d'une liste de prix
    - PUT/PATCH /price-lists/{id}/ : Modifier une liste de prix
    - DELETE /price-lists/{id}/ : Supprimer une liste de prix
    """
    
    queryset = PriceList.objects.all()
    serializer_class = PriceListSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'is_default']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    action_permissions = {
        'list': 'products.view',
        'retrieve': 'products.view',
        'create': 'products.create',
        'update': 'products.edit',
        'partial_update': 'products.edit',
        'destroy': 'products.delete',
    }


# Import manquant pour l'annotation
from django.db import models
