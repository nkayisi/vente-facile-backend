"""
Le GROS et le DÉTAIL par le chemin du JOURNAL, sur les transferts et les
ajustements.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE CHEMIN N'AVAIT AUCUN TEST, ET LE TERMINAL VIENT DE COMMENCER À L'EMPRUNTER│
│                                                                              │
│ `StockTransferItemSerializer.validate` et `StockAdjustmentItemSerializer     │
│ .validate` recomposent la quantité depuis les deux compteurs depuis          │
│ toujours, et `apps/inventory/tests/test_transfer_packaging.py` comme         │
│ `test_adjustment_packaging.py` le vérifient - PAR LA VUE. Le journal passe   │
│ par les mêmes serializers, donc la parité est structurelle ; mais aucun test │
│ ne l'affirmait, et jusqu'à présent le terminal n'envoyait qu'une quantité    │
│ simple. Il envoie désormais « 2 casiers + 5 bouteilles » sur les deux actes. │
│                                                                              │
│ Ce fichier tient donc le CONTRAT DE TRANSPORT de ce chemin : ce que le corps │
│ de l'acte porte, et ce que le serveur en écrit.                              │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`.** C'est la règle de tout ce chantier, et
son oubli avait caché pendant un lot entier un refus qui frappait tous les
non-propriétaires : `accessible_warehouse_ids` sort en amont pour un
propriétaire, et la moitié du code de périmètre n'est alors pas exécutée.

Le rôle retenu est le GÉRANT, à qui `make_org_with_users` assigne l'entrepôt
principal. Le magasinier porterait bien `stock_transfers.create` et
`stock_adjustments.create`, mais pas `stock_adjustments.approve` : le test de
l'approbation serait alors bloqué pour une raison qui n'est pas la sienne, et
il passerait pour la mauvaise - défaut que ce dépôt a déjà payé.
"""
from decimal import Decimal
from uuid import uuid4

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    Stock, StockAdjustment, StockAdjustmentItem, StockTransfer,
    StockTransferItem, Warehouse,
)
from apps.products.models import Product, Unit
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _BasePackagee(APITestCase):
    """Un casier de 12 bouteilles, vendu en gros ET au détail."""

    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.autre_entrepot = Warehouse.objects.create(
            organization=self.org, name='Dépôt 2', code='D2',
        )
        # LES DEUX entrepôts sont assignés au gérant, et il en faut deux : le
        # handler contrôle la SOURCE ET LA DESTINATION, comme `perform_create`
        # - un magasinier ne doit pouvoir ni sortir du stock d'un dépôt qu'il
        # ne voit pas, ni s'en faire livrer. `make_org_with_users` n'assigne
        # que l'entrepôt principal.
        self.manager.memberships.get(organization=self.org).assigned_warehouses.add(
            self.autre_entrepot
        )
        self.bouteille = Unit.objects.create(
            organization=self.org, name='BOUTEILLE', symbol='btl',
        )
        self.casier = Unit.objects.create(
            organization=self.org, name='CASIER', symbol='cs',
        )
        self.produit = Product.objects.create(
            organization=self.org, name='Boisson', slug='boisson', sku='B1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True,
            selling_mode='wholesale_and_retail', units_per_package=12,
            unit=self.bouteille, packaging_unit=self.casier,
        )
        # Un rayon qui porte 5 casiers scellés et 7 bouteilles isolées : 67 au
        # total. Le partage est ENREGISTRÉ, il ne se déduit pas du total.
        self.stock = Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('67.000'), avg_cost=Decimal('1500.00'),
            package_quantity=Decimal('5.000'), loose_quantity=Decimal('7.000'),
        )
        # Le GÉRANT, pas le propriétaire : voir la docstring du module.
        self.client.force_authenticate(user=self.manager)

    def _send(self, kind, payload, op_id=None):
        # `operation_id` est un UUIDField : une chaîne libre fait répondre 500.
        op_id = op_id or str(uuid4())
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-09-06T09:00:00Z',
                'payload': payload,
            }]},
            format='json',
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], attendu, verdict.get('errors'))
        return verdict


