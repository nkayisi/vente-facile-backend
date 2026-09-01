"""
Les rapports comptent en DEVISE PRINCIPALE, comme le tableau de bord.

┌──────────────────────────────────────────────────────────────────────────────┐
│ L'ARGENT QUI SORT ÉTAIT CONVERTI, CELUI QUI ENTRE NE L'ÉTAIT PAS.            │
│                                                                              │
│ Dépenses, mouvements de caisse, créances et achats passaient déjà par        │
│ `primary_sum`. Tout ce qui touchait à une VENTE - chiffre d'affaires, panier │
│ moyen, ventilation par catégorie, par moyen de paiement, produits les plus   │
│ vendus, et jusqu'au BÉNÉFICE - sommait des montants bruts à travers les      │
│ monnaies. Sur un établissement qui facture en francs et en dollars, un       │
│ `Sum('total')` ajoute 107 000 à 50 et rend 107 050 : un nombre qui n'existe  │
│ pas, affiché sous le symbole de la principale.                               │
│                                                                              │
│ Le bénéfice était le plus trompeur des trois : son chiffre d'affaires était  │
│ brut, ses dépenses converties. On retranchait donc des dollars d'une somme   │
│ de francs et de dollars, et le commentaire à côté affirmait pourtant que les │
│ deux étaient « en principale ».                                              │
└──────────────────────────────────────────────────────────────────────────────┘

**Le COÛT n'est jamais converti** : `SaleItem.cost_price` vient du catalogue,
qui n'a pas de devise, et il est déjà en principale. C'est la moitié de tout
calcul de marge, et l'oublier ferait passer une marge de 40 % à 100 %.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.products.models import Product
from apps.sales.models import Payment, PaymentMethod, Sale, SaleItem
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

#: 1 CDF = 0,000357142857 USD, soit 1 USD = 2 800 CDF.
TAUX_CDF = Decimal('0.000357142857')


class _BaseRapports(APITestCase):
    """
    Une organisation en USD qui facture aussi en CDF.

    Rôle BORNÉ plutôt que propriétaire : `_scope_sales` sort en amont pour un
    propriétaire, et la moitié du code de périmètre ne serait pas exécutée.
    C'est la règle posée par `test_pull_scope_resolves`, et son oubli avait
    caché tout un lot de défauts.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)

        self.org.currency = 'USD'
        self.org.save(update_fields=['currency'])
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=True,
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=Currency.objects.get(code='CDF'),
            is_primary=False, exchange_rate=TAUX_CDF,
        )
        from apps.settings.services import CurrencyService
        CurrencyService.invalidate_cache(self.org)

        self.produit = Product.objects.create(
            organization=self.org, name='Article', sku='ART-1',
            selling_price=Decimal('100'), cost_price=Decimal('0.60'),
            track_inventory=False,
        )
        self.methode = PaymentMethod.objects.create(
            organization=self.org, name='Espèces', code='CASH',
            method_type='cash', is_active=True, is_default=True,
        )

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _get(self, chemin, **params):
        # ATTENTION AUX CHEMINS : `@action` sans `url_path` laisse le nom de la
        # méthode TEL QUEL et ne remplace les soulignés que dans `url_name`. Le
        # nom de route est donc « sales-by-period » et le chemin
        # « sales_by_period ». Viser le tiret rend un 404 de Django, sans corps.
        params.setdefault('period', 'month')
        reponse = self.client.get(
            f'/api/v1/reports/statistics/{chemin}/', params, **self._headers
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        return reponse.data

    def _vente(self, montant, devise, avec_ligne=True, avec_reglement=False):
        vente = Sale.objects.create(
            organization=self.org,
            warehouse=self.warehouse,
            register=self.register,
            reference=f'VT-{Sale.objects.count() + 1:04d}',
            status='completed',
            subtotal=montant,
            total=montant,
            amount_paid=montant,
            currency=devise,
            sold_by=self.manager,
            sale_date=timezone.now(),
        )
        if avec_ligne:
            SaleItem.objects.create(
                organization=self.org, sale=vente, product=self.produit,
                quantity=Decimal('1'), unit_price=montant,
                cost_price=Decimal('0.60'), subtotal=montant, total=montant,
            )
        if avec_reglement:
            Payment.objects.create(
                organization=self.org, sale=vente, payment_method=self.methode,
                amount=montant, tendered_amount=montant, currency=devise,
                exchange_rate=Decimal('1.000000'), status='completed',
            )
        return vente

    def _deux_ventes(self, **kw):
        """100 USD et 2 800 CDF : ensemble, 101 USD. Bruts, ils font 2 900."""
        self._vente(Decimal('100.00'), 'USD', **kw)
        self._vente(Decimal('2800.00'), 'CDF', **kw)


class ChiffreDAffairesTests(_BaseRapports):
    def test_le_resume_convertit_au_lieu_d_additionner(self):
        self._deux_ventes()
        resume = self._get('summary')['sales']
        self.assertEqual(Decimal(str(resume['total_sales'])), Decimal('101.00'))

    def test_le_panier_moyen_est_converti(self):
        # Une moyenne de montants bruts est pire qu'une somme brute : 1 450 ne
        # ressemble à aucune des deux factures, ni à leur moyenne réelle.
        self._deux_ventes()
        resume = self._get('summary')['sales']
        self.assertEqual(Decimal(str(resume['average_order_value'])), Decimal('50.50'))

    def test_les_ventes_par_periode_sont_converties(self):
        self._deux_ventes()
        lignes = self._get('sales_by_period')['results']
        total = sum(Decimal(str(l['total'])) for l in lignes)
        self.assertEqual(total, Decimal('101.00'))

    def test_les_ventes_par_categorie_sont_converties(self):
        self._deux_ventes()
        lignes = self._get('sales_by_category')['results']
        total = sum(Decimal(str(l['total_revenue'])) for l in lignes)
        self.assertEqual(total, Decimal('101.00'))

    def test_les_reglements_par_moyen_sont_convertis(self):
        self._deux_ventes(avec_reglement=True)
        lignes = self._get('sales_by_payment_method')['results']
        total = sum(Decimal(str(l['total'])) for l in lignes)
        self.assertEqual(total, Decimal('101.00'))

    def test_les_produits_les_plus_vendus_sont_convertis(self):
        self._deux_ventes()
        lignes = self._get('top_products')['results']
        total = sum(Decimal(str(l['total_revenue'])) for l in lignes)
        self.assertEqual(total, Decimal('101.00'))


class BeneficeTests(_BaseRapports):
    """
    Le rapport qui a motivé ce lot : « normalement le bénéfice doit être dans
    la devise principale ».
    """

    def test_le_chiffre_d_affaires_du_benefice_est_converti(self):
        self._deux_ventes()
        data = self._get('profit_margins')
        self.assertEqual(Decimal(str(data['total_revenue'])), Decimal('101.00'))

    def test_le_cout_n_est_PAS_converti(self):
        # Deux lignes à 0,60 de coût catalogue, déjà en principale. Les
        # convertir au taux de leur vente rendrait 0,60 + 0,000214, et la marge
        # de la vente en francs passerait de 40 % à 100 %.
        self._deux_ventes()
        data = self._get('profit_margins')
        self.assertEqual(Decimal(str(data['total_cost'])), Decimal('1.20'))

    def test_le_benefice_et_la_marge_tiennent_debout(self):
        self._deux_ventes()
        data = self._get('profit_margins')
        self.assertEqual(Decimal(str(data['gross_profit'])), Decimal('99.80'))
        # 99,80 / 101,00 = 98,81 %
        self.assertEqual(Decimal(str(data['gross_margin_percentage'])), Decimal('98.81'))

    def test_la_ventilation_gros_detail_est_convertie(self):
        # Celui-ci porte un `url_path` explicite, d'où le tiret.
        self._deux_ventes()
        data = self._get('sales-by-packaging')
        self.assertEqual(Decimal(str(data['total_revenue'])), Decimal('101.00'))
        cout = sum(Decimal(str(r['cost'])) for r in data['results'])
        self.assertEqual(cout, Decimal('1.20'))


class ActiviteUtilisateurTests(_BaseRapports):
    def test_l_activite_d_un_vendeur_est_convertie(self):
        self._deux_ventes()
        # Le CHEMIN garde son souligné là où le nom de route prend un tiret :
        # `@action` sans `url_path` laisse le nom de la méthode tel quel et ne
        # remplace les soulignés que dans `url_name`. Viser « user-activity »
        # rend un 404 de Django, sans corps, et non un 404 de DRF.
        data = self.client.get(
            '/api/v1/reports/statistics/user_activity/',
            {'period': 'month', 'user': str(self.manager.id)},
            **self._headers,
        )
        self.assertEqual(data.status_code, status.HTTP_200_OK, data.data)
        self.assertEqual(Decimal(str(data.data['sales']['total'])), Decimal('101.00'))
        total_seaux = sum(
            Decimal(str(l['total'])) for l in data.data['breakdown']
        )
        self.assertEqual(total_seaux, Decimal('101.00'))
        # Cet endpoint n'a pas de serializer : sans quantification, un montant
        # converti sortait à douze décimales (« 1.5217391315 » pour une dépense).
        self.assertEqual(str(data.data['sales']['total']), '101.00')
