"""
Une livraison ne se range pas dans un rayon en cours de comptage, et un retour
fournisseur ne l'en sort pas.

Deux des onze écritures de stock que le verrou d'inventaire ignorait : voir
`apps/inventory/verrous.py`.
"""
from datetime import date
from decimal import Decimal

from apps.contacts.supplier_debt import get_balance
from apps.inventory.models import InventorySession, Stock
from apps.inventory.verrous import InventaireEnCours
from apps.purchases.models import PurchaseReturn, PurchaseReturnItem
from apps.purchases.services import approuver_retour, completer_reception
from apps.purchases.tests.test_dette_fournisseur import _Socle


class _AvecComptage(_Socle):
    def _compter(self):
        session = InventorySession.objects.create(
            organization=self.org, warehouse=self.warehouse, reference='INV-ACH-0001',
            name='Inventaire huile', scope_type=InventorySession.ScopeType.PRODUCT,
            status=InventorySession.Status.IN_PROGRESS, is_stock_locked=True,
        )
        session.products.add(self.product)
        return session


class ReceptionTests(_AvecComptage):
    def test_une_reception_n_entre_pas_sous_un_comptage(self):
        self._compter()
        grn = self._reception(Decimal('5.000'))
        with self.assertRaises(InventaireEnCours):
            completer_reception(grn, self.owner)
        grn.refresh_from_db()
        self.assertEqual(grn.status, 'draft')
        self.assertFalse(Stock.objects.filter(product=self.product).exists())
        # Rien n'est reçu, donc rien n'est dû : la dette ne s'inscrit pas non plus.
        self.assertEqual(get_balance(self.supplier, 'CDF'), Decimal('0'))

    def test_sans_comptage_la_reception_passe(self):
        # Contrôle.
        completer_reception(self._reception(Decimal('5.000')), self.owner)
        self.assertEqual(
            Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal('5.000'),
        )


class RetourFournisseurTests(_AvecComptage):
    def setUp(self):
        super().setUp()
        completer_reception(self._reception(Decimal('5.000')), self.owner)
        self.retour = PurchaseReturn.objects.create(
            organization=self.org, reference='RTF-ACH-0001',
            supplier=self.supplier, purchase_order=self.po,
            warehouse=self.warehouse, status='draft',
            reason='Bidons percés', return_date=date.today(),
            total_amount=Decimal('16000.00'), created_by=self.owner,
        )
        PurchaseReturnItem.objects.create(
            organization=self.org, purchase_return=self.retour, product=self.product,
            quantity=Decimal('2.000'), unit_cost=Decimal('8000.00'),
            total=Decimal('16000.00'),
        )

    def test_un_retour_ne_sort_rien_sous_un_comptage(self):
        self._compter()
        with self.assertRaises(InventaireEnCours):
            approuver_retour(self.retour, self.owner)
        self.retour.refresh_from_db()
        self.assertEqual(self.retour.status, 'draft')
        self.assertEqual(
            Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal('5.000'),
        )

    def test_sans_comptage_le_retour_s_approuve(self):
        approuver_retour(self.retour, self.owner)
        self.assertEqual(
            Stock.objects.get(product=self.product, warehouse=self.warehouse).quantity,
            Decimal('3.000'),
        )