class TransfertEnContenantsTests(_BasePackagee):
    def test_la_demande_en_contenants_est_recomposee_par_le_serveur(self):
        """« 2 casiers + 5 bouteilles » vaut 29, et c'est le SERVEUR qui le dit.

        Le terminal n'envoie PAS `quantity_requested` : le lui souffler ferait
        cohabiter deux vérités sur la même ligne, et la condition du serveur
        teste la véracité (`if packages or loose`), donc un « 0 + 0 » retomberait
        en silence sur le total du client.
        """
        self._verdict(self._send('stock_transfer.create', {
            'id': 'aaaaaaaa-0000-4000-8000-000000000001',
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{
                'product': str(self.produit.id),
                'package_quantity': '2',
                'loose_quantity': '5',
            }],
        }))

        ligne = StockTransferItem.objects.get()
        self.assertEqual(ligne.quantity_requested, Decimal('29.000'))
        self.assertEqual(ligne.package_quantity, Decimal('2.000'))
        self.assertEqual(ligne.loose_quantity, Decimal('5.000'))
        # Le facteur est FIGÉ sur la ligne : il a pu changer depuis, et
        # l'historique ne doit pas se réécrire avec lui.
        self.assertEqual(ligne.packaging_factor, 12)

    def test_un_seul_canal_suffit(self):
        self._verdict(self._send('stock_transfer.create', {
            'id': 'aaaaaaaa-0000-4000-8000-000000000002',
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{'product': str(self.produit.id), 'package_quantity': '3'}],
        }))
        self.assertEqual(StockTransferItem.objects.get().quantity_requested, Decimal('36.000'))

    def test_le_transfert_naît_en_BROUILLON_et_ne_touche_pas_au_stock(self):
        """Créer n'expédie rien : le stock ne bouge qu'à l'expédition."""
        avant = self.stock.quantity
        self._verdict(self._send('stock_transfer.create', {
            'id': 'aaaaaaaa-0000-4000-8000-000000000003',
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{'product': str(self.produit.id), 'package_quantity': '2'}],
        }))
        self.assertEqual(StockTransfer.objects.get().status, 'draft')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, avant)

    def test_le_vrac_est_refuse_sur_un_article_vendu_en_gros_seul(self):
        """Refus DÉTERMINISTE : il ne doit jamais repartir en `retry`.

        Le terminal ferme d'ailleurs le champ à l'écran, mais un panier rangé
        d'une version antérieure pourrait le porter.
        """
        self.produit.selling_mode = 'wholesale_only'
        self.produit.save(update_fields=['selling_mode'])

        verdict = self._send('stock_transfer.create', {
            'id': 'aaaaaaaa-0000-4000-8000-000000000004',
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{
                'product': str(self.produit.id),
                'package_quantity': '2',
                'loose_quantity': '5',
            }],
        })
        self._verdict(verdict, 'rejected')

    def test_un_article_sans_conditionnement_refuse_la_saisie_en_contenants(self):
        self.produit.selling_mode = 'retail_only'
        self.produit.save(update_fields=['selling_mode'])

        self._verdict(self._send('stock_transfer.create', {
            'id': 'aaaaaaaa-0000-4000-8000-000000000005',
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{'product': str(self.produit.id), 'package_quantity': '2'}],
        }), 'rejected')


class AjustementEnContenantsTests(_BasePackagee):
    def _compter(self, contenants, vrac, op_id=None):
        return self._send('stock_adjustment.create', {
            'id': 'bbbbbbbb-0000-4000-8000-000000000001',
            'warehouse': str(self.warehouse.id),
            'adjustment_type': 'count',
            'reason': 'Comptage du 6 septembre',
            'items': [{
                'product': str(self.produit.id),
                'quantity_expected': '67',
                'counted_package_quantity': contenants,
                'counted_loose_quantity': vrac,
            }],
        }, op_id)

    def test_le_comptage_en_contenants_est_recompose_par_le_serveur(self):
        self._verdict(self._compter('3', '7'))

        ligne = StockAdjustmentItem.objects.get()
        self.assertEqual(ligne.quantity_counted, Decimal('43.000'))
        self.assertEqual(ligne.counted_package_quantity, Decimal('3.000'))
        self.assertEqual(ligne.counted_loose_quantity, Decimal('7.000'))
        self.assertEqual(ligne.quantity_difference, Decimal('-24.000'))

    def test_la_part_vrac_ATTENDUE_est_relevee_par_le_SERVEUR(self):
        """Le terminal ne l'envoie pas, et il ne doit pas.

        `expected_loose_quantity` est lue sur la ligne de stock à la création
        (`loose_by_product`). Sans elle, l'écart s'afficherait contre un attendu
        redécoupé au facteur du jour - « 5 casiers + 7 bouteilles » deviendrait
        « 5 casiers + 7 » par chance ici, mais « 1 casier + 30 » deviendrait
        « 2 casiers + 6 », un rayon qui n'a jamais existé.
        """
        self._verdict(self._compter('3', '7'))
        self.assertEqual(
            StockAdjustmentItem.objects.get().expected_loose_quantity,
            Decimal('7.000'),
        )

    def test_UN_RAYON_VIDE_EST_UN_COMPTAGE_VALIDE(self):
        """Zéro est une valeur, et c'est l'écart le plus important qui soit.

        Le serveur teste `is not None` et non la véracité, précisément pour
        laisser passer ce cas. Le back-office le REFUSAIT côté navigateur ; ce
        test dit que le serveur, lui, l'a toujours accepté.
        """
        self._verdict(self._compter('0', '0'))

        ligne = StockAdjustmentItem.objects.get()
        self.assertEqual(ligne.quantity_counted, Decimal('0.000'))
        self.assertEqual(ligne.quantity_difference, Decimal('-67.000'))

    def test_l_ajustement_naît_en_BROUILLON_et_ne_touche_pas_au_stock(self):
        avant = self.stock.quantity
        self._verdict(self._compter('3', '7'))
        self.assertEqual(StockAdjustment.objects.get().status, 'draft')
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, avant)

    def test_l_approbation_POSE_le_partage_compte_sur_les_deux_canaux(self):
        """Le comptage physique fait foi sur les DEUX canaux.

        Il ne se redécoupe pas : « j'ai compté 3 casiers et 7 bouteilles » se
        pose tel quel.
        """
        self._verdict(self._compter('3', '7'))
        ajustement = StockAdjustment.objects.get()

        self._verdict(self._send('stock_adjustment.approve', {
            'id': 'cccccccc-0000-4000-8000-000000000001',
            'adjustment': str(ajustement.id),
        }), 'applied')

        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, Decimal('43.000'))
        self.assertEqual(self.stock.package_quantity, Decimal('3.000'))
        self.assertEqual(self.stock.loose_quantity, Decimal('7.000'))
