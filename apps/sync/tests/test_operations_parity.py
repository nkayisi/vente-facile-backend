"""
Parité entre le chemin web et le chemin mobile.

Le test central de ce fichier compare, champ par champ, l'état de la base après
la MÊME vente envoyée par `POST /sales/` et par l'opération `sale.create`.

C'est ce qui manquait. L'ancienne synchronisation écrivait avec
`objects.create()` : une vente poussée depuis le mobile n'inscrivait aucune
dette, n'attribuait aucun point de fidélité, n'entrait pas en caisse, ne
recevait pas de numéro de document et ne vérifiait aucun plafond de crédit. Rien
ne le signalait, parce que rien ne comparait les deux chemins.
"""
from decimal import Decimal

from django.db import connection
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import CashMovement
from apps.contacts.models import Customer, CustomerTransaction
from apps.inventory.models import Stock, StockMovement
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.sync.models import SyncOperation

OPERATIONS = '/api/v1/sync/operations/'


class _OperationsBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.product = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='0900000000',
            credit_limit=Decimal('1000000'),
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _cart(self, **surcharges):
        corps = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{
                'product': str(self.product.id),
                'quantity': '2',
                'unit_price': '2000.00',
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'tendered_amount': '4000.00',
            }],
        }
        corps.update(surcharges)
        return corps

    def _send(self, operations):
        return self.client.post(
            OPERATIONS, {'operations': operations}, format='json', **self._headers()
        )

    def _op(self, kind, payload, op_id, seq=1, depends_on=None):
        return {
            'operation_id': op_id,
            'kind': kind,
            'seq': seq,
            'depends_on': depends_on or [],
            'occurred_at': '2026-08-28T09:00:00Z',
            'payload': payload,
        }


class ParityTests(_OperationsBaseTest):
    """
    Le test le plus important du lot.

    Toute divergence ici est un défaut de câblage du répartiteur : les deux
    chemins doivent traverser exactement le même code métier.
    """

    def _etat(self, sale):
        """Ce que la vente a laissé derrière elle, au-delà d'elle-même."""
        return {
            'total': sale.total,
            'amount_paid': sale.amount_paid,
            'amount_due': sale.amount_due,
            'status': sale.status,
            'currency': sale.currency,
            'lignes': sale.items.count(),
            'reglements': sale.payments.count(),
            'mouvements_stock': StockMovement.objects.filter(
                organization=sale.organization, reference_id=sale.id
            ).count(),
            'mouvements_caisse': CashMovement.objects.filter(sale=sale).count(),
            'dettes': CustomerTransaction.objects.filter(sale=sale).count(),
        }

    def test_a_cash_sale_leaves_the_same_state_by_both_paths(self):
        web = self.client.post('/api/v1/sales/', self._cart(), format='json', **self._headers())
        self.assertEqual(web.status_code, status.HTTP_201_CREATED, web.data)
        vente_web = Sale.objects.get(id=web.data['id'])

        resp = self._send([self._op('sale.create', self._cart(), 'aaaa1111-0000-4000-8000-000000000001')])
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        verdict = resp.data['results'][0]
        self.assertEqual(verdict['verdict'], 'applied', verdict.get('errors'))
        vente_mobile = Sale.objects.get(id=verdict['server_ids']['sale'])

        self.assertEqual(self._etat(vente_web), self._etat(vente_mobile))

    def test_a_credit_sale_registers_the_debt_by_both_paths(self):
        """
        Le manque le plus grave de l'ancienne synchronisation.

        Une vente à crédit poussée depuis le mobile laissait la dette du client
        intacte, alors que son règlement ultérieur la décrémentait : le solde
        divergeait de la somme des factures ouvertes dès la première vente.
        """
        panier = self._cart(
            sale_type='credit', customer=str(self.customer.id), payments=[]
        )

        web = self.client.post('/api/v1/sales/', panier, format='json', **self._headers())
        self.assertEqual(web.status_code, status.HTTP_201_CREATED, web.data)
        vente_web = Sale.objects.get(id=web.data['id'])
        self.customer.refresh_from_db()
        solde_apres_web = self.customer.current_balance

        resp = self._send([self._op('sale.create', panier, 'aaaa1111-0000-4000-8000-000000000002')])
        vente_mobile = Sale.objects.get(id=resp.data['results'][0]['server_ids']['sale'])
        self.customer.refresh_from_db()

        # La dette a doublé : chaque chemin a inscrit la sienne, du même montant.
        self.assertEqual(
            self.customer.current_balance - solde_apres_web, solde_apres_web
        )
        self.assertEqual(self._etat(vente_web), self._etat(vente_mobile))
        self.assertEqual(
            CustomerTransaction.objects.filter(sale=vente_mobile).count(), 1,
            "la vente mobile n'a inscrit aucune dette",
        )

    def test_stock_is_decremented_by_both_paths(self):
        stock_initial = Stock.objects.get(
            product=self.product, warehouse=self.warehouse
        ).quantity

        self.client.post('/api/v1/sales/', self._cart(), format='json', **self._headers())
        apres_web = Stock.objects.get(
            product=self.product, warehouse=self.warehouse
        ).quantity

        self._send([self._op('sale.create', self._cart(), 'aaaa1111-0000-4000-8000-000000000003')])
        apres_mobile = Stock.objects.get(
            product=self.product, warehouse=self.warehouse
        ).quantity

        self.assertEqual(stock_initial - apres_web, Decimal('2.000'))
        self.assertEqual(apres_web - apres_mobile, Decimal('2.000'))

    def test_the_client_reference_is_kept(self):
        """
        Le ticket est déjà entre les mains du client.

        Le renuméroter côté serveur donnerait deux numéros pour une seule vente,
        ce que la numérotation serveur avait justement été créée pour supprimer.
        """
        panier = self._cart()
        panier['reference'] = 'VT-20260828-K7QM-0042'

        resp = self._send([self._op('sale.create', panier, 'aaaa1111-0000-4000-8000-000000000004')])
        vente = Sale.objects.get(id=resp.data['results'][0]['server_ids']['sale'])

        self.assertEqual(vente.reference, 'VT-20260828-K7QM-0042')

    def test_the_client_identifier_is_kept(self):
        """Sans quoi la vente locale ne pourrait pas se réconcilier avec elle-même."""
        panier = self._cart()
        panier['id'] = 'bbbb2222-0000-4000-8000-000000000001'

        resp = self._send([self._op('sale.create', panier, 'aaaa1111-0000-4000-8000-000000000005')])

        self.assertEqual(
            resp.data['results'][0]['server_ids']['sale'],
            'bbbb2222-0000-4000-8000-000000000001',
        )
        self.assertTrue(Sale.objects.filter(id='bbbb2222-0000-4000-8000-000000000001').exists())


