"""
La monnaie se calcule depuis les billets remis, avec un seul arrondi.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LES MÊMES CAS QUE `core/test/pos-monnaie.test.ts`.                           │
│                                                                              │
│ Le terminal calcule la monnaie hors ligne (`monnaieARendre`) et l'imprime   │
│ avant toute synchronisation ; le serveur la recalcule (`resolve_change`) et │
│ c'est la sienne qui s'écrit. Changer un attendu ici sans le changer là-bas,  │
│ c'est laisser un ticket annoncer une monnaie que la base contredira.         │
└──────────────────────────────────────────────────────────────────────────────┘

Établissement en dollars, franc congolais à 2 300 (taux enregistré
0,000434782609, douze décimales, comme le serveur le stocke).

Le rôle est BORNÉ : un caissier, qui vend dans sa session.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import Payment, RegisterSession, Sale
from apps.sales.services import resolve_change
from apps.settings.models import Currency, OrganizationCurrency

from ._helpers import make_cash_payment_method, make_org_with_users

TAUX_CDF = Decimal('0.000434782609')


class _Base(APITestCase):

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.org.currency = 'USD'
        self.org.save(update_fields=['currency'])
        self.methode = make_cash_payment_method(self.org)

        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar US', 'symbol': '$', 'decimal_places': 2},
        )
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC', 'decimal_places': 0},
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=usd, is_primary=True,
            exchange_rate=Decimal('1'), is_active=True,
        )
        OrganizationCurrency.objects.create(
            organization=self.org, currency=cdf, is_primary=False,
            exchange_rate=TAUX_CDF, is_active=True,
        )
        from apps.settings.services import CurrencyService
        CurrencyService.invalidate_cache(self.org)

        RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.produit = Product.objects.create(
            organization=self.org, name='Article', sku='ART-M',
            cost_price=Decimal('100.00'), selling_price=Decimal('575.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('1000.000'), avg_cost=Decimal('100.00'),
        )
        self.acheteur = Customer.objects.create(
            organization=self.org, name='Client Monnaie', phone='0810000000',
        )

    def _vendre(self, reglements, monnaie_en, total='575.00', sale_type='retail'):
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': sale_type,
            'is_pos': True,
            'currency': 'USD',
            'exchange_rate': '1',
            'change_currency': monnaie_en,
            **({'customer': str(self.acheteur.id)} if sale_type == 'credit' else {}),
            'items': [{'product': str(self.produit.id), 'quantity': '1', 'unit_price': total}],
            'payments': [
                {'payment_method': str(self.methode.id), **r} for r in reglements
            ],
        }
        reponse = self.client.post(
            '/api/v1/sales/', payload, format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.content)
        return Sale.objects.get(pk=reponse.data['id'])


def _francs(montant, avec_taux=True):
    r = {'tendered_amount': str(montant), 'currency': 'CDF'}
    if avec_taux:
        r['exchange_rate'] = str(TAUX_CDF)
    return r


class MonnaieDepuisLesBilletsTests(_Base):

    def test_575_dollars_regles_1_700_000_francs_rendent_377_500(self):
        """Le franc perdu : 739,13 $ arrondis rendaient 377 499 FC."""
        vente = self._vendre([_francs(1_700_000)], 'CDF')
        self.assertEqual(vente.status, 'completed')
        self.assertEqual(vente.amount_paid, Decimal('739.13'))
        self.assertEqual(vente.change_currency, 'CDF')
        self.assertEqual(vente.change_amount, Decimal('377500.00'))
        sortie = CashMovement.objects.get(sale=vente, movement_type='change')
        self.assertEqual((sortie.currency, sortie.amount), ('CDF', Decimal('377500.00')))

    def test_sans_taux_envoye_le_serveur_convertit_au_taux_brut(self):
        """Le taux qu'il enregistre est celui qui a servi : douze décimales."""
        vente = self._vendre([_francs(1_700_000, avec_taux=False)], 'CDF')
        self.assertEqual(vente.change_amount, Decimal('377500.00'))
        reglement = vente.payments.get()
        self.assertEqual(reglement.exchange_rate, TAUX_CDF)
        self.assertEqual(reglement.amount, Decimal('739.13'))

    def test_un_reglement_presque_exact_rend_ses_dix_francs(self):
        vente = self._vendre([_francs(1_322_510)], 'CDF')
        self.assertEqual(vente.change_amount, Decimal('10.00'))

    def test_un_reglement_exact_ne_rend_rien(self):
        vente = self._vendre([_francs(1_322_500)], 'CDF')
        self.assertEqual(vente.status, 'completed')
        self.assertEqual(vente.change_amount, Decimal('0.00'))
        self.assertFalse(CashMovement.objects.filter(sale=vente, movement_type='change').exists())

    def test_monnaie_dans_la_devise_de_la_facture(self):
        vente = self._vendre([{'tendered_amount': '600.00', 'currency': 'USD'}], 'USD')
        self.assertEqual(vente.change_amount, Decimal('25.00'))

    def test_un_reglement_en_dollars_rendu_en_francs(self):
        vente = self._vendre([{'tendered_amount': '600.00', 'currency': 'USD'}], 'CDF')
        self.assertEqual((vente.change_currency, vente.change_amount), ('CDF', Decimal('57500.00')))

    def test_deux_devises_un_seul_arrondi(self):
        """500 $ + 200 000 FC pour 575 $ : 1 150 000 + 200 000 - 1 322 500."""
        vente = self._vendre(
            [{'tendered_amount': '500.00', 'currency': 'USD'}, _francs(200_000)], 'CDF',
        )
        self.assertEqual(vente.change_amount, Decimal('27500.00'))

    def test_une_facture_non_couverte_ne_rend_rien(self):
        vente = self._vendre([_francs(1_300_000)], 'CDF', sale_type='credit')
        self.assertEqual(vente.status, 'partially_paid')
        self.assertEqual(vente.change_amount, Decimal('0.00'))


class MonnaieAuReglementUlterieurTests(_Base):

    def test_le_reglement_d_une_facture_a_credit_rend_377_500(self):
        vente = self._vendre([], 'CDF', sale_type='credit')
        self.assertEqual(vente.status, 'pending')
        self.client.force_authenticate(user=self.cashier_a)
        reponse = self.client.post(
            f'/api/v1/sales/{vente.id}/add-payment/',
            {
                'payment_method': str(self.methode.id),
                'amount': '1700000', 'currency': 'CDF', 'change_currency': 'CDF',
            },
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.content)
        vente.refresh_from_db()
        self.assertEqual(vente.status, 'completed')
        self.assertEqual(vente.change_amount, Decimal('377500.00'))


class ReglementAncienTests(_Base):

    def test_un_taux_qui_n_a_pas_servi_n_ajoute_pas_des_centaines_de_francs(self):
        """
        Les anciens règlements convertis par le serveur gardaient un taux à six
        décimales (0,000435) avec un montant juste (739,13) : `tendered × taux`
        vaut 739,50, et compter cet « écart » rendrait 851 FC de trop. Un écart
        plus grand qu'un demi-centime n'est pas un arrondi : il est ignoré.
        """
        vente = self._vendre([_francs(1_700_000)], 'CDF')
        Payment.objects.filter(sale=vente).update(exchange_rate=Decimal('0.000435'))
        montant, devise, _ = resolve_change(vente, 'CDF')
        # Le repli d'avant : surplus arrondi, converti au taux brut.
        self.assertEqual((devise, montant), ('CDF', Decimal('377499.00')))
