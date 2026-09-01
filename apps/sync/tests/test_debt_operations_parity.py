"""
Parité des actes de DETTE entre le back-office et le terminal.

Ces trois actes - régler, avancer, ajuster - passaient par deux corps
différents : la vue pour le web, une réécriture dans `sync.handlers` pour le
mobile. La réécriture avait déjà dérivé sur trois points, chacun couvert ici :

  1. `settle_currency` était ignoré. Un client devant en USD qui paie en francs
     ne voyait pas sa dette bouger : l'argent partait en avance CDF, et le
     marchand voyait à la fois une dette et une avance chez le même client.
  2. le taux de change n'était pas résolu.
  3. le reçu était toujours numéroté `RGL`, même quand aucune facture n'était
     ouverte. Un versement sans facture est une avance, et son papier porte
     `AVC` - c'est la seule chose qui distingue les deux dans une liasse.

Et un quatrième point, propre au hors-ligne : le terminal IMPRIME le reçu avant
de pouvoir envoyer l'acte. Son numéro est sur le papier que le client détient ;
le serveur doit le reprendre, pas en allouer un second.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.contacts.models import Customer, CustomerTransaction
from apps.contacts import services as contacts_services
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency

OPERATIONS = '/api/v1/sync/operations/'


class _DebtBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _client_avec_facture(self, nom, montant, devise=None):
        """Un client et une facture ouverte à son nom."""
        customer = Customer.objects.create(
            organization=self.org, name=nom, code=nom[:4].upper(),
            credit_limit=Decimal('0'),
        )
        vente = Sale.objects.create(
            organization=self.org, reference=f'VT-{nom}', customer=customer,
            register=self.register, warehouse=self.warehouse,
            status=Sale.Status.PENDING, subtotal=montant, total=montant,
            amount_paid=Decimal('0'), amount_due=montant,
            sold_by=self.owner, **({'currency': devise} if devise else {}),
        )
        contacts_services.apply_debt(
            customer, montant, currency=vente.currency, user=self.owner,
        )
        customer.refresh_from_db()
        return customer, vente

    def _send(self, kind, payload, op_id):
        """
        Le MÊME instant des deux côtés, et ce n'est pas une commodité.

        Ces tests comparent l'état laissé par le MÊME acte joué de deux façons.
        Depuis que le journal date ses écritures à l'heure de l'acte
        (`apps.core.clock`), une date figée dans le passé décalerait les rangs
        du grand livre du seul côté mobile : l'écriture s'y rangerait avant la
        facture que la fixture vient de créer, et la comparaison échouerait sur
        un ORDRE, non sur un effet. « Même acte » comprend « même moment ».

        La parité des DATES a son propre fichier,
        `test_operation_dates.py`, où elle est vérifiée pour elle-même.
        """
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id,
                'kind': kind,
                'seq': 1,
                'depends_on': [],
                'occurred_at': timezone.now().isoformat(),
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))
        return verdict


class ReglementParityTests(_DebtBaseTest):
    def _etat(self, customer):
        """Ce que le versement a laissé derrière lui."""
        customer.refresh_from_db()
        return {
            'solde': customer.current_balance,
            'soldes': contacts_services.balances_by_currency(customer),
            'ventes_soldees': list(
                Sale.objects.filter(customer=customer)
                .order_by('reference').values_list('status', 'amount_due')
            ),
            'mouvements_caisse': CashMovement.objects.filter(
                organization=self.org, customer=customer
            ).count(),
            'transactions': list(
                CustomerTransaction.objects.filter(customer=customer)
                .order_by('created_at').values_list('transaction_type', 'amount')
            ),
        }

    def test_a_payment_leaves_the_same_state_by_both_paths(self):
        web_client, _ = self._client_avec_facture('Web', Decimal('5000.00'))
        mob_client, _ = self._client_avec_facture('Mob', Decimal('5000.00'))

        corps = {'amount': '3000.00', 'payment_method': 'cash'}
        reponse = self.client.post(
            f'/api/v1/customers/{web_client.id}/record-payment/',
            corps, format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(mob_client.id), **corps},
            'bbbb1111-0000-4000-8000-000000000001',
        ))

        self.assertEqual(self._etat(web_client), self._etat(mob_client))

    def test_a_payment_without_any_open_invoice_is_numbered_as_an_advance(self):
        """
        Le préfixe n'est pas décoratif : `AVC` et `RGL` sont ce qui distingue une
        avance d'un règlement dans une liasse de reçus. Le gestionnaire mobile
        écrivait `RGL` dans les deux cas.
        """
        customer = Customer.objects.create(
            organization=self.org, name='Sans facture', code='SF',
        )
        verdict = self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(customer.id), 'amount': '2000.00'},
            'bbbb1111-0000-4000-8000-000000000002',
        ))
        self.assertTrue(
            verdict['server_ids']['receipt_number'].startswith('AVC-'),
            verdict['server_ids']['receipt_number'],
        )

    def test_a_payment_that_settles_an_invoice_is_numbered_as_a_payment(self):
        customer, _ = self._client_avec_facture('Avec', Decimal('5000.00'))
        verdict = self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(customer.id), 'amount': '2000.00'},
            'bbbb1111-0000-4000-8000-000000000003',
        ))
        self.assertTrue(
            verdict['server_ids']['receipt_number'].startswith('RGL-'),
            verdict['server_ids']['receipt_number'],
        )

    def test_the_receipt_number_printed_offline_is_kept(self):
        """
        Le terminal imprime AVANT de pouvoir envoyer. Si le serveur allouait son
        propre numéro, le client détiendrait un papier désignant un reçu qui
        n'existe nulle part.
        """
        customer, vente = self._client_avec_facture('Papier', Decimal('5000.00'))
        numero = 'RGL-20260829-K7QM-0007'

        verdict = self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(customer.id), 'amount': '5000.00',
             'receipt_number': numero},
            'bbbb1111-0000-4000-8000-000000000004',
        ))

        self.assertEqual(verdict['server_ids']['receipt_number'], numero)
        vente.refresh_from_db()
        self.assertEqual(
            list(vente.payments.values_list('receipt_number', flat=True)), [numero],
        )


class DeviseDImputationTests(_DebtBaseTest):
    """
    `settle_currency` : la devise des FACTURES visées, distincte de celle des
    billets remis.
    """

    def setUp(self):
        super().setUp()
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2},
        )
        OrganizationCurrency.objects.get_or_create(
            organization=self.org, currency=usd,
            defaults={'exchange_rate': Decimal('2800'), 'is_active': True},
        )

    def test_francs_can_settle_a_dollar_invoice_by_the_mobile_path(self):
        customer, vente = self._client_avec_facture('Devise', Decimal('10.00'), devise='USD')
        self.assertEqual(vente.currency, 'USD')

        self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(customer.id), 'amount': '28000.00',
             'currency': 'CDF', 'settle_currency': 'USD'},
            'cccc1111-0000-4000-8000-000000000001',
        ))

        vente.refresh_from_db()
        self.assertEqual(vente.amount_due, Decimal('0.00'))
        self.assertEqual(vente.status, Sale.Status.COMPLETED)

    def test_ignoring_settle_currency_would_leave_the_invoice_untouched(self):
        """
        Le comportement CONTRE lequel le correctif protège, laissé explicite : le
        même versement sans `settle_currency` ne touche pas la facture en USD et
        part en avance CDF. Le marchand voit alors une dette ET une avance chez
        le même client.
        """
        customer, vente = self._client_avec_facture('Sans', Decimal('10.00'), devise='USD')

        self._verdict(self._send(
            'customer.record_payment',
            {'customer': str(customer.id), 'amount': '28000.00', 'currency': 'CDF'},
            'cccc1111-0000-4000-8000-000000000002',
        ))

        vente.refresh_from_db()
        self.assertEqual(vente.amount_due, Decimal('10.00'))
        self.assertEqual(
            contacts_services.get_balance(customer, 'CDF'), Decimal('-28000.00'),
        )


class AjustementParityTests(_DebtBaseTest):
    def _etat(self, customer):
        customer.refresh_from_db()
        return {
            'solde': customer.current_balance,
            'soldes': contacts_services.balances_by_currency(customer),
            'mouvements_caisse': CashMovement.objects.filter(
                organization=self.org, customer=customer
            ).count(),
            'transactions': list(
                CustomerTransaction.objects.filter(customer=customer)
                .order_by('created_at').values_list('transaction_type', 'amount')
            ),
        }

    def test_a_positive_adjustment_leaves_the_same_state_by_both_paths(self):
        web = Customer.objects.create(organization=self.org, name='AjWeb', code='AW')
        mob = Customer.objects.create(organization=self.org, name='AjMob', code='AM')

        corps = {'amount': '1500.00', 'notes': 'Correction inventaire'}
        reponse = self.client.post(
            f'/api/v1/customers/{web.id}/adjust-balance/',
            corps, format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self._verdict(self._send(
            'customer.adjust_balance', {'customer': str(mob.id), **corps},
            'dddd1111-0000-4000-8000-000000000001',
        ))

        self.assertEqual(self._etat(web), self._etat(mob))

    def test_a_negative_adjustment_enters_the_drawer_by_both_paths(self):
        """Réduire une dette, c'est recevoir de l'argent : il entre au tiroir."""
        web, _ = self._client_avec_facture('NegW', Decimal('4000.00'))
        mob, _ = self._client_avec_facture('NegM', Decimal('4000.00'))

        corps = {'amount': '-1000.00', 'notes': 'Geste commercial'}
        reponse = self.client.post(
            f'/api/v1/customers/{web.id}/adjust-balance/',
            corps, format='json', **self._headers(),
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)

        self._verdict(self._send(
            'customer.adjust_balance', {'customer': str(mob.id), **corps},
            'dddd1111-0000-4000-8000-000000000002',
        ))

        self.assertEqual(self._etat(web), self._etat(mob))
        self.assertEqual(
            CashMovement.objects.filter(organization=self.org, customer=mob).count(), 1,
        )

    def test_an_unknown_customer_is_rejected_not_retried(self):
        reponse = self._send(
            'customer.adjust_balance',
            {'customer': '00000000-0000-4000-8000-000000000000', 'amount': '10.00'},
            'dddd1111-0000-4000-8000-000000000003',
        )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], 'rejected')
