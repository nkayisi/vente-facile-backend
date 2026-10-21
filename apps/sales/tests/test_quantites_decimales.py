"""
Vendre au poids : 0,5 kg de farine, 1,5 L d'huile.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE SERVEUR ACCEPTAIT DÉJÀ LES DÉCIMALES PARTOUT, SAUF À LA VENTE.            │
│                                                                              │
│ Tous les champs de quantité du projet sont des                               │
│ `DecimalField(max_digits=15, decimal_places=3)`, et `SaleItem.quantity`      │
│ porte `MinValueValidator(Decimal('0.001'))`. Mais le sérialiseur de vente     │
│ refusait toute fraction dès qu'un produit était conditionné, au motif        │
│ qu'elle « rendrait le partage scellé/vrac incalculable » - ce qui était vrai │
│ du RAYON et non de la vente.                                                 │
│                                                                              │
│ Le rayon sait maintenant le dire : `_settle` reverse la part entamée d'un    │
│ contenant au vrac, parce qu'un demi-carton ouvert EST du vrac.               │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product, Unit
from apps.sales.models import RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users


class _VenteAuPoidsSetup(APITestCase):
    """Deux produits : l'un au poids, l'autre conditionné."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)

        self.kg = Unit.objects.create(organization=self.org, name='kg', symbol='kg')
        self.sac = Unit.objects.create(organization=self.org, name='sac', symbol='sac')

        # Au poids, sans conditionnement : le cas de la demande.
        self.farine = Product.objects.create(
            organization=self.org, name='Farine', slug='farine', sku='FAR',
            unit=self.kg,
            cost_price=Decimal('1000.00'), selling_price=Decimal('1500.00'),
            tax_rate=Decimal('0.00'), is_taxable=False,
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.farine, warehouse=self.warehouse,
            quantity=Decimal('25.000'), avg_cost=Decimal('1000.00'),
        )

        # Conditionné : sac de 25 kg. On y vérifie la normalisation du rayon.
        self.riz = Product.objects.create(
            organization=self.org, name='Riz', slug='riz', sku='RIZ',
            unit=self.kg, packaging_unit=self.sac,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL,
            units_per_package=25, allow_auto_unpacking=True,
            cost_price=Decimal('1000.00'), selling_price=Decimal('1500.00'),
            wholesale_price=Decimal('35000.00'),
            tax_rate=Decimal('0.00'), is_taxable=False,
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.riz, warehouse=self.warehouse,
            quantity=Decimal('250.000'), package_quantity=Decimal('10.000'),
            loose_quantity=Decimal('0.000'), avg_cost=Decimal('1000.00'),
        )

        # Vente À CRÉDIT dans tout ce fichier : une vente comptant exige un
        # règlement exact, et calculer ce montant dans chaque cas ferait porter
        # le test sur l'arithmétique des totaux plutôt que sur les quantités.
        # Le crédit décrémente le stock dès l'émission, c'est ce qu'on mesure.
        self.client_credit = Customer.objects.create(
            organization=self.org, code='C-DEC', name='Client au poids',
            credit_limit=Decimal('1000000'), current_balance=Decimal('0'),
        )

        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _vendre(self, items, attendu=status.HTTP_201_CREATED):
        resp = self.client.post(
            '/api/v1/sales/',
            {
                'register': str(self.register.id),
                'warehouse': str(self.warehouse.id),
                'sale_type': 'credit', 'customer': str(self.client_credit.id),
                'is_pos': True,
                'items': items, 'payments': [],
            },
            format='json', **self._headers,
        )
        self.assertEqual(resp.status_code, attendu, resp.data)
        return resp

    def _stock(self, product):
        return Stock.objects.get(product=product, warehouse=self.warehouse)


