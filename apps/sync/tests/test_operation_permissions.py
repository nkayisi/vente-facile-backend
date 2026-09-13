"""
Le DROIT de poser un acte, vérifié par OPÉRATION.

┌──────────────────────────────────────────────────────────────────────────────┐
│ AUCUNE PERMISSION D'ACTION N'ÉTAIT APPLIQUÉE SUR LE CHEMIN MOBILE.          │
│                                                                              │
│ `SyncOperationsView.permission_classes` porte `IsAuthenticated`,             │
│ `IsTenantMember` et `HasActiveSubscription` - pas `HasPermission`. Toutes    │
│ les `action_permissions` du back-office étaient donc contournables depuis un │
│ terminal : un caissier pouvait approuver un ajustement de stock ou valider   │
│ un inventaire, ce que le back-office lui refuse.                             │
│                                                                              │
│ La correction ne pouvait PAS consister à ajouter `HasPermission` aux         │
│ `permission_classes` : c'est une permission de VUE, un échec rendrait 403    │
│ pour tout l'envoi, et un caissier avec deux cents ventes en file les         │
│ perdrait toutes à cause d'une seule opération d'inventaire. C'est le défaut  │
│ exact de l'ancienne file, que §5.5 interdit. Le contrôle est donc PAR        │
│ OPÉRATION, et `test_un_lot_mixte...` est le test qui attrape la solution     │
│ naïve.                                                                       │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import InventorySession, Stock, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.models import RegisterSession, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.sync.models import SyncOperation
from apps.sync.operations import HANDLERS, HANDLER_PERMISSIONS

OPERATIONS = '/api/v1/sync/operations/'


class ContratDesPermissionsTests(APITestCase):
    """Contrôles statiques : ils ne touchent ni la base ni le réseau."""

    def test_tout_acte_declare_une_permission(self):
        """
        Un acte sans permission déclarée est REFUSÉ, jamais ouvert à tous.

        Ce test le rend visible au développeur plutôt qu'au marchand.
        """
        sans = sorted(set(HANDLERS) - set(HANDLER_PERMISSIONS))
        self.assertEqual(sans, [], f"Actes sans permission déclarée : {sans}")
        self.assertEqual(sorted(HANDLERS), sorted(HANDLER_PERMISSIONS))

    def test_la_permission_est_CELLE_DE_LA_VUE(self):
        """
        Le miroir, acte par acte.

        C'est ce croisement qui rend la parité structurelle : changer la
        permission d'un côté fait échouer le test en nommant l'autre.
        """
        from apps.cashbook.views import (
            CashMovementViewSet, ExpenseCategoryViewSet, ExpenseViewSet,
            IncomeCategoryViewSet,
        )
        from apps.contacts.views import CustomerViewSet
        from apps.inventory.views import (
            InventorySessionViewSet, StockAdjustmentViewSet, StockMovementViewSet,
            StockTransferViewSet, StockViewSet,
        )
        from apps.products.views import (
            BrandViewSet, CategoryViewSet, ProductViewSet, UnitViewSet,
        )
        from apps.sales.views import (
            QuotationViewSet, RegisterSessionViewSet, SaleReturnViewSet, SaleViewSet,
        )

        #: kind -> (ViewSet, action de la vue qui rejoue le même acte)
        contrat = {
            'sale.create': (SaleViewSet, 'create'),
            'sale.add_payment': (SaleViewSet, 'add_payment'),
            'sale.cancel': (SaleViewSet, 'cancel'),
            'register_session.open': (RegisterSessionViewSet, 'open'),
            'register_session.close': (RegisterSessionViewSet, 'close'),
            'customer.create': (CustomerViewSet, 'create'),
            'customer.record_payment': (CustomerViewSet, 'record_payment'),
            'customer.adjust_balance': (CustomerViewSet, 'adjust_balance'),
            'stock_movement.create': (StockMovementViewSet, 'create'),
            'stock.unpack': (StockViewSet, 'unpack'),
            'stock_transfer.create': (StockTransferViewSet, 'create'),
            'stock_transfer.approve': (StockTransferViewSet, 'approve'),
            'stock_transfer.ship': (StockTransferViewSet, 'ship'),
            'stock_transfer.receive': (StockTransferViewSet, 'receive'),
            'stock_transfer.cancel': (StockTransferViewSet, 'cancel'),
            'stock_adjustment.create': (StockAdjustmentViewSet, 'create'),
            'stock_adjustment.approve': (StockAdjustmentViewSet, 'approve'),
            'stock_adjustment.reject': (StockAdjustmentViewSet, 'reject'),
            'sale_return.create': (SaleReturnViewSet, 'create'),
            'sale_return.approve': (SaleReturnViewSet, 'approve'),
            'sale_return.reject': (SaleReturnViewSet, 'reject'),
            'quotation.create': (QuotationViewSet, 'create'),
            'quotation.convert': (QuotationViewSet, 'convert'),
            'inventory_session.create': (InventorySessionViewSet, 'create'),
            'inventory_session.start': (InventorySessionViewSet, 'start'),
            'inventory_session.count': (InventorySessionViewSet, 'count'),
            'inventory_session.submit': (InventorySessionViewSet, 'submit'),
            'inventory_session.validate': (InventorySessionViewSet, 'validate'),
            'inventory_session.cancel': (InventorySessionViewSet, 'cancel'),
            'product.create': (ProductViewSet, 'create'),
            'category.create': (CategoryViewSet, 'create'),
            'brand.create': (BrandViewSet, 'create'),
            'unit.create': (UnitViewSet, 'create'),
            # `partial_update` et non `update` : les deux portent le même code
            # aujourd'hui, mais l'acte est de forme PATCH (`partial=True`) et le
            # contrat doit dire lequel il rejoue.
            'category.update': (CategoryViewSet, 'partial_update'),
            'brand.update': (BrandViewSet, 'partial_update'),
            'unit.update': (UnitViewSet, 'partial_update'),
            'expense.create': (ExpenseViewSet, 'create'),
            'expense.submit': (ExpenseViewSet, 'submit'),
            'expense.approve': (ExpenseViewSet, 'approve'),
            'expense.reject': (ExpenseViewSet, 'reject'),
            'expense.pay': (ExpenseViewSet, 'pay'),
            'expense.cancel': (ExpenseViewSet, 'cancel'),
            'cash_movement.create': (CashMovementViewSet, 'create'),
            'cash_movement.cancel': (CashMovementViewSet, 'cancel'),
            'income_category.create': (IncomeCategoryViewSet, 'create'),
            'expense_category.create': (ExpenseCategoryViewSet, 'create'),
            # `partial_update`, comme le back-office : les deux vues y
            # retiennent leur `…CreateSerializer`, et c'est ce que le journal
            # rejoue. Voir `test_cashbook_category_update.py`.
            'income_category.update': (IncomeCategoryViewSet, 'partial_update'),
            'expense_category.update': (ExpenseCategoryViewSet, 'partial_update'),
        }
        self.assertEqual(
            sorted(contrat), sorted(HANDLERS),
            "Le contrat et le registre des actes ont divergé.",
        )
        ecarts = []
        for kind, (vue, action) in contrat.items():
            attendu = vue.action_permissions.get(action)
            obtenu = HANDLER_PERMISSIONS[kind]
            if attendu != obtenu:
                ecarts.append(f"{kind} : journal={obtenu!r} vue={attendu!r}")
        self.assertEqual(ecarts, [], "\n".join(ecarts))


class _RefusBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.moyen = make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.session_caisse = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.cashier_a)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _op(self, kind, payload, op_id, seq=1):
        return {
            'operation_id': op_id, 'kind': kind, 'seq': seq,
            'depends_on': [], 'occurred_at': '2026-08-31T09:00:00Z',
            'payload': payload,
        }

    def _send(self, operations):
        return self.client.post(
            OPERATIONS, {'operations': operations}, format='json', **self._headers()
        )

    def _panier(self):
        return {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'items': [{
                'product': str(self.produit.id),
                'quantity': '1', 'unit_price': '2000.00',
            }],
            'payments': [{
                'payment_method': str(self.moyen.id),
                'tendered_amount': '2000.00',
            }],
        }


class RefusParPermissionTests(_RefusBaseTest):
    def _inventaire_en_revision(self):
        return InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse,
            reference='INV-TEST-0001', scope_type='full', status='review',
        )

    def test_un_caissier_ne_peut_pas_valider_un_inventaire(self):
        """
        Le trou refermé, nommé.

        Le back-office refuse `inventory.validate` à un caissier ; le journal
        l'acceptait. Le verdict est `blocked` et non `rejected` : l'opération
        est conservée et repassera si la permission est accordée.
        """
        session = self._inventaire_en_revision()
        reponse = self._send([self._op(
            'inventory_session.validate', {'session': str(session.id)},
            'b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1',
        )])
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], SyncOperation.Verdict.BLOCKED, verdict)
        self.assertIn('inventory.validate', verdict['errors']['detail'])
        session.refresh_from_db()
        self.assertEqual(session.status, 'review', "L'inventaire a été validé malgré le refus.")

    def test_un_lot_mixte_ne_perd_QUE_l_operation_bloquee(self):
        """
        ┌──────────────────────────────────────────────────────────────────────┐
        │ LE TEST QUI ATTRAPE LA SOLUTION NAÏVE.                              │
        │                                                                      │
        │ Ajouter `HasPermission` aux `permission_classes` rendrait 403 pour   │
        │ TOUT l'envoi : les deux ventes de ce lot seraient perdues à cause de │
        │ l'opération d'inventaire. Un caissier qui a vendu toute la journée   │
        │ hors ligne perdrait sa journée.                                      │
        └──────────────────────────────────────────────────────────────────────┘
        """
        session = self._inventaire_en_revision()
        reponse = self._send([
            self._op('sale.create', self._panier(), 'c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1', seq=1),
            self._op(
                'inventory_session.validate', {'session': str(session.id)},
                'c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2', seq=2,
            ),
            self._op('sale.create', self._panier(), 'c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3', seq=3),
        ])
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdicts = {r['operation_id']: r['verdict'] for r in reponse.data['results']}
        self.assertEqual(verdicts['c1c1c1c1-c1c1-4c1c-8c1c-c1c1c1c1c1c1'], SyncOperation.Verdict.APPLIED)
        self.assertEqual(verdicts['c2c2c2c2-c2c2-4c2c-8c2c-c2c2c2c2c2c2'], SyncOperation.Verdict.BLOCKED)
        self.assertEqual(verdicts['c3c3c3c3-c3c3-4c3c-8c3c-c3c3c3c3c3c3'], SyncOperation.Verdict.APPLIED)
        self.assertEqual(Sale.objects.count(), 2, "Les ventes du lot ont été emportées.")

    def test_une_operation_bloquee_REPASSE_une_fois_le_droit_accorde(self):
        """
        `blocked` n'est pas `is_settled` : c'est ce qui rend le déblocage possible.

        Sans cette propriété, accorder la permission ne servirait à rien et
        l'opération resterait morte dans le journal du terminal.
        """
        session = self._inventaire_en_revision()
        op = self._op(
            'inventory_session.validate', {'session': str(session.id)},
            'd1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1',
        )
        self.assertEqual(
            self._send([op]).data['results'][0]['verdict'], SyncOperation.Verdict.BLOCKED,
        )

        adhesion = OrganizationMembership.objects.get(user=self.cashier_a, organization=self.org)
        adhesion.extra_permissions = ['inventory.validate']
        adhesion.save(update_fields=['extra_permissions'])

        verdict = self._send([op]).data['results'][0]
        self.assertEqual(verdict['verdict'], SyncOperation.Verdict.APPLIED, verdict.get('errors'))
        session.refresh_from_db()
        self.assertEqual(session.status, 'validated')

    def test_un_caissier_vend_toujours(self):
        """Le garde-fou du garde-fou : le contrôle ne doit pas tout refuser."""
        reponse = self._send([self._op(
            'sale.create', self._panier(), 'e1e1e1e1-e1e1-4e1e-8e1e-e1e1e1e1e1e1',
        )])
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], SyncOperation.Verdict.APPLIED, verdict.get('errors'))