class IdempotencyTests(_OperationsBaseTest):
    """
    Le renvoi d'un lot ne doit jamais encaisser deux fois.

    Le réseau peut lâcher entre le moment où le serveur valide une vente et
    celui où le client reçoit la réponse. Le client renverra alors la même
    opération : sans idempotence, le marchand encaisserait deux fois et le stock
    sortirait deux fois.
    """

    def test_the_same_operation_twice_creates_one_sale(self):
        op = self._op('sale.create', self._cart(), 'cccc3333-0000-4000-8000-000000000001')

        premier = self._send([op])
        second = self._send([op])

        self.assertEqual(premier.data['results'][0]['verdict'], 'applied')
        self.assertEqual(second.data['results'][0]['verdict'], 'duplicate')
        self.assertEqual(Sale.objects.count(), 1)

    def test_a_replayed_operation_returns_the_same_result(self):
        """Le client doit pouvoir se réconcilier sur un renvoi comme sur un envoi."""
        op = self._op('sale.create', self._cart(), 'cccc3333-0000-4000-8000-000000000002')

        premier = self._send([op]).data['results'][0]
        second = self._send([op]).data['results'][0]

        self.assertEqual(premier['server_ids'], second['server_ids'])
        self.assertEqual(
            premier['authoritative']['reference'], second['authoritative']['reference']
        )

    def test_stock_is_decremented_only_once(self):
        op = self._op('sale.create', self._cart(), 'cccc3333-0000-4000-8000-000000000003')
        avant = Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity

        self._send([op])
        self._send([op])

        apres = Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity
        self.assertEqual(avant - apres, Decimal('2.000'))


class SavepointTests(_OperationsBaseTest):
    """
    Une opération fautive n'emporte pas les autres.

    C'était le défaut de l'ancienne file d'attente : elle envoyait tout d'un
    bloc, et un seul enregistrement refusé faisait échouer les deux cents
    autres, en incrémentant leur compteur d'échecs par-dessus le marché.
    """

    def test_transactions_are_not_wrapped_around_the_whole_batch(self):
        """
        Garde-fou de configuration.

        `ATOMIC_REQUESTS` envelopperait toute la requête dans une transaction
        unique et désactiverait les points de sauvegarde EN SILENCE : le lot
        redeviendrait tout-ou-rien sans que rien ne le signale.
        """
        self.assertFalse(
            connection.settings_dict.get('ATOMIC_REQUESTS'),
            "ATOMIC_REQUESTS annulerait l'isolation par operation",
        )

    def test_one_invalid_operation_does_not_sink_the_others(self):
        valide_1 = self._op('sale.create', self._cart(), 'dddd4444-0000-4000-8000-000000000001', seq=1)
        invalide = self._op(
            'sale.create',
            self._cart(items=[{'product': 'ffffffff-0000-4000-8000-999999999999',
                               'quantity': '1', 'unit_price': '10'}]),
            'dddd4444-0000-4000-8000-000000000002', seq=2,
        )
        valide_2 = self._op('sale.create', self._cart(), 'dddd4444-0000-4000-8000-000000000003', seq=3)

        resp = self._send([valide_1, invalide, valide_2])
        verdicts = [r['verdict'] for r in resp.data['results']]

        self.assertEqual(verdicts, ['applied', 'rejected', 'applied'])
        self.assertEqual(Sale.objects.count(), 2)

    def test_a_rejected_operation_is_never_retried(self):
        """
        Un refus métier est définitif.

        Le réessayer serait marteler le serveur avec une vente qu'il refusera
        toujours, et retarder d'autant celles qui passeraient.
        """
        invalide = self._op(
            'sale.create',
            self._cart(items=[{'product': 'ffffffff-0000-4000-8000-999999999999',
                               'quantity': '1', 'unit_price': '10'}]),
            'dddd4444-0000-4000-8000-000000000004',
        )
        self._send([invalide])
        second = self._send([invalide])

        self.assertEqual(second.data['results'][0]['verdict'], 'rejected')
        trace = SyncOperation.objects.get(pk='dddd4444-0000-4000-8000-000000000004')
        self.assertEqual(trace.verdict, 'rejected')
        self.assertIsNotNone(trace.error, "le motif du refus doit rester consultable")

    def test_a_dependent_operation_is_short_circuited(self):
        invalide = self._op(
            'sale.create',
            self._cart(items=[{'product': 'ffffffff-0000-4000-8000-999999999999',
                               'quantity': '1', 'unit_price': '10'}]),
            'dddd4444-0000-4000-8000-000000000005', seq=1,
        )
        dependante = self._op(
            'sale.add_payment', {'sale': 'peu-importe', 'amount': '100'},
            'dddd4444-0000-4000-8000-000000000006', seq=2,
            depends_on=['dddd4444-0000-4000-8000-000000000005'],
        )

        resp = self._send([invalide, dependante])
        second = resp.data['results'][1]

        self.assertEqual(second['verdict'], 'rejected')
        self.assertEqual(second['errors']['code'], 'dependency_rejected')


