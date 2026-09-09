"""
ViewSets DRF pour l'app Organizations.
"""
import secrets
from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from datetime import date, timedelta

from apps.core.api_mixins import TenantViewSetMixin, AuditMixin
from apps.core.api_permissions import (
    DENY,
    IsTenantMember, IsTenantAdmin, IsTenantOwner, IsTenantManager,
    HasActiveSubscription, HasPermission, _get_membership
)
from apps.core.services import OrganizationService, PermissionService
from apps.subscriptions.services import SubscriptionService
from rest_framework.exceptions import ValidationError
from .models import Organization, OrganizationMembership, Branch, OrganizationInvitation
from .serializers import (
    OrganizationListSerializer, OrganizationDetailSerializer,
    OrganizationCreateSerializer, OrganizationUpdateSerializer,
    OrganizationMembershipSerializer, MembershipCreateSerializer,
    MemberCreateWithUserSerializer, MembershipUpdateSerializer,
    MembershipPermissionsSerializer,
    BranchListSerializer, BranchDetailSerializer, BranchCreateSerializer,
    OrganizationInvitationSerializer, InvitationCreateSerializer
)


# =============================================================================
# ORGANIZATION VIEWSET
# =============================================================================

#: Longueur des trois périodes glissantes exprimées en jours, bornes incluses.
#: `year` fait bande à part : elle recule de douze MOIS, dont la longueur varie.
_LONGUEUR_EN_JOURS = {'day': 1, 'week': 7, 'month': 30}


