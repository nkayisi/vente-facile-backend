"""
ViewSets DRF pour l'app Products.
Tous les ViewSets héritent de TenantViewSetMixin pour le filtrage multi-tenant.
"""
from datetime import datetime

from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, Q, F, Exists, OuterRef
from django.http import HttpResponse

from apps.core.api_mixins import TenantViewSetMixin, BulkActionMixin, AuditMixin
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    filter_queryset_by_warehouse_ids,
    get_membership_for_request,
)
from apps.core.api_permissions import (
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from apps.subscriptions.services import SubscriptionService
from .models import Category, Brand, Unit, Product, ProductImage, ProductVariant, PriceList, ProductPrice
from apps.inventory.models import Stock
from .serializers import (
    CategoryListSerializer, CategoryDetailSerializer, CategoryCreateSerializer,
    BrandSerializer, UnitSerializer,
    ProductListSerializer, ProductDetailSerializer, ProductCreateSerializer, ProductUpdateSerializer,
    ProductImageSerializer, ProductVariantSerializer,
    PriceListSerializer, ProductPriceSerializer,
    ProductBulkUpdateSerializer
)
from .services import ProductExcelService


def _truthy_query_param(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in ('true', '1', 'yes')


def _annotate_product_total_stock(queryset, wh_ids):
    """Somme total_stock : restreinte aux entrepôts du membership si applicable."""
    if wh_ids is not None:
        warehouse_stock_filter = Q(stocks__warehouse_id__in=wh_ids)
        return queryset.annotate(
            total_stock=Sum('stocks__quantity', filter=warehouse_stock_filter)
        )
    return queryset.annotate(total_stock=Sum('stocks__quantity'))


def _filter_products_by_membership_warehouses(queryset, wh_ids):
    """Produits visibles dans le périmètre entrepôt (hors owner)."""
    if wh_ids is None:
        return queryset
    return queryset.filter(
        Q(track_inventory=False) | Q(stocks__warehouse_id__in=wh_ids)
    ).distinct()


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

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action != 'list':
            return queryset

        organization = self.get_organization()
        with_stock = _truthy_query_param(self.request.query_params.get('with_stock'))
        warehouse_id = self.request.query_params.get('warehouse')

        if not with_stock:
            return queryset

        membership = get_membership_for_request(self.request)
        if warehouse_id:
            assert_warehouse_allowed_for_request(self.request, warehouse_id)
            stock_qs = Stock.objects.filter(
                product__organization=organization,
                product__category_id=OuterRef('pk'),
                warehouse_id=warehouse_id,
                quantity__gt=F('reserved_quantity'),
                product__is_deleted=False,
            )
        else:
            wh_ids = accessible_warehouse_ids(membership) if membership else None
            stock_qs = Stock.objects.filter(
                product__organization=organization,
                product__category_id=OuterRef('pk'),
                quantity__gt=F('reserved_quantity'),
                product__is_deleted=False,
            )
            if wh_ids is not None:
                if not wh_ids:
                    return queryset.none()
                stock_qs = stock_qs.filter(warehouse_id__in=wh_ids)

        return queryset.filter(Exists(stock_qs))

    @action(detail=True, methods=['get'])
    def products(self, request, pk=None):
        """Retourne les produits d'une catégorie (scope warehouse appliqué)."""
        category = self.get_object()

        m = get_membership_for_request(request)
        wh_ids = accessible_warehouse_ids(m) if m else None

        base_qs = Product.objects.filter(
            organization=category.organization,
            category=category,
            is_deleted=False,
        ).select_related('brand', 'unit')

        if wh_ids is not None:
            wh_filter = Q(stocks__warehouse_id__in=wh_ids)
            products = (
                base_qs
                .filter(Q(track_inventory=False) | Q(stocks__warehouse_id__in=wh_ids))
                .distinct()
                .annotate(total_stock=Sum('stocks__quantity', filter=wh_filter))
            )
        else:
            products = base_qs.annotate(total_stock=Sum('stocks__quantity'))

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

    Liste (GET /products/) : par défaut, produits restreints aux entrepôts assignés au
    membership (comme le POS). Avec ``full_catalog=true``, tous les produits de
    l'organisation sont listés ; ``total_stock`` reste sommé sur le périmètre entrepôt
    lorsque le membre est restreint.

    Lecture fiche (retrieve) et détail stock (stock) : pas de filtre sur les lignes
    produit au niveau entrepôt ; les lignes de stock renvoyées restent filtrées dans
    l'action ``stock``.

    Mutations et recherche code-barres : comportement restreint par entrepôt inchangé.

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
    prefetch_related_fields = ['stocks__warehouse', 'stocks__location', 'images', 'variants']
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
        'import_template': 'products.view',
        'import_products': 'products.create',
        'export_excel': 'products.view',
        'export_pdf': 'products.view',
        'check_duplicate': 'products.view',
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
        Scope par entrepôt sur les lignes produit sauf :
        - GET list avec ``full_catalog=true``
        - retrieve et action ``stock`` (lecture catalogue / fiche)
        """
        queryset = super().get_queryset()

        m = get_membership_for_request(self.request)
        wh_ids = accessible_warehouse_ids(m) if m else None
        warehouse_param = self.request.query_params.get('warehouse')
        effective_wh_ids = wh_ids
        if warehouse_param:
            assert_warehouse_allowed_for_request(self.request, warehouse_param)
            effective_wh_ids = [warehouse_param]

        action = getattr(self, 'action', None)

        apply_row_scope = True
        if action == 'list':
            apply_row_scope = not _truthy_query_param(
                self.request.query_params.get('full_catalog')
            )
        elif action in ('retrieve', 'stock'):
            apply_row_scope = False
        elif action in (
            'update',
            'partial_update',
            'destroy',
            'bulk_update',
            'bulk_delete',
            'search_barcode',
        ):
            apply_row_scope = True

        if apply_row_scope:
            queryset = _filter_products_by_membership_warehouses(queryset, effective_wh_ids)

        queryset = _annotate_product_total_stock(queryset, effective_wh_ids)

        # Filtre in_stock : uniquement les produits avec du stock disponible
        in_stock = self.request.query_params.get('in_stock')
        if in_stock and in_stock.lower() in ('true', '1'):
            queryset = queryset.filter(
                Q(track_inventory=False) |
                Q(total_stock__gt=0)
            )

        return queryset

    def perform_create(self, serializer):
        organization = self.get_organization()
        SubscriptionService.assert_can_add_products(organization, 1)
        extra_kwargs = {}
        if hasattr(serializer.Meta.model, "created_by"):
            extra_kwargs["created_by"] = self.request.user
        instance = serializer.save(organization=organization, **extra_kwargs)
        self._assign_guardian_permissions(instance)
        return instance

    @action(detail=True, methods=['get'])
    def stock(self, request, pk=None):
        """Retourne le détail du stock par entrepôt."""
        product = self.get_object()
        stocks = product.stocks.select_related('warehouse', 'location').all()
        m = get_membership_for_request(request)
        if m:
            stocks = filter_queryset_by_warehouse_ids(stocks, m, 'warehouse_id')
        
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
        m = get_membership_for_request(request)
        wh_ids = accessible_warehouse_ids(m) if m else None

        products_qs = Product.objects.filter(
            organization=organization,
            is_deleted=False,
            is_active=True,
            track_inventory=True,
        )
        if wh_ids is not None and not wh_ids:
            products = Product.objects.none()
        elif wh_ids is not None:
            wh_filter = Q(stocks__warehouse_id__in=wh_ids)
            products = products_qs.annotate(
                total_stock=Sum('stocks__quantity', filter=wh_filter)
            ).filter(total_stock__lte=F('reorder_point')).select_related('category', 'brand')
        else:
            products = products_qs.annotate(
                total_stock=Sum('stocks__quantity')
            ).filter(total_stock__lte=F('reorder_point')).select_related('category', 'brand')
        
        page = self.paginate_queryset(products)
        if page is not None:
            serializer = ProductListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = ProductListSerializer(products, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='search-barcode')
    def search_barcode(self, request):
        """Recherche un produit par code-barres (respecte le scope warehouse du membership)."""
        barcode = request.query_params.get('barcode', None)
        if not barcode:
            return Response(
                {'error': 'Le paramètre barcode est requis'},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Chercher dans les produits via get_queryset() pour appliquer le scope warehouse
        product = (
            self.get_queryset()
            .filter(barcode=barcode)
            .select_related('category', 'brand', 'unit')
            .first()
        )

        if product:
            serializer = ProductDetailSerializer(product, context=self.get_serializer_context())
            return Response(serializer.data)

        # Chercher dans les variantes ; vérifier que le produit parent est accessible
        organization = self.get_organization()
        variant = ProductVariant.objects.filter(
            organization=organization,
            barcode=barcode,
            is_deleted=False
        ).select_related('product').first()

        if variant:
            parent = self.get_queryset().filter(id=variant.product_id).first()
            if parent:
                serializer = ProductDetailSerializer(parent, context=self.get_serializer_context())
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

    @action(detail=False, methods=['get'], url_path='import-template')
    def import_template(self, request):
        """Télécharge le template Excel pour l'importation de produits."""
        organization = self.get_organization()
        
        buffer = ProductExcelService.generate_template(organization)
        
        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename="template_import_produits.xlsx"'
        return response

    @action(detail=False, methods=['post'], url_path='import', parser_classes=[MultiPartParser, FormParser])
    def import_products(self, request):
        """
        Importe des produits depuis un fichier Excel.
        Le fichier doit être basé sur le template officiel.
        """
        if 'file' not in request.FILES:
            return Response(
                {'error': 'Aucun fichier fourni'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        file = request.FILES['file']
        
        # Vérifier l'extension
        if not file.name.endswith(('.xlsx', '.xls')):
            return Response(
                {'error': 'Format de fichier invalide. Utilisez un fichier Excel (.xlsx)'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        organization = self.get_organization()
        file_content = file.read()
        
        result = ProductExcelService.import_products(file_content, organization, request.user)
        
        if result['success']:
            return Response(result, status=status.HTTP_200_OK)
        else:
            return Response(result, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='export/excel')
    def export_excel(self, request):
        """Exporte tous les produits de l'organisation au format Excel."""
        organization = self.get_organization()
        buffer = ProductExcelService.export_excel(organization)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        response = HttpResponse(
            buffer.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="produits_export_{timestamp}.xlsx"'
        )
        return response

    @action(detail=False, methods=['get'], url_path='export/pdf')
    def export_pdf(self, request):
        """Exporte tous les produits de l'organisation au format PDF."""
        organization = self.get_organization()
        buffer = ProductExcelService.export_pdf(organization)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = (
            f'attachment; filename="produits_export_{timestamp}.pdf"'
        )
        return response

    @action(detail=False, methods=['post'], url_path='check-duplicate')
    def check_duplicate(self, request):
        """
        Vérifie si un produit avec le même SKU ou code-barres existe déjà.
        Utilisé pour la validation en temps réel lors de la création.
        """
        organization = self.get_organization()
        sku = request.data.get('sku')
        barcode = request.data.get('barcode')
        exclude_id = request.data.get('exclude_id')
        
        duplicates = ProductExcelService.check_duplicate(organization, sku, barcode, exclude_id)
        
        return Response({
            'has_duplicate': bool(duplicates['sku'] or duplicates['barcode']),
            'duplicates': duplicates
        })


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
