"""
Tests de la numérotation des documents imprimés.

Le numéro d'un reçu était jusqu'ici fabriqué dans le navigateur à partir de
``Date.now()`` : jamais stocké, jamais retrouvable, et différent à chaque
réimpression. Ces tests fixent les trois propriétés qui rendent un numéro
opposable à un client : unicité sous concurrence, isolement par organisation,
et stabilité d'une réimpression à l'autre.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer, CustomerTransaction
from apps.core.models import DocumentSequence
from apps.core.numbering import (
    PREFIX_ADJUSTMENT,
    PREFIX_DEBT_PAYMENT,
    allocate_document_number,
)
from apps.sales.models import Payment

from apps.contacts.tests.test_debt_flows import _DebtBaseTest


class AllocateDocumentNumberTests(APITestCase):
    """Forme du numéro, incrément, cloisonnement."""

    def setUp(self):
        from apps.sales.tests._helpers import make_org_with_users

        self.__dict__.update(make_org_with_users())

    def test_format_is_prefix_year_and_rank(self):
        number = allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        self.assertEqual(number, 'RGL-2026-00001')

    def test_rank_increments_within_the_same_year(self):
        first = allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        second = allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        self.assertEqual((first, second), ('RGL-2026-00001', 'RGL-2026-00002'))

    def test_prefixes_have_independent_counters(self):
        allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        adjustment = allocate_document_number(self.org, PREFIX_ADJUSTMENT, year=2026)
        self.assertEqual(adjustment, 'AJU-2026-00001')

    def test_counter_restarts_on_year_change(self):
        allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        self.assertEqual(
            allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2027),
            'RGL-2027-00001',
        )

    def test_organizations_never_share_a_counter(self):
        from apps.organizations.models import Organization

        # Organisation créée à la main : `make_org_with_users` fixe les e-mails
        # et le slug, un second appel se heurterait à leurs contraintes d'unicité.
        other = Organization.objects.create(name='Autre Org', slug='autre-org')
        allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        allocate_document_number(self.org, PREFIX_DEBT_PAYMENT, year=2026)
        self.assertEqual(
            allocate_document_number(other, PREFIX_DEBT_PAYMENT, year=2026),
            'RGL-2026-00001',
        )
        self.assertEqual(DocumentSequence.objects.count(), 2)


class ConcurrentAllocationTests(TransactionTestCase):
    """
    Deux allocations simultanées ne doivent jamais rendre le même numéro.

    ``TransactionTestCase`` et non ``APITestCase`` : il faut de vraies
    transactions validées pour que le ``select_for_update`` s'exerce. Sous
    ``APITestCase``, tout se déroule dans une transaction unique et le verrou ne
    départage rien, ce qui est exactement le piège qui avait laissé passer un
    ``select_for_update`` hors ``atomic`` dans ``apply_payment_to_sale``.
    """

    reset_sequences = True

    def setUp(self):
        from apps.sales.tests._helpers import make_org_with_users

        self.__dict__.update(make_org_with_users())

    def test_parallel_allocations_are_all_distinct(self):
        if connection.vendor != 'postgresql':
            self.skipTest("Le verrou de ligne n'existe pas sous SQLite.")

        workers = 8

        def allocate():
            try:
                with transaction.atomic():
                    return allocate_document_number(
                        self.org, PREFIX_DEBT_PAYMENT, year=2026,
                    )
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            numbers = list(pool.map(lambda _: allocate(), range(workers)))

        self.assertEqual(len(set(numbers)), workers, numbers)
        self.assertEqual(
            sorted(numbers),
            [f'RGL-2026-{i:05d}' for i in range(1, workers + 1)],
        )


class PaymentReceiptNumberTests(_DebtBaseTest):
    """Le numéro voyage jusqu'aux lignes écrites, et jusqu'à la réponse."""

    def test_settling_an_invoice_stamps_the_payment_and_the_debt_movement(self):
        sale_id = self._create_sale()
        self.client.force_authenticate(user=self.cashier_a)

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        payment = Payment.objects.filter(sale_id=sale_id).latest('paid_at')
        self.assertTrue(payment.receipt_number.startswith('RGL-'))

        movement = CustomerTransaction.objects.filter(
            customer=self.customer,
            transaction_type=CustomerTransaction.TransactionType.PAYMENT,
        ).latest('created_at')
        self.assertEqual(movement.receipt_number, payment.receipt_number)

    def test_one_number_for_a_payment_that_settles_several_invoices(self):
        """
        Le client repart avec UN papier : les trois factures soldées par le même
        versement doivent porter le même numéro, pas trois numéros distincts.
        """
        for _ in range(3):
            self._create_sale()

        # Ces actions demandent un rôle gestionnaire : le caissier est refusé.
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '6000.00', 'payment_method': 'cash'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(len(resp.data['settled_invoices']), 3)

        numbers = set(
            Payment.objects.filter(sale__customer=self.customer)
            .exclude(receipt_number='')
            .values_list('receipt_number', flat=True)
        )
        self.assertEqual(len(numbers), 1, numbers)
        self.assertEqual(numbers.pop(), resp.data['receipt_number'])

    def test_envelope_carries_the_number_even_without_a_transaction_row(self):
        """
        ``transaction`` est nul quand le versement solde intégralement des
        factures sans laisser de reliquat, c'est-à-dire dans le cas nominal. Le
        reçu ne peut donc pas en dépendre pour son numéro ni pour les soldes.
        """
        self._create_sale()
        self.client.force_authenticate(user=self.owner)

        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '2000.00', 'payment_method': 'cash'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIsNone(resp.data['transaction'])
        self.assertTrue(resp.data['receipt_number'].startswith('RGL-'))
        self.assertEqual(Decimal(resp.data['balance_before']), Decimal('2000.00'))
        self.assertEqual(Decimal(resp.data['balance_after']), Decimal('0.00'))

    def test_a_pure_advance_is_numbered_as_an_advance(self):
        """Sans facture ouverte, l'argent reçu est une avance : préfixe AVC."""
        # Ces actions demandent un rôle gestionnaire : le caissier est refusé.
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '5000.00', 'payment_method': 'cash'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertTrue(resp.data['receipt_number'].startswith('AVC-'), resp.data)
        self.assertEqual(
            resp.data['transaction']['receipt_number'], resp.data['receipt_number'],
        )

    def test_balance_adjustment_is_numbered_and_reports_both_balances(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/adjust-balance/',
            {'amount': '750.00', 'notes': 'Régularisation'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertTrue(resp.data['receipt_number'].startswith('AJU-'))
        self.assertEqual(Decimal(resp.data['balance_before']), Decimal('0.00'))
        self.assertEqual(Decimal(resp.data['balance_after']), Decimal('750.00'))

    def test_reprinting_never_changes_the_number(self):
        """
        La propriété qui manquait : relire la facture doit rendre le même numéro.
        Avec ``PAY-${Date.now()}``, chaque réimpression en inventait un nouveau.
        """
        sale_id = self._create_sale()
        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )

        def read_number():
            resp = self.client.get(
                f'/api/v1/sales/{sale_id}/', **self._headers(),
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            return [p['receipt_number'] for p in resp.data['payments']]

        self.assertEqual(read_number(), read_number())
        self.assertTrue(all(n.startswith('RGL-') for n in read_number()))
