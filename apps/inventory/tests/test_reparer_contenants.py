"""
`reparer_contenants_fractionnaires` : un mouvement retrouve des contenants entiers.

Le cas réel : un casier de 12 bouteilles est passé à 24 pendant que la
marchandise était en route. La réception a découpé 20 bouteilles au facteur du
PRODUIT (24, donc tout en vrac... ou presque) puis divisé par le facteur FIGÉ
(12) : 1,667 casier. Le partage juste, au facteur figé, est 1 casier + 8.
"""
from datetime import datetime, timezone as tz
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.inventory.models import StockMovement
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users

D = Decimal
ANCIEN = datetime(2026, 1, 1, tzinfo=tz.utc)


class ReparerContenantsFractionnairesTests(TestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.org, self.warehouse = ctx['org'], ctx['warehouse']
        bouteille = Unit.objects.create(organization=self.org, name='bouteille', symbol='btl')
        casier = Unit.objects.create(organization=self.org, name='casier', symbol='cs')
        self.produit = Product.objects.create(
            organization=self.org, name='Eau 50cl', slug='eau', sku='EAU',
            unit=bouteille, packaging_unit=casier,
            selling_mode=Product.SellingMode.WHOLESALE_AND_RETAIL, units_per_package=24,
            cost_price=D('400.00'), selling_price=D('600.00'),
        )

    def _mouvement(self, quantite, paquets, vrac, facteur, type_=StockMovement.MovementType.TRANSFER_IN):
        m = StockMovement.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            movement_type=type_, quantity=D(quantite),
            quantity_before=D('0.000'), quantity_after=D(quantite),
            input_package_quantity=D(paquets), input_loose_quantity=D(vrac),
            packaging_factor=facteur,
        )
        StockMovement.objects.filter(pk=m.pk).update(updated_at=ANCIEN)
        m.refresh_from_db()
        return m

    def _lancer(self, *args):
        sortie = StringIO()
        call_command('reparer_contenants_fractionnaires', *args, stdout=sortie)
        return sortie.getvalue()

    def test_la_simulation_n_ecrit_rien(self):
        m = self._mouvement('20.000', '1.667', '0.000', 12)
        sortie = self._lancer()
        m.refresh_from_db()
        self.assertEqual(m.input_package_quantity, D('1.667'))
        self.assertEqual(m.updated_at, ANCIEN)
        self.assertIn("rien n'a été écrit", sortie)

    def test_le_partage_se_refait_au_facteur_fige(self):
        m = self._mouvement('20.000', '1.667', '0.000', 12)
        self._lancer('--appliquer')
        m.refresh_from_db()
        self.assertEqual((m.input_package_quantity, m.input_loose_quantity), (D('1.000'), D('8.000')))
        # La quantité, elle, ne bouge pas : seule la lecture du mouvement change.
        self.assertEqual(m.quantity, D('20.000'))

    def test_une_sortie_garde_son_signe_sur_la_quantite_seule(self):
        """Les compteurs saisis sont toujours positifs ; le signe vit sur `quantity`."""
        m = self._mouvement('-30.000', '2.500', '0.000', 12, StockMovement.MovementType.RETURN_OUT)
        self._lancer('--appliquer')
        m.refresh_from_db()
        self.assertEqual((m.input_package_quantity, m.input_loose_quantity), (D('2.000'), D('6.000')))
        self.assertEqual(m.quantity, D('-30.000'))

    def test_la_ligne_corrigee_redescend_aux_terminaux(self):
        m = self._mouvement('20.000', '1.667', '0.000', 12)
        self._lancer('--appliquer')
        m.refresh_from_db()
        self.assertGreater(m.updated_at, ANCIEN)

    def test_une_ligne_juste_n_est_pas_touchee(self):
        juste = self._mouvement('25.000', '2.000', '1.000', 12)
        self._lancer('--appliquer')
        juste.refresh_from_db()
        self.assertEqual(juste.updated_at, ANCIEN)

    def test_sans_facteur_fige_la_ligne_est_nommee_et_laissee(self):
        m = self._mouvement('20.000', '1.667', '0.000', None)
        sortie = self._lancer('--appliquer')
        m.refresh_from_db()
        self.assertEqual(m.input_package_quantity, D('1.667'))
        self.assertIn('NON RÉPARABLE', sortie)

    def test_une_seconde_passe_ne_trouve_plus_rien(self):
        self._mouvement('20.000', '1.667', '0.000', 12)
        self._lancer('--appliquer')
        self.assertIn('Aucun mouvement', self._lancer('--appliquer'))

    def test_les_deux_options_s_excluent(self):
        with self.assertRaises(CommandError):
            self._lancer('--appliquer', '--dry-run')
