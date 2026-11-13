"""
`retoucher_lignes_figees` : une ligne figée redescend, sans qu'une valeur change.
"""
from datetime import datetime, timezone as tz
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.organizations.models import Organization
from apps.products.models import Product
from apps.sales.models import Payment, Sale
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users
from apps.sync.management.commands.retoucher_lignes_figees import CIBLES
from apps.sync.pull import PULL_TABLES_BY_NAME

D = Decimal
ANCIEN = datetime(2026, 1, 1, tzinfo=tz.utc)


class RetoucherLignesFigeesTests(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.org, self.caissier = ctx['org'], ctx['cashier_a']
        self.produit = Product.objects.create(
            organization=self.org, name='Savon', sku='SAV', slug='sav',
            cost_price=D('10'), selling_price=D('15'),
        )
        moyen = make_cash_payment_method(self.org)

        def vente(ref, numero_de_recu):
            v = Sale.objects.create(
                organization=self.org, warehouse=ctx['warehouse'], sold_by=self.caissier,
                reference=ref, status=Sale.Status.COMPLETED, subtotal=D('15'),
                total=D('15'), amount_paid=D('15'), currency='CDF', exchange_rate=D('1'),
            )
            Payment.objects.create(
                sale=v, organization=self.org, payment_method=moyen,
                amount=D('15'), tendered_amount=D('15'), receipt_number=numero_de_recu,
            )
            return v

        self.vente_recu = vente('VT-1', 'RGL-20260101-0001')
        self.vente_sans = vente('VT-2', '')

        # Toutes les lignes en cause, figées comme un `update()` nu les laissait.
        for modele in (Product, Sale, Organization):
            modele._default_manager.update(updated_at=ANCIEN)
        self.prix = self.produit.selling_price

    def _lancer(self, *args):
        sortie = StringIO()
        call_command('retoucher_lignes_figees', *args, stdout=sortie)
        return sortie.getvalue()

    def _horodatage(self, objet):
        return type(objet)._default_manager.get(pk=objet.pk).updated_at

    def test_les_cibles_sont_toutes_des_tables_de_tirage_de_tete(self):
        """
        Une cible hors du registre, ou un ENFANT (qui descend dans son parent),
        serait touchée pour rien : le terminal ne la relirait pas.
        """
        enfants = {
            enfant.table for t in PULL_TABLES_BY_NAME.values() for enfant in t.children
        }
        for cible in CIBLES:
            with self.subTest(table=cible.table):
                self.assertIn(cible.table, PULL_TABLES_BY_NAME)
                self.assertNotIn(cible.table, enfants)

    def test_la_simulation_n_ecrit_rien(self):
        sortie = self._lancer()
        self.assertEqual(self._horodatage(self.produit), ANCIEN)
        self.assertIn("rien n'a été écrit", sortie)

    def test_le_catalogue_est_touche_sans_que_le_prix_change(self):
        self._lancer('--appliquer')
        self.assertGreater(self._horodatage(self.produit), ANCIEN)
        self.produit.refresh_from_db()
        self.assertEqual(self.produit.selling_price, self.prix)

    def test_l_etablissement_est_touche(self):
        self._lancer('--appliquer')
        self.assertGreater(self._horodatage(self.org), ANCIEN)

    def test_seule_une_vente_a_numero_de_recu_est_touchee(self):
        """Toucher toutes les ventes ferait retélécharger tout l'historique."""
        self._lancer('--appliquer')
        self.assertGreater(self._horodatage(self.vente_recu), ANCIEN)
        self.assertEqual(self._horodatage(self.vente_sans), ANCIEN)

    def test_tables_restreint_aux_cibles_choisies(self):
        self._lancer('--appliquer', '--tables', 'products')
        self.assertGreater(self._horodatage(self.produit), ANCIEN)
        self.assertEqual(self._horodatage(self.vente_recu), ANCIEN)

    def test_une_table_hors_des_cibles_est_refusee_en_les_nommant(self):
        with self.assertRaisesMessage(CommandError, 'Cibles possibles'):
            self._lancer('--tables', 'payments')

    def test_une_autre_organisation_n_est_pas_touchee(self):
        autre = Organization.objects.create(name='Autre', slug='autre')
        Organization.objects.filter(pk=autre.pk).update(updated_at=ANCIEN)
        self._lancer('--appliquer', '--organization', str(self.org.pk))
        self.assertEqual(self._horodatage(autre), ANCIEN)
        self.assertGreater(self._horodatage(self.org), ANCIEN)

    def test_les_deux_options_s_excluent(self):
        with self.assertRaises(CommandError):
            self._lancer('--appliquer', '--dry-run')
