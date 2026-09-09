"""
Parité du MOUVEMENT DE STOCK entre le back-office et le terminal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE HANDLER N'APPELAIT PAS `perform_create`, ET C'EST TOUT L'EFFET QUI Y VIT. │
│                                                                              │
│ `StockMovementViewSet.perform_create` fait cent vingt lignes : verrou sur la │
│ ligne de stock, lots FIFO à l'entrée comme à la sortie, coût moyen pondéré,  │
│ partage scellé/vrac, `quantity_before`/`quantity_after`, auteur, report des  │
│ prix. Le handler appelait `serializer.save()` en direct : le mouvement était │
│ écrit sans organisation - une FK non nulle, donc rien n'était écrit du tout  │
│ - et le stock ne bougeait pas.                                               │
│                                                                              │
│ Aucun test ne couvrait cet acte. C'est ce qui l'a laissé cassé depuis le     │
│ lot 7.                                                                       │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** : `accessible_warehouse_ids` sort en amont
pour un propriétaire, et le code de périmètre ne serait jamais exécuté. Le
magasinier est le bon rôle ici, et un SECOND entrepôt non assigné sert à
prouver que le refus tombe des deux côtés.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import Stock, StockMovement, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users, make_user

OPERATIONS = '/api/v1/sync/operations/'


class _MouvementBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.magasinier = make_user('stock@vf.test', 'Sto', 'Ck')
        adhesion = OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        adhesion.assigned_warehouses.add(self.warehouse)

        # Un entrepôt hors périmètre : c'est lui qui prouve le refus.
        self.entrepot_interdit = Warehouse.objects.create(
            organization=self.org, name='Dépôt 2', code='D2',
        )

        self.produit = Product.objects.create(
            organization=self.org, name='Coca 50cl', slug='coca-50cl', sku='C50',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1200.00'),
            track_inventory=True,
        )
        self.client.force_authenticate(user=self.magasinier)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, payload, op_id):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': 'stock_movement.create', 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-31T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(
            verdict['verdict'], attendu,
            f"verdict={verdict['verdict']} errors={verdict.get('errors')} "
            f"detail={verdict.get('detail')}",
        )
        return verdict

    def _payload_terminal(self, op_id, **surcharges):
        """Le payload de `features/stock/actes.ts::creerMouvement`."""
        base = {
            'id': op_id,
            'product': str(self.produit.id),
            'warehouse': str(self.warehouse.id),
            'movement_type': 'purchase',
            'quantity': '10',
            'unit_cost': '1500',
            'notes': '',
        }
        base.update(surcharges)
        # Le terminal OMET une clé plutôt que d'envoyer `null` : c'est ce que
        # fait `creerMouvement` avec ses `...(x != null ? {...} : {})`.
        return {k: v for k, v in base.items() if v is not None}


class MouvementParityTests(_MouvementBaseTest):
    def test_le_terminal_peut_enregistrer_une_entree(self):
        """Le test qui porte le lot : sans lui, le mouvement n'existe pas."""
        op = '33333333-3333-4333-8333-333333333333'
        self._verdict(self._journal(self._payload_terminal(op), op), 'applied')
        self.assertEqual(StockMovement.objects.count(), 1)

    def test_l_entree_du_journal_fait_bouger_le_stock_comme_la_vue(self):
        """
        Le cœur du sujet : ce n'est pas le mouvement qui compte, c'est son EFFET.

        Un mouvement écrit sans que `Stock` bouge est pire qu'un mouvement
        refusé : l'historique dit qu'on a reçu dix bouteilles, et le rayon
        n'en sait rien.
        """
        vue = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.produit.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase',
                'quantity': '10',
                'unit_cost': '1500',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_201_CREATED, vue.data)
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal('10.000'))
        apres_la_vue = stock.avg_cost

        op = '33333333-3333-4333-8333-333333333333'
        self._verdict(self._journal(self._payload_terminal(op), op), 'applied')

        stock.refresh_from_db()
        self.assertEqual(stock.quantity, Decimal('20.000'), "Le stock n'a pas bougé.")
        # Coût moyen pondéré : dix unités à 1 500 puis dix autres à 1 500.
        self.assertEqual(stock.avg_cost, apres_la_vue)
        self.assertEqual(stock.avg_cost, Decimal('1500.00'))

        mouvement = StockMovement.objects.get(id=op)
        self.assertEqual(mouvement.organization_id, self.org.id)
        self.assertEqual(mouvement.quantity_before, Decimal('10.000'))
        self.assertEqual(mouvement.quantity_after, Decimal('20.000'))
        self.assertEqual(mouvement.created_by_id, self.magasinier.id)
        # Une entrée crée un LOT : c'est lui que le FIFO consommera plus tard,
        # et sans lui une sortie ultérieure ne saurait pas à quel coût sortir.
        self.assertIsNotNone(mouvement.batch_id)

    def test_la_saisie_par_contenants_garde_son_partage(self):
        """
        « 2 casiers + 3 bouteilles » ne doit pas se relire « 51 bouteilles ».

        C'est la raison d'être du passage par `PackagingService` : l'ancienne
        synchronisation faisait `stock.quantity += ...` à la main et réparait
        le partage en silence.
        """
        self.produit.selling_mode = 'both'
        self.produit.units_per_package = 24
        self.produit.save(update_fields=['selling_mode', 'units_per_package'])

        op = '44444444-4444-4444-8444-444444444444'
        self._verdict(
            self._journal(
                self._payload_terminal(
                    op, quantity=None, package_quantity='2', loose_quantity='3',
                ),
                op,
            ),
            'applied',
        )
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        self.assertEqual(stock.package_quantity, Decimal('2.000'))
        self.assertEqual(stock.loose_quantity, Decimal('3.000'))
        self.assertEqual(stock.quantity, Decimal('51.000'))

    def test_un_entrepot_hors_perimetre_est_refuse_des_deux_cotes(self):
        """Le magasinier n'a qu'un entrepôt : le second lui est fermé."""
        vue = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.produit.id),
                'warehouse': str(self.entrepot_interdit.id),
                'movement_type': 'purchase', 'quantity': '10', 'unit_cost': '1500',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_400_BAD_REQUEST, vue.data)

        op = '55555555-5555-4555-8555-555555555555'
        self._verdict(
            self._journal(
                self._payload_terminal(op, warehouse=str(self.entrepot_interdit.id)),
                op,
            ),
            'rejected',
        )
        self.assertEqual(StockMovement.objects.count(), 0)


