"""
Le coût de la LISTE des ventes ne dépend pas du nombre de ventes (lot L15).

Même motif que `test_serialization_query_budget.py`, qui tient le DÉTAIL : on
mesure l'INVARIANT - deux ventes, puis douze, chacune avec son client - et non
un nombre absolu, qu'on relâcherait au premier champ ajouté.

Rôle GÉRANT : `_scope_sales` sort en amont pour un propriétaire.
"""
from decimal import Decimal

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.sales.models import Payment, PaymentMethod, Sale
from apps.sales.tests._helpers import make_org_with_users


class BudgetDeLaListeDesVentesTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.client.force_authenticate(user=self.manager)
        self.especes = PaymentMethod.objects.filter(organization=self.org).first() or PaymentMethod.objects.create(
            organization=self.org, name='Espèces', code='CASH', method_type='cash',
        )
        self.rang = 0

    def _ventes(self, n):
        for _ in range(n):
            self.rang += 1
            client = Customer.objects.create(
                organization=self.org, name=f'Client {self.rang}', code=f'CL{self.rang}',
                phone=f'09900{self.rang:05d}',
            )
            vente = Sale.objects.create(
                organization=self.org, reference=f'VTE-L-{self.rang:03d}',
                warehouse=self.warehouse, register=self.register, customer=client,
                status='completed', currency='CDF',
                subtotal=Decimal('10'), total=Decimal('10'), amount_paid=Decimal('10'),
                sold_by=self.manager, sale_date=timezone.now(),
            )
            Payment.objects.create(
                organization=self.org, sale=vente, payment_method=self.especes,
                amount=Decimal('10'), currency='CDF',
            )

    def _compte(self) -> int:
        with CaptureQueriesContext(connection) as capture:
            r = self.client.get(
                '/api/v1/sales/', {'page_size': 50}, HTTP_X_ORGANIZATION_ID=str(self.org.id),
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        return len(capture)

    @override_settings(DEBUG=False)
    def test_la_liste_ne_paie_PAS_par_vente(self):
        self._ventes(2)
        # Amorce les caches d'identité et d'appartenance.
        self._compte()
        peu = self._compte()
        self._ventes(10)
        beaucoup = self._compte()
        self.assertEqual(
            peu, beaucoup,
            f"La liste des ventes coûte des requêtes PAR VENTE ({peu} pour 2, "
            f"{beaucoup} pour 12) : il manque un `select_related` ou un "
            "`prefetch_related` dans `SaleViewSet.get_queryset`.",
        )
