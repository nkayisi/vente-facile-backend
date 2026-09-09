"""
Tests de référence pour le cycle de session d'inventaire physique.

Ces tests épinglent le comportement **actuel** de ``InventorySessionViewSet``
(``start`` → ``count`` → ``submit`` → ``validate``) avant l'introduction du
comptage « X paquets + Y pièces ».

Point le plus structurant à ne pas casser : ``validate`` écrit
``stock.quantity = count.quantity_counted`` en **valeur absolue**, pas en delta.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    InventoryCount, InventorySession, Stock, StockMovement,
)
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users


class _InventorySessionSetup(APITestCase):
    """Organisation + un produit disposant de 10 unités en stock."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.product = Product.objects.create(
            organization=self.org,
            name='Savon de Marseille',
            sku='SAV-01',
            cost_price=Decimal('500.00'),
            selling_price=Decimal('800.00'),
            track_inventory=True,
            is_active=True,
        )
        self.stock = Stock.objects.create(
            organization=self.org,
            product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('10.000'),
            avg_cost=Decimal('500.00'),
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _create_session(self):
        response = self.client.post(
            '/api/v1/inventory-sessions/',
            {'warehouse': str(self.warehouse.id), 'scope_type': 'full'},
            format='json',
            **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # `InventorySessionCreateSerializer` n'expose pas `id` dans ses champs :
        # la réponse de création ne le contient pas. On relit la session créée.
        return str(
            InventorySession.objects.filter(
                organization=self.org, warehouse=self.warehouse
            ).latest('created_at').id
        )

    def _run_until_review(self, counted_quantity):
        """Crée une session, compte `counted_quantity`, et la soumet."""
        session_id = self._create_session()

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/start/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/count/',
            {'counts': [{'id': str(count.id), 'quantity_counted': counted_quantity}]},
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/submit/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return session_id

    def _validate(self, session_id):
        response = self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/validate/',
            format='json', **self._headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.stock.refresh_from_db()
        return response


class InventorySessionCycleBaselineTests(_InventorySessionSetup):

    def test_start_prend_un_snapshot_du_stock(self):
        session_id = self._create_session()
        self.client.post(
            f'/api/v1/inventory-sessions/{session_id}/start/',
            format='json', **self._headers,
        )

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        self.assertEqual(count.quantity_expected, Decimal('10.000'))
        self.assertEqual(count.unit_cost, Decimal('500.00'))
        self.assertFalse(count.is_counted)

    def test_count_calcule_l_ecart(self):
        session_id = self._run_until_review(counted_quantity=8)

        count = InventoryCount.objects.get(session_id=session_id, product=self.product)
        self.assertTrue(count.is_counted)
        self.assertEqual(count.quantity_counted, Decimal('8.000'))
        self.assertEqual(count.quantity_difference, Decimal('-2.000'))
        self.assertEqual(count.difference_value, Decimal('-1000.00'))
        self.assertEqual(count.counted_by, self.owner)

    def test_validate_ecrase_la_quantite_en_absolu(self):
        session_id = self._run_until_review(counted_quantity=8)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('8.000'))
        self.assertIsNotNone(self.stock.last_counted_at)

    def test_validate_cree_un_mouvement_d_ajustement(self):
        session_id = self._run_until_review(counted_quantity=8)
        self._validate(session_id)

        movement = StockMovement.objects.get(
            reference_type='inventory_session',
            reference_id=session_id,
        )
        self.assertEqual(movement.movement_type, 'adjustment_out')
        self.assertEqual(movement.quantity, Decimal('-2.000'))
        self.assertEqual(movement.quantity_before, Decimal('10.000'))
        self.assertEqual(movement.quantity_after, Decimal('8.000'))

    def test_ecart_positif_produit_un_ajustement_entrant(self):
        session_id = self._run_until_review(counted_quantity=14)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('14.000'))
        movement = StockMovement.objects.get(reference_type='inventory_session')
        self.assertEqual(movement.movement_type, 'adjustment_in')
        self.assertEqual(movement.quantity, Decimal('4.000'))

    def test_ecart_nul_ne_cree_aucun_mouvement(self):
        session_id = self._run_until_review(counted_quantity=10)
        self._validate(session_id)

        self.assertEqual(self.stock.quantity, Decimal('10.000'))
        self.assertFalse(
            StockMovement.objects.filter(reference_type='inventory_session').exists()
        )

    def test_validate_deverrouille_le_stock(self):
        session_id = self._run_until_review(counted_quantity=8)
        session = InventorySession.objects.get(id=session_id)
        self.assertTrue(session.is_stock_locked)

        self._validate(session_id)
        session.refresh_from_db()
        self.assertEqual(session.status, 'validated')
        self.assertFalse(session.is_stock_locked)
        self.assertEqual(session.validated_by, self.owner)


