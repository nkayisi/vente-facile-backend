"""
ViewSets DRF pour l'app Inventory.
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db import models as db_models
from django.db.models import Sum, F, Q
from django.db import transaction
from django.utils import timezone
from decimal import Decimal

from apps.core.api_mixins import (
    TenantViewSetMixin, AuditMixin, WarehouseScopedQuerysetMixin, ExportResponseMixin,
)
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.core.exceptions import ValidationError as DjangoValidationError
from apps.core.warehouse_scope import (
    LECTURE,
    TOUT,
    entrepots_du_perimetre,
    etendue_de_la_requete,
    exiger_cote_du_transfert,
    exiger_entrepot_actionnable_requete,
    exiger_entrepot_de_destination,
    exiger_entrepot_lisible_requete,
    filter_queryset_by_warehouse_ids,
    filter_stock_transfer_queryset,
    get_membership_for_request,
    nature_de_la_requete,
)
from apps.core.api_permissions import (
    DENY,
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission
)
from apps.subscriptions.services import SubscriptionService
from .models import (
    Warehouse, StockLocation, Stock, StockBatch, StockMovement,
    StockTransfer, StockTransferItem, StockAdjustment, StockAdjustmentItem,
    InventorySession, InventoryCount, STOCK_IN_MOVEMENT_TYPES
)
from .filters import (
    InventorySessionFilter, StockAdjustmentFilter, StockFilter,
    StockMovementFilter, StockTransferFilter,
)
from .report_params import build_export_context
from .serializers import (
    WarehouseListSerializer, WarehouseDetailSerializer, WarehouseCreateSerializer,
    StockLocationSerializer,
    StockListSerializer, StockDetailSerializer,
    StockBatchSerializer,
    StockMovementListSerializer, StockMovementDetailSerializer, StockMovementCreateSerializer,
    StockTransferListSerializer, StockTransferDetailSerializer, StockTransferCreateSerializer,
    StockAdjustmentListSerializer, StockAdjustmentDetailSerializer, StockAdjustmentCreateSerializer,
    InventorySessionListSerializer, InventorySessionDetailSerializer, InventorySessionCreateSerializer,
    InventoryCountSerializer
)


# =============================================================================
# WAREHOUSE VIEWSET
# =============================================================================

class WarehouseViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des entrepôts.
    
    Endpoints:
    - GET /warehouses/ : Liste des entrepôts
    - POST /warehouses/ : Créer un entrepôt
    - GET /warehouses/{id}/ : Détail d'un entrepôt
    - PUT/PATCH /warehouses/{id}/ : Modifier un entrepôt
    - DELETE /warehouses/{id}/ : Supprimer un entrepôt (soft delete)
    - GET /warehouses/{id}/stock-summary/ : Résumé du stock
    """
    
    queryset = Warehouse.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['is_active', 'is_default', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']

    warehouse_scope_field = 'id'
    
    select_related_fields = ['branch', 'manager']
    prefetch_related_fields = ['locations']
    
    action_permissions = {
        'list': 'warehouses.view',
        'retrieve': 'warehouses.view',
        'create': 'warehouses.create',
        'update': 'warehouses.edit',
        'partial_update': 'warehouses.edit',
        'destroy': 'warehouses.delete',
        'stock_summary': 'stock.view',
        # L'annuaire nomme les entrepôts, sans adresse ni stock : c'est la
        # destination d'un transfert, qu'on choisit sans y travailler.
        #
        # ⚠ OUVERT À TOUT MEMBRE (2026-11-16). Le réveil du terminal le porte
        # déjà pour tous les rôles (`warehouse_directory`), et le back-office
        # en a besoin pour le filtre « Entrepôt » d'un CAISSIER à qui la case
        # « Consulter tous les entrepôts » est cochée : il lit tous les dépôts
        # sans porter `warehouses.view`, et son filtre restait vide.
        'directory': '*',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return WarehouseListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return WarehouseCreateSerializer
        return WarehouseDetailSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if self.action in ('list', 'retrieve'):
            # ⚠ Une requête pour toute la liste, plus une par entrepôt : la
            # valeur du stock se calculait en hydratant chaque ligne de stock.
            queryset = queryset.annotate(_valeur_stock=Stock.valeur_par_entrepot())
        return queryset

    def create(self, request, *args, **kwargs):
        """
        Rend l'entrepôt AVEC son identifiant.

        ⚠ Le sérialiseur d'écriture ne porte pas `id` : la page Stock ajoutait
        l'entrepôt reçu à sa liste, et sa carte menait à « introuvable ».
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entrepot = self.perform_create(serializer)
        return Response(
            WarehouseDetailSerializer(entrepot, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def perform_create(self, serializer):
        from .entrepots import designer_principal

        organization = self.get_organization()
        SubscriptionService.assert_can_add_warehouse(organization)
        principal = serializer.validated_data.pop('is_default', False)
        actif = serializer.validated_data.get('is_active', True)
        entrepot = super().perform_create(serializer) or serializer.instance
        # Le premier entrepôt d'un établissement en est le principal, d'office.
        aucun_principal = not Warehouse.objects.filter(
            organization=organization, is_default=True, is_deleted=False,
        ).exclude(id=entrepot.id).exists()
        if actif and (principal or aucun_principal):
            designer_principal(entrepot)
        return entrepot

    def perform_update(self, serializer):
        """
        Le gérant renomme et réadresse SES entrepôts ; le reste est au propriétaire.

        ┌──────────────────────────────────────────────────────────────────┐
        │ ACTIVER, DÉSACTIVER ET DÉSIGNER LE PRINCIPAL PASSENT PAR LES     │
        │ RÈGLES, JAMAIS PAR UN PATCH NU.                                  │
        │                                                                  │
        │ Un `is_active: false` posé directement fermait un entrepôt où    │
        │ une caisse était ouverte, ou le seul entrepôt d'un caissier -    │
        │ qui ne voyait alors plus rien. `inventory.entrepots` refuse en   │
        │ nommant ce qui bloque.                                           │
        └──────────────────────────────────────────────────────────────────┘
        """
        from apps.core.api_permissions import _get_membership
        from apps.organizations.models import OrganizationMembership

        from .entrepots import (
            CHAMPS_DU_GERANT, EntrepotRefuse, ReserveAuProprietaire,
            activer, designer_principal, desactiver,
        )

        entrepot = serializer.instance
        changees = {
            cle for cle, valeur in serializer.validated_data.items()
            if getattr(entrepot, cle, None) != valeur
        }
        membre = _get_membership(self.request)
        proprietaire = membre is not None and membre.role == OrganizationMembership.Role.OWNER
        hors_droit = changees - CHAMPS_DU_GERANT
        if hors_droit and not proprietaire:
            raise ReserveAuProprietaire(sorted(hors_droit))

        # Les deux champs de vie ne s'écrivent pas par le sérialiseur : ils
        # passent par les règles, qui refusent en nommant ce qui bloque.
        actif = serializer.validated_data.pop('is_active', entrepot.is_active)
        principal = serializer.validated_data.pop('is_default', entrepot.is_default)
        if entrepot.is_default and not principal:
            raise EntrepotRefuse(
                f"« {entrepot.name} » est l'entrepôt principal : désignez-en un autre, "
                "il perdra ce titre de lui-même.",
                code='entrepot_principal',
            )
        with transaction.atomic():
            super().perform_update(serializer)
            entrepot.refresh_from_db()
            if actif and not entrepot.is_active:
                activer(entrepot)
            if principal and not entrepot.is_default:
                designer_principal(entrepot)
            if not actif and entrepot.is_active:
                desactiver(entrepot)

    def perform_destroy(self, instance):
        from .entrepots import supprimer

        supprimer(instance)

    @action(detail=False, methods=['get'])
    def directory(self, request):
        """
        `GET /warehouses/directory/` : tous les entrepôts de l'établissement,
        nommés, pour choisir la DESTINATION d'un transfert.

        ⚠ Ni adresse, ni stock, ni responsable : l'expéditeur choisit où livrer,
        il n'obtient aucun accès à la boutique d'en face.
        """
        from .entrepots import annuaire

        return Response(annuaire(self.get_organization()))

    @action(detail=True, methods=['get'], url_path='stock-summary')
    def stock_summary(self, request, pk=None):
        """Retourne un résumé du stock de l'entrepôt."""
        warehouse = self.get_object()
        
        stocks = Stock.objects.filter(warehouse=warehouse).select_related('product')
        
        summary = {
            'total_products': stocks.values('product').distinct().count(),
            'total_quantity': stocks.aggregate(total=Sum('quantity'))['total'] or 0,
            'total_value': str(sum(
                s.quantity * (s.avg_cost if s.avg_cost > 0 else (s.product.cost_price or 0))
                for s in stocks
            )),
            'low_stock_count': stocks.filter(
                quantity__lte=F('product__reorder_point')
            ).count(),
            'out_of_stock_count': stocks.filter(quantity__lte=0).count()
        }
        
        return Response(summary)


# =============================================================================
# STOCK LOCATION VIEWSET
# =============================================================================

class StockLocationViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des emplacements de stock.
    
    Endpoints:
    - GET /stock-locations/ : Liste des emplacements
    - POST /stock-locations/ : Créer un emplacement
    - GET /stock-locations/{id}/ : Détail d'un emplacement
    - PUT/PATCH /stock-locations/{id}/ : Modifier un emplacement
    - DELETE /stock-locations/{id}/ : Supprimer un emplacement
    - GET /stock-locations/by-warehouse/{warehouse_id}/ : Emplacements d'un entrepôt
    """
    
    queryset = StockLocation.objects.all()
    serializer_class = StockLocationSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['warehouse', 'is_active', 'parent']
    search_fields = ['name', 'code']
    
    select_related_fields = ['warehouse', 'parent']
    
    # ⚠ `warehouses.edit` et non `.create` / `.delete` : un emplacement se
    #   range DANS un entrepôt, et le gérant, qui ne crée plus d'entrepôt,
    #   doit continuer d'organiser les siens.
    action_permissions = {
        'list': 'warehouses.view',
        'retrieve': 'warehouses.view',
        'create': 'warehouses.edit',
        'update': 'warehouses.edit',
        'partial_update': 'warehouses.edit',
        'destroy': 'warehouses.edit',
        'by_warehouse': 'warehouses.view',
    }

    def perform_create(self, serializer):
        exiger_entrepot_actionnable_requete(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def perform_update(self, serializer):
        if 'warehouse' in serializer.validated_data:
            exiger_entrepot_actionnable_requete(
                self.request, serializer.validated_data['warehouse'].id
            )
        super().perform_update(serializer)

    @action(detail=False, methods=['get'], url_path='by-warehouse/(?P<warehouse_id>[^/.]+)')
    def by_warehouse(self, request, warehouse_id=None):
        """Retourne tous les emplacements actifs d'un entrepôt."""
        exiger_entrepot_lisible_requete(request, warehouse_id)
        organization = self.get_organization()
        locations = StockLocation.objects.filter(
            organization=organization,
            warehouse_id=warehouse_id,
            is_active=True
        ).select_related('warehouse', 'parent').order_by('name')
        
        serializer = StockLocationSerializer(locations, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK VIEWSET
# =============================================================================

class StockViewSet(ExportResponseMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la consultation du stock.
    
    Endpoints:
    - GET /stocks/ : Liste du stock
    - GET /stocks/{id}/ : Détail du stock
    - GET /stocks/by-product/{product_id}/ : Stock par produit
    - GET /stocks/by-warehouse/{warehouse_id}/ : Stock par entrepôt
    - GET /stocks/low-stock/ : Produits en stock bas
    - GET /stocks/expiring/ : Lots bientôt périmés
    """
    
    queryset = Stock.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockFilter
    search_fields = ['product__name', 'product__sku']
    ordering_fields = ['quantity', 'last_movement_at']
    ordering = ['-last_movement_at']
    
    # Les unités alimentent `stock_display` (« 12 cartons + 3 bouteilles ») :
    # sans elles, deux requêtes de plus par ligne de stock.
    select_related_fields = [
        'product', 'product__unit', 'product__packaging_unit',
        'variant', 'warehouse', 'location',
    ]
    
    action_permissions = {
        'list': 'stock.view',
        'retrieve': 'stock.view',
        'by_product': 'stock.view',
        'by_warehouse': 'stock.view',
        'low_stock': 'stock.view',
        'expiring': 'stock.view',
        # Lots d'un produit, en ordre FIFO : une lecture de stock comme ses
        # voisines. Non déclarée, elle répondait 403 à tous les rôles.
        'product_batches': 'stock.view',
        'export': 'stock.view',
        'unpack': 'stock_movements.create',
        # Le corps de `create` répond déjà 405 avec la marche à suivre. Le
        # laisser non déclaré le rendait inatteignable derrière un 403, qui
        # dit « il vous manque un droit » là où la vérité est « cette écriture
        # n'existe pas ». C'est la vue qui refuse, et elle le dit mieux.
        'create': '*',
    }

    # Stock est en lecture seule - les modifications passent par les mouvements.
    # `post` n'est autorisé que pour l'action `unpack` ci-dessous ; `create` est
    # explicitement refusé.
    http_method_names = ['get', 'post', 'head', 'options']

    def create(self, request, *args, **kwargs):
        return Response(
            {'error': "Le stock ne se modifie pas directement : passez par un mouvement."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def get_serializer_class(self):
        if self.action == 'list':
            return StockListSerializer
        return StockDetailSerializer

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """
        Exporte la situation de stock filtrée, en PDF ou en Excel.

        Le fichier porte TOUTES les lignes du périmètre, pas la seule page
        affichée : c'est la raison d'être d'un export côté serveur. Le queryset
        traverse les mêmes filtres et le même scoping d'entrepôt que la liste,
        donc un magasinier n'exporte jamais un entrepôt qui ne lui est pas
        assigné.
        """
        from apps.settings.services import CurrencyService

        from .reports import build_stock_levels_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)
        queryset = self.filter_queryset(self.get_queryset())

        filters_applied, _ = build_export_context(request, organization)
        group_by_category = request.query_params.get('group_by', 'category') == 'category'

        spec = build_stock_levels_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            group_by_category=group_by_category,
        )
        return self.render_export(spec, 'niveau_de_stock', fmt)

    @action(detail=True, methods=['post'])
    def unpack(self, request, pk=None):
        """
        Ouvre un ou plusieurs conditionnements sans attendre une vente.

        Corps dans `inventory.services.unpack_stock`, partagé avec le journal du
        terminal : ouvrir un carton est un geste de comptoir, il doit
        fonctionner hors ligne.

        Corps : ``{"packages": 1}``
        """
        from .services import TransitionRefusee, unpack_stock
        try:
            return Response(unpack_stock(
                self.get_object(), request.user, request.data.get('packages', 1),
            ))
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=['get'], url_path='by-product/(?P<product_id>[^/.]+)')
    def by_product(self, request, product_id=None):
        """Retourne le stock d'un produit dans tous les entrepôts."""
        organization = self.get_organization()
        stocks = Stock.objects.filter(
            organization=organization,
            product_id=product_id
        ).select_related('warehouse', 'location')
        m = get_membership_for_request(request)
        if m:
            stocks = filter_queryset_by_warehouse_ids(
                stocks, m, 'warehouse_id', nature=LECTURE,
                etendue=etendue_de_la_requete(request),
            )
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='by-warehouse/(?P<warehouse_id>[^/.]+)')
    def by_warehouse(self, request, warehouse_id=None):
        """Retourne tout le stock d'un entrepôt."""
        organization = self.get_organization()
        exiger_entrepot_lisible_requete(request, warehouse_id)
        stocks = Stock.objects.filter(
            organization=organization,
            warehouse_id=warehouse_id
        ).select_related('product', 'variant')
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        """Retourne les produits en stock bas."""
        organization = self.get_organization()
        
        stocks = Stock.objects.filter(
            organization=organization,
            quantity__lte=F('product__reorder_point'),
            product__track_inventory=True
        ).select_related(
            'product', 'product__unit', 'product__packaging_unit', 'warehouse'
        )
        m = get_membership_for_request(request)
        if m:
            stocks = filter_queryset_by_warehouse_ids(
                stocks, m, 'warehouse_id', nature=LECTURE,
                etendue=etendue_de_la_requete(request),
            )
        
        page = self.paginate_queryset(stocks)
        if page is not None:
            serializer = StockListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockListSerializer(stocks, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def expiring(self, request):
        """Retourne les lots bientôt périmés."""
        organization = self.get_organization()
        days = int(request.query_params.get('days', 30))
        
        expiry_date = timezone.localdate() + timezone.timedelta(days=days)
        
        batches = StockBatch.objects.filter(
            organization=organization,
            expiry_date__lte=expiry_date,
            expiry_date__gte=timezone.localdate(),
            quantity__gt=0
        ).select_related('product', 'product__unit', 'warehouse').order_by('expiry_date')
        m = get_membership_for_request(request)
        if m:
            batches = filter_queryset_by_warehouse_ids(
                batches, m, 'warehouse_id', nature=LECTURE,
                etendue=etendue_de_la_requete(request),
            )
        
        page = self.paginate_queryset(batches)
        if page is not None:
            serializer = StockBatchSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockBatchSerializer(batches, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='batches/(?P<product_id>[^/.]+)')
    def product_batches(self, request, product_id=None):
        """Retourne tous les lots d'un produit avec stock disponible (FIFO order)."""
        organization = self.get_organization()
        warehouse_id = request.query_params.get('warehouse')
        include_empty = request.query_params.get('include_empty', 'false').lower() == 'true'
        include_expired = request.query_params.get('include_expired', 'false').lower() == 'true'
        
        batches = StockBatch.objects.filter(
            organization=organization,
            product_id=product_id
        ).select_related('product', 'product__unit', 'warehouse', 'variant')
        
        if warehouse_id:
            exiger_entrepot_lisible_requete(request, warehouse_id)
            batches = batches.filter(warehouse_id=warehouse_id)
        else:
            m = get_membership_for_request(request)
            if m:
                batches = filter_queryset_by_warehouse_ids(
                    batches, m, 'warehouse_id', nature=LECTURE,
                    etendue=etendue_de_la_requete(request),
                )
        
        if not include_empty:
            batches = batches.filter(quantity__gt=0)
        
        if not include_expired:
            today = timezone.localdate()
            batches = batches.filter(
                db_models.Q(expiry_date__isnull=True) | db_models.Q(expiry_date__gte=today)
            )
        
        # Ordre FIFO (les plus anciens en premier)
        batches = batches.order_by('received_at')
        
        page = self.paginate_queryset(batches)
        if page is not None:
            serializer = StockBatchSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = StockBatchSerializer(batches, many=True)
        return Response(serializer.data)


# =============================================================================
# STOCK BATCH VIEWSET
# =============================================================================

class StockBatchViewSet(WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des lots de stock.
    
    Endpoints:
    - GET /stock-batches/ : Liste des lots
    - GET /stock-batches/{id}/ : Détail d'un lot
    - GET /stock-batches/expiring/ : Lots bientôt périmés
    """
    
    queryset = StockBatch.objects.all()
    serializer_class = StockBatchSerializer
    # ⚠ `HasPermission` MANQUAIT : tout membre lisait les lots, caissier compris,
    #   quand les mêmes lots servis par `StockViewSet` exigent `stock.view`.
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    action_permissions = {
        'list': 'stock.view',
        'retrieve': 'stock.view',
    }
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['warehouse', 'product']
    search_fields = ['batch_number', 'product__name']
    ordering_fields = ['expiry_date', 'received_at']
    ordering = ['expiry_date']
    
    # `product__unit` alimente `quantity_display` (« 240 bouteilles ») : sans
    # elle, une requête de plus par lot listé.
    select_related_fields = ['product', 'product__unit', 'warehouse']
    
    # Lots en lecture seule - créés via réception de marchandises
    http_method_names = ['get', 'head', 'options']


# =============================================================================
# STOCK MOVEMENT VIEWSET
# =============================================================================

class StockMovementViewSet(ExportResponseMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les mouvements de stock.
    
    Endpoints:
    - GET /stock-movements/ : Liste des mouvements
    - POST /stock-movements/ : Créer un mouvement manuel
    - GET /stock-movements/{id}/ : Détail d'un mouvement
    """
    
    queryset = StockMovement.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockMovementFilter
    search_fields = ['product__name', 'product__sku', 'notes']
    ordering_fields = ['created_at', 'quantity']
    ordering = ['-created_at']
    
    # `product__unit` et `product__packaging_unit` alimentent `quantity_display`
    # (« 10 cartons + 5 bouteilles ») : sans eux, deux requêtes par ligne.
    select_related_fields = [
        'product', 'product__unit', 'product__packaging_unit',
        'variant', 'warehouse', 'batch', 'created_by',
    ]

    action_permissions = {
        'list': 'stock_movements.view',
        'retrieve': 'stock_movements.view',
        'create': 'stock_movements.create',
        'export': 'stock_movements.view',
        'supplies_export': 'stock_movements.view',
        # ⚠ LA FEUILLE EXIGE LE DROIT DE CRÉER, PAS CELUI DE LIRE. Elle ne sert
        #   qu'à préparer une écriture : l'ouvrir à `stock.view` donnerait un
        #   formulaire que son lecteur ne peut pas soumettre.
        'supply_sheet': 'stock_movements.create',
        'bulk': 'stock_movements.create',
        # Un mouvement de stock est une ÉCRITURE, au sens comptable : il se
        # contrepasse par un autre mouvement, il ne se rature pas. Le
        # modifier laisserait les `quantity_before`/`after` des mouvements
        # suivants décrire un stock qui n'a jamais existé.
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    # La feuille est un GET, mais elle prépare une écriture : elle ne propose
    # que les entrepôts où le membre peut AGIR.
    actions_preparatoires = ('supply_sheet',)

    def get_serializer_class(self):
        if self.action == 'list':
            return StockMovementListSerializer
        elif self.action == 'create':
            return StockMovementCreateSerializer
        return StockMovementDetailSerializer

    @action(detail=False, methods=['get'], url_path='supply-sheet')
    def supply_sheet(self, request):
        """
        La feuille d'un approvisionnement de masse, pré-remplie par périmètre.

        `warehouse` (requis), `scope=all|category|product|low`, `categories=`,
        `products=`, `search=`.
        """
        from .bulk_supply import build_supply_sheet

        organization = self.get_organization()
        entrepot = self._entrepot_demande(request, organization)

        def liste(nom):
            brut = request.query_params.get(nom, '')
            return [v for v in (x.strip() for x in brut.split(',')) if v]

        feuille = build_supply_sheet(
            organization, entrepot,
            scope=request.query_params.get('scope', 'all'),
            categories=liste('categories'),
            products=liste('products'),
            search=(request.query_params.get('search') or '').strip() or None,
        )
        return Response(feuille)

    @action(detail=False, methods=['post'], url_path='bulk')
    def bulk(self, request):
        """
        Enregistre N entrées de stock d'un seul bloc.

        Corps : `{warehouse, movement_type, notes, lines: [...]}`, chaque ligne
        portant exactement les champs que `StockMovementCreateSerializer`
        accepte déjà.
        """
        from .bulk_supply import create_bulk_supply

        organization = self.get_organization()
        entrepot = self._entrepot_demande(request, organization, source=request.data)

        mouvements = create_bulk_supply(
            organization=organization,
            user=request.user,
            warehouse=entrepot,
            movement_type=request.data.get('movement_type'),
            lines=request.data.get('lines') or [],
            notes=(request.data.get('notes') or '').strip(),
            request=request,
        )
        # Le DÉTAIL de chaque mouvement écrit, pas un simple compte : l'écran
        # doit pouvoir annoncer ce qui est entré, rayon par rayon.
        return Response(
            {
                'created': len(mouvements),
                'movements': StockMovementListSerializer(mouvements, many=True).data,
            },
            status=status.HTTP_201_CREATED,
        )

    def _entrepot_demande(self, request, organization, source=None):
        """
        L'entrepôt visé, contrôlé contre le périmètre du demandeur.

        ⚠ IL EST REQUIS, et « aucun entrepôt » n'est pas « tous les entrepôts » :
          un approvisionnement entre quelque part. Le contrôle de périmètre vit
          dans `create_bulk_supply` pour la POUSSÉE - la vue ET le journal y
          passent - et ici pour la LECTURE de la feuille.
        """
        donnees = request.query_params if source is None else source
        identifiant = donnees.get('warehouse')
        if not identifiant:
            raise DRFValidationError({'warehouse': "Indiquez l'entrepôt."})
        exiger_entrepot_actionnable_requete(request, identifiant)
        try:
            return Warehouse.objects.get(id=identifiant, organization=organization)
        except (Warehouse.DoesNotExist, DjangoValidationError, ValueError, TypeError):
            # ⚠ Un identifiant ILLISIBLE est un 400, pas un 500 : `get()` sur un
            #   UUID malformé lève `DjangoValidationError`, que DRF ne traduit
            #   pas. C'est le défaut déjà payé par `_parse_date_range`.
            raise DRFValidationError({'warehouse': "Entrepôt introuvable."})

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """Exporte le journal des mouvements filtré, en PDF ou en Excel."""
        from apps.settings.services import CurrencyService

        from .reports import build_movements_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)
        queryset = self.filter_queryset(self.get_queryset())

        filters_applied, period = build_export_context(
            request, organization, include_period=True,
        )

        spec = build_movements_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            period_label=period,
        )
        return self.render_export(spec, 'mouvements_de_stock', fmt)

    @action(detail=False, methods=['get'], url_path='supplies-export')
    def supplies_export(self, request):
        """
        Exporte le rapport d'approvisionnement valorisé.

        Part des mouvements qui font ENTRER de la marchandise. Le paramètre
        `source` resserre au besoin sur les seules réceptions fournisseur :
        `all` (défaut) répond à « tout ce qui est entré », `receipts` à « ce que
        j'ai acheté », deux questions distinctes que le même écran doit servir.

        `group_by=product` (défaut) donne la valeur d'achat par produit,
        `group_by=movement` déroule le détail chronologique.
        """
        from apps.settings.services import CurrencyService

        from .reports import build_supplies_report

        organization = self.get_organization()
        fmt = self.get_export_format(request)

        queryset = self.filter_queryset(self.get_queryset()).filter(
            movement_type__in=STOCK_IN_MOVEMENT_TYPES,
        )
        if request.query_params.get('source') == 'receipts':
            queryset = queryset.filter(reference_type='goods_receipt')

        filters_applied, period = build_export_context(
            request, organization, include_period=True,
        )
        group_by = (
            'movement' if request.query_params.get('group_by') == 'movement'
            else 'product'
        )

        spec = build_supplies_report(
            queryset,
            organization,
            currency=CurrencyService.primary_code(organization),
            filters_applied=filters_applied,
            group_by=group_by,
            period_label=period,
        )
        return self.render_export(spec, 'approvisionnement', fmt)

    def perform_create(self, serializer):
        """Le corps vit dans `stock_movements.create_stock_movement`.

        Il y est descendu pour que le journal de synchronisation puisse le
        rejouer : `stock_movement.create` appelait `serializer.save()` en
        direct, et ni le stock ni les lots ne bougeaient.
        """
        from .stock_movements import create_stock_movement

        create_stock_movement(
            serializer,
            organization=self.get_organization(),
            user=self.request.user,
            request=self.request,
        )


# =============================================================================
# STOCK TRANSFER VIEWSET
# =============================================================================

class TransitionActionMixin:
    """
    Exécute une transition de `inventory.services` et traduit son refus.

    Le refus est DÉTERMINISTE (« déjà expédié », « pas en révision ») : il
    devient un 400 ici, et un verdict `rejected` côté journal - jamais un
    réessai, qui rejouerait un effet déjà appliqué.

    En mixin plutôt qu'en trois copies : les transferts, les ajustements et les
    sessions d'inventaire appellent tous le même schéma, et trois copies
    auraient divergé sur le traitement de l'erreur.
    """

    #: Serializer employé quand la transition rend un OBJET. Sans lui, le
    #: résultat de la fonction part tel quel : les transferts et les
    #: ajustements rendent `{'status': 'shipped'}`, et ce contrat était déjà
    #: publié - le changer casserait le back-office en silence.
    transition_serializer = None

    #: Champ de l'entrepôt de la pièce, pour exiger qu'il soit ACTIF avant une
    #: transition. Les transferts n'en ont pas besoin : le côté s'en charge.
    transition_entrepot = None

    def _transition(self, fonction, serialiser=False, cote=None, exiger_actif=True, **extra):
        from apps.core.warehouse_scope import exiger_entrepot_actionnable

        from .services import TransitionRefusee
        objet = self.get_object()
        membre = get_membership_for_request(self.request)
        if cote is not None:
            # Approuver, expédier et annuler appartiennent à la SOURCE ;
            # recevoir appartient à la DESTINATION. Le même contrôle vit dans
            # le journal du terminal (`_transition_transfert`).
            exiger_cote_du_transfert(membre, objet, cote)
        elif self.transition_entrepot and membre is not None:
            # Un entrepôt désactivé est fermé : ni ajustement appliqué ni
            # inventaire avancé. Lever (rejeter, annuler) reste permis.
            exiger_entrepot_actionnable(
                membre, getattr(objet, self.transition_entrepot),
                exiger_actif=exiger_actif,
            )
        try:
            resultat = fonction(objet, self.request.user, **extra)
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if serialiser and self.transition_serializer is not None:
            return Response(self.transition_serializer(resultat).data)
        return Response(resultat)


class StockTransferViewSet(TransitionActionMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les transferts de stock entre entrepôts.
    
    Endpoints:
    - GET /stock-transfers/ : Liste des transferts
    - POST /stock-transfers/ : Créer un transfert
    - GET /stock-transfers/{id}/ : Détail d'un transfert
    - POST /stock-transfers/{id}/approve/ : Approuver un transfert
    - POST /stock-transfers/{id}/ship/ : Marquer comme expédié
    - POST /stock-transfers/{id}/receive/ : Marquer comme reçu
    - POST /stock-transfers/{id}/cancel/ : Annuler un transfert
    """
    
    queryset = StockTransfer.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockTransferFilter
    search_fields = ['reference']
    ordering = ['-requested_at']
    
    select_related_fields = ['source_warehouse', 'destination_warehouse', 'requested_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'stock_transfers.view',
        'retrieve': 'stock_transfers.view',
        'create': 'stock_transfers.create',
        'update': 'stock_transfers.create',
        'partial_update': 'stock_transfers.create',
        'destroy': 'stock_transfers.cancel',
        'approve': 'stock_transfers.ship',
        'ship': 'stock_transfers.ship',
        'receive': 'stock_transfers.receive',
        'cancel': 'stock_transfers.cancel',
    }

    def get_queryset(self):
        qs = super().get_queryset()
        m = get_membership_for_request(self.request)
        if m:
            qs = filter_stock_transfer_queryset(
                qs, m, nature=nature_de_la_requete(self.request),
                # Une liste part du défaut ; une fiche se cherche dans tout ce
                # qui est lisible (voir `WarehouseScopedQuerysetMixin._etendue`).
                etendue=TOUT if self.detail else etendue_de_la_requete(self.request),
            )
        return qs

    def perform_create(self, serializer):
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LA SOURCE EST À SOI, LA DESTINATION EST À TOUS.                  │
        # │                                                                  │
        # │ Les deux côtés devaient être affectés au membre : un magasinier  │
        # │ du dépôt central ne pouvait pas approvisionner une boutique. On  │
        # │ expédie depuis SES entrepôts vers n'importe quel entrepôt actif. │
        # └──────────────────────────────────────────────────────────────────┘
        va = serializer.validated_data
        exiger_entrepot_actionnable_requete(
            self.request, va['source_warehouse'].id, champ='source_warehouse',
        )
        exiger_entrepot_de_destination(
            get_membership_for_request(self.request), va['destination_warehouse'].id,
        )
        super().perform_create(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return StockTransferListSerializer
        elif self.action == 'create':
            return StockTransferCreateSerializer
        return StockTransferDetailSerializer

    # Les quatre transitions ci-dessous ne portent PLUS de logique : leur corps
    # vit dans `inventory.services`, que le journal d'opérations du terminal
    # mobile rejoue aussi. Deux corps auraient divergé, comme la dette client
    # l'avait fait avant le lot 6.

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve un transfert en attente."""
        from .services import approve_transfer
        return self._transition(approve_transfer, cote='source')

    @action(detail=True, methods=['post'])
    def ship(self, request, pk=None):
        """Marque un transfert comme expédié et déduit le stock source."""
        from .services import ship_transfer
        return self._transition(ship_transfer, cote='source')

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """Marque un transfert comme reçu et ajoute le stock destination."""
        from .services import receive_transfer
        return self._transition(
            receive_transfer, cote='destination',
            received_items=request.data.get('items', []),
        )

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule un transfert."""
        from .services import cancel_transfer
        return self._transition(cancel_transfer, cote='source')



class StockAdjustmentViewSet(TransitionActionMixin, WarehouseScopedQuerysetMixin, TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour les ajustements de stock (inventaire).
    
    Endpoints:
    - GET /stock-adjustments/ : Liste des ajustements
    - POST /stock-adjustments/ : Créer un ajustement
    - GET /stock-adjustments/{id}/ : Détail d'un ajustement
    - POST /stock-adjustments/{id}/approve/ : Approuver et appliquer
    - POST /stock-adjustments/{id}/reject/ : Rejeter
    """
    
    queryset = StockAdjustment.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = StockAdjustmentFilter
    search_fields = ['reference', 'reason']
    ordering = ['-created_at']
    
    select_related_fields = ['warehouse', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__product']
    
    transition_entrepot = 'warehouse_id'

    action_permissions = {
        'list': 'stock_adjustments.view',
        'retrieve': 'stock_adjustments.view',
        'create': 'stock_adjustments.create',
        'update': 'stock_adjustments.create',
        'partial_update': 'stock_adjustments.create',
        'destroy': 'stock_adjustments.create',
        'approve': 'stock_adjustments.approve',
        'reject': 'stock_adjustments.approve',
    }

    def perform_create(self, serializer):
        exiger_entrepot_actionnable_requete(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def perform_update(self, serializer):
        if 'warehouse' in serializer.validated_data:
            exiger_entrepot_actionnable_requete(
                self.request, serializer.validated_data['warehouse'].id
            )
        super().perform_update(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return StockAdjustmentListSerializer
        elif self.action == 'create':
            return StockAdjustmentCreateSerializer
        return StockAdjustmentDetailSerializer

    # Corps dans `inventory.services`, partagé avec le journal du terminal.

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approuve et applique l'ajustement de stock."""
        from .services import approve_adjustment
        return self._transition(approve_adjustment)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejette un ajustement."""
        from .services import reject_adjustment
        return self._transition(reject_adjustment, exiger_actif=False)


# =============================================================================

class InventorySessionViewSet(ExportResponseMixin, TransitionActionMixin,
                              WarehouseScopedQuerysetMixin, TenantViewSetMixin,
                              AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des sessions d'inventaire.
    
    Endpoints:
    - GET    /inventory-sessions/                    : Liste des sessions
    - POST   /inventory-sessions/                    : Créer une session (brouillon)
    - GET    /inventory-sessions/{id}/               : Détail d'une session
    - DELETE /inventory-sessions/{id}/               : Supprimer (brouillon uniquement)
    - POST   /inventory-sessions/{id}/start/         : Démarrer (verrouille le stock, génère les lignes)
    - POST   /inventory-sessions/{id}/count/         : Enregistrer les comptages
    - POST   /inventory-sessions/{id}/submit/        : Soumettre pour révision
    - POST   /inventory-sessions/{id}/validate/      : Valider et appliquer les ajustements
    - POST   /inventory-sessions/{id}/cancel/        : Annuler (déverrouille le stock)
    - GET    /inventory-sessions/{id}/counts/        : Liste des lignes de comptage
    """

    transition_serializer = InventorySessionDetailSerializer
    
    queryset = InventorySession.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = InventorySessionFilter
    search_fields = ['reference', 'name']
    ordering = ['-created_at']
    
    select_related_fields = ['warehouse', 'created_by', 'validated_by']
    prefetch_related_fields = ['categories', 'products']
    
    transition_entrepot = 'warehouse_id'

    action_permissions = {
        'list': 'inventory.view',
        'retrieve': 'inventory.view',
        'create': 'inventory.create',
        'destroy': 'inventory.cancel',
        'start': 'inventory.start',
        'count': 'inventory.count',
        'submit': 'inventory.submit',
        'validate': 'inventory.validate',
        'cancel': 'inventory.cancel',
        'counts': 'inventory.view',
        # Les deux documents (fiche et rapport) se lisent avec le même droit
        # que l'impression : ils ne portent rien que l'écran n'affiche déjà.
        'export': 'inventory.print',
        # Le VERROU se lit au comptoir, donc par le caissier, qui n'a pas
        # `inventory.view`. Sans cette ligne l'action n'était pas listée, donc
        # refusée (403) à TOUS les rôles : le POS web appelait une route qui
        # refusait, échouait en silence, et n'a jamais posé le moindre verrou.
        # C'est ce qui a fait refuser une vente déjà encaissée et imprimée.
        'locked_products': ['sales.view', 'inventory.view'],
        # Une session avance par ses transitions. La modifier en écriture
        # directe permettrait d'en changer le périmètre ou l'entrepôt après
        # le verrouillage du stock, donc de compter un jeu de produits et
        # d'en ajuster un autre.
        'update': DENY,
        'partial_update': DENY,
    }

    def perform_create(self, serializer):
        exiger_entrepot_actionnable_requete(
            self.request, serializer.validated_data['warehouse'].id
        )
        super().perform_create(serializer)

    def get_serializer_class(self):
        if self.action == 'list':
            return InventorySessionListSerializer
        elif self.action == 'create':
            return InventorySessionCreateSerializer
        return InventorySessionDetailSerializer

    def destroy(self, request, *args, **kwargs):
        session = self.get_object()
        if session.status != 'draft':
            return Response(
                {'error': 'Seules les sessions en brouillon peuvent être supprimées'},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    def _get_target_products(self, session):
        """Produits visés. Corps dans `inventory.services.target_products`."""
        from .services import target_products
        return target_products(session)

    @action(detail=True, methods=['post'])
    def start(self, request, pk=None):
        """Démarre la session : verrouille le stock, engendre les comptages."""
        from .services import start_inventory_session
        return self._transition(start_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def count(self, request, pk=None):
        """
        Enregistre les comptages pour une ou plusieurs lignes.

        Body: { "counts": [{ "id": "<count_id>", "quantity_counted": 10, "notes": "" }, ...] }
        """
        from .services import record_inventory_counts
        return self._transition(
            record_inventory_counts, lignes=request.data.get('counts', []),
        )

    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        """Soumet la session pour révision après le comptage."""
        from .services import submit_inventory_session
        return self._transition(submit_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def validate(self, request, pk=None):
        """Valide la session : applique les écarts et déverrouille le stock."""
        from .services import validate_inventory_session
        return self._transition(validate_inventory_session, serialiser=True)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Annule une session d'inventaire et déverrouille le stock."""
        from .services import cancel_inventory_session
        # ⚠ `serialiser=True` comme ses quatre soeurs : le service rend une
        # `InventorySession`, et sans cet argument l'objet partait tel quel au
        # rendu JSON. Django répondait 500 - APRÈS que le service ait écrit,
        # puisque le rendu est la dernière étape : le stock était bel et bien
        # déverrouillé, et le gérant lisait un échec. C'est le défaut déjà
        # corrigé sur le chemin du JOURNAL, resté ouvert sur celui de la VUE.
        return self._transition(cancel_inventory_session, serialiser=True, exiger_actif=False)

    @action(detail=True, methods=['get'])
    def counts(self, request, pk=None):
        """Retourne les lignes de comptage d'une session avec filtres."""
        session = self.get_object()
        
        qs = session.counts.select_related('product', 'variant', 'counted_by')
        
        # Filters
        is_counted = request.query_params.get('is_counted')
        if is_counted is not None:
            qs = qs.filter(is_counted=is_counted.lower() == 'true')
        
        has_difference = request.query_params.get('has_difference')
        if has_difference is not None and has_difference.lower() == 'true':
            qs = qs.filter(is_counted=True).exclude(quantity_difference=Decimal('0.000'))
        
        search = request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(product__name__icontains=search) |
                Q(product__sku__icontains=search)
            )
        
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(product__category_id=category)
        
        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = InventoryCountSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = InventoryCountSerializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='export')
    def export(self, request, pk=None):
        """
        La fiche de comptage ou le rapport d'écarts, en PDF, classeur ou CSV.

        Les deux documents étaient dessinés dans le navigateur, en jsPDF, à
        partir du JSON de `print-data/`. Ils portent désormais la marque de tous
        les autres documents du produit.
        """
        from apps.inventory.session_documents import (
            DOCUMENT_BASENAMES,
            build_inventory_report,
            build_inventory_sheet,
        )
        from apps.settings.services import CurrencyService

        fmt = self.get_export_format(request)
        document = (request.query_params.get('document') or 'sheet').strip()
        if document not in DOCUMENT_BASENAMES:
            raise DRFValidationError({
                'document': "Document inconnu. Attendu : "
                            + ', '.join(sorted(DOCUMENT_BASENAMES)) + '.',
            })

        session = self.get_object()
        organization = self.get_organization()
        counts = session.counts.select_related(
            'product', 'product__category', 'product__unit',
            'product__packaging_unit',
        ).order_by('product__category__name', 'product__name')

        constructeur = (
            build_inventory_sheet if document == 'sheet' else build_inventory_report
        )
        spec = constructeur(
            session, counts, organization,
            currency=CurrencyService.primary_code(organization),
        )
        return self.render_export(
            spec, f"{DOCUMENT_BASENAMES[document]}_{session.reference}", fmt
        )


    @action(detail=False, methods=['get'], url_path='locked-products')
    def locked_products(self, request):
        """
        Retourne les IDs des produits bloqués par des inventaires en cours.
        Utilisé par le frontend pour désactiver ces produits dans le POS.
        """
        organization = self.get_organization()
        m = get_membership_for_request(request)
        wh_scope = (
            entrepots_du_perimetre(m, LECTURE, etendue_de_la_requete(request))
            if m else None
        )
        # `?warehouse=` : les verrous de la CAISSE, et d'elle seule. Montrer
        # au comptoir l'inventaire d'un autre dépôt griserait des articles
        # qu'il peut vendre. Un entrepôt hors périmètre de lecture LÈVE.
        caisse = request.query_params.get('warehouse')
        if caisse:
            exiger_entrepot_lisible_requete(request, caisse)
            wh_scope = [caisse]
        if wh_scope is not None and not wh_scope:
            return Response({
                'locked_product_ids': [],
                'active_sessions': [],
                'has_active_inventory': False,
            })
        locked_product_ids = InventorySession.get_all_locked_product_ids(
            organization, warehouse_ids=wh_scope
        )

        # Récupérer les sessions actives pour information
        active_sessions_qs = InventorySession.objects.filter(
            organization=organization,
            is_stock_locked=True,
            status__in=['in_progress', 'review'],
            is_deleted=False,
        )
        if wh_scope is not None:
            active_sessions_qs = active_sessions_qs.filter(warehouse_id__in=wh_scope)
        active_sessions = active_sessions_qs.select_related('warehouse').values(
            'id', 'reference', 'name', 'warehouse__name', 'status'
        )
        
        return Response({
            'locked_product_ids': list(locked_product_ids),
            'active_sessions': list(active_sessions),
            'has_active_inventory': len(locked_product_ids) > 0
        })