class ReportDesPrixParLeJournalTests(_MouvementBaseTest):
    """
    Le report des prix sur la fiche produit exige `products.edit`, DES DEUX CÔTÉS.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE CONTRÔLE ÉTAIT SAUTÉ SUR LE CHEMIN DU TERMINAL.                       │
    │                                                                          │
    │ `_validate_product_prices` résolvait l'organisation par                  │
    │ `self.context['view'].get_organization()`. Le gestionnaire de            │
    │ synchronisation construit le serializer avec le seul `request` : il n'y  │
    │ a pas de `view`, `organization` valait `None`, et tout le `if` tombait.  │
    │                                                                          │
    │ Latent tant que le terminal n'envoyait pas le drapeau. Le jour où son    │
    │ formulaire gagne la case, n'importe quel caissier réécrit les prix du    │
    │ catalogue depuis son téléphone, en silence, quand le back-office le lui  │
    │ refuse.                                                                  │
    └──────────────────────────────────────────────────────────────────────────┘

    Le rôle est celui qui MORD : un magasinier porte `products.edit`, un
    caissier ne porte pas `stock_movements.create`. Le cas réel est donc le
    caissier à qui le marchand a accordé la saisie de stock par
    `extra_permissions`, et à qui il n'a pas accordé la retarification.
    """

    def setUp(self):
        super().setUp()
        # `cashier_a` vient de `make_org_with_users`, avec l'entrepôt principal
        # déjà assigné.
        self.adhesion_caissier = OrganizationMembership.objects.get(
            user=self.cashier_a, organization=self.org,
        )
        self.adhesion_caissier.extra_permissions = ['stock_movements.create']
        self.adhesion_caissier.save(update_fields=['extra_permissions'])
        self.client.force_authenticate(user=self.cashier_a)

    def _prix_du_produit(self):
        self.produit.refresh_from_db()
        return (self.produit.cost_price, self.produit.selling_price)

    def test_le_caissier_ne_retarifie_pas_le_catalogue_par_le_journal(self):
        avant = self._prix_du_produit()

        op = '66666666-6666-4666-8666-666666666666'
        verdict = self._verdict(
            self._journal(
                self._payload_terminal(
                    op,
                    unit_cost='1500',
                    update_product_prices=True,
                    selling_price='9999',
                ),
                op,
            ),
            'rejected',
        )
        self.assertIn('update_product_prices', str(verdict.get('errors')))
        self.assertEqual(
            self._prix_du_produit(), avant,
            'Les prix du catalogue ont bougé sur un droit que le caissier n\'a pas.',
        )

    def test_le_back_office_refuse_le_meme_geste(self):
        """La parité est le sujet : les deux surfaces doivent refuser."""
        avant = self._prix_du_produit()

        vue = self.client.post(
            '/api/v1/stock-movements/',
            {
                'product': str(self.produit.id),
                'warehouse': str(self.warehouse.id),
                'movement_type': 'purchase', 'quantity': '10', 'unit_cost': '1500',
                'update_product_prices': True, 'selling_price': '9999',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(vue.status_code, status.HTTP_400_BAD_REQUEST, vue.data)
        self.assertIn('update_product_prices', vue.data)
        self.assertEqual(self._prix_du_produit(), avant)

    def test_avec_le_droit_le_report_passe_par_le_journal(self):
        """
        Le refus ne doit pas se transformer en interdiction générale.

        Sans ce contre-test, poser le contrôle trop haut (un `blocked` sur
        l'opération entière, ou un refus inconditionnel) passerait inaperçu.
        """
        self.adhesion_caissier.extra_permissions = [
            'stock_movements.create', 'products.edit',
        ]
        self.adhesion_caissier.save(update_fields=['extra_permissions'])

        op = '77777777-7777-4777-8777-777777777777'
        self._verdict(
            self._journal(
                self._payload_terminal(
                    op,
                    unit_cost='1500',
                    update_product_prices=True,
                    selling_price='9999',
                ),
                op,
            ),
            'applied',
        )
        self.produit.refresh_from_db()
        self.assertEqual(self.produit.selling_price, Decimal('9999.00'))
