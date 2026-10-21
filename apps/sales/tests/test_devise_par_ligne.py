"""
Une vente dont les lignes sont annoncées dans deux monnaies.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE DÉPÔT VENTILAIT DÉJÀ PARTOUT, SAUF SUR UNE VENTE.                        │
│                                                                              │
│ La dette client, le tiroir, le livre de caisse et la balance âgée portent    │
│ tous une ligne par devise. Une vente, elle, avait une devise unique et huit  │
│ montants scalaires : un marchand qui annonce la farine en francs et le savon │
│ importé en dollars ne pouvait pas l'écrire.                                  │
│                                                                              │
│ ⚠ LES SCALAIRES DE `Sale` RESTENT, et ces tests le vérifient explicitement.  │
│ C'est ce qui permet aux dix-sept agrégats `primary_sum` de lire une COLONNE  │
│ sans joindre la table de ventilation - donc sans le fan-out qui, mesuré ici, │
│ faisait rendre 217 227,70 à une somme qui valait 107 356,40.                 │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale, SaleCurrencyTotal
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency


class _DeuxDevisesSetup(APITestCase):
    """Établissement tenu en CDF, qui facture aussi en USD. 1 USD = 2 800 CDF."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)

        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=usd,
            defaults={'exchange_rate': Decimal('2800'), 'is_active': True},
        )

        self.farine = self._produit('Farine', 'FAR', '1500.00')
        self.savon = self._produit('Savon importé', 'SAV', '2.00')

        self.client_credit = Customer.objects.create(
            organization=self.org, code='C-MIX', name='Client deux devises',
            credit_limit=Decimal('10000000'), current_balance=Decimal('0'),
        )
        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _produit(self, nom, sku, pv):
        p = Product.objects.create(
            organization=self.org, name=nom, slug=sku.lower(), sku=sku,
            cost_price=Decimal('1.00'), selling_price=Decimal(pv),
            tax_rate=Decimal('0.00'), is_taxable=False,
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=p, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1.00'),
        )
        return p

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _vendre(self, items, devise_facture=None):
        corps = {
            'register': str(self.register.id), 'warehouse': str(self.warehouse.id),
            'sale_type': 'credit', 'customer': str(self.client_credit.id),
            'is_pos': True, 'items': items, 'payments': [],
        }
        if devise_facture:
            corps['currency'] = devise_facture
        resp = self.client.post('/api/v1/sales/', corps, format='json', **self._headers)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return Sale.objects.get(pk=resp.data['id'])


class VenteMonoDeviseInchangeeTests(_DeuxDevisesSetup):
    """
    Le CONTRÔLE : une vente d'une seule devise doit rendre exactement ce
    qu'elle rendait avant. Sans lui, on ne saurait pas si la ventilation a
    déplacé un centime sur l'écrasante majorité des ventes.
    """

    def test_les_scalaires_ne_bougent_pas(self):
        v = self._vendre([{
            'product': str(self.farine.id), 'quantity': '2', 'unit_price': '1500.00',
        }])
        self.assertEqual(v.total, Decimal('3000.00'))
        self.assertEqual(v.subtotal, Decimal('3000.00'))
        self.assertEqual(v.amount_due, Decimal('3000.00'))

    def test_une_seule_ligne_de_ventilation(self):
        v = self._vendre([{
            'product': str(self.farine.id), 'quantity': '2', 'unit_price': '1500.00',
        }])
        lignes = list(v.currency_totals.all())
        self.assertEqual(len(lignes), 1)
        self.assertEqual(lignes[0].currency, 'CDF')
        self.assertEqual(lignes[0].total, Decimal('3000.00'))

    def test_la_ligne_herite_de_la_devise_de_la_facture(self):
        v = self._vendre([{
            'product': str(self.farine.id), 'quantity': '1', 'unit_price': '1500.00',
        }])
        item = v.items.first()
        self.assertEqual(item.price_currency, 'CDF')


class VenteMixteTests(_DeuxDevisesSetup):

    def _vente_mixte(self):
        return self._vendre([
            {'product': str(self.farine.id), 'quantity': '2',
             'unit_price': '1500.00', 'price_currency': 'CDF'},
            {'product': str(self.savon.id), 'quantity': '3',
             'unit_price': '2.00', 'price_currency': 'USD'},
        ])

    def test_chaque_devise_a_sa_ligne(self):
        v = self._vente_mixte()
        par_code = {l.currency: l for l in v.currency_totals.all()}
        self.assertEqual(set(par_code), {'CDF', 'USD'})

    def test_chaque_ligne_porte_le_montant_ANNONCE_au_client(self):
        """Aucune conversion dans la ventilation : c'est le chiffre du ticket."""
        v = self._vente_mixte()
        par_code = {l.currency: l for l in v.currency_totals.all()}
        self.assertEqual(par_code['CDF'].total, Decimal('3000.00'))
        self.assertEqual(par_code['USD'].total, Decimal('6.00'))

    def test_le_taux_est_FIGE_sur_chaque_ligne(self):
        v = self._vente_mixte()
        par_code = {l.currency: l for l in v.currency_totals.all()}
        self.assertEqual(par_code['USD'].exchange_rate, Decimal('2800.000000000000'))
        self.assertEqual(par_code['CDF'].exchange_rate, Decimal('1.000000000000'))

    def test_le_total_de_la_facture_est_la_somme_CONVERTIE(self):
        """3 000 FC + (6 $ x 2 800) = 3 000 + 16 800 = 19 800 FC."""
        v = self._vente_mixte()
        self.assertEqual(v.total, Decimal('19800.00'))
        self.assertEqual(v.currency, 'CDF')

    def test_le_scalaire_reste_lisible_sans_jointure(self):
        """
        ⚠ C'EST CE QUI REND LE LOT TENABLE. Un agrégat qui joindrait la table
        de ventilation compterait la vente une fois par devise - le fan-out
        déjà mesuré dans ce dépôt, à facteur 2,02.
        """
        self._vente_mixte()
        from django.db.models import Count, Sum
        brut = Sale.objects.filter(organization=self.org).aggregate(
            n=Count('id'), total=Sum('total'),
        )
        self.assertEqual(brut['n'], 1)
        self.assertEqual(brut['total'], Decimal('19800.00'))

    def test_la_dette_du_client_suit_la_facture(self):
        v = self._vente_mixte()
        self.assertEqual(v.amount_due, Decimal('19800.00'))

    def test_une_ligne_porte_sa_devise_et_son_taux(self):
        v = self._vente_mixte()
        par_sku = {i.product.sku: i for i in v.items.all()}
        self.assertEqual(par_sku['USD' if False else 'SAV'].price_currency, 'USD')
        self.assertEqual(
            par_sku['SAV'].price_exchange_rate, Decimal('2800.000000000000'),
        )
        self.assertEqual(par_sku['FAR'].price_currency, 'CDF')

    def test_le_cout_n_est_JAMAIS_converti(self):
        """
        `cost_price` vient du catalogue, qui n'a aucune devise : il est DÉJÀ en
        principale. Lui appliquer le taux de sa ligne le diviserait par 2 800 et
        ferait passer la marge de 40 % à 100 %.
        """
        v = self._vente_mixte()
        savon = v.items.get(product=self.savon)
        self.assertEqual(savon.cost_price, Decimal('1.00'))

    def test_la_ventilation_est_rendue_par_l_API(self):
        v = self._vente_mixte()
        resp = self.client.get(f'/api/v1/sales/{v.id}/', **self._headers)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        codes = {l['currency'] for l in resp.data['currency_totals']}
        self.assertEqual(codes, {'CDF', 'USD'})
