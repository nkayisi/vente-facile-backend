"""
`allocate_daily_reference` : les séries quotidiennes du serveur.

Trois défauts des anciens générateurs, un test chacun : le jour de GREENWICH, le
rang pris dans un tri alphabétique (qu'une référence d'appareil relevait), et
la reprise d'un numéro déjà émis le matin même par l'ancien code.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from apps.core.numbering import allocate_daily_reference
from apps.core.utils import ReferenceGenerator
from apps.sales.models import Sale
from apps.sales.tests._helpers import make_org_with_users

JOUR = date(2026, 11, 13)


def _au(jour):
    """Fige le jour LOCAL ; l'horloge UTC, elle, n'est plus jamais lue."""
    return mock.patch('apps.core.numbering.timezone.localdate', return_value=jour)


class ReferenceDuJourTests(TestCase):
    def setUp(self):
        self.d = make_org_with_users()
        self.org = self.d['org']

    def _vente(self, reference):
        return Sale.objects.create(
            organization=self.org, warehouse=self.d['warehouse'],
            sold_by=self.d['cashier_a'], reference=reference,
            status=Sale.Status.COMPLETED, subtotal=Decimal('1'), total=Decimal('1'),
            currency='CDF', exchange_rate=Decimal('1'),
        )

    def test_le_prefixe_porte_le_jour_du_marchand(self):
        with _au(JOUR):
            self.assertEqual(
                ReferenceGenerator.generate_inventory_reference(self.org),
                'INV-20261113-0001',
            )

    def test_les_numeros_se_suivent_sans_se_repeter(self):
        with _au(JOUR):
            premier = ReferenceGenerator.generate_transfer_reference(self.org)
            second = ReferenceGenerator.generate_transfer_reference(self.org)
        self.assertEqual((premier, second), ('TR-20261113-0001', 'TR-20261113-0002'))

    def test_chaque_jour_repart_de_un(self):
        with _au(JOUR):
            ReferenceGenerator.generate_cash_movement_reference(self.org)
        with _au(date(2026, 11, 14)):
            self.assertEqual(
                ReferenceGenerator.generate_cash_movement_reference(self.org),
                'MC-20261114-0001',
            )

    def test_les_series_sont_independantes(self):
        with _au(JOUR):
            ReferenceGenerator.generate_adjustment_reference(self.org)
            self.assertEqual(
                ReferenceGenerator.generate_batch_number(self.org), 'LOT-20261113-0001',
            )

    def test_le_premier_numero_reprend_le_rang_deja_emis(self):
        """Un déploiement en pleine journée ne réémet pas `…-0001`."""
        self._vente('ZZ-20261113-0007')
        with _au(JOUR):
            self.assertEqual(
                allocate_daily_reference(self.org, 'ZZ', Sale), 'ZZ-20261113-0008',
            )

    def test_une_reference_d_appareil_ne_releve_pas_la_serie(self):
        """`K` passe au-dessus de `0` dans un tri alphabétique : plus de trou."""
        self._vente('ZZ-20261113-0002')
        self._vente('ZZ-20261113-K7QM-0042')
        with _au(JOUR):
            self.assertEqual(
                allocate_daily_reference(self.org, 'ZZ', Sale), 'ZZ-20261113-0003',
            )

    def test_une_piece_supprimee_garde_son_numero(self):
        self._vente('ZZ-20261113-0004').soft_delete()
        with _au(JOUR):
            self.assertEqual(
                allocate_daily_reference(self.org, 'ZZ', Sale), 'ZZ-20261113-0005',
            )

    def test_une_autre_organisation_a_sa_propre_serie(self):
        from apps.organizations.models import Organization
        autre = Organization.objects.create(name='Autre', slug='autre-serie')
        with _au(JOUR):
            ReferenceGenerator.generate_inventory_reference(self.org)
            self.assertEqual(
                ReferenceGenerator.generate_inventory_reference(autre), 'INV-20261113-0001',
            )
