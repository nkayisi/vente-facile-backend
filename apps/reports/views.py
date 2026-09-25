from uuid import UUID

from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import ValidationError as DRFValidationError
from django.db.models import (
    Sum,
    Count,
    Avg,
    F,
    Q,
    DecimalField,
    ExpressionWrapper,
    Case,
    When,
    Value,
    DateField,
)
from django.db.models.functions import (
    Cast, Coalesce, TruncDate, TruncWeek, TruncMonth, TruncHour,
)
from django.utils import timezone
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from apps.core.mixins import TenantQuerysetMixin
from apps.core.api_mixins import ActionPaginationMixin, ExportResponseMixin
from apps.core.api_permissions import IsTenantMember, HasActiveSubscription, HasPermission
from apps.core.warehouse_scope import (
    accessible_warehouse_ids,
    assert_user_allowed_for_request,
    filter_cash_movement_queryset,
    assert_warehouse_allowed_for_request,
    get_membership_for_request,
)
from apps.organizations.models import OrganizationMembership
from apps.sales.models import Sale, SaleItem, Payment
from apps.sales.profit_allocation import allocated_line_ht_revenues_for_sale, effective_unit_cost
from apps.products.models import Product
from apps.inventory.models import Stock, StockBatch
from apps.inventory.packaging import PackagingProfile, PackagingService
from apps.contacts.models import Customer
from apps.settings.services import CurrencyService
from apps.cashbook.models import CashMovement, Expense

# Les deux refus du rapport d'activité, partagés par l'action paginée et par
# l'export : deux surfaces qui refusent le même acte doivent le dire pareil.
USER_ACTIVITY_REQUIS = "Le paramètre 'user' est requis."
USER_ACTIVITY_INCONNU = "Utilisateur introuvable dans cette organisation."
# Agrégations comptables : les montants de caisse/dépenses sont convertis en
# devise principale (montant × exchange_rate) avant d'être sommés. Le livre de
# caisse, lui, reste ventilé par devise - voir apps/cashbook/views.py.
from apps.cashbook.services import (
    balance_in_primary,
    last_balance_by_currency,
    primary_avg,
    primary_sum,
)

from .models import ReportTemplate, SavedReport, Dashboard
from .serializers import (
    ReportTemplateSerializer,
    SavedReportSerializer,
    DashboardSerializer,
    SalesStatsSerializer,
    SalesByPeriodSerializer,
    TopProductSerializer,
    TopCustomerSerializer,
    StockStatsSerializer,
    CashbookStatsSerializer,
    CashFlowByPeriodSerializer,
    CustomerStatsSerializer,
    SalesByCategorySerializer,
    SalesByPaymentMethodSerializer,
    DashboardSummarySerializer,
    DailyCashReportSerializer,
    DailyCashMovementSerializer,
    ProfitMarginSerializer,
    ProductProfitSerializer,
    StockDetailSerializer,
    StockMovementSummarySerializer,
)


class ReportTemplateViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet pour les modèles de rapports"""
    
    queryset = ReportTemplate.objects.all()
    serializer_class = ReportTemplateSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'reports.view',
        'retrieve': 'reports.view',
        'create': 'reports.create',
        'update': 'reports.create',
        # PATCH était absent quand PUT était là : la modification partielle
        # répondait 403 pendant que la modification complète passait. Une
        # asymétrie de ce genre ne se lit dans aucun message d'erreur.
        'partial_update': 'reports.create',
        'destroy': 'reports.delete',
    }
    
    def perform_create(self, serializer):
        serializer.save(
            organization=self.get_organization(),
            created_by=self.request.user
        )


class DashboardViewSet(TenantQuerysetMixin, viewsets.ModelViewSet):
    """ViewSet pour les dashboards personnalisés"""
    
    queryset = Dashboard.objects.all()
    serializer_class = DashboardSerializer
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        'list': 'reports.view',
        'retrieve': 'reports.view',
        'create': 'reports.create',
        'update': 'reports.create',
        # PATCH était absent quand PUT était là : la modification partielle
        # répondait 403 pendant que la modification complète passait. Une
        # asymétrie de ce genre ne se lit dans aucun message d'erreur.
        'partial_update': 'reports.create',
        'destroy': 'reports.delete',
    }
    
    def perform_create(self, serializer):
        serializer.save(
            organization=self.get_organization(),
            created_by=self.request.user
        )


class StatisticsViewSet(ExportResponseMixin, ActionPaginationMixin,
                        TenantQuerysetMixin, viewsets.ViewSet):
    """
    ViewSet pour les statistiques et rapports en temps réel.
    Fournit des endpoints pour différentes métriques de l'entreprise.
    """
    
    permission_classes = [IsAuthenticated, IsTenantMember, HasActiveSubscription, HasPermission]
    
    action_permissions = {
        # Le résumé alimente la page d'accueil (dashboard) de tous les rôles :
        # accessible via `dashboard.view`, les données restant scopées par rôle
        # (entrepôt pour gérant/magasinier, propres données pour le caissier).
        'summary': 'dashboard.view',
        'sales': 'reports.view',
        'sales_by_period': 'reports.view',
        'sales_by_category': 'reports.view',
        'sales_by_payment_method': 'reports.view',
        'top_products': 'reports.view',
        'top_customers': 'reports.view',
        'stock': 'reports.view',
        'stock_details': 'reports.view',
        'stock_movements_summary': 'reports.view',
        # Sans cette entrée, `HasPermission` refuse l'action à TOUS les rôles
        # (« action non listée = accès refusé »), alors que le frontend l'appelle.
        'product_supplies': 'reports.view',
        'cashbook': 'reports.view',
        'cash_flow': 'reports.view',
        'daily_cash_report': 'reports.view',
        'customers': 'reports.view',
        'receivables': 'reports.view',
        'profit_margins': 'reports.view',
        'product_profits': 'reports.view',
        'sales_by_packaging': 'reports.view',
        'user_activity': 'reports.view',
        # Exporter, c'est LIRE : le fichier ne porte rien que l'écran n'affiche
        # déjà, et les mêmes `_scope_*` s'appliquent. Sans cette entrée,
        # `HasPermission` refuse la route à TOUS les rôles, en silence.
        'export': 'reports.view',
    }
    
    def _accessible_warehouse_ids(self, request):
        """Renvoie ``None`` (owner = tout) ou la liste des UUID autorisés.

        Utilisé pour scoper toutes les statistiques au périmètre du membre.
        """
        membership = get_membership_for_request(request)
        if not membership:
            return None
        return accessible_warehouse_ids(membership)

    def _is_cashier(self, request):
        """True si le membre courant est un caissier (visibilité = ses données)."""
        membership = get_membership_for_request(request)
        return (
            membership is not None
            and membership.role == OrganizationMembership.Role.CASHIER
        )

    def _apply_creator_scope(self, qs, request, creator_field):
        """Restreint aux enregistrements créés par l'utilisateur si caissier.

        Garantit que les KPIs/rapports d'un caissier n'agrègent que ses propres
        données (ventes, dépenses, mouvements), conformément à la visibilité
        par rôle. Sans effet pour owner/gérant/magasinier.
        """
        if self._is_cashier(request):
            return qs.filter(**{creator_field: request.user})
        return qs

    def _perimetre_voulu(self, request):
        """
        Le filtre VOLONTAIRE de la requête, déjà opposé au périmètre du rôle.

        Rend `(warehouse_id|None, user_id|None)`.

        ┌──────────────────────────────────────────────────────────────────┐
        │ ON REFUSE, ON N'IGNORE JAMAIS.                                   │
        │                                                                  │
        │ Retirer en silence un entrepôt hors périmètre rendrait un écran  │
        │ qui affiche « Entrepôt B » au-dessus des chiffres de A, et le    │
        │ marchand n'aurait aucun moyen de s'en apercevoir. `ValidationError`│
        │ rend 400, et la forme `{'user': "..."}` est celle que            │
        │ `user_activity` emploie déjà : les deux clients savent la rendre. │
        └──────────────────────────────────────────────────────────────────┘

        Mémoïsé sur la requête : `summary` appelle six `_scope_*`, et valider
        six fois lèverait six fois - ou, pire, cesserait de lever le jour où un
        appelant avalerait la première exception.
        """
        cache = getattr(request, '_reports_perimetre_voulu', None)
        if cache is None:
            wid = request.query_params.get('warehouse') or None
            uid = request.query_params.get('user') or None
            if wid:
                assert_warehouse_allowed_for_request(request, wid)
            if uid:
                assert_user_allowed_for_request(request, uid)
            cache = (wid, uid)
            request._reports_perimetre_voulu = cache
        return cache

    def _vouloir(self, qs, request, *, warehouse_field, user_field):
        """
        Superpose le filtre volontaire au périmètre du rôle, jamais l'inverse.

        ⚠ LE VOLONTAIRE N'HÉRITE PAS DU `| warehouse__isnull=True` que portent
        les `_scope_*`. Cette tolérance est délibérée pour le RÔLE (ventes
        anciennes sans entrepôt, dépenses d'établissement) ; l'étendre au
        filtre ferait entrer les mêmes lignes dans le total de CHAQUE entrepôt,
        et la somme des dépôts dépasserait le total.

        `user_field` à `None` : cette rubrique n'a pas d'auteur (un stock est un
        état, pas un acte). On IGNORE alors l'utilisateur voulu plutôt que de
        rendre `.none()` : un onglet qui affiche zéro parce qu'un filtre d'un
        autre onglet a traîné se lit comme une perte de données.
        """
        wid, uid = self._perimetre_voulu(request)
        if wid:
            qs = qs.filter(**{warehouse_field: wid})
        if uid and user_field:
            qs = qs.filter(**{user_field: uid})
        return qs

    def _scope_sales(self, qs, request):
        """Applique le filtre warehouse aux ventes (+ créateur si caissier)."""
        qs = self._apply_creator_scope(qs, request, 'sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            qs = qs.filter(warehouse__isnull=True)
        else:
            qs = qs.filter(Q(warehouse_id__in=wh_ids) | Q(warehouse__isnull=True))
        return self._vouloir(
            qs, request, warehouse_field='warehouse_id', user_field='sold_by_id'
        )

    def _scope_sale_items(self, qs, request):
        """Applique le filtre warehouse aux items de vente via ``sale``."""
        qs = self._apply_creator_scope(qs, request, 'sale__sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            qs = qs.filter(sale__warehouse__isnull=True)
        else:
            qs = qs.filter(
                Q(sale__warehouse_id__in=wh_ids) | Q(sale__warehouse__isnull=True)
            )
        return self._vouloir(
            qs, request,
            warehouse_field='sale__warehouse_id', user_field='sale__sold_by_id',
        )

    def _scope_payments(self, qs, request):
        """Applique le filtre warehouse aux paiements via ``sale``."""
        qs = self._apply_creator_scope(qs, request, 'sale__sold_by')
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            qs = qs.filter(sale__warehouse__isnull=True)
        else:
            qs = qs.filter(
                Q(sale__warehouse_id__in=wh_ids) | Q(sale__warehouse__isnull=True)
            )
        return self._vouloir(
            qs, request,
            warehouse_field='sale__warehouse_id', user_field='sale__sold_by_id',
        )

    def _scope_cash_movements(self, qs, request):
        """
        Le périmètre d'un mouvement de caisse, par le helper PARTAGÉ.

        ┌──────────────────────────────────────────────────────────────────┐
        │ CE CORPS TOLÉRAIT CE QUE LE LIVRE DE CAISSE REFUSE.              │
        │                                                                  │
        │ Il portait trois clauses `isnull` de plus que                     │
        │ `CashMovementViewSet`, si bien que le même gérant lisait un solde │
        │ ici et un AUTRE là - deux écrans voisins du même back-office.     │
        │                                                                  │
        │ Le strict l'emporte : un apport sans tiroir est une opération     │
        │ d'établissement, comme un loyer, et elle reste au propriétaire.   │
        │ C'est la règle que `ExpenseViewSet` applique déjà aux dépenses    │
        │ sans entrepôt.                                                    │
        └──────────────────────────────────────────────────────────────────┘
        """
        qs = filter_cash_movement_queryset(qs, get_membership_for_request(request))
        # Le filtre VOLONTAIRE, lui, reste sans clause `isnull` : un apport sans
        # pièce apparaîtrait sinon sous chaque dépôt, et la somme des dépôts
        # dépasserait le total du tiroir.
        wid, uid = self._perimetre_voulu(request)
        if wid:
            qs = qs.filter(
                Q(sale__warehouse_id=wid)
                | Q(expense__warehouse_id=wid)
                | Q(session__register__warehouse_id=wid)
            )
        if uid:
            qs = qs.filter(created_by_id=uid)
        return qs

    def _scope_expenses(self, qs, request):
        """Applique le filtre warehouse aux dépenses (+ créateur si caissier)."""
        if self._is_cashier(request):
            qs = qs.filter(created_by=request.user)
        else:
            wh_ids = self._accessible_warehouse_ids(request)
            if wh_ids is None:
                pass
            elif not wh_ids:
                qs = qs.filter(warehouse__isnull=True)
            else:
                qs = qs.filter(
                    Q(warehouse_id__in=wh_ids) | Q(warehouse__isnull=True)
                )
        return self._vouloir(
            qs, request, warehouse_field='warehouse_id', user_field='created_by_id'
        )

    def _scope_stocks(self, qs, request):
        """Applique le filtre warehouse aux stocks (warehouse strict)."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            return qs.none()
        else:
            qs = qs.filter(warehouse_id__in=wh_ids)
        # `user_field=None` : un stock est un ÉTAT, pas un acte. L'utilisateur
        # voulu y est ignoré, jamais traduit en `.none()`.
        return self._vouloir(
            qs, request, warehouse_field='warehouse_id', user_field=None
        )

    def _scope_stock_batches(self, qs, request):
        """Applique le filtre warehouse aux lots de stock (warehouse strict)."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            return qs.none()
        else:
            qs = qs.filter(warehouse_id__in=wh_ids)
        return self._vouloir(
            qs, request, warehouse_field='warehouse_id', user_field=None
        )

    def _scope_stock_movements(self, qs, request):
        """Applique le filtre warehouse aux mouvements de stock."""
        wh_ids = self._accessible_warehouse_ids(request)
        if wh_ids is None:
            pass
        elif not wh_ids:
            return qs.none()
        else:
            qs = qs.filter(warehouse_id__in=wh_ids)
        return self._vouloir(
            qs, request, warehouse_field='warehouse_id', user_field='created_by_id'
        )

    def _parse_date_range(self, request):
        """
        Bornes de la période demandée.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE DÉFAUT ÉTAIT `month`, ET `month` EST CALENDAIRE.                  │
        │                                                                      │
        │ Un appel sans paramètre couvrait donc du 1er du mois à aujourd'hui.  │
        │ Relevé le 2 septembre 2026 sur les vraies données : DEUX JOURS,      │
        │ zéro vente - la dernière datait du 31 août - pendant que la même     │
        │ base rendait 18 ventes sur trente jours glissants. Toutes les        │
        │ rubriques annonçaient « Aucune donnée », et le marchand y lit une    │
        │ perte de données. Le défaut revenait les premiers jours de CHAQUE    │
        │ mois, et le terminal, qui n'envoie aucune date, y tombait toujours.  │
        │                                                                      │
        │ Le défaut est désormais GLISSANT sur trente jours. Les périodes      │
        │ calendaires restent offertes et gardent leur sens : le back-office   │
        │ les nomme « Ce mois », « Cette année », et une fenêtre nommée par un │
        │ calendrier doit suivre le calendrier. Ce qui était faux, c'était de  │
        │ l'imposer à qui ne demande rien.                                     │
        └──────────────────────────────────────────────────────────────────────┘

        Deux familles, et les libellés de l'interface disent laquelle :

        - CALENDAIRES : `today`, `week` (depuis lundi), `month` (depuis le 1er),
          `quarter`, `year` (depuis le 1er janvier).
        - GLISSANTES : `last_7_days`, `last_30_days`, `last_12_months`. Seules
          celles-ci s'emboîtent quel que soit le quantième, ce qui est la règle
          déjà retenue pour le tableau de bord (`_periode_glissante`).

        Les DATES EXPLICITES (`date_from` / `date_to`) priment sur tout, et
        c'est ce que le terminal envoie : la fenêtre ne dépend alors plus d'un
        défaut serveur qu'il ne voit pas.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ « AUJOURD'HUI » EST LE JOUR DU MARCHAND, PAS CELUI DE GREENWICH.     │
        │                                                                      │
        │ Les bornes se lisaient en `timezone.now().date()`, donc en UTC,      │
        │ pendant que le filtre `sale_date__date` résout en `TIME_ZONE`        │
        │ (Africa/Kinshasa, UTC+1). Entre 23h et minuit UTC, il est déjà le    │
        │ lendemain à Kinshasa : `end_date` désignait la veille, et TOUTE      │
        │ vente de la première heure du jour sortait de la fenêtre. Le         │
        │ marchand qui ouvre après minuit lit « Aucune donnée » sur les huit   │
        │ onglets, une heure durant, alors qu'il vient d'encaisser.            │
        │                                                                      │
        │ D'où `localdate()` ici et partout où ce module date un jour. C'est   │
        │ la règle déjà posée par `day_bounds()` côté inventaire.              │
        └──────────────────────────────────────────────────────────────────────┘
        """
        period = request.query_params.get('period', 'last_30_days')
        date_from = request.query_params.get('date_from')
        date_to = request.query_params.get('date_to')
        
        today = timezone.localdate()
        
        if date_from and date_to:
            from datetime import datetime

            # Une date malformée est une erreur de l'appelant, pas du serveur :
            # sans ce garde-fou, `strptime` remonte en 500 et masque la cause.
            try:
                start_date = datetime.strptime(date_from, '%Y-%m-%d').date()
                end_date = datetime.strptime(date_to, '%Y-%m-%d').date()
            except ValueError:
                raise DRFValidationError({
                    'date_from': "Dates attendues au format AAAA-MM-JJ.",
                })
            if start_date > end_date:
                raise DRFValidationError({
                    'date_from': "La date de début doit précéder la date de fin.",
                })
        elif period == 'today':
            start_date = today
            end_date = today
        elif period == 'week':
            start_date = today - timedelta(days=today.weekday())
            end_date = today
        elif period == 'month':
            start_date = today.replace(day=1)
            end_date = today
        elif period == 'quarter':
            quarter = (today.month - 1) // 3
            start_date = today.replace(month=quarter * 3 + 1, day=1)
            end_date = today
        elif period == 'year':
            start_date = today.replace(month=1, day=1)
            end_date = today
        elif period == 'last_7_days':
            # Sept jours AUJOURD'HUI INCLUS : `- 7` en ferait huit, et la
            # comparaison à la période précédente porterait sur deux fenêtres
            # inégales, ce qui inventerait une variation.
            start_date = today - timedelta(days=6)
            end_date = today
        elif period == 'last_12_months':
            # Douze mois PLEINS, dont le mois en cours : on part du 1er du mois
            # situé onze mois en arrière. Une fenêtre à cheval rendrait treize
            # seaux dont deux partiels, avec deux étiquettes « sept. » sur le
            # même axe. Même règle qu'au tableau de bord.
            premier = today.replace(day=1)
            recule = premier.year * 12 + (premier.month - 1) - 11
            start_date = premier.replace(year=recule // 12, month=recule % 12 + 1)
            end_date = today
        else:
            # `last_30_days`, et tout ce qui n'est pas reconnu : trente jours
            # glissants, aujourd'hui inclus.
            start_date = today - timedelta(days=29)
            end_date = today
        
        # Période précédente pour comparaison
        period_length = (end_date - start_date).days + 1
        prev_end_date = start_date - timedelta(days=1)
        prev_start_date = prev_end_date - timedelta(days=period_length - 1)
        
        return start_date, end_date, prev_start_date, prev_end_date

    @staticmethod
    def _resolve_activity_member(org, target_id):
        """
        L'employé visé par le rapport d'activité, ou `None`.

        Rend `None` plutôt que de lever : l'action et l'export refusent tous
        deux, mais ils doivent le faire avec LEURS codes (400 puis 404) et avec
        le message porté par `USER_ACTIVITY_REQUIS` / `USER_ACTIVITY_INCONNU`,
        que les deux chemins partagent pour ne pas dériver.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ `is_active` N'EST PAS UN DÉTAIL : SANS LUI, L'IDENTITÉ FUIT.         │
        │                                                                      │
        │ La composition protège les CHIFFRES - `_scope_sales` et ses sœurs    │
        │ bornent au périmètre du demandeur - mais pas le bloc `user` de la    │
        │ réponse, qui porte le nom, l'E-MAIL et le rôle de la cible.          │
        │                                                                      │
        │ Et rien ne rattrapait : `assert_user_allowed_for_membership` cherche │
        │ la cible en `is_active=True`, ne la trouvait pas, et prenait sa      │
        │ branche PERMISSIVE (« un membre introuvable n'est pas notre          │
        │ affaire »). Un gérant du dépôt A visait donc un employé DÉSACTIVÉ du │
        │ dépôt B et recevait 200 avec son adresse.                            │
        │                                                                      │
        │ ⚠ Le commentaire qui justifie cette branche permissive (« un         │
        │ magasinier reçoit déjà la liste complète de l'équipe par sa          │
        │ session ») n'est plus vrai depuis que `build_team_payload` est borné.│
        │ Le roster est désormais plus STRICT que la validation, et c'est le   │
        │ seul sens que le croisement à deux sens ne couvrait pas.             │
        │                                                                      │
        │ On s'aligne donc sur le roster : un membre désactivé n'est ni        │
        │ proposé, ni visable. `USER_ACTIVITY_INCONNU` est la bonne réponse -  │
        │ pour ce demandeur, il n'existe pas.                                  │
        └──────────────────────────────────────────────────────────────────────┘

        ⚠ L'identifiant est LU avant d'atteindre l'ORM. `?user=oops` faisait
        lever `django.core.exceptions.ValidationError` à `.filter()`, que DRF
        ne sait pas traduire : 500. La garde de `warehouse_scope` existe, mais
        elle est en aval - cette méthode la devance.
        """
        if not target_id:
            return None
        try:
            uid = target_id if isinstance(target_id, UUID) else UUID(str(target_id))
        except (TypeError, ValueError):
            return None
        return OrganizationMembership.objects.filter(
            organization=org, user_id=uid, is_active=True
        ).select_related('user').first()

    def _user_activity_data(self, request, org, membership, start_date, end_date, group_by):
        """
        L'activité d'un employé sur la période, déjà quantifiée.

        Le `breakdown` n'a jamais été paginé : cette méthode rend donc la
        période entière, et l'action comme l'export en tirent le même contenu.
        """
        target_user = membership.user

        money = dict(output_field=DecimalField())

        # --- Ventes de l'utilisateur (scopées au périmètre du demandeur) ---
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sold_by=target_user,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
            ),
            request,
        )
        completed_sales = sales.filter(status__in=['completed', 'partially_paid'])
        sales_agg = completed_sales.aggregate(
            count=Count('id'),
            total=primary_sum('total'),
        )

        # Ventilation par méthode de paiement
        payments = self._scope_payments(
            Payment.objects.filter(
                organization=org,
                sale__sold_by=target_user,
                status='completed',
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
            ),
            request,
        ).select_related('payment_method', 'sale')
        by_method = {}
        for p in payments:
            name = p.payment_method.name if p.payment_method else 'Autre'
            # `Payment.amount` est déjà dans la devise de la VENTE : c'est donc
            # le taux de celle-ci qui ramène en principale, pas le sien, qui
            # convertit le billet reçu vers la facture. Sans `select_related`
            # sur la vente, cette ligne ferait une requête par règlement.
            taux = (p.sale.exchange_rate if p.sale_id else None) or Decimal('1')
            by_method[name] = by_method.get(name, Decimal('0')) + p.amount * taux

        # --- Dépenses créées par l'utilisateur ---
        expenses = self._scope_expenses(
            Expense.objects.filter(
                organization=org,
                created_by=target_user,
                expense_date__gte=start_date,
                expense_date__lte=end_date,
            ),
            request,
        )
        # Converti en devise principale : une dépense peut être en USD dans une
        # org CDF, on ne somme jamais des montants bruts de devises différentes.
        expenses_agg = expenses.aggregate(
            count=Count('id'),
            total=primary_sum('amount'),
        )

        # --- Mouvements de caisse créés par l'utilisateur ---
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                created_by=target_user,
                is_cancelled=False,
                movement_date__date__gte=start_date,
                movement_date__date__lte=end_date,
            ),
            request,
        )
        cash_in = movements.filter(direction='in').aggregate(
            total=primary_sum('amount'))['total']
        cash_out = movements.filter(direction='out').aggregate(
            total=primary_sum('amount'))['total']

        # --- Ventilation temporelle des ventes (heure ou jour) ---
        trunc = TruncHour('sale_date') if group_by == 'hour' else TruncDate('sale_date')
        breakdown_qs = (
            completed_sales.annotate(bucket=trunc)
            .values('bucket')
            .annotate(
                count=Count('id'),
                total=primary_sum('total'),
            )
            .order_by('bucket')
        )
        fmt = '%Y-%m-%d %H:00' if group_by == 'hour' else '%Y-%m-%d'
        breakdown = [
            {
                'bucket': row['bucket'].strftime(fmt) if row['bucket'] else '',
                'count': row['count'],
                'total': (row['total'] or Decimal('0')).quantize(Decimal('0.01')),
            }
            for row in breakdown_qs
        ]

        # ┌──────────────────────────────────────────────────────────────────┐
        # │ CET ENDPOINT N'A PAS DE SERIALIZER : IL QUANTIFIE LUI-MÊME.      │
        # │                                                                  │
        # │ Un montant converti est le produit d'une somme par un taux à      │
        # │ douze décimales, et il sortait ici tel quel : la dépense d'un     │
        # │ vendeur s'affichait « 1.5217391315 ». Ses voisins passent par un  │
        # │ serializer qui arrondit ; celui-ci rend un dictionnaire nu.       │
        # └──────────────────────────────────────────────────────────────────┘
        def sou(montant):
            return (montant or Decimal('0')).quantize(Decimal('0.01'))

        return {
            'user': {
                'id': str(target_user.id),
                'name': f"{target_user.first_name} {target_user.last_name}".strip() or target_user.email,
                'email': target_user.email,
                'role': membership.role,
            },
            'period': {
                'start': start_date.strftime('%Y-%m-%d'),
                'end': end_date.strftime('%Y-%m-%d'),
                'group_by': group_by,
            },
            'sales': {
                'count': sales_agg['count'],
                'total': sou(sales_agg['total']),
                'by_payment_method': [
                    {'method': k, 'total': sou(v)} for k, v in by_method.items()
                ],
            },
            'expenses': {
                'count': expenses_agg['count'],
                'total': sou(expenses_agg['total']),
            },
            'cash': {
                'cash_in': sou(cash_in),
                'cash_out': sou(cash_out),
                'net': sou(cash_in - cash_out),
            },
            'breakdown': breakdown,
        }

    @action(detail=False, methods=['get'], url_path='export')
    def export(self, request):
        """
        Exporte un onglet de « Rapports & Statistiques », en PDF, classeur ou CSV.

        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE PÉRIMÈTRE ENTIER, JAMAIS LA PAGE AFFICHÉE.                        │
        │                                                                      │
        │ Les deux surfaces fabriquaient leur document à partir des vingt       │
        │ lignes chargées, sous un en-tête qui annonçait le total. Ici les      │
        │ constructeurs de lignes sont appelés SANS pagination.                 │
        └──────────────────────────────────────────────────────────────────────┘

        Ni `ExportableListMixin` ni `filter_queryset` : ce ViewSet n'a pas de
        queryset unique, et un onglet croise jusqu'à trois rubriques. Le
        périmètre vient donc des mêmes `_scope_*` que les actions paginées.
        """
        from .exports import EXPORT_BASENAMES, build_tab_spec

        fmt = self.get_export_format(request)
        tab = (request.query_params.get('tab') or 'overview').strip()

        # Le refus se fait ICI et non dans le constructeur, pour porter le même
        # littéral et la même FORME que l'action paginée : `ValidationError`
        # rendrait une liste là où l'action rend une chaîne, et le back-office
        # branche son message de champ sur cette forme.
        if tab == 'user-activity' and not request.query_params.get('user'):
            return Response(
                {'user': USER_ACTIVITY_REQUIS},
                status=status.HTTP_400_BAD_REQUEST,
            )

        spec = build_tab_spec(self, request, tab)
        return self.render_export(spec, EXPORT_BASENAMES.get(tab, 'rapport'), fmt)

    @action(detail=False, methods=['get'])
    def user_activity(self, request):
        """Rapport d'activité d'un utilisateur sur une période (avec heures).

        Params : ``user`` (id, requis), ``date_from``/``date_to`` ou ``period``,
        ``group_by=hour|day`` (défaut ``day``). Réservé à ``reports.view``
        (owner + gérant) ; les données restent scopées au périmètre du
        demandeur (un gérant ne voit que l'activité dans ses entrepôts).
        """
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')

        target_id = request.query_params.get('user')
        if not target_id:
            return Response(
                {'user': USER_ACTIVITY_REQUIS},
                status=status.HTTP_400_BAD_REQUEST,
            )
        membership = self._resolve_activity_member(org, target_id)
        if not membership:
            return Response(
                {'user': USER_ACTIVITY_INCONNU},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(self._user_activity_data(
            request, org, membership, start_date, end_date, group_by
        ))

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Résumé global pour le dashboard principal"""
        org = self.get_organization()
        start_date, end_date, prev_start, prev_end = self._parse_date_range(request)
        
        sales_stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end, request=request)
        stock_stats = self._get_stock_stats(org, request=request)
        cashbook_stats = self._get_cashbook_stats(org, start_date, end_date, request=request)
        customer_stats = self._get_customer_stats(org, start_date, end_date)
        
        data = {
            'sales': sales_stats,
            'stock': stock_stats,
            'cashbook': cashbook_stats,
            'customers': customer_stats,
        }
        
        serializer = DashboardSummarySerializer(data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales(self, request):
        """Statistiques détaillées des ventes"""
        org = self.get_organization()
        start_date, end_date, prev_start, prev_end = self._parse_date_range(request)
        
        stats = self._get_sales_stats(org, start_date, end_date, prev_start, prev_end, request=request)
        serializer = SalesStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def sales_by_period(self, request):
        """Ventes groupées par période (jour/semaine/mois) avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        if group_by == 'week':
            trunc_func = TruncWeek('sale_date')
        elif group_by == 'month':
            trunc_func = TruncMonth('sale_date')
        else:
            trunc_func = TruncDate('sale_date')
        
        data = sales.annotate(
            period=trunc_func
        ).values('period').annotate(
            total=primary_sum('total'),
            count=Count('id')
        ).order_by('period')
        
        all_data = list(data)
        total_count = len(all_data)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'total': item['total'],
                'count': item['count']
            }
            for item in paginated_data
        ]
        
        serializer = SalesByPeriodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def sales_by_category(self, request):
        """Ventes par catégorie de produit avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product__category')
        
        data = items.values(
            'product__category__id',
            'product__category__name'
        ).annotate(
            total_revenue=primary_sum('total', rate='sale__exchange_rate'),
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField())
        ).order_by('-total_revenue')
        
        # Calculer le total pour les pourcentages
        all_data = list(data)
        total_revenue = sum(item['total_revenue'] for item in all_data)
        total_count = len(all_data)
        
        # Pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'category_id': item['product__category__id'],
                'category_name': item['product__category__name'] or 'Sans catégorie',
                'total_revenue': item['total_revenue'],
                'quantity_sold': item['quantity_sold'],
                'percentage': round((item['total_revenue'] / total_revenue * 100) if total_revenue > 0 else 0, 2)
            }
            for item in paginated_data
        ]
        
        serializer = SalesByCategorySerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def sales_by_payment_method(self, request):
        """Ventes par méthode de paiement avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        payments = self._scope_payments(
            Payment.objects.filter(
                sale__organization=org,
                paid_at__date__gte=start_date,
                paid_at__date__lte=end_date
            ),
            request,
        ).select_related('payment_method')
        
        data = payments.values(
            'payment_method__id',
            'payment_method__name'
        ).annotate(
            total=primary_sum('amount', rate='sale__exchange_rate'),
            count=Count('id')
        ).order_by('-total')
        
        all_data = list(data)
        total_amount = sum(item['total'] for item in all_data)
        total_count = len(all_data)
        
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_data = all_data[start_idx:end_idx]
        
        result = [
            {
                'payment_method': str(item['payment_method__id']) if item['payment_method__id'] else 'unknown',
                'payment_method_name': item['payment_method__name'] or 'Inconnu',
                'total': item['total'],
                'count': item['count'],
                'percentage': round((item['total'] / total_amount * 100) if total_amount > 0 else 0, 2)
            }
            for item in paginated_data
        ]
        
        serializer = SalesByPaymentMethodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @staticmethod
    def _top_product_row(item):
        """
        Traduit une ligne agrégée de ventes en quantité lisible.

        L'agrégation travaille sur ``values()`` : il n'y a donc pas de ``Product``
        sous la main. On en reconstitue le strict nécessaire, un objet léger dont
        ``PackagingService`` ne lit que ``selling_mode``, ``units_per_package``
        et le nom des deux unités.
        """
        packages = Decimal(item.get('packages_sold') or 0)
        packaged_units = Decimal(item.get('packaged_units') or 0)
        quantity = Decimal(item['quantity_sold'] or 0)

        stand_in = PackagingProfile.from_values(item)
        factor = PackagingService.factor(stand_in)

        if factor is None or packages <= 0:
            display = PackagingService.format_quantity(stand_in, quantity)
            packages_out = None if factor is None else Decimal('0')
            loose_out = None if factor is None else quantity
        else:
            # Le vrac ne peut pas être négatif : une ligne dont la part
            # conditionnée dépasse le total signale un facteur modifié après
            # coup, on retombe alors sur la lecture au total.
            loose_out = max(Decimal('0'), quantity - packaged_units)
            display = PackagingService.format_split(stand_in, packages, loose_out)
            packages_out = packages

        return {
            'product_id': item['product__id'],
            'product_name': item['product__name'],
            'product_sku': item['product__sku'],
            'quantity_sold': quantity,
            'quantity_display': display,
            'packages_sold': packages_out,
            'loose_sold': loose_out,
            'packaging_factor': factor,
            'total_revenue': item['total_revenue'],
        }

    def _top_products_values(self, request, org, start_date, end_date):
        """
        Ventes agrégées par produit, ordonnées, SANS pagination.

        ⚠ NE PAS RÉÉCRIRE CE `values()` AILLEURS. `PackagingProfile.from_values`
        y lit `product__selling_mode`, `product__units_per_package` et le nom
        des deux unités ; une clé oubliée rend `factor=None` en SILENCE, et
        toutes les quantités retombent en nombres nus (« 72 » au lieu de
        « 5 casiers + 12 bouteilles »), sans la moindre erreur.
        """
        items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product')
        
        # `packages_sold` additionne les contenants réellement facturés et
        # `packaged_units` ce qu'ils pèsent en unités de détail : la part vraiment
        # vendue à la pièce est le reste. Redécouper `quantity_sold` au facteur
        # d'aujourd'hui donnerait « 10 casiers » là où le marchand a vendu
        # 5 casiers et 120 bouteilles, et le facteur a pu changer depuis.
        data = items.values(
            'product__id',
            'product__name',
            'product__sku',
            'product__selling_mode',
            'product__units_per_package',
            'product__unit__name',
            'product__packaging_unit__name',
        ).annotate(
            quantity_sold=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField()),
            packages_sold=Coalesce(
                Sum('package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
            total_revenue=primary_sum('total', rate='sale__exchange_rate')
        ).order_by('-quantity_sold')

        return data

    @action(detail=False, methods=['get'])
    def top_products(self, request):
        """Top produits les plus vendus avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        data = self._top_products_values(request, org, start_date, end_date)
        total_count = data.count()
        start_idx = (page - 1) * page_size
        result = [
            self._top_product_row(item)
            for item in data[start_idx:start_idx + page_size]
        ]

        serializer = TopProductSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    def _top_customer_rows(self, request, org, start_date, end_date):
        """Meilleurs clients, ordonnés, sur le PÉRIMÈTRE ENTIER."""
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid'],
                customer__isnull=False
            ),
            request,
        ).select_related('customer')
        
        # Converti en devise principale : un `Sum('total')` brut classait un
        # client à 50 USD derrière un client à 40 000 CDF en comparant des
        # nombres nus. `current_balance` est déjà exprimé en principale.
        from apps.cashbook.services import primary_sum

        data = sales.values(
            'customer__id',
            'customer__name',
            'customer__current_balance'
        ).annotate(
            total_purchases=primary_sum('total'),
            order_count=Count('id')
        ).order_by('-total_purchases')
        
        return [
            {
                'customer_id': item['customer__id'],
                'customer_name': item['customer__name'],
                'total_purchases': item['total_purchases'],
                'order_count': item['order_count'],
                'current_balance': item['customer__current_balance'] or Decimal('0')
            }
            for item in data
        ]

    @action(detail=False, methods=['get'])
    def top_customers(self, request):
        """Meilleurs clients avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        rows = self._top_customer_rows(request, org, start_date, end_date)
        total_count = len(rows)
        start_idx = (page - 1) * page_size
        result = rows[start_idx:start_idx + page_size]

        serializer = TopCustomerSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def stock(self, request):
        """Statistiques du stock"""
        org = self.get_organization()
        stats = self._get_stock_stats(org, request=request)
        serializer = StockStatsSerializer(stats)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def cashbook(self, request):
        """Statistiques de la caisse"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_cashbook_stats(org, start_date, end_date, request=request)
        serializer = CashbookStatsSerializer(stats)
        return Response(serializer.data)
    
    def _cash_flow_rows(self, request, org, start_date, end_date, group_by):
        """
        Flux de trésorerie par période, sur le PÉRIMÈTRE ENTIER.

        Extraite de l'action pour que l'export la rejoue sans pagination : un
        fichier qui ne porterait que les vingt lignes affichées mentirait sur
        son propre contenu, sous un en-tête qui annonce le total.
        """
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                movement_date__date__gte=start_date,
                movement_date__date__lte=end_date
            ),
            request,
        )

        if group_by == 'week':
            trunc_func = TruncWeek('movement_date')
        elif group_by == 'month':
            trunc_func = TruncMonth('movement_date')
        else:
            trunc_func = TruncDate('movement_date')

        # Montants convertis en devise principale : un flux de trésorerie est un
        # chiffre comptable unique, il ne peut pas mélanger USD et CDF bruts.
        data = movements.annotate(
            period=trunc_func
        ).values('period').annotate(
            income=primary_sum('amount', filter=Q(direction='in')),
            expenses=primary_sum('amount', filter=Q(direction='out')),
        ).order_by('period')

        return [
            {
                'period': item['period'].strftime('%Y-%m-%d') if item['period'] else '',
                'income': item['income'],
                'expenses': item['expenses'],
                'net': item['income'] - item['expenses']
            }
            for item in data
        ]

    @action(detail=False, methods=['get'])
    def cash_flow(self, request):
        """Flux de trésorerie par période avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        group_by = request.query_params.get('group_by', 'day')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        rows = self._cash_flow_rows(request, org, start_date, end_date, group_by)
        total_count = len(rows)

        start_idx = (page - 1) * page_size
        result = rows[start_idx:start_idx + page_size]

        serializer = CashFlowByPeriodSerializer(result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    @action(detail=False, methods=['get'])
    def customers(self, request):
        """Statistiques des clients"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        stats = self._get_customer_stats(org, start_date, end_date)
        serializer = CustomerStatsSerializer(stats)
        return Response(serializer.data)

    def _receivables_data(self, request, org):
        """
        Balance âgée des créances clients, ventilée par devise.

        Le seul chiffre existant jusqu'ici (`total_receivables`) était la somme
        des `current_balance`, un agrégat converti en devise principale : utile
        pour un total, muet sur l'ancienneté et sur la devise réellement due.
        Un marchand qui doit relancer a besoin de savoir QUI est en retard, de
        COMBIEN et DEPUIS QUAND.

        Tranches calculées depuis `due_date`, avec repli sur `sale_date` quand
        aucune échéance n'a été fixée : une facture sans échéance vieillit
        quand même. Les montants ne sont jamais additionnés entre devises ;
        `total_primary` fournit à part la conversion en devise principale.
        """
        from apps.cashbook.services import primary_sum

        today = timezone.localdate()

        open_invoices = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
                amount_due__gt=0,
                customer__isnull=False,
            ),
            request,
        ).select_related('customer')

        # Ancienneté : échéance si elle existe, sinon date de vente.
        reference_date = Coalesce('due_date', Cast('sale_date', DateField()))
        open_invoices = open_invoices.annotate(aging_date=reference_date)

        buckets = [
            ('current', None, 0),
            ('d1_30', 1, 30),
            ('d31_60', 31, 60),
            ('d61_90', 61, 90),
            ('d90_plus', 91, None),
        ]

        def bucket_for(days_late):
            if days_late <= 0:
                return 'current'
            if days_late <= 30:
                return 'd1_30'
            if days_late <= 60:
                return 'd31_60'
            if days_late <= 90:
                return 'd61_90'
            return 'd90_plus'

        by_currency = {}
        by_customer = {}

        for sale in open_invoices:
            days_late = (today - sale.aging_date).days
            slot = bucket_for(days_late)

            row = by_currency.setdefault(
                sale.currency,
                {'currency': sale.currency, 'total': Decimal('0.00'),
                 **{name: Decimal('0.00') for name, _, _ in buckets}},
            )
            row[slot] += sale.amount_due
            row['total'] += sale.amount_due

            key = (sale.customer_id, sale.currency)
            entry = by_customer.setdefault(key, {
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                # Le téléphone vient du SERVEUR : le terminal le joignait dans
                # sa base locale et le back-office ne l'avait pas du tout, si
                # bien que son écran de relance ne portait aucun numéro. On
                # relance au téléphone, pas par la pensée.
                'customer_phone': sale.customer.phone or '',
                'currency': sale.currency,
                'amount_due': Decimal('0.00'),
                'invoice_count': 0,
                'oldest_days': 0,
                'overdue_amount': Decimal('0.00'),
            })
            entry['amount_due'] += sale.amount_due
            entry['invoice_count'] += 1
            entry['oldest_days'] = max(entry['oldest_days'], days_late)
            if days_late > 0:
                entry['overdue_amount'] += sale.amount_due

        total_primary = open_invoices.aggregate(
            total=primary_sum('amount_due')
        )['total'] or Decimal('0.00')

        overdue_primary = open_invoices.filter(
            aging_date__lt=today
        ).aggregate(total=primary_sum('amount_due'))['total'] or Decimal('0.00')

        debtors = sorted(
            by_customer.values(), key=lambda e: e['amount_due'], reverse=True,
        )

        return {
            'as_of': str(today),
            'buckets': [name for name, _, _ in buckets],
            'by_currency': sorted(by_currency.values(), key=lambda r: r['currency']),
            'by_customer': debtors,
            'invoice_count': open_invoices.count(),
            'debtor_count': len({cid for cid, _ in by_customer}),
            # Seuls chiffres convertis, et clairement nommés comme tels.
            'total_primary': total_primary,
            'overdue_primary': overdue_primary,
            'primary_currency': CurrencyService.primary_code(org),
        }

    @action(detail=False, methods=['get'])
    def receivables(self, request):
        """Balance âgée des créances clients, ventilée par devise."""
        return Response(self._receivables_data(request, self.get_organization()))
    
    # ========================================================================
    # HELPER METHODS
    # ========================================================================
    
    def _get_sales_stats(self, org, start_date, end_date, prev_start, prev_end, request=None):
        """Calcule les statistiques des ventes (filtrées par périmètre warehouse)."""
        sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=start_date,
            sale_date__date__lte=end_date
        )
        if request is not None:
            sales = self._scope_sales(sales, request)
        
        completed_sales = sales.filter(status__in=['completed', 'partially_paid'])
        
        total_sales = completed_sales.aggregate(total=primary_sum('total'))['total']
        
        total_orders = completed_sales.count()
        
        avg_order = completed_sales.aggregate(avg=primary_avg('total'))['avg']
        
        total_items = SaleItem.objects.filter(
            sale__in=completed_sales
        ).aggregate(
            total=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        # Période précédente
        prev_sales = Sale.objects.filter(
            organization=org,
            sale_date__date__gte=prev_start,
            sale_date__date__lte=prev_end,
            status__in=['completed', 'partially_paid']
        )
        if request is not None:
            prev_sales = self._scope_sales(prev_sales, request)
        
        prev_total = prev_sales.aggregate(total=primary_sum('total'))['total']
        
        prev_orders = prev_sales.count()
        
        # Calcul de la croissance
        sales_growth = None
        if prev_total > 0:
            sales_growth = round(((total_sales - prev_total) / prev_total) * 100, 2)
        
        orders_growth = None
        if prev_orders > 0:
            orders_growth = round(((total_orders - prev_orders) / prev_orders) * 100, 2)
        
        return {
            'total_sales': total_sales,
            'total_orders': total_orders,
            'average_order_value': round(avg_order, 2),
            'total_items_sold': total_items,
            'completed_sales': sales.filter(status='completed').count(),
            'pending_sales': sales.filter(status__in=['pending', 'partially_paid']).count(),
            'cancelled_sales': sales.filter(status='cancelled').count(),
            'sales_growth': sales_growth,
            'orders_growth': orders_growth,
        }
    
    def _get_stock_stats(self, org, request=None):
        """Calcule les statistiques du stock (filtrées par périmètre warehouse)."""
        products = Product.objects.filter(organization=org, is_active=True)
        
        stocks = Stock.objects.filter(
            organization=org,
            product__is_active=True
        )
        if request is not None:
            stocks = self._scope_stocks(stocks, request)
        
        # ``total_products`` doit refléter ce qui est visible : on compte les
        # produits qui ont au moins une position de stock dans le périmètre.
        if request is not None and self._accessible_warehouse_ids(request) is not None:
            scoped_product_ids = stocks.values_list('product_id', flat=True).distinct()
            total_products = products.filter(id__in=scoped_product_ids).count()
        else:
            total_products = products.count()
        
        # Valeur totale du stock, et décomptes de rupture / stock bas.
        #
        # Tout est agrégé en BASE. Ce bloc hydratait auparavant chaque ligne de
        # stock pour sommer en Python : sur une organisation de 5 000 produits
        # répartis sur trois entrepôts, cela fabriquait quinze mille objets à
        # chaque ouverture du rapport, sans cache ni pagination.
        #
        # La valorisation passe par `Stock.unit_cost_expression()`, donc par la
        # règle unique « ``avg_cost`` s'il est renseigné, sinon le prix d'achat
        # catalogue ». Elle valorisait ici au seul ``cost_price`` : le même
        # stock ressortait à une valeur au tableau de bord et à une autre dans
        # ce rapport.
        total_value = Stock.total_value_for(stocks)

        decomptes = stocks.aggregate(
            out_of_stock=Count('id', filter=Q(quantity__lte=0)),
            low_stock=Count(
                'id',
                filter=Q(quantity__gt=0)
                & Q(product__min_stock_level__isnull=False)
                & Q(product__min_stock_level__gt=0)
                & Q(quantity__lte=F('product__min_stock_level')),
            ),
        )
        out_of_stock = decomptes['out_of_stock']
        low_stock = decomptes['low_stock']
        
        # Lots expirant bientôt (30 jours)
        expiring_date = timezone.localdate() + timedelta(days=30)
        batches = StockBatch.objects.filter(
            organization=org,
            expiry_date__lte=expiring_date,
            expiry_date__gte=timezone.localdate(),
            quantity__gt=0
        )
        if request is not None:
            batches = self._scope_stock_batches(batches, request)
        expiring_count = batches.count()
        
        return {
            'total_products': total_products,
            'total_stock_value': total_value,
            'low_stock_count': low_stock,
            'out_of_stock_count': out_of_stock,
            'expiring_soon_count': expiring_count,
        }
    
    def _get_cashbook_stats(self, org, start_date, end_date, request=None):
        """Calcule les statistiques de la caisse (filtrées par périmètre warehouse)."""
        movements = CashMovement.objects.filter(
            organization=org,
            movement_date__date__gte=start_date,
            movement_date__date__lte=end_date
        )
        if request is not None:
            movements = self._scope_cash_movements(movements, request)
        
        # Convertis en devise principale (montant × taux) : sans cela, une
        # dépense de 10 USD comptait pour 10 CDF.
        totals = movements.aggregate(
            income=primary_sum('amount', filter=Q(direction='in')),
            expenses=primary_sum('amount', filter=Q(direction='out')),
        )

        # Solde actuel : le tiroir est suivi PAR DEVISE, donc prendre le dernier
        # mouvement toutes devises confondues renvoyait le solde de la devise qui
        # a bougé en dernier. On somme le dernier solde de CHAQUE devise, converti.
        last_movement_qs = CashMovement.objects.filter(
            organization=org, is_cancelled=False
        )
        if request is not None:
            last_movement_qs = self._scope_cash_movements(last_movement_qs, request)

        balances = last_balance_by_currency(last_movement_qs)
        current_balance = sum(
            (bal * rate for bal, rate in balances.values()), Decimal('0')
        )
        balance_by_currency = [
            {'currency': ccy, 'balance': bal}
            for ccy, (bal, _rate) in sorted(balances.items())
        ]

        # Dépenses en attente
        pending_expenses_qs = Expense.objects.filter(
            organization=org,
            status__in=['draft', 'pending', 'approved']
        )
        if request is not None:
            pending_expenses_qs = self._scope_expenses(pending_expenses_qs, request)
        pending_expenses = pending_expenses_qs.count()
        
        return {
            # Scalaires = devise principale (montants convertis).
            'currency': org.currency or 'CDF',
            'current_balance': current_balance,
            # Détail du tiroir : chaque devise avec son solde réel, non converti.
            'balance_by_currency': balance_by_currency,
            'total_income': totals['income'],
            'total_expenses': totals['expenses'],
            'net_flow': totals['income'] - totals['expenses'],
            'pending_expenses': pending_expenses,
        }
    
    def _get_customer_stats(self, org, start_date, end_date):
        """Calcule les statistiques des clients"""
        customers = Customer.objects.filter(organization=org)
        
        total = customers.count()
        active = customers.filter(is_active=True).count()
        
        # Nouveaux clients sur la période
        new_customers = customers.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        ).count()
        
        # Total des créances (soldes positifs = dette client)
        receivables = customers.filter(
            current_balance__gt=0
        ).aggregate(
            total=Coalesce(Sum('current_balance'), Decimal('0'), output_field=DecimalField())
        )['total']
        
        customers_with_debt = customers.filter(current_balance__gt=0).count()
        
        return {
            'total_customers': total,
            'active_customers': active,
            'new_customers_period': new_customers,
            'total_receivables': receivables,
            'customers_with_debt': customers_with_debt,
        }
    
    # ========================================================================
    # RAPPORT JOURNALIER DE CAISSE
    # ========================================================================
    
    def _parse_report_date(self, request):
        """
        La date du rapport journalier, ou aujourd'hui.

        ⚠ `strptime` n'était pas gardé : `?date=oops` remontait en **500**, une
        panne serveur là où l'appelant s'est trompé. Le refus est déterministe
        et porte le nom du champ, comme `_parse_date_range` le fait déjà.

        Le corps vit dans `apps.core.report_params` : l'export de caisse avait
        le même défaut sans le même correctif, et deux gardes recopiées finissent
        par refuser dans deux termes différents la même faute de saisie.
        """
        from apps.core.report_params import parse_day

        date_str = request.query_params.get('date')
        if not date_str:
            return timezone.localdate()
        return parse_day(date_str)

    def _daily_cash_data(self, request, org, report_date):
        """
        Synthèse du jour et mouvements du jour.

        Rend `(report_data, movements)` : les agrégats couvrent DÉJÀ la journée
        entière, seule la LISTE était paginée par l'action.
        """
        # Mouvements du jour
        movements = self._scope_cash_movements(
            CashMovement.objects.filter(
                organization=org,
                movement_date__date=report_date
            ),
            request,
        ).order_by('movement_date')
        
        # Soldes d'ouverture / clôture : le tiroir étant suivi PAR DEVISE, on
        # somme le dernier solde de CHAQUE devise converti en principale (avant :
        # le solde de la dernière devise ayant bougé, toutes devises confondues).
        opening_balance = balance_in_primary(
            self._scope_cash_movements(
                CashMovement.objects.filter(
                    organization=org,
                    is_cancelled=False,
                    movement_date__date__lt=report_date,
                ),
                request,
            )
        )

        # Sans mouvement jusqu'à cette date, `balance_in_primary` renvoie 0, ce
        # qui est aussi la valeur d'ouverture : pas de cas particulier à traiter.
        closing_balance = balance_in_primary(
            self._scope_cash_movements(
                CashMovement.objects.filter(
                    organization=org,
                    is_cancelled=False,
                    movement_date__date__lte=report_date,
                ),
                request,
            )
        )

        # Ventes du jour
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date=report_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        # Montants convertis en devise principale (total × exchange_rate) pour
        # additionner des ventes de devises différentes sans les mélanger.
        total_sales = sales.aggregate(
            total=Coalesce(Sum(F('total') * F('exchange_rate')), Decimal('0'), output_field=DecimalField())
        )['total']
        total_sales_count = sales.count()
        
        # Paiements par type
        payments = self._scope_payments(
            Payment.objects.filter(
                sale__organization=org,
                paid_at__date=report_date
            ),
            request,
        ).select_related('payment_method', 'sale')

        cash_sales = Decimal('0')
        mobile_money_sales = Decimal('0')
        card_sales = Decimal('0')

        for payment in payments:
            method_name = payment.payment_method.name.lower() if payment.payment_method else ''
            # payment.amount est dans la devise de la vente ; on convertit dans la
            # devise principale (sale.exchange_rate = principale par unité de vente)
            # afin de pouvoir additionner des ventes de devises différentes.
            amount_primary = payment.amount * (payment.sale.exchange_rate if payment.sale else Decimal('1'))
            if 'cash' in method_name or 'espèce' in method_name or 'liquide' in method_name:
                cash_sales += amount_primary
            elif 'mobile' in method_name or 'mpesa' in method_name or 'airtel' in method_name or 'orange' in method_name:
                mobile_money_sales += amount_primary
            elif 'card' in method_name or 'carte' in method_name or 'visa' in method_name:
                card_sales += amount_primary
        
        # Ventes à crédit (montant restant dû, converti en devise principale)
        credit_sales = sales.filter(amount_due__gt=0).aggregate(
            total=Coalesce(Sum(F('amount_due') * F('exchange_rate')), Decimal('0'), output_field=DecimalField())
        )['total']
        
        # Mouvements de caisse convertis en devise principale, comme les ventes
        # ci-dessus (un mouvement en USD ne peut pas être sommé brut avec du CDF).
        debt_collections = movements.filter(
            movement_type='debt_collection'
        ).aggregate(total=primary_sum('amount'))['total']

        # Dépenses
        expenses_movements = movements.filter(direction='out')
        expenses_total = expenses_movements.aggregate(total=primary_sum('amount'))['total']
        expenses_count = expenses_movements.count()

        # Flux net
        income = movements.filter(direction='in').aggregate(total=primary_sum('amount'))['total']
        net_cash_flow = income - expenses_total
        
        report_data = {
            'date': report_date,
            'opening_balance': opening_balance,
            'closing_balance': closing_balance,
            'total_sales': total_sales,
            'total_sales_count': total_sales_count,
            'cash_sales': cash_sales,
            'mobile_money_sales': mobile_money_sales,
            'card_sales': card_sales,
            'credit_sales': credit_sales,
            'debt_collections': debt_collections,
            'expenses': expenses_total,
            'expenses_count': expenses_count,
            'net_cash_flow': net_cash_flow,
        }

        return report_data, movements

    @staticmethod
    def _daily_cash_movement_rows(movements):
        """Les mouvements en lignes lisibles, sans pagination."""
        return [
            {
                'id': m.id,
                'time': m.movement_date.time(),
                'type': m.movement_type,
                'type_display': m.get_movement_type_display(),
                'description': m.description or '',
                'reference': m.reference,
                'amount': m.amount,
                'direction': m.direction,
                'balance_after': m.balance_after,
            }
            for m in movements
        ]

    @action(detail=False, methods=['get'])
    def daily_cash_report(self, request):
        """Rapport journalier de caisse détaillé"""
        org = self.get_organization()
        report_date = self._parse_report_date(request)
        report_data, movements = self._daily_cash_data(request, org, report_date)

        # Liste des mouvements avec pagination
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        all_rows = self._daily_cash_movement_rows(movements)
        total_movements = len(all_rows)
        start_idx = (page - 1) * page_size
        movements_list = all_rows[start_idx:start_idx + page_size]

        return Response({
            'report': DailyCashReportSerializer(report_data).data,
            'movements': {
                'results': DailyCashMovementSerializer(movements_list, many=True).data,
                'count': total_movements,
                'page': page,
                'page_size': page_size,
                'total_pages': (total_movements + page_size - 1) // page_size if page_size > 0 else 0,
            },
        })
    
    # ========================================================================
    # BÉNÉFICES ET MARGES
    # ========================================================================
    
    def _profit_margins_data(self, request, org, start_date, end_date):
        """Bénéfices et marges globaux. Déjà sur la période entière."""
        # Ventes complétées
        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid']
            ),
            request,
        )
        
        # ┌──────────────────────────────────────────────────────────────────┐
        # │ LE BÉNÉFICE SE LIT EN DEVISE PRINCIPALE, DES DEUX CÔTÉS.         │
        # │                                                                  │
        # │ Ce rapport retranchait des dépenses CONVERTIES (`primary_sum`,   │
        # │ plus bas) d'un chiffre d'affaires BRUT, et le commentaire d'à     │
        # │ côté affirmait pourtant que les deux étaient « en principale ».   │
        # │ Sur un établissement qui facture en francs et en dollars, le      │
        # │ chiffre d'affaires ajoutait 107 000 à 50 : le bénéfice net qui en │
        # │ sortait n'avait aucun sens, et rien ne le signalait.              │
        # │                                                                  │
        # │ LE COÛT, LUI, RESTE BRUT, et c'est correct : `cost_price` vient   │
        # │ du catalogue, qui n'a pas de devise, donc il est DÉJÀ en          │
        # │ principale. Le convertir au taux de sa vente diviserait le coût   │
        # │ d'une vente en francs par deux mille huit cents, et la marge      │
        # │ passerait de 40 % à 100 %. Un test l'épingle explicitement.       │
        # └──────────────────────────────────────────────────────────────────┘
        #
        # CA HT net (toutes remises), cohérent avec sale.total = subtotal - discount_amount + tax
        revenue_expr = ExpressionWrapper(
            (F('subtotal') - F('discount_amount')) * F('exchange_rate'),
            output_field=DecimalField(max_digits=24, decimal_places=6),
        )
        total_revenue = sales.aggregate(
            total=Coalesce(Sum(revenue_expr), Decimal('0'), output_field=DecimalField(max_digits=24, decimal_places=6))
        )['total']
        total_revenue = (total_revenue or Decimal('0')).quantize(Decimal('0.01'))

        # CMV : coût ligne (FIFO) si > 0, sinon coût produit
        cost_unit = Case(
            When(cost_price__gt=0, then=F('cost_price')),
            default=Coalesce(F('product__cost_price'), Value(Decimal('0'))),
            output_field=DecimalField(max_digits=15, decimal_places=2),
        )
        line_cmv = ExpressionWrapper(
            F('quantity') * cost_unit,
            output_field=DecimalField(max_digits=24, decimal_places=6),
        )
        total_cost = SaleItem.objects.filter(sale__in=sales).aggregate(
            tc=Coalesce(Sum(line_cmv), Decimal('0'), output_field=DecimalField(max_digits=24, decimal_places=6))
        )['tc']
        total_cost = (total_cost or Decimal('0')).quantize(Decimal('0.01'))
        
        # Bénéfice brut
        gross_profit = total_revenue - total_cost
        gross_margin = (gross_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')
        
        # Dépenses de la période, converties en devise principale (montant × taux)
        # pour être soustraites d'un chiffre d'affaires lui aussi en principale,
        # ce qu'il est désormais.
        expenses = self._scope_expenses(
            Expense.objects.filter(
                organization=org,
                expense_date__gte=start_date,
                expense_date__lte=end_date,
                status='paid'
            ),
            request,
        ).aggregate(total=primary_sum('amount'))['total']
        
        # Bénéfice net
        net_profit = gross_profit - expenses
        net_margin = (net_profit / total_revenue * 100) if total_revenue > 0 else Decimal('0')
        
        data = {
            'total_revenue': total_revenue,
            'total_cost': total_cost,
            'gross_profit': gross_profit,
            'gross_margin_percentage': round(gross_margin, 2),
            'total_expenses': expenses,
            'net_profit': net_profit,
            'net_margin_percentage': round(net_margin, 2),
        }

        return data

    @action(detail=False, methods=['get'])
    def profit_margins(self, request):
        """Calcul des bénéfices et marges globaux"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        data = self._profit_margins_data(request, org, start_date, end_date)
        serializer = ProfitMarginSerializer(data)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'], url_path='sales-by-packaging')
    def sales_by_packaging(self, request):
        """
        Chiffre d'affaires et marge ventilés entre vente en gros et vente au détail.

        La marge n'est pas la même selon la forme de vente : c'est ce que ce
        rapport rend visible. Une ligne mixte (« 2 paquets + 3 bouteilles ») est
        répartie **au prorata** de chaque part, de sorte que la somme des deux
        colonnes égale exactement le chiffre d'affaires de la période.

        La recette est convertie en devise principale au taux figé sur sa vente,
        le coût NON : il vient du catalogue, qui n'a pas de devise. Voir
        l'encadré de `profit_margins`, dont ce rapport est la ventilation.
        """
        from apps.sales.profit_allocation import (
            allocated_line_ht_revenues_for_sale, effective_unit_cost,
        )

        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)

        sales = self._scope_sales(
            Sale.objects.filter(
                organization=org,
                sale_date__date__gte=start_date,
                sale_date__date__lte=end_date,
                status__in=['completed', 'partially_paid'],
            ),
            request,
        ).prefetch_related('items__product')

        buckets = {
            'wholesale': {'revenue': Decimal('0'), 'cost': Decimal('0'), 'quantity': Decimal('0')},
            'retail': {'revenue': Decimal('0'), 'cost': Decimal('0'), 'quantity': Decimal('0')},
        }

        for sale in sales:
            # `allocated_line_ht_revenues_for_sale` rend des montants dans la
            # devise de la FACTURE : la conversion se fait ici, une fois par
            # vente, plutôt que dans le module d'allocation, qui répartit une
            # recette entre deux canaux et n'a pas à connaître les monnaies.
            taux = sale.exchange_rate or Decimal('1')
            for item, line_revenue in allocated_line_ht_revenues_for_sale(sale):
                line_revenue = (line_revenue * taux).quantize(Decimal('0.01'))
                unit_cost = effective_unit_cost(item)
                line_cost = (item.quantity * unit_cost).quantize(Decimal('0.01'))

                packaged_units = (
                    (item.package_quantity or Decimal('0')) * (item.packaging_factor or 0)
                )
                if packaged_units <= 0:
                    buckets['retail']['revenue'] += line_revenue
                    buckets['retail']['cost'] += line_cost
                    buckets['retail']['quantity'] += item.quantity
                    continue

                if packaged_units >= item.quantity:
                    buckets['wholesale']['revenue'] += line_revenue
                    buckets['wholesale']['cost'] += line_cost
                    buckets['wholesale']['quantity'] += item.quantity
                    continue

                # Ligne mixte : répartition au prorata du nombre d'unités, pour
                # que les deux colonnes se recollent au total de la vente.
                share = packaged_units / item.quantity
                wholesale_revenue = (line_revenue * share).quantize(Decimal('0.01'))
                wholesale_cost = (line_cost * share).quantize(Decimal('0.01'))

                buckets['wholesale']['revenue'] += wholesale_revenue
                buckets['wholesale']['cost'] += wholesale_cost
                buckets['wholesale']['quantity'] += packaged_units
                buckets['retail']['revenue'] += line_revenue - wholesale_revenue
                buckets['retail']['cost'] += line_cost - wholesale_cost
                buckets['retail']['quantity'] += item.quantity - packaged_units

        def summarize(key, label):
            revenue = buckets[key]['revenue'].quantize(Decimal('0.01'))
            cost = buckets[key]['cost'].quantize(Decimal('0.01'))
            profit = revenue - cost
            margin = (profit / revenue * 100) if revenue > 0 else Decimal('0')
            return {
                'sale_form': key,
                'label': label,
                'revenue': revenue,
                'cost': cost,
                'gross_profit': profit,
                'margin_percentage': round(margin, 2),
                'quantity': buckets[key]['quantity'].quantize(Decimal('0.001')),
            }

        results = [
            summarize('wholesale', 'Vente en gros'),
            summarize('retail', 'Vente au détail'),
        ]
        return Response({
            'results': results,
            'total_revenue': sum((r['revenue'] for r in results), Decimal('0')),
            'total_gross_profit': sum((r['gross_profit'] for r in results), Decimal('0')),
        })

    def _product_profit_rows(self, request, org, start_date, end_date):
        """Bénéfices par produit, triés, sur le PÉRIMÈTRE ENTIER."""
        sale_items = self._scope_sale_items(
            SaleItem.objects.filter(
                sale__organization=org,
                sale__sale_date__date__gte=start_date,
                sale__sale_date__date__lte=end_date,
                sale__status__in=['completed', 'partially_paid']
            ),
            request,
        ).select_related('product', 'sale').order_by('sale_id', 'id')

        sale_ids = list(sale_items.values_list('sale_id', flat=True).distinct())
        sales_by_id = {
            s.id: s
            for s in Sale.objects.filter(id__in=sale_ids).prefetch_related('items__product')
        }

        by_sale_lines = defaultdict(list)
        for row in sale_items:
            by_sale_lines[row.sale_id].append(row)

        product_data = {}
        for sid, lines in by_sale_lines.items():
            sale = sales_by_id.get(sid)
            if not sale:
                continue
            alloc_list = allocated_line_ht_revenues_for_sale(sale)
            alloc_by_item_id = {i.id: rev for i, rev in alloc_list}

            for item in lines:
                pid = str(item.product.id)
                if pid not in product_data:
                    product_data[pid] = {
                        'product_id': item.product.id,
                        'product_name': item.product.name,
                        'product_sku': item.product.sku,
                        'quantity_sold': Decimal('0'),
                        'total_revenue': Decimal('0'),
                        'total_cost': Decimal('0'),
                        # Ventilation gros/détail, cumulée depuis les lignes :
                        # elles portent le nombre de contenants facturés et le
                        # facteur en vigueur au moment de la vente.
                        '_product': item.product,
                        '_packages': Decimal('0'),
                        '_packaged_units': Decimal('0'),
                    }

                cu = effective_unit_cost(item)
                rev = alloc_by_item_id.get(item.id, Decimal('0')).quantize(Decimal('0.01'))
                product_data[pid]['quantity_sold'] += item.quantity
                product_data[pid]['total_revenue'] += rev
                product_data[pid]['total_cost'] += (cu * item.quantity).quantize(Decimal('0.01'))
                packages = Decimal(item.package_quantity or 0)
                product_data[pid]['_packages'] += packages
                product_data[pid]['_packaged_units'] += packages * (item.packaging_factor or 0)
        
        # Calculer profit et marge
        result = []
        for data in product_data.values():
            profit = data['total_revenue'] - data['total_cost']
            margin = (profit / data['total_revenue'] * 100) if data['total_revenue'] > 0 else Decimal('0')
            product = data.pop('_product')
            packages = data.pop('_packages')
            packaged_units = data.pop('_packaged_units')
            if packages > 0:
                loose = max(Decimal('0'), data['quantity_sold'] - packaged_units)
                display = PackagingService.format_split(product, packages, loose)
            else:
                display = PackagingService.format_quantity(product, data['quantity_sold'])
            result.append({
                **data,
                'quantity_display': display,
                'packaging_factor': PackagingService.factor(product),
                'profit': profit,
                'margin_percentage': round(margin, 2),
            })
        
        # Trier par profit décroissant
        result.sort(key=lambda x: x['profit'], reverse=True)
        return result

    @action(detail=False, methods=['get'])
    def product_profits(self, request):
        """Bénéfices par produit avec pagination"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        result = self._product_profit_rows(request, org, start_date, end_date)
        total_count = len(result)
        start_idx = (page - 1) * page_size
        paginated_result = result[start_idx:start_idx + page_size]

        serializer = ProductProfitSerializer(paginated_result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    # ========================================================================
    # STOCKS DÉTAILLÉS
    # ========================================================================
    
    def _stock_detail_rows(self, request, org, filter_status=None):
        """
        Stock détaillé par produit, trié, sur le PÉRIMÈTRE ENTIER.

        ⚠ La variable d'état s'appelle `state` et non `status` : ce module
        importe `status` de DRF, et le masquer dans une méthode partagée est un
        piège qui n'apparaît qu'au premier `status.HTTP_*` ajouté plus bas.
        """
        # `product__unit` et `product__packaging_unit` alimentent l'affichage
        # « 12 cartons + 3 bouteilles » : sans eux, deux requêtes par ligne.
        stocks = self._scope_stocks(
            Stock.objects.filter(
                organization=org,
                product__is_active=True
            ),
            request,
        ).select_related(
            'product', 'product__category',
            'product__unit', 'product__packaging_unit',
        )
        
        result = []
        for stock in stocks:
            qty = Decimal(str(stock.quantity))
            reserved = Decimal(str(stock.reserved_quantity))
            available = qty - reserved
            cost = stock.product.cost_price or Decimal('0')
            min_level = stock.product.min_stock_level
            
            # Déterminer le statut
            if qty <= 0:
                state = 'out_of_stock'
            elif min_level and qty <= min_level:
                state = 'low_stock'
            else:
                state = 'available'

            # Filtrer si demandé
            if filter_status:
                if filter_status == 'low' and state != 'low_stock':
                    continue
                elif filter_status == 'out' and state != 'out_of_stock':
                    continue
                elif filter_status == 'available' and state != 'available':
                    continue
            
            # Le stock d'un produit vendu en gros se lit en contenants : « 147 »
            # ne dit pas au gérant s'il peut honorer une commande de 10 cartons.
            # Les deux compteurs sont LUS sur la ligne de stock, jamais
            # reconstitués depuis le total : « 3 casiers + 27 bouteilles » ne
            # doit pas se réécrire « 4 casiers + 3 bouteilles ».
            factor = PackagingService.factor(stock.product)
            packages, loose_split = (
                PackagingService.stored_split(stock, factor)
                if factor is not None else (None, None)
            )

            result.append({
                'product_id': stock.product.id,
                'product_name': stock.product.name,
                'product_sku': stock.product.sku,
                'category_name': stock.product.category.name if stock.product.category else None,
                'current_stock': qty,
                'stock_display': PackagingService.format_stock(stock),
                'stock_packages': packages,
                'stock_loose': loose_split,
                'reserved_stock': reserved,
                'reserved_display': PackagingService.format_base_total(
                    stock.product, reserved
                ),
                'available_stock': available,
                'available_display': PackagingService.format_available(stock),
                'packaging_factor': factor,
                'min_stock_level': min_level,
                'cost_price': cost,
                'stock_value': qty * cost,
                'status': state,
            })
        
        # Trier par statut (ruptures en premier) puis par nom
        status_order = {'out_of_stock': 0, 'low_stock': 1, 'available': 2}
        result.sort(key=lambda x: (status_order.get(x['status'], 3), x['product_name']))
        return result

    @action(detail=False, methods=['get'])
    def stock_details(self, request):
        """Liste détaillée du stock par produit avec pagination"""
        org = self.get_organization()
        filter_status = request.query_params.get('status')  # low, out, available
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))

        result = self._stock_detail_rows(request, org, filter_status)
        total_count = len(result)
        start_idx = (page - 1) * page_size
        paginated_result = result[start_idx:start_idx + page_size]

        serializer = StockDetailSerializer(paginated_result, many=True)
        return Response({
            'results': serializer.data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': (total_count + page_size - 1) // page_size if page_size > 0 else 0,
        })
    
    def _stock_movements_summary_data(self, request, org, start_date, end_date):
        """Totaux de mouvements par type. Sans pagination : il n'y a qu'une ligne."""
        from apps.inventory.models import StockMovement

        movements = self._scope_stock_movements(
            StockMovement.objects.filter(
                organization=org,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date
            ),
            request,
        )
        
        # Agrégation par type
        data = {
            'total_in': Decimal('0'),
            'total_out': Decimal('0'),
            'sales_out': Decimal('0'),
            'adjustments_in': Decimal('0'),
            'adjustments_out': Decimal('0'),
            'transfers_in': Decimal('0'),
            'transfers_out': Decimal('0'),
            'returns_in': Decimal('0'),
        }
        
        for m in movements:
            qty = Decimal(str(m.quantity))
            if m.movement_type == 'sale':
                data['total_out'] += qty
                data['sales_out'] += qty
            elif m.movement_type == 'return_in':
                data['total_in'] += qty
                data['returns_in'] += qty
            elif m.movement_type == 'adjustment_in':
                data['total_in'] += qty
                data['adjustments_in'] += qty
            elif m.movement_type == 'adjustment_out':
                data['total_out'] += qty
                data['adjustments_out'] += qty
            elif m.movement_type == 'transfer_in':
                data['total_in'] += qty
                data['transfers_in'] += qty
            elif m.movement_type == 'transfer_out':
                data['total_out'] += qty
                data['transfers_out'] += qty
            elif m.movement_type in ['purchase', 'initial']:
                data['total_in'] += qty

        return data

    @action(detail=False, methods=['get'])
    def stock_movements_summary(self, request):
        """Résumé des mouvements de stock par type"""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        data = self._stock_movements_summary_data(request, org, start_date, end_date)
        serializer = StockMovementSummarySerializer(data)
        return Response(serializer.data)
    
    def _product_supplies_map(self, request, org, start_date, end_date):
        """
        Approvisionnements par produit pour une période donnée.

        Chaque entrée porte le total en unité de détail ET sa lecture en
        contenants (« 10 cartons + 5 bouteilles »). Les contenants viennent des
        champs de saisie figés sur le mouvement, jamais d'une division du total :
        une réception de 5 cartons plus 120 bouteilles ne doit pas se relire
        « 10 cartons ».

        Rendue par identifiant de produit EN CHAÎNE : c'est la clé que l'onglet
        « Produits » croise, et un UUID ne se compare pas à une chaîne.
        """
        from apps.inventory.models import StockMovement
        
        # Récupérer les mouvements d'entrée (approvisionnements) par produit
        supplies = self._scope_stock_movements(
            StockMovement.objects.filter(
                organization=org,
                created_at__date__gte=start_date,
                created_at__date__lte=end_date,
                movement_type__in=['purchase', 'initial', 'transfer_in', 'adjustment_in', 'return_in']
            ),
            request,
        ).values(
            'product_id',
            'product__name',
            'product__selling_mode',
            'product__units_per_package',
            'product__unit__name',
            'product__packaging_unit__name',
        ).annotate(
            total_supply=Coalesce(Sum('quantity'), Decimal('0'), output_field=DecimalField()),
            total_packages=Coalesce(
                Sum('input_package_quantity'), Decimal('0'), output_field=DecimalField()
            ),
            packaged_units=Coalesce(
                Sum(F('input_package_quantity') * F('packaging_factor')),
                Decimal('0'), output_field=DecimalField(),
            ),
        )
        
        result = {}
        for row in supplies:
            profile = PackagingProfile.from_values(row)
            factor = PackagingService.factor(profile)
            quantity = Decimal(row['total_supply'] or 0)
            packages = Decimal(row['total_packages'] or 0)
            packaged_units = Decimal(row['packaged_units'] or 0)

            if factor is not None and packages > 0:
                loose = max(Decimal('0'), quantity - packaged_units)
                display = PackagingService.format_split(profile, packages, loose)
            else:
                loose = quantity if factor is not None else None
                packages = Decimal('0') if factor is not None else None
                display = PackagingService.format_quantity(profile, quantity)

            result[str(row['product_id'])] = {
                'quantity': float(quantity),
                'display': display,
                'packages': float(packages) if packages is not None else None,
                'loose': float(loose) if loose is not None else None,
            }
        return result

    @action(detail=False, methods=['get'])
    def product_supplies(self, request):
        """Approvisionnements par produit, pour la période demandée."""
        org = self.get_organization()
        start_date, end_date, _, _ = self._parse_date_range(request)
        return Response(self._product_supplies_map(request, org, start_date, end_date))
