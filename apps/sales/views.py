"""
ViewSets DRF pour l'app Sales (POS).
"""
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, F
from django.utils import timezone
from decimal import Decimal
from rest_framework.exceptions import ValidationError as DRFValidationError

from apps.core.api_mixins import (
    TenantViewSetMixin,
    AuditMixin,
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
)
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_warehouse_allowed_for_request,
    filter_queryset_by_related_warehouse,
    filter_queryset_by_warehouse_ids,
    get_membership_for_request,
)
from apps.core.api_permissions import (
    DENY,
    IsTenantMember, HasActiveSubscription, TenantObjectPermission, HasPermission,
    has_perm_code, is_manager_or_above,
)
from .models import (
    Register, RegisterSession, Sale, SaleItem, PaymentMethod, Payment,
    SaleReturn, SaleReturnItem, Quotation, QuotationItem
)
from .serializers import (
    RegisterSerializer,
    RegisterSessionListSerializer, RegisterSessionDetailSerializer,
    RegisterSessionOpenSerializer, RegisterSessionCloseSerializer,
    PaymentMethodSerializer,
    SaleListSerializer, SaleDetailSerializer, SaleCreateSerializer, SalePaymentSerializer,
    SaleUpdateSerializer,
    SaleReturnListSerializer, SaleReturnDetailSerializer, SaleReturnCreateSerializer,
    QuotationListSerializer, QuotationDetailSerializer, QuotationCreateSerializer
)


# =============================================================================
# REGISTER VIEWSET
# =============================================================================