class TransitionsRepondentDuJSONTests(APITestCase):
    """
    Les cinq transitions d'une session rendent une RÉPONSE, jamais un objet.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ `cancel` RÉPONDAIT 500, ET L'ANNULATION AVAIT POURTANT LIEU.            │
    │                                                                          │
    │ `cancel_inventory_session` rend l'`InventorySession` ; `_transition`     │
    │ l'appelait SANS `serialiser=True`, contrairement à ses quatre sœurs.     │
    │ L'objet partait tel quel au rendu JSON : « Object of type                │
    │ InventorySession is not JSON serializable », donc 500 - APRÈS que le     │
    │ service a écrit, puisque le rendu est la dernière étape.                 │
    │                                                                          │
    │ Le gérant lit une erreur, croit son inventaire toujours en cours, et le  │
    │ stock est pourtant déverrouillé. Il peut relancer, ou attendre en vain.  │
    │ C'est le défaut exact déjà corrigé sur le chemin du JOURNAL, resté       │
    │ ouvert sur celui de la VUE.                                              │
    └──────────────────────────────────────────────────────────────────────────┘

    Le balayage porte sur les CINQ, pas sur `cancel` seule : c'est un oubli
    d'un argument, et il se refera sur la prochaine transition ajoutée.
    """

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.piece = Unit.objects.create(
            organization=self.org, name='PIECE', symbol='pc'
        )
        self.product = Product.objects.create(
            organization=self.org, name='Savon', slug='savon-tr', sku='SAV-TR',
            unit=self.piece,
            cost_price=Decimal('400.00'), selling_price=Decimal('600.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product,
            warehouse=self.warehouse,
            quantity=Decimal('40.000'), avg_cost=Decimal('400.00'),
        )
        self.client.force_authenticate(user=self.owner)

    @property
    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _session(self):
        return InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse,
            reference=f'INV-TR-{InventorySession.objects.count():04d}',
            scope_type='full', status='draft',
        )

    def _poste(self, session, transition, corps=None):
        return self.client.post(
            f'/api/v1/inventory-sessions/{session.id}/{transition}/',
            corps or {}, format='json', **self._headers,
        )

    def test_annuler_une_session_repond_du_JSON(self):
        session = self._session()
        self._poste(session, 'start')

        reponse = self._poste(session, 'cancel', {'reason': 'erreur de saisie'})

        self.assertEqual(
            reponse.status_code, status.HTTP_200_OK,
            "L'annulation a répondu une erreur alors qu'elle a bien eu lieu : "
            "le service rend un objet que le rendu JSON ne sait pas écrire.",
        )
        session.refresh_from_db()
        self.assertEqual(session.status, 'cancelled')

    def test_les_CINQ_transitions_rendent_du_JSON(self):
        """
        Le parcours COMPLET, transition par transition.

        ⚠ Un refus métier (400) rend parfaitement du JSON : c'est un contrat,
        pas une panne. Une première version de ce test attendait 200 partout et
        échouait sur `count` d'une liste vide, `submit` d'une feuille non
        comptée et `validate` d'une session non soumise - trois refus JUSTES.
        Ce qu'on éprouve ici, c'est que la réponse s'ÉCRIT : un objet du modèle
        rendu tel quel lève au moment du rendu, donc après que le service a
        écrit en base.
        """
        session = self._session()

        self.assertEqual(self._poste(session, 'start').status_code,
                         status.HTTP_200_OK)

        ligne = InventoryCount.objects.get(session=session)
        compte = self._poste(session, 'count', {
            'counts': [{'id': str(ligne.id), 'quantity_counted': 38}],
        })
        self.assertEqual(compte.status_code, status.HTTP_200_OK, compte.data)

        self.assertEqual(self._poste(session, 'submit').status_code,
                         status.HTTP_200_OK)
        self.assertEqual(self._poste(session, 'validate').status_code,
                         status.HTTP_200_OK)

        # `cancel` a son propre parcours : une session validée ne s'annule plus.
        autre = self._session()
        self._poste(autre, 'start')
        self.assertEqual(
            self._poste(autre, 'cancel', {'reason': 'x'}).status_code,
            status.HTTP_200_OK,
        )