class VenteAuPoidsTests(_VenteAuPoidsSetup):

    def test_un_demi_kilo_se_vend(self):
        """Le cas rapporté du terrain, sur un produit sans conditionnement."""
        resp = self._vendre([{
            'product': str(self.farine.id),
            'quantity': '0.5', 'unit_price': '1500.00',
        }])
        sale = Sale.objects.get(pk=resp.data['id'])
        self.assertEqual(sale.items.first().quantity, Decimal('0.500'))
        self.assertEqual(sale.total, Decimal('750.00'))

    def test_le_stock_sort_au_gramme(self):
        self._vendre([{
            'product': str(self.farine.id),
            'quantity': '0.750', 'unit_price': '1500.00',
        }])
        self.assertEqual(self._stock(self.farine).quantity, Decimal('24.250'))

    def test_le_plus_petit_pas_enregistrable(self):
        self._vendre([{
            'product': str(self.farine.id),
            'quantity': '0.001', 'unit_price': '1500.00',
        }])
        self.assertEqual(self._stock(self.farine).quantity, Decimal('24.999'))

    def test_en_dessous_de_ce_pas_le_serveur_refuse(self):
        """`MinValueValidator(0.001)` : la borne basse ne bouge pas."""
        self._vendre(
            [{'product': str(self.farine.id),
              'quantity': '0.0004', 'unit_price': '1500.00'}],
            attendu=status.HTTP_400_BAD_REQUEST,
        )

    def test_au_dela_de_max_digits_le_serveur_refuse(self):
        """La borne que le comptoir oppose désormais avant d'imprimer."""
        self._vendre(
            [{'product': str(self.farine.id),
              'quantity': '1000000000000', 'unit_price': '1.00'}],
            attendu=status.HTTP_400_BAD_REQUEST,
        )


class RayonApresUneVenteFractionnaireTests(_VenteAuPoidsSetup):
    """
    ┌──────────────────────────────────────────────────────────────────────────┐
    │ « 7,5 SACS SCELLÉS » N'EXISTE PAS EN MAGASIN.                            │
    │                                                                          │
    │ Il y a 7 sacs fermés et un sac OUVERT, dont le reliquat se compte au     │
    │ détail. C'est la règle que `split()` tenait déjà pour la reconstitution  │
    │ (« un carton entamé ne se rescelle pas ») et que `_settle` applique       │
    │ maintenant aussi au chemin de la vente : sans elle, les deux             │
    │ normalisaient différemment le même rayon.                                │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def test_le_contenant_entame_devient_du_vrac(self):
        self._vendre([{
            'product': str(self.riz.id),
            'package_quantity': '2.5', 'loose_quantity': '0',
            'unit_price': '1500.00', 'package_unit_price': '35000.00',
        }])

        stock = self._stock(self.riz)
        # 10 sacs - 2,5 = 7,5 → 7 sacs scellés + un demi-sac au détail.
        self.assertEqual(stock.package_quantity, Decimal('7.000'))
        self.assertEqual(stock.loose_quantity, Decimal('12.500'))
        # L'invariant tient : 7 x 25 + 12,5 = 187,5, soit 250 - 62,5.
        self.assertEqual(stock.quantity, Decimal('187.500'))

    def test_l_invariant_du_partage_est_preserve(self):
        """
        Le contrôle qui compte plus que les deux compteurs : quoi qu'on
        normalise, `scellés x facteur + vrac` doit redonner `quantity`.
        """
        self._vendre([{
            'product': str(self.riz.id),
            'package_quantity': '1.25', 'loose_quantity': '3.5',
            'unit_price': '1500.00', 'package_unit_price': '35000.00',
        }])
        stock = self._stock(self.riz)
        self.assertEqual(
            stock.package_quantity * self.riz.units_per_package + stock.loose_quantity,
            stock.quantity,
        )

    def test_la_ligne_de_vente_garde_ses_deux_compteurs(self):
        resp = self._vendre([{
            'product': str(self.riz.id),
            'package_quantity': '2.5', 'loose_quantity': '0',
            'unit_price': '1500.00', 'package_unit_price': '35000.00',
        }])
        item = Sale.objects.get(pk=resp.data['id']).items.first()
        self.assertEqual(item.package_quantity, Decimal('2.500'))
        self.assertEqual(item.quantity, Decimal('62.500'))