class RegisterViewSet(
    WarehouseScopedQuerysetMixin,
    WarehouseAssertCreateMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des caisses.
    
    Endpoints:
    - GET /registers/ : Liste des caisses
    - POST /registers/ : Créer une caisse
    - GET /registers/{id}/ : Détail d'une caisse
    - PUT/PATCH /registers/{id}/ : Modifier une caisse
    - DELETE /registers/{id}/ : Supprimer une caisse
    """
    
    queryset = Register.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'branch']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    select_related_fields = ['branch', 'warehouse']

    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = False
    warehouse_write_required = True
    warehouse_write_allow_none = False
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.manage_registers',
        'update': 'sales.manage_registers',
        'partial_update': 'sales.manage_registers',
        'destroy': 'sales.manage_registers',
    }


# =============================================================================
# REGISTER SESSION VIEWSET
# =============================================================================

class RegisterSessionViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des sessions de caisse.
    
    Endpoints:
    - GET /register-sessions/ : Liste des sessions
    - GET /register-sessions/{id}/ : Détail d'une session
    - POST /register-sessions/open/ : Ouvrir une session
    - POST /register-sessions/{id}/close/ : Fermer une session
    - GET /register-sessions/current/ : Session courante de l'utilisateur
    """
    
    queryset = RegisterSession.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['status', 'register', 'opened_by']
    ordering = ['-opened_at']
    
    select_related_fields = ['register', 'opened_by', 'closed_by']

    # Filtre par l'entrepôt de la caisse : uniquement les sessions dont la
    # caisse est dans le périmètre ``assigned_warehouses`` du membre (owner : tout).
    warehouse_scope_field = 'register__warehouse_id'
    warehouse_scope_include_null = False
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'open': 'sales.create',
        'close': 'sales.create',
        'current': 'sales.view',
        # Une session de caisse s'ouvre par `open` et se ferme par `close` :
        # ces deux actes portent l'héritage des fonds par devise, le
        # périmètre entrepôt et le comptage. Un POST direct en fabriquerait
        # une sans rien de tout cela.
        'create': DENY,
    }
    
    # Sessions en lecture seule sauf pour open/close
    http_method_names = ['get', 'post', 'head', 'options']

    def get_serializer_class(self):
        if self.action == 'list':
            return RegisterSessionListSerializer
        elif self.action == 'open':
            return RegisterSessionOpenSerializer
        elif self.action == 'close':
            return RegisterSessionCloseSerializer
        return RegisterSessionDetailSerializer

    @action(detail=False, methods=['post'])
    def open(self, request):
        """
        Ouvre une nouvelle session de caisse.

        Le corps vit dans `sales.register_sessions`, que le journal
        d'opérations du terminal appelle aussi : le fonds d'ouverture est
        hérité PAR DEVISE de la dernière clôture, et une seule écriture de
        cette arithmétique garantit que les deux surfaces la font pareil.
        """
        from .register_sessions import (
            CaisseIntrouvable, SessionDejaOuverte, open_register_session,
        )

        serializer = RegisterSessionOpenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            session = open_register_session(
                self.get_organization(),
                serializer.validated_data['register'],
                request.user,
                opening_balance=serializer.validated_data.get('opening_balance'),
                opening_balances=serializer.validated_data.get('opening_balances'),
                request=request,
            )
        except CaisseIntrouvable as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)
        except SessionDejaOuverte:
            # Le contrat de réponse ne bouge pas : le back-office branche son
            # message sur cette phrase exacte.
            return Response(
                {'error': 'Une session est déjà ouverte sur cette caisse'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            RegisterSessionDetailSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """
        Ferme une session de caisse.

        - Tout membre ayant accès à l'entrepôt de la caisse peut fermer la
          session, y compris une session ouverte par un autre utilisateur
          (``get_object`` est déjà filtré par périmètre entrepôt).
        - Accepte `counted_balance` / `counted_balances` (comptage manuel) ;
          calcule l'écart par devise.
        - Si un écart est non nul, `notes` est obligatoire.

        Le corps vit dans `sales.register_sessions`, que le journal d'opérations du
        terminal rejoue aussi : le Z de caisse se tire au comptoir, souvent
        avant que le réseau ne revienne.
        """
        from .register_sessions import (
            NoteRequise, TransitionRefusee, close_register_session,
        )

        serializer = RegisterSessionCloseSerializer(
            data=request.data if request.data else {}
        )
        serializer.is_valid(raise_exception=True)

        try:
            session = close_register_session(
                self.get_object(), request.user, serializer.validated_data,
                ip=request.META.get('REMOTE_ADDR'),
                agent=request.META.get('HTTP_USER_AGENT', '')[:500],
            )
        except NoteRequise as exc:
            # `notes` et non `error` : contrat publié, sur lequel le
            # back-office branche son message de champ.
            return Response({'notes': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(RegisterSessionDetailSerializer(session).data)


    @action(detail=False, methods=['get'])
    def current(self, request):
        """Retourne la session courante de l'utilisateur."""
        organization = self.get_organization()

        session_qs = RegisterSession.objects.filter(
            organization=organization,
            opened_by=request.user,
            status='open',
        ).select_related('register')
        membership = get_membership_for_request(request)
        if membership:
            session_qs = filter_queryset_by_related_warehouse(
                session_qs,
                membership,
                'register__warehouse_id',
                include_null=False,
            )
        session = session_qs.first()

        if not session:
            return Response(
                {'error': 'Aucune session ouverte'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        return Response(RegisterSessionDetailSerializer(session).data)


# =============================================================================
# PAYMENT METHOD VIEWSET
# =============================================================================

class PaymentMethodViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des méthodes de paiement.
    
    Endpoints:
    - GET /payment-methods/ : Liste des méthodes
    - POST /payment-methods/ : Créer une méthode
    - GET /payment-methods/{id}/ : Détail d'une méthode
    - PUT/PATCH /payment-methods/{id}/ : Modifier une méthode
    - DELETE /payment-methods/{id}/ : Supprimer une méthode
    """
    
    queryset = PaymentMethod.objects.all()
    serializer_class = PaymentMethodSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'method_type']
    search_fields = ['name', 'code']
    ordering = ['name']
    
    action_permissions = {
        'list': 'payment_methods.view',
        'retrieve': 'payment_methods.view',
        'create': 'payment_methods.manage',
        'update': 'payment_methods.manage',
        'partial_update': 'payment_methods.manage',
        'destroy': 'payment_methods.manage',
    }


# =============================================================================
# SALE VIEWSET
# =============================================================================

class SaleViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des ventes (POS).
    
    Endpoints:
    - GET /sales/ : Liste des ventes
    - POST /sales/ : Créer une vente
    - GET /sales/{id}/ : Détail d'une vente
    - POST /sales/{id}/add-payment/ : Ajouter un paiement
    - POST /sales/{id}/cancel/ : Annuler une vente
    - GET /sales/today/ : Ventes du jour
    - GET /sales/stats/ : Statistiques de ventes
    """
    
    queryset = Sale.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'sale_type', 'customer', 'register', 'is_pos']
    search_fields = ['reference', 'customer__name']
    ordering_fields = ['sale_date', 'total', 'reference', 'due_date', 'amount_due']
    ordering = ['-sale_date']
    
    # Champs relationnels communs à toutes les actions. La liste et le détail
    # ayant des besoins différents, ils sont affinés par action dans
    # get_queryset() - pour éviter à la fois le N+1 (détail) et le
    # sur-préchargement des items/paiements (liste).
    select_related_fields = ['customer', 'sold_by']
    prefetch_related_fields = []

    # Filtre direct par ``warehouse_id`` ; les ventes legacy sans entrepôt
    # restent visibles aux non-owner pour ne pas masquer l'historique.
    warehouse_scope_field = 'warehouse_id'
    warehouse_scope_include_null = True
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.create',
        'update': 'sales.create',
        'partial_update': 'sales.create',
        'destroy': 'sales.cancel',
        'add_payment': 'sales.create',
        'cancel': 'sales.cancel',
        'today': 'sales.view',
        'stats': 'sales.view',
        # Marquer un reçu imprimé est la CONSÉQUENCE d'une impression, donc de
        # la lecture d'une vente. `frontend/actions/sales.actions.ts` l'appelle
        # depuis le POS et depuis le détail de vente ; l'action n'était pas
        # déclarée, donc 403 pour tous les rôles, en silence. `receipt_printed`
        # n'a jamais été écrit depuis le web, et la pastille DUPLICATA ne
        # pouvait donc pas distinguer une réimpression d'une première sortie.
        'mark_receipt_printed': 'sales.view',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SaleListSerializer
        elif self.action == 'create':
            return SaleCreateSerializer
        elif self.action == 'add_payment':
            return SalePaymentSerializer
        elif self.action in ('update', 'partial_update'):
            # Les montants et le statut d'une vente ne se modifient pas par PATCH :
            # ils suivent les règlements, l'annulation et les retours, seuls
            # chemins qui tiennent la dette client à jour.
            return SaleUpdateSerializer
        return SaleDetailSerializer

    def create(self, request, *args, **kwargs):
        """Créer une vente et retourner le détail complet (avec reference, id, etc.)."""
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Le serializer expose ``warehouse`` ; on revalide le périmètre ici
        # pour bloquer toute tentative de soumettre un entrepôt non autorisé.
        wh = serializer.validated_data.get('warehouse')
        wh_id = getattr(wh, 'id', None) if wh is not None else None
        assert_warehouse_allowed_for_request(request, wh_id, allow_none=True)
        sale = serializer.save()
        sale.refresh_from_db()
        detail_serializer = SaleDetailSerializer(sale)
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        """
        Filtres : (1) par date, (2) restriction aux ventes de l'utilisateur si
        celui-ci n'a pas la permission `sales.view_all` (i.e. caissier).
        """
        queryset = super().get_queryset()

        # Optimisation des requêtes par action :
        # - liste : compteur d'items via annotation `_items_count` (un seul COUNT
        #   agrégé au lieu d'une requête par ligne), sans précharger items/paiements
        #   que la liste ne sérialise pas.
        # - détail : préchargement complet des relations lues par SaleDetailSerializer
        #   (items → product/variant, payments → payment_method/received_by).
        if self.action == 'list':
            queryset = queryset.annotate(_items_count=Count('items'))
        else:
            queryset = queryset.select_related('register', 'warehouse', 'session').prefetch_related(
                'items__product', 'items__variant',
                'payments__payment_method', 'payments__received_by',
            )

        # Restriction par caissier : un cashier ne voit que ses propres ventes.
        # Owner et manager ont `sales.view_all` et voient tout.
        if not has_perm_code(self.request, 'sales.view_all'):
            queryset = queryset.filter(sold_by=self.request.user)

        # Filtres de date
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')

        if date_from:
            queryset = queryset.filter(sale_date__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(sale_date__date__lte=date_to)

        # Créances en retard : facture encore due dont l'échéance est passée.
        # `due_date` était jusqu'ici un champ mort - stocké, exposé, mais jamais
        # ni saisi ni interrogé.
        overdue = self.request.query_params.get('overdue')
        if overdue and overdue.lower() in ('1', 'true', 'yes'):
            queryset = queryset.filter(
                status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
                amount_due__gt=0,
                due_date__lt=timezone.now().date(),
            )

        return queryset
    
    @action(detail=True, methods=['post'], url_path='add-payment')
    def add_payment(self, request, pk=None):
        """Ajoute un paiement à une vente existante."""
        # Pré-validation hors transaction (permissions, statut grossier).
        # Le sale qu'on lit ici sert uniquement à valider l'accès - le vrai
        # objet utilisé pour la mise à jour est relu avec `select_for_update`
        # dans `apply_payment_to_sale`, qui ouvre sa propre transaction, pour
        # bloquer les race conditions sur `amount_paid` quand deux add_payment
        # concurrents arrivent.
        sale_for_check = self.get_object()
        if sale_for_check.sold_by_id != request.user.id and not is_manager_or_above(request):
            return Response(
                {'error': "Vous ne pouvez ajouter un paiement qu'à vos propres ventes."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if sale_for_check.status in ['completed', 'cancelled', 'refunded']:
            return Response(
                {'error': 'Impossible d\'ajouter un paiement à cette vente'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = SalePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Toute la mécanique (verrou, recalcul, stock, points, caisse, dette)
        # vit dans `apply_payment_to_sale` : la fiche client l'appelle aussi,
        # pour que les deux chemins de règlement produisent le même état.
        from .services import apply_payment_to_sale
        try:
            sale, _payment = apply_payment_to_sale(
                pk, request.user,
                payment_method_id=serializer.validated_data.get('payment_method'),
                tendered_amount=serializer.validated_data['amount'],
                currency=serializer.validated_data.get('currency'),
                exchange_rate=serializer.validated_data.get('exchange_rate'),
                change_currency=serializer.validated_data.get('change_currency'),
                reference=serializer.validated_data.get('reference', ''),
                notes=serializer.validated_data.get('notes', ''),
                points_used=serializer.validated_data.get('points_used', 0),
            )
        except DRFValidationError as exc:
            return Response({'error': exc.detail}, status=status.HTTP_400_BAD_REQUEST)

        return Response(SaleDetailSerializer(sale).data)

    def perform_destroy(self, instance):
        """
        Refuse la suppression d'une facture encore due.

        La suppression est un soft delete : la vente sort du manager, donc de
        `open_credit_sales`, alors que la dette reste inscrite au
        `CustomerBalance`. Elle devenait impossible à solder par facture et le
        solde du client ne correspondait plus à la somme de ses factures
        ouvertes. Annuler est le geste correct : `cancel` retire la dette,
        restitue le stock et enregistre le remboursement.
        """
        if instance.customer_id and instance.amount_due > 0:
            raise DRFValidationError(
                "Cette vente porte une dette client de "
                f"{instance.amount_due} {instance.currency}. Annulez-la "
                "(action « cancel ») plutôt que de la supprimer : la dette doit "
                "être retirée du solde du client."
            )
        return super().perform_destroy(instance)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """
        Annule une vente.

        Permission : `sold_by` (le caissier auteur) OU manager+. Le corps vit
        dans `services.cancel_sale`, que le journal du terminal appelle aussi.
        """
        from .services import AnnulationRefusee, cancel_sale

        sale = self.get_object()
        try:
            annulee = cancel_sale(
                sale, request.user,
                reason=request.data.get('reason', ''),
                autorise_toutes_ventes=is_manager_or_above(request),
            )
        except AnnulationRefusee as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)

        if not annulee:
            return Response(
                {'error': 'Cette vente est déjà annulée'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'cancelled'})

    @action(detail=False, methods=['get'])
    def today(self, request):
        """Retourne les ventes du jour avec pagination."""
        organization = self.get_organization()
        today = timezone.now().date()
        
        sales = Sale.objects.filter(
            organization=organization,
            sale_date__date=today,
            is_deleted=False
        ).select_related('customer', 'sold_by').order_by('-sale_date')
        
        page = self.paginate_queryset(sales)
        if page is not None:
            serializer = SaleListSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        
        serializer = SaleListSerializer(sales, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'], url_path='mark-receipt-printed')
    def mark_receipt_printed(self, request, pk=None):
        """Marque le reçu comme imprimé."""
        sale = self.get_object()
        
        sale.receipt_printed = True
        sale.save(update_fields=['receipt_printed'])
        
        return Response(SaleDetailSerializer(sale).data)

    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Retourne les statistiques de ventes."""
        organization = self.get_organization()
        
        # Paramètres de période
        period = request.query_params.get('period', 'today')
        today = timezone.now().date()
        
        if period == 'today':
            date_filter = {'sale_date__date': today}
        elif period == 'week':
            start = today - timezone.timedelta(days=7)
            date_filter = {'sale_date__date__gte': start}
        elif period == 'month':
            start = today - timezone.timedelta(days=30)
            date_filter = {'sale_date__date__gte': start}
        else:
            date_filter = {}
        
        sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            **date_filter
        )
        
        stats = sales.aggregate(
            total_sales=Sum('total'),
            total_tax=Sum('tax_amount'),
            total_discount=Sum('discount_amount'),
            count=Count('id')
        )
        
        avg_sale = 0
        if stats['count'] and stats['count'] > 0 and stats['total_sales']:
            avg_sale = stats['total_sales'] / stats['count']
        
        # Ventes par méthode de paiement
        by_payment = Payment.objects.filter(
            sale__in=sales,
            status='completed'
        ).values('payment_method__name').annotate(
            total=Sum('amount'),
            count=Count('id')
        )
        
        # Ventes par type
        by_type = sales.values('sale_type').annotate(
            total=Sum('total'),
            count=Count('id')
        )
        
        return Response({
            'summary': {
                'total_sales': str(stats['total_sales'] or 0),
                'total_tax': str(stats['total_tax'] or 0),
                'total_discount': str(stats['total_discount'] or 0),
                'count': stats['count'],
                'average': str(avg_sale)
            },
            'by_payment_method': list(by_payment),
            'by_type': list(by_type)
        })


# =============================================================================
# SALE RETURN VIEWSET
# =============================================================================

class SaleReturnViewSet(
    WarehouseScopedQuerysetMixin,
    TenantViewSetMixin,
    AuditMixin,
    viewsets.ModelViewSet,
):
    """
    ViewSet pour la gestion des retours de vente.
    
    Endpoints:
    - GET /sale-returns/ : Liste des retours
    - POST /sale-returns/ : Créer un retour
    - GET /sale-returns/{id}/ : Détail d'un retour
    - POST /sale-returns/{id}/approve/ : Approuver un retour
    - POST /sale-returns/{id}/reject/ : Rejeter un retour
    """
    
    queryset = SaleReturn.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'return_type']
    search_fields = ['reference', 'original_sale__reference']
    ordering = ['-return_date']
    
    select_related_fields = ['original_sale', 'created_by', 'approved_by']
    prefetch_related_fields = ['items', 'items__original_item__product']

    warehouse_scope_field = 'original_sale__warehouse_id'
    warehouse_scope_include_null = True
    
    action_permissions = {
        'list': 'sale_returns.view',
        'retrieve': 'sale_returns.view',
        'create': 'sale_returns.create',
        'approve': 'sale_returns.approve',
        'reject': 'sale_returns.approve',
        # Un retour avance par `approve` / `reject` : c'est là que le stock
        # est remis et la dette éteinte. Le réécrire ensuite ferait mentir
        # le bon déjà imprimé.
        'update': DENY,
        'partial_update': DENY,
        'destroy': DENY,
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return SaleReturnListSerializer
        elif self.action == 'create':
            return SaleReturnCreateSerializer
        return SaleReturnDetailSerializer

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """
        Approuve un retour et remet le stock.

        Corps dans `sales.returns_quotations`, partagé avec le journal du
        terminal : créer et approuver un retour sont des gestes de comptoir.
        """
        from .returns_quotations import TransitionRefusee, approve_return
        try:
            approve_return(self.get_object(), request.user)
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'approved'})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Rejette un retour."""
        from .returns_quotations import TransitionRefusee, reject_return
        try:
            reject_return(self.get_object(), request.user)
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'rejected'})

# =============================================================================
# QUOTATION VIEWSET
# =============================================================================

class QuotationViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des devis.
    
    Endpoints:
    - GET /quotations/ : Liste des devis
    - POST /quotations/ : Créer un devis
    - GET /quotations/{id}/ : Détail d'un devis
    - PUT/PATCH /quotations/{id}/ : Modifier un devis
    - DELETE /quotations/{id}/ : Supprimer un devis
    - POST /quotations/{id}/convert/ : Convertir en vente
    - POST /quotations/{id}/send/ : Marquer comme envoyé
    """
    
    queryset = Quotation.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission, TenantObjectPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'customer']
    search_fields = ['reference', 'customer__name']
    ordering = ['-created_at']
    
    select_related_fields = ['customer', 'created_by', 'converted_sale']
    prefetch_related_fields = ['items', 'items__product']
    
    action_permissions = {
        'list': 'sales.view',
        'retrieve': 'sales.view',
        'create': 'sales.create',
        'update': 'sales.create',
        'partial_update': 'sales.create',
        'destroy': 'sales.cancel',
        'convert': 'sales.create',
        'send': 'sales.create',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return QuotationListSerializer
        elif self.action == 'create':
            return QuotationCreateSerializer
        return QuotationDetailSerializer

    @action(detail=True, methods=['post'])
    def convert(self, request, pk=None):
        """
        Convertit un devis en vente.

        Le corps vit dans `sales.returns_quotations` ; la RÉSOLUTION de
        l'entrepôt reste ici, parce que c'est elle qui a besoin de la requête
        pour contrôler le périmètre du membre.
        """
        from .returns_quotations import (
            TransitionRefusee, convert_quotation, resolve_conversion_warehouse,
        )

        quotation = self.get_object()
        membership = get_membership_for_request(request)
        autorises = accessible_warehouse_ids(membership) if membership else None

        explicite = request.data.get('warehouse') if hasattr(request, 'data') else None
        if explicite:
            assert_warehouse_allowed_for_request(request, explicite)

        warehouse = resolve_conversion_warehouse(quotation, explicite, autorises)

        try:
            sale = convert_quotation(
                quotation, request.user, warehouse,
                perimetre_borne=autorises is not None,
            )
        except TransitionRefusee as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'status': 'converted',
            'sale_id': str(sale.id),
            'sale_reference': sale.reference,
        })

    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Marque un devis comme envoyé."""
        quotation = self.get_object()
        
        if quotation.status != 'draft':
            return Response(
                {'error': 'Seuls les devis en brouillon peuvent être envoyés'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        quotation.status = 'sent'
        quotation.save()

        # Email envoyé en best-effort : si l'API SMTP est down ou pas
        # configurée, on continue (la vente / le devis reste créé). Le mode
        # console (dev) affiche le mail en stdout pour vérification.
        from apps.core.email_service import send_quotation_email
        recipient = request.data.get('recipient_email') if hasattr(request, 'data') else None
        send_quotation_email(quotation, recipient_email=recipient)

        return Response({'status': 'sent'})