class ContractTests(_OperationsBaseTest):
    def test_an_unknown_kind_is_refused_without_sinking_the_batch(self):
        resp = self._send([
            self._op('quelque.chose', {}, 'eeee5555-0000-4000-8000-000000000001', seq=1),
            self._op('sale.create', self._cart(), 'eeee5555-0000-4000-8000-000000000002', seq=2),
        ])
        verdicts = [r['verdict'] for r in resp.data['results']]
        self.assertEqual(verdicts, ['rejected', 'applied'])
        self.assertEqual(resp.data['results'][0]['errors']['code'], 'unknown_kind')

    def test_an_empty_batch_is_refused(self):
        resp = self.client.post(
            OPERATIONS, {'operations': []}, format='json', **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_operations_without_an_identifier_are_refused(self):
        resp = self.client.post(
            OPERATIONS, {'operations': [{'kind': 'sale.create', 'payload': {}}]},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data['code'], 'malformed_operations')

    def test_operations_are_applied_in_sequence_order(self):
        """Le client émet dans un ordre ; le serveur doit le respecter."""
        premier = self._op('sale.create', self._cart(), 'eeee5555-0000-4000-8000-000000000003', seq=2)
        second = self._op('sale.create', self._cart(), 'eeee5555-0000-4000-8000-000000000004', seq=1)

        self._send([premier, second])
        ordre = list(
            SyncOperation.objects.order_by('received_at').values_list('seq', flat=True)
        )
        self.assertEqual(ordre, [1, 2])


class SubscriptionGateTests(_OperationsBaseTest):
    """
    Lire reste ouvert, écrire se ferme.

    `/api/v1/sync/` figurait dans une liste testée en `startswith` :
    `/api/v1/sync/operations/` en aurait hérité l'exemption, et un marchand dont
    l'abonnement a expiré aurait pu écrire des ventes indéfiniment.
    """

    def _expirer_abonnement(self):
        from django.utils import timezone
        from apps.subscriptions.models import Subscription
        Subscription.objects.filter(organization=self.org).update(
            status=Subscription.Status.EXPIRED,
            current_period_end=timezone.now() - __import__('datetime').timedelta(days=1),
        )

    def test_pull_still_works_with_an_expired_subscription(self):
        self._expirer_abonnement()
        resp = self.client.get(
            '/api/v1/sync/pull/', {'table': 'products'}, **self._headers()
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_operations_are_refused_with_an_expired_subscription(self):
        self._expirer_abonnement()
        resp = self._send([
            self._op('sale.create', self._cart(), 'ffff6666-0000-4000-8000-000000000001')
        ])
        # 403, comme partout ailleurs sur la plateforme : c'est ce que rend
        # `HasActiveSubscription`, et `SaleViewSet` répond la même chose. Un 402
        # serait plus parlant, mais s'en écarter ici ferait de cet endpoint le
        # seul à répondre autrement.
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('abonnement', str(resp.data).lower())
        self.assertEqual(Sale.objects.count(), 0)

    def test_the_web_path_is_refused_the_same_way(self):
        """La parité vaut aussi pour les refus."""
        self._expirer_abonnement()
        web = self.client.post(
            '/api/v1/sales/', self._cart(), format='json', **self._headers()
        )
        mobile = self._send([
            self._op('sale.create', self._cart(), 'ffff6666-0000-4000-8000-000000000002')
        ])
        self.assertEqual(web.status_code, mobile.status_code)