def _premier_du_mois_recule(jour, mois):
    """Le 1er du mois situé `mois` mois avant celui de `jour`."""
    rang = jour.year * 12 + (jour.month - 1) - mois
    return date(rang // 12, rang % 12 + 1, 1)


def _periode_glissante(period, today):
    """
    Bornes du tableau de bord : ``(début, début précédent, fin précédente)``.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LES QUATRE PÉRIODES SONT GLISSANTES, ET C'EST LA SEULE FAÇON DE LES      │
    │ EMBOÎTER.                                                                │
    │                                                                          │
    │ Elles ne parlaient pas la même langue : `week` était glissante           │
    │ (`today - 6`), `month` et `year` calendaires (le 1er du mois, le 1er     │
    │ janvier). Le 1er septembre, « Mois » couvrait donc UNE SEULE JOURNÉE     │
    │ pendant que « Semaine » remontait au 26 août : une vente du 28 août      │
    │ figurait dans « Semaine » et dans « Année », et disparaissait de         │
    │ « Mois ». Le marchand y lisait une perte de données, et le défaut        │
    │ revenait les six premiers jours de CHAQUE mois.                          │
    │                                                                          │
    │ Tout passer en calendaire n'aurait rien réglé : le 1er septembre, la     │
    │ semaine calendaire commence le 31 août et déborde encore du mois. Seul   │
    │ le glissant garantit `jour ⊆ semaine ⊆ mois ⊆ année`, quel que soit le   │
    │ quantième. Les libellés le disent : « 7 jours », « 30 jours », « 12      │
    │ mois ».                                                                  │
    └──────────────────────────────────────────────────────────────────────────┘

    L'ANNÉE part du 1er d'un mois et non de ``today - 364`` : le graphique
    groupe par mois (``TruncMonth``), et une fenêtre à cheval rendrait treize
    seaux dont deux partiels, avec deux étiquettes « sept. » sur le même axe.

    La borne haute est toujours ``today``, jamais le futur. La période
    précédente s'arrête la veille du début de la courante et a la même
    longueur : sans cela, la variation comparerait deux fenêtres inégales et
    inventerait une hausse.

    **Recopiée dans `mobile/vf-marchand/src/features/tableau-de-bord/series.ts`**
    (`bornes`), et son invariant y est tenu par les mêmes dates témoins.
    La changer d'un côté seulement ferait donner deux chiffres différents au
    même établissement, sans que rien ne le signale.
    """
    if period == 'year':
        debut = _premier_du_mois_recule(today, 11)
        return debut, _premier_du_mois_recule(today, 23), debut - timedelta(days=1)

    jours = _LONGUEUR_EN_JOURS.get(period, _LONGUEUR_EN_JOURS['month'])
    debut = today - timedelta(days=jours - 1)
    fin_precedente = debut - timedelta(days=1)
    return debut, fin_precedente - timedelta(days=jours - 1), fin_precedente


def _dashboard_top_product(row):
    """
    Ligne « produit le plus vendu » du tableau de bord, quantité ventilée.

    Le partage vient des contenants réellement facturés, non d'une division du
    total : 5 casiers plus 120 bouteilles ne se relisent pas « 10 casiers », et
    le facteur d'un produit peut avoir changé depuis la vente.
    """
    from decimal import Decimal

    from apps.inventory.packaging import PackagingProfile, PackagingService

    profile = PackagingProfile.from_values(row)
    factor = PackagingService.factor(profile)
    quantity = Decimal(row['quantity_sold'] or 0)
    packages = Decimal(row.get('packages_sold') or 0)
    packaged_units = Decimal(row.get('packaged_units') or 0)

    if factor is not None and packages > 0:
        loose = max(Decimal('0'), quantity - packaged_units)
        display = PackagingService.format_split(profile, packages, loose)
    else:
        display = PackagingService.format_quantity(profile, quantity)

    return {
        'id': str(row['product__id']),
        'name': row['product__name'],
        'sku': row['product__sku'],
        'quantity': quantity,
        'quantity_display': display,
        'packaging_factor': factor,
        'revenue': str(row['total_revenue']),
    }


class OrganizationViewSet(viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des organisations.
    
    Endpoints:
    - GET /organizations/ : Liste des organisations de l'utilisateur
    - POST /organizations/ : Créer une organisation
    - GET /organizations/{id}/ : Détail d'une organisation
    - PUT/PATCH /organizations/{id}/ : Modifier une organisation
    - DELETE /organizations/{id}/ : Supprimer une organisation
    - POST /organizations/{id}/switch/ : Changer d'organisation active
    """
    
    queryset = Organization.objects.all()
    permission_classes = [IsAuthenticated]
    filter_backends = [filters.SearchFilter]
    search_fields = ['name']

    def get_queryset(self):
        """Retourne uniquement les organisations de l'utilisateur."""
        return Organization.objects.filter(
            memberships__user=self.request.user,
            memberships__is_active=True,
            is_deleted=False
        ).distinct()

    def get_serializer_class(self):
        if self.action == 'list':
            return OrganizationListSerializer
        elif self.action == 'create':
            return OrganizationCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return OrganizationUpdateSerializer
        return OrganizationDetailSerializer

    def get_permissions(self):
        if self.action in ['update', 'partial_update']:
            return [IsAuthenticated(), IsTenantAdmin()]
        elif self.action == 'destroy':
            return [IsAuthenticated(), IsTenantOwner()]
        return super().get_permissions()

    def perform_create(self, serializer):
        """Crée une organisation avec l'utilisateur comme owner."""
        data = serializer.validated_data
        organization = OrganizationService.create_organization(
            user=self.request.user,
            name=data['name'],
            business_type=data.get('business_type', 'boutique'),
            **{k: v for k, v in data.items() if k not in ['name', 'business_type']}
        )
        serializer.instance = organization

    def perform_destroy(self, instance):
        """Soft delete de l'organisation."""
        instance.soft_delete()

    @action(detail=True, methods=['post'])
    def switch(self, request, pk=None):
        """Change l'organisation active de l'utilisateur."""
        organization = self.get_object()
        
        # Vérifier que l'utilisateur est membre
        if not request.user.memberships.filter(
            organization=organization,
            is_active=True
        ).exists():
            return Response(
                {'error': 'Vous n\'êtes pas membre de cette organisation'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        request.user.active_organization = organization
        request.user.save(update_fields=['active_organization'])
        
        return Response({
            'message': 'Organisation active changée',
            'organization': OrganizationDetailSerializer(organization).data
        })

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Statistiques de l'organisation."""
        organization = self.get_object()
        
        from apps.products.models import Product
        from apps.contacts.models import Customer, Supplier
        from apps.sales.models import Sale
        from django.db.models import Sum, Count
        
        today = timezone.localdate()
        month_start = today.replace(day=1)
        
        stats = {
            'products': Product.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'customers': Customer.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'suppliers': Supplier.objects.filter(
                organization=organization, is_deleted=False
            ).count(),
            'members': organization.memberships.filter(is_active=True).count(),
            'branches': organization.branches.filter(is_deleted=False).count(),
            'sales_today': Sale.objects.filter(
                organization=organization,
                status='completed',
                sale_date__date=today
            ).aggregate(
                count=Count('id'),
                total=Sum('total')
            ),
            'sales_month': Sale.objects.filter(
                organization=organization,
                status='completed',
                sale_date__date__gte=month_start
            ).aggregate(
                count=Count('id'),
                total=Sum('total')
            )
        }
        
        return Response(stats)

    @action(detail=True, methods=['get'])
    def dashboard(self, request, pk=None):
        """Statistiques complètes pour le dashboard avec données d'évolution."""
        from django.core.cache import cache
        from django.db.models import Sum, Count, Avg, F, DecimalField
        from django.db.models.functions import TruncDate, TruncWeek, TruncMonth, Coalesce
        from decimal import Decimal
        from apps.products.models import Product
        from apps.contacts.models import Customer, Supplier
        from apps.sales.models import Sale, SaleItem, Payment
        from apps.inventory.models import Stock
        from apps.cashbook.services import primary_sum
        from apps.settings.services import CurrencyService

        organization = self.get_object()
        period = request.query_params.get('period', 'month')  # day, week, month, year

        # Cache court (60s) du payload dashboard par (org, période) : le dashboard
        # est rafraîchi souvent et ces agrégations sont lourdes. Une péremption de
        # 60s est acceptable pour des statistiques.
        cache_key = f"vf:dash:{organization.id}:{period}"
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)

        today = timezone.localdate()

        # Les quatre périodes sont GLISSANTES et donc emboîtées : voir
        # `_periode_glissante`, qui porte la règle et le motif.
        current_start, previous_start, previous_end = _periode_glissante(period, today)

        # Devise de lecture de TOUT cet écran. Chaque montant y est converti au
        # taux figé sur sa vente : voir l'encadré sur `primary_sum` plus bas.
        primary_currency = CurrencyService.primary_code(organization)
        
        # Ventes période actuelle
        current_sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            sale_date__date__gte=current_start,
            sale_date__date__lte=today
        )
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LE TABLEAU DE BORD EST UN ÉCRAN EN DEVISE PRINCIPALE.            │
        # │                                                                  │
        # │ Il sommait `Sum('total')` sans regarder `currency` : sur un       │
        # │ établissement qui facture en francs ET en dollars, il ajoutait    │
        # │ 107 000 à 50 et affichait 107 050 sous le symbole de la           │
        # │ principale, `CurrencyProvider` l'ayant posé pour tout le          │
        # │ back-office. Le chiffre n'existait pas, et rien ne le signalait.  │
        # │                                                                  │
        # │ `primary_sum` multiplie par `exchange_rate`, le taux FIGÉ sur la  │
        # │ vente au moment où elle a été faite. Jamais le taux du jour : un  │
        # │ tableau de bord dont les chiffres d'hier bougent avec le cours    │
        # │ n'est pas relisable, et le marchand ne saurait pas lequel croire. │
        # │                                                                  │
        # │ LE COÛT, LUI, N'EST PAS CONVERTI. `SaleItem.cost_price` vient de  │
        # │ `product.cost_price` ou du FIFO, et le catalogue n'a pas de       │
        # │ devise : il est DÉJÀ en principale. Lui appliquer le taux d'une   │
        # │ vente en francs le diviserait par deux mille huit cents, et la    │
        # │ marge affichée passerait de 40 % à 100 %.                         │
        # │                                                                  │
        # │ Le livre de caisse, lui, RESTE multi-devise : il rend la réalité  │
        # │ physique du tiroir, où les liasses ne se mélangent pas.           │
        # └──────────────────────────────────────────────────────────────────┘
        #
        # DEUX agrégats, et c'est OBLIGATOIRE : mêler dans un seul `aggregate`
        # un `Sum` sur la vente et un `Sum` sur `items__…` fait joindre les
        # lignes, et chaque vente est alors comptée UNE FOIS PAR LIGNE. Mesuré
        # sur un mois réel : `Sum('total')` seul rendait 107 356,40, la même
        # somme jointe rendait 217 227,70, soit un facteur 2,02 pour 22 lignes
        # réparties sur 12 ventes. Le chiffre d'affaires ET le nombre de ventes
        # étaient donc gonflés, et le bénéfice avec eux puisqu'il vaut ce total
        # moins un coût, lui correct : la marge affichée passait de 28,4 % à
        # 64,6 %. Un marchand décide sur ce chiffre.
        current_stats = current_sales.aggregate(
            total_sales=primary_sum('total'),
            count=Count('id'),
        )
        current_stats.update(
            SaleItem.objects.filter(sale__in=current_sales).aggregate(
                total_cost=Sum(F('cost_price') * F('quantity')),
                units_sold=Sum('quantity'),
            )
        )
        
        # Ventes période précédente
        previous_sales = Sale.objects.filter(
            organization=organization,
            status='completed',
            is_deleted=False,
            sale_date__date__gte=previous_start,
            sale_date__date__lte=previous_end
        )
        
        # Même séparation pour la période précédente : sans elle, la VARIATION
        # comparerait un total gonflé à un autre, avec des facteurs de jointure
        # différents selon le nombre de lignes de chaque période.
        previous_stats = previous_sales.aggregate(
            total_sales=primary_sum('total'),
            count=Count('id'),
        )
        previous_stats.update(
            SaleItem.objects.filter(sale__in=previous_sales).aggregate(
                units_sold=Sum('quantity'),
            )
        )
        
        # Calcul des variations
        def calc_variation(current, previous):
            if not previous or previous == 0:
                return 100 if current else 0
            return round(((current - previous) / previous) * 100, 1)
        
        # Le produit d'un montant par un taux à douze décimales en rend autant :
        # on arrête à deux, comme tout montant affiché. Le calcul, lui, s'est
        # fait en pleine précision.
        def en_principale(montant):
            return (montant or Decimal('0')).quantize(Decimal('0.01'))

        current_total = en_principale(current_stats['total_sales'])
        previous_total = en_principale(previous_stats['total_sales'])
        current_count = current_stats['count'] or 0
        previous_count = previous_stats['count'] or 0
        current_units = current_stats['units_sold'] or 0
        previous_units = previous_stats['units_sold'] or 0
        current_cost = en_principale(current_stats['total_cost'])
        gross_profit = current_total - current_cost
        
        # Clients
        total_customers = Customer.objects.filter(
            organization=organization, is_deleted=False
        ).count()
        
        new_customers_current = Customer.objects.filter(
            organization=organization,
            is_deleted=False,
            created_at__date__gte=current_start
        ).count()
        
        new_customers_previous = Customer.objects.filter(
            organization=organization,
            is_deleted=False,
            created_at__date__gte=previous_start,
            created_at__date__lte=previous_end
        ).count()
        
        # Données pour le graphique d'évolution des ventes. L'année groupe par
        # MOIS (elle en couvre douze, d'où le départ au 1er d'un mois dans
        # `_periode_glissante`), tout le reste par jour. Les trois branches
        # d'origine écrivaient la même requête à deux détails près, dont un
        # commentaire annonçant des heures que `TruncDate` ne produisait pas.
        troncature = TruncMonth('sale_date') if period == 'year' else TruncDate('sale_date')
        sales_evolution = current_sales.annotate(
            period=troncature
        ).values('period').annotate(
            total=primary_sum('total'),
            count=Count('id')
        ).order_by('period')
        
        # Formater les données d'évolution
        evolution_data = []
        for item in sales_evolution:
            evolution_data.append({
                'date': item['period'].isoformat() if item['period'] else None,
                'total': str(en_principale(item['total'])),
                'count': item['count'] or 0
            })
        
        # Top produits vendus. `packages_sold` cumule les contenants réellement
        # facturés : « 10 casiers + 5 bouteilles » se lit tout autrement que
        # « 245 unités » quand il s'agit de décider d'un réassort.
        top_products = SaleItem.objects.filter(
            sale__in=current_sales
        ).values(
            'product__id', 'product__name', 'product__sku',
            'product__selling_mode', 'product__units_per_package',
            'product__unit__name', 'product__packaging_unit__name',
        ).annotate(
            quantity_sold=Sum('quantity'),
            packages_sold=Coalesce(
                Sum('package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
            # `SaleItem` ne porte pas de taux : c'est celui de sa VENTE qui
            # ramène la recette en devise principale.
            total_revenue=Coalesce(
                Sum(F('total') * F('sale__exchange_rate')),
                Decimal('0'),
                output_field=DecimalField(max_digits=24, decimal_places=6),
            )
        ).order_by('-quantity_sold')[:10]
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ `Payment.amount` EST DÉJÀ DANS LA DEVISE DE LA VENTE.            │
        # │                                                                  │
        # │ Ce n'est PAS le billet reçu : celui-là est `tendered_amount`,     │
        # │ exprimé dans `Payment.currency`, et c'est lui qui entre           │
        # │ physiquement au tiroir. Pour ramener un règlement en principale,  │
        # │ le taux qui compte est donc celui de la VENTE, pas celui du       │
        # │ règlement (qui, lui, convertit le billet vers la facture).        │
        # └──────────────────────────────────────────────────────────────────┘
        paiements_periode = Payment.objects.filter(
            sale__in=current_sales,
            status='completed'
        )
        reglement_converti = Coalesce(
            Sum(F('amount') * F('sale__exchange_rate')),
            Decimal('0'),
            output_field=DecimalField(max_digits=24, decimal_places=6),
        )

        # Ventes par méthode de paiement
        by_payment_method = paiements_periode.values(
            'payment_method__name'
        ).annotate(
            total=reglement_converti,
            count=Count('id')
        ).order_by('-total')

        # Encaissements par DEVISE du billet reçu. C'est la question première
        # d'un établissement multi-devise - « en quelle monnaie l'argent est
        # entré » - et elle ne se lit nulle part ailleurs sur cet écran, qui est
        # tout entier converti. La PART se calcule sur le montant converti, sans
        # quoi 7 728 FC et 132 775 $ ne seraient pas comparables et l'anneau
        # mentirait ; le montant natif, lui, est celui que le caissier a compté.
        #
        # `tendered_amount` est nullable pour compatibilité : les anciennes
        # lignes mono-devise sont backfillées à `amount`, et le repli n'est juste
        # que dans ce cas précis - devise du règlement égale à celle de la vente.
        by_currency = paiements_periode.values('currency').annotate(
            native_total=Coalesce(
                Sum(Coalesce(F('tendered_amount'), F('amount'))),
                Decimal('0'),
                output_field=DecimalField(max_digits=24, decimal_places=6),
            ),
            primary_total=reglement_converti,
            count=Count('id'),
        ).order_by('-primary_total')
        
        # Stock bas
        low_stock_count = Stock.objects.filter(
            organization=organization,
            quantity__lte=F('product__reorder_point'),
            product__track_inventory=True,
            product__is_deleted=False
        ).count()
        
        # Valeur totale du stock - agrégée en base (une seule requête) au lieu de
        # charger tous les stocks en mémoire et sommer en Python. La règle de
        # coût unitaire vit dans `Stock.unit_cost_expression()` : elle était
        # recopiée ici, et `reports/summary` en appliquait une troisième,
        # différente. Un même stock s'affichait donc à deux valeurs.
        stock_value = Stock.total_value_for(
            Stock.objects.filter(
                organization=organization,
                product__is_deleted=False,
            )
        )

        payload = {
            'cards': {
                'total_sales': {
                    'value': str(current_total),
                    'variation': calc_variation(float(current_total), float(previous_total)),
                    'previous': str(previous_total)
                },
                'total_customers': {
                    'value': total_customers,
                    'new_count': new_customers_current,
                    'variation': calc_variation(new_customers_current, new_customers_previous)
                },
                'units_sold': {
                    'value': current_units,
                    'variation': calc_variation(current_units, previous_units),
                    'previous': previous_units
                },
                'gross_profit': {
                    'value': str(gross_profit),
                    'margin': round((float(gross_profit) / float(current_total) * 100), 1) if current_total > 0 else 0
                }
            },
            'charts': {
                'sales_evolution': evolution_data,
                'by_payment_method': [
                    {
                        'name': p['payment_method__name'] or 'Non défini',
                        'value': str(en_principale(p['total'])),
                        'count': p['count'],
                    }
                    for p in by_payment_method
                ],
                'by_currency': [
                    {
                        'code': c['currency'] or primary_currency,
                        'native_total': str(en_principale(c['native_total'])),
                        'primary_total': str(en_principale(c['primary_total'])),
                        'count': c['count'],
                    }
                    for c in by_currency
                ],
                'top_products': [
                    _dashboard_top_product(p) for p in top_products
                ]
            },
            'inventory': {
                'low_stock_count': low_stock_count,
                'stock_value': str(stock_value)
            },
            'period': period,
            # La devise de lecture de tout l'écran. Exposée plutôt que devinée :
            # le frontend étiquetterait sinon des montants convertis avec le
            # symbole que son contexte porte, sans jamais savoir s'ils le sont.
            'currency': primary_currency,
            'date_range': {
                'start': current_start.isoformat(),
                'end': today.isoformat()
            }
        }

        cache.set(cache_key, payload, timeout=60)
        return Response(payload)


# =============================================================================
# MEMBERSHIP VIEWSET
# =============================================================================

class OrganizationMembershipViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des membres d'organisation.
    
    Hiérarchie des rôles :
    - owner (Admin) peut gérer : manager, stock_keeper, cashier
    - manager (Gérant) peut gérer : stock_keeper, cashier
    - stock_keeper et cashier ne peuvent gérer personne
    
    Endpoints:
    - GET /memberships/ : Liste des membres
    - POST /memberships/ : Ajouter un membre existant (par email)
    - POST /memberships/create_user/ : Créer un nouvel utilisateur + l'ajouter
    - GET /memberships/{id}/ : Détail d'un membre
    - PUT/PATCH /memberships/{id}/ : Modifier le rôle / activer/désactiver
    - DELETE /memberships/{id}/ : Retirer un membre
    """
    
    queryset = OrganizationMembership.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['role', 'is_active']
    search_fields = ['user__email', 'user__first_name', 'user__last_name']
    ordering = ['user__email']
    
    select_related_fields = ['user', 'organization', 'invited_by']
    
    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'create': 'users.create',
        'create_user': 'users.create',
        'update': 'users.edit',
        'partial_update': 'users.edit',
        'destroy': 'users.deactivate',
        'manage_permissions': 'users.edit',
        'reset_password': 'users.edit',
    }

    def get_serializer_class(self):
        if self.action == 'create':
            return MembershipCreateSerializer
        elif self.action == 'create_user':
            return MemberCreateWithUserSerializer
        elif self.action in ['update', 'partial_update']:
            return MembershipUpdateSerializer
        return OrganizationMembershipSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if organization:
            queryset = queryset.filter(organization=organization).prefetch_related(
                'assigned_warehouses'
            )
        return queryset

    def _check_role_hierarchy(self, request, target_role):
        """
        Vérifie que l'utilisateur courant peut gérer le rôle cible.
        Retourne (ok, error_response).
        """
        membership = _get_membership(request)
        if not membership:
            return False, Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        if not membership.can_manage_role(target_role):
            return False, Response(
                {'detail': f"Vous n'avez pas la permission de gérer le rôle '{dict(OrganizationMembership.Role.choices).get(target_role, target_role)}'."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        return True, None

    def create(self, request, *args, **kwargs):
        """Ajoute un membre existant par email."""
        organization = self.get_organization()
        serializer = MembershipCreateSerializer(
            data=request.data,
            context={'organization': organization},
        )
        serializer.is_valid(raise_exception=True)
        
        email = serializer.validated_data['email']
        role = serializer.validated_data['role']
        warehouse_ids = serializer.validated_data['warehouse_ids']
        
        # Vérifier la hiérarchie des rôles
        ok, error = self._check_role_hierarchy(request, role)
        if not ok:
            return error
        
        # Empêcher de créer un owner
        if role == 'owner':
            return Response(
                {'detail': "Impossible de créer un autre administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        # Chercher l'utilisateur
        from apps.users.models import User
        user = User.objects.filter(email=email).first()
        
        if not user:
            return Response(
                {'detail': "Aucun utilisateur trouvé avec cet email. Utilisez 'Créer un utilisateur' pour créer un nouveau compte."},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Vérifier s'il est déjà membre
        existing = OrganizationMembership.objects.filter(
            user=user, organization=organization
        ).first()
        if existing:
            if existing.is_active:
                return Response(
                    {'detail': "Cet utilisateur est déjà membre de l'organisation."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            # Réactiver le membre
            existing.role = role
            existing.is_active = True
            existing.save()
            PermissionService.assign_role_permissions(user, organization, role)
            OrganizationService.sync_membership_assigned_warehouses(
                existing, warehouse_ids, organization
            )
            return Response(
                OrganizationMembershipSerializer(existing).data,
                status=status.HTTP_200_OK
            )
        
        membership = OrganizationService.add_member(
            organization=organization,
            user=user,
            role=role,
            invited_by=request.user,
            warehouse_ids=warehouse_ids,
        )
        return Response(
            OrganizationMembershipSerializer(membership).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=False, methods=['post'], url_path='create-user')
    def create_user(self, request):
        """
        Crée un nouvel utilisateur et l'ajoute à l'organisation.
        L'admin peut créer manager, stock_keeper, cashier.
        Le gérant peut créer stock_keeper, cashier uniquement.
        """
        organization = self.get_organization()
        serializer = MemberCreateWithUserSerializer(
            data=request.data,
            context={'organization': organization},
        )
        serializer.is_valid(raise_exception=True)
        
        role = serializer.validated_data['role']
        warehouse_ids = serializer.validated_data['warehouse_ids']
        
        # Vérifier la hiérarchie des rôles
        ok, error = self._check_role_hierarchy(request, role)
        if not ok:
            return error
        
        # Empêcher de créer un owner
        if role == 'owner':
            return Response(
                {'detail': "Impossible de créer un autre administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)

        # Créer l'utilisateur
        from apps.users.models import User
        from django.db import transaction
        
        with transaction.atomic():
            user = User.objects.create(
                email=serializer.validated_data['email'],
                first_name=serializer.validated_data['first_name'],
                last_name=serializer.validated_data['last_name'],
                phone=serializer.validated_data.get('phone', ''),
                is_active=True,
            )
            user.set_password(serializer.validated_data['password'])
            user.active_organization = organization
            user.save()
            
            membership = OrganizationService.add_member(
                organization=organization,
                user=user,
                role=role,
                invited_by=request.user,
                warehouse_ids=warehouse_ids,
            )
        
        return Response(
            OrganizationMembershipSerializer(membership).data,
            status=status.HTTP_201_CREATED
        )

    def update(self, request, *args, **kwargs):
        """Met à jour le rôle d'un membre."""
        membership = self.get_object()
        
        # Empêcher de modifier le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de modifier l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Empêcher de se modifier soi-même
        if membership.user == request.user:
            return Response(
                {'detail': "Vous ne pouvez pas modifier votre propre rôle."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        organization = self.get_organization()
        serializer = MembershipUpdateSerializer(
            data=request.data,
            context={
                'organization': organization,
                'membership': membership,
            },
        )
        serializer.is_valid(raise_exception=True)
        
        new_role = serializer.validated_data.get('role', membership.role)
        is_active = serializer.validated_data.get('is_active', membership.is_active)
        warehouse_ids = serializer.validated_data.get('warehouse_ids')
        extra_permissions = serializer.validated_data.get('extra_permissions')
        
        # Vérifier la hiérarchie pour le rôle actuel ET le nouveau rôle
        current_membership = _get_membership(request)
        if not current_membership:
            return Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut gérer le rôle actuel du membre
        if not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut attribuer le nouveau rôle
        if new_role != membership.role:
            if not current_membership.can_manage_role(new_role):
                return Response(
                    {'detail': f"Vous n'avez pas la permission d'attribuer le rôle '{dict(OrganizationMembership.Role.choices).get(new_role, new_role)}'."},
                    status=status.HTTP_403_FORBIDDEN
                )
        
        PermissionService.remove_all_permissions(membership.user, membership.organization)
        
        membership.role = new_role
        membership.is_active = is_active
        if extra_permissions is not None:
            membership.extra_permissions = extra_permissions
        membership.save()
        
        if is_active:
            PermissionService.assign_role_permissions(
                membership.user, membership.organization, new_role
            )

        if warehouse_ids is not None:
            OrganizationService.sync_membership_assigned_warehouses(
                membership, warehouse_ids, organization
            )
        
        return Response(OrganizationMembershipSerializer(membership).data)

    @action(detail=True, methods=['post'], url_path='reset-password')
    def reset_password(self, request, pk=None):
        """Réinitialise le mot de passe d'un membre (admin / gérant selon hiérarchie).

        Réservé aux rôles qui peuvent gérer le membre cible (owner → tous ;
        gérant → magasinier/caissier). On ne réinitialise pas l'administrateur
        principal ni son propre compte (utiliser « changer mon mot de passe »).
        """
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        membership = self.get_object()

        if membership.role == OrganizationMembership.Role.OWNER:
            return Response(
                {'detail': "Impossible de réinitialiser le mot de passe de l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if membership.user_id == request.user.id:
            return Response(
                {'detail': "Utilisez « changer mon mot de passe » pour votre propre compte."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        current_membership = _get_membership(request)
        if not current_membership or not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de gérer cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN,
            )

        new_password = (request.data or {}).get('new_password') or ''
        try:
            validate_password(new_password, user=membership.user)
        except DjangoValidationError as exc:
            return Response({'new_password': list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user = membership.user
        user.set_password(new_password)
        user.save(update_fields=['password'])

        # Invalide les refresh tokens en cours pour forcer une reconnexion.
        try:
            from rest_framework_simplejwt.token_blacklist.models import (
                OutstandingToken, BlacklistedToken,
            )
            for token in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=token)
        except Exception:
            pass  # token_blacklist non installé/migré : non bloquant

        from apps.users.models import UserActivity
        UserActivity.objects.create(
            user=request.user,
            organization=membership.organization,
            action=UserActivity.ActionType.UPDATE,
            resource_type='user',
            resource_id=str(user.id),
            details={'event': 'password_reset_by_admin', 'target_user_id': str(user.id)},
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:500],
        )
        return Response({'detail': 'Mot de passe réinitialisé avec succès.'})

    @action(detail=True, methods=['get', 'patch'], url_path='permissions')
    def manage_permissions(self, request, pk=None):
        """
        GET: Retourne les permissions du membre (rôle, extra, effectives).
        PATCH: Met à jour les permissions additionnelles du membre.
        """
        membership = self.get_object()
        
        if request.method == 'GET':
            return Response({
                'role': membership.role,
                'role_display': membership.get_role_display(),
                'role_permissions': PermissionService.get_role_permissions(membership.role),
                'extra_permissions': membership.extra_permissions or [],
                'effective_permissions': PermissionService.get_effective_permissions(membership),
                'all_permissions': PermissionService.get_all_permissions(),
            })
        
        # PATCH: modifier les permissions additionnelles
        # Empêcher de modifier le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de modifier les permissions de l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        current_membership = _get_membership(request)
        if not current_membership:
            return Response(
                {'detail': "Vous n'êtes pas membre de cette organisation."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Seuls owner et manager peuvent modifier les permissions
        if current_membership.role not in ['owner', 'manager']:
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier les permissions."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        # Vérifier qu'on peut gérer le rôle du membre cible
        if not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de modifier cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        serializer = MembershipPermissionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        membership.extra_permissions = serializer.validated_data['extra_permissions']
        membership.save(update_fields=['extra_permissions', 'updated_at'])
        
        return Response({
            'role': membership.role,
            'role_display': membership.get_role_display(),
            'role_permissions': PermissionService.get_role_permissions(membership.role),
            'extra_permissions': membership.extra_permissions,
            'effective_permissions': PermissionService.get_effective_permissions(membership),
        })

    def destroy(self, request, *args, **kwargs):
        """Retire un membre de l'organisation."""
        membership = self.get_object()
        
        # Empêcher de retirer le owner
        if membership.role == 'owner':
            return Response(
                {'detail': "Impossible de retirer l'administrateur principal."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Empêcher de se retirer soi-même
        if membership.user == request.user:
            return Response(
                {'detail': "Vous ne pouvez pas vous retirer vous-même."},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Vérifier la hiérarchie
        current_membership = _get_membership(request)
        if current_membership and not current_membership.can_manage_role(membership.role):
            return Response(
                {'detail': "Vous n'avez pas la permission de retirer cet utilisateur."},
                status=status.HTTP_403_FORBIDDEN
            )
        
        OrganizationService.remove_member(membership.organization, membership.user)
        
        return Response(status=status.HTTP_204_NO_CONTENT)


# =============================================================================
# BRANCH VIEWSET
# =============================================================================

class BranchViewSet(TenantViewSetMixin, AuditMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des branches/succursales.
    
    Endpoints:
    - GET /branches/ : Liste des branches
    - POST /branches/ : Créer une branche
    - GET /branches/{id}/ : Détail d'une branche
    - PUT/PATCH /branches/{id}/ : Modifier une branche
    - DELETE /branches/{id}/ : Supprimer une branche
    """
    
    queryset = Branch.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['is_active', 'is_main']
    search_fields = ['name', 'code', 'city']
    ordering = ['name']
    
    select_related_fields = ['manager']
    
    action_permissions = {
        'list': 'organization.view',
        'retrieve': 'organization.view',
        'create': 'organization.edit',
        'update': 'organization.edit',
        'partial_update': 'organization.edit',
        'destroy': 'organization.edit',
    }

    def get_serializer_class(self):
        if self.action == 'list':
            return BranchListSerializer
        elif self.action in ['create', 'update', 'partial_update']:
            return BranchCreateSerializer
        return BranchDetailSerializer

    def perform_create(self, serializer):
        """Vérifie les limites d'abonnement avant création."""
        organization = self.get_organization()
        SubscriptionService.assert_can_add_branch(organization)
        serializer.save(organization=organization)


# =============================================================================
# INVITATION VIEWSET
# =============================================================================

class OrganizationInvitationViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """
    ViewSet pour la gestion des invitations.
    
    Endpoints:
    - GET /invitations/ : Liste des invitations
    - POST /invitations/ : Créer une invitation
    - DELETE /invitations/{id}/ : Annuler une invitation
    - POST /invitations/{id}/resend/ : Renvoyer une invitation
    """
    
    queryset = OrganizationInvitation.objects.all()
    permission_classes = [IsAuthenticated, IsTenantMember, HasPermission]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'role']
    search_fields = ['email']
    ordering = ['-created_at']
    
    select_related_fields = ['organization', 'invited_by']
    
    action_permissions = {
        'list': 'users.view',
        'retrieve': 'users.view',
        'create': 'users.create',
        'destroy': 'users.deactivate',
        'resend': 'users.create',
        # Une invitation se renvoie (`resend`) ou s'annule (`destroy`). La
        # modifier changerait le rôle promis à une adresse déjà prévenue.
        'update': DENY,
        'partial_update': DENY,
    }

    def get_serializer_class(self):
        if self.action == 'create':
            return InvitationCreateSerializer
        return OrganizationInvitationSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        organization = self.get_organization()
        if organization:
            queryset = queryset.filter(organization=organization)
        return queryset

    def create(self, request, *args, **kwargs):
        """Crée une nouvelle invitation."""
        serializer = InvitationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        organization = self.get_organization()
        email = serializer.validated_data['email']
        role = serializer.validated_data['role']
        
        # Vérifier si déjà membre
        from apps.users.models import User
        user = User.objects.filter(email=email).first()
        if user and organization.memberships.filter(user=user, is_active=True).exists():
            return Response(
                {'error': 'Cet utilisateur est déjà membre'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Vérifier si invitation en cours
        existing = OrganizationInvitation.objects.filter(
            organization=organization,
            email=email,
            status='pending'
        ).first()
        
        if existing:
            return Response(
                {'error': 'Une invitation est déjà en cours pour cet email'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            SubscriptionService.assert_can_add_user(organization)
        except ValidationError as e:
            return Response(e.detail, status=status.HTTP_400_BAD_REQUEST)
        
        invitation = OrganizationInvitation.objects.create(
            organization=organization,
            email=email,
            role=role,
            token=secrets.token_urlsafe(32),
            invited_by=request.user,
            expires_at=timezone.now() + timedelta(days=7)
        )

        from apps.core.email_service import send_invitation_email
        send_invitation_email(invitation)

        return Response(
            OrganizationInvitationSerializer(invitation).data,
            status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=['post'])
    def resend(self, request, pk=None):
        """Renvoie une invitation."""
        invitation = self.get_object()
        
        if invitation.status != 'pending':
            return Response(
                {'error': 'Cette invitation n\'est plus en attente'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Prolonger l'expiration
        invitation.expires_at = timezone.now() + timedelta(days=7)
        invitation.save()

        from apps.core.email_service import send_invitation_email
        send_invitation_email(invitation)

        return Response({'message': 'Invitation renvoyée'})
