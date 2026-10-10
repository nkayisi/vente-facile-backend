"""
Parité des réquisitions entre la vue et le journal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ DEUX DÉFAUTS MESURÉS SUR LE VRAI SERVEUR, PAS DÉDUITS.                      │
│                                                                              │
│ 1. `requisition.create` appelait `assert_warehouse_allowed_for_membership`   │
│    sans l'importer. Le module se charge sans broncher - un nom libre ne lève │
│    qu'à l'exécution - et le premier magasinier qui a demandé du              │
│    réapprovisionnement a reçu « name '…' is not defined », sa demande        │
│    partant en quarantaine. `test_handler_imports` ne voyait que les imports  │
│    ÉCRITS, donc rien ici ; il balaie désormais les noms LIBRES.              │
│                                                                              │
│ 2. `create_requisition` ignorait l'identifiant du terminal et allouait un    │
│    UUID neuf. Le terminal route sa fiche et adresse ses transitions avec     │
│    l'identifiant qu'il a tiré : sa propre fiche déclarait la pièce           │
│    introuvable, et un `submit` mis en file juste après la création revenait   │
│    « Réquisition introuvable » - refus DÉTERMINISTE, donc quarantaine, pour  │
│    un geste parfaitement légitime.                                           │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`** : c'est le magasinier qui constate un
rayon vide, et c'est à lui que `requisitions.create` est accordée. Son oubli
est ce qui a caché le trou du `sale_return.create` pendant tout un lot.
"""
from decimal import Decimal
from uuid import uuid4

from rest_framework.test import APITestCase

from apps.inventory.models import Stock, Warehouse
from apps.organizations.models import OrganizationMembership
from apps.products.models import Product
from apps.purchases.models import Requisition
from apps.sales.tests._helpers import make_org_with_users, make_user

OPERATIONS = '/api/v1/sync/operations/'


class RequisitionParityTests(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.magasinier = make_user('stock@vf.test', 'Stock', 'Keeper')
        OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )
        membership = OrganizationMembership.objects.get(
            user=self.magasinier, organization=self.org
        )
        membership.assigned_warehouses.set([self.warehouse])

        # Un dépôt que le magasinier n'a PAS : c'est lui qui prouve la garde.
        self.depot_ferme = Warehouse.objects.create(
            organization=self.org, name='Dépôt fermé', code='DF',
        )

        self.produit = Product.objects.create(
            organization=self.org, name='Lait frais 1L', sku='LAIT-1L',
            slug='lait-frais-1l', track_inventory=True,
            reorder_point=Decimal('12.000'),
            cost_price=Decimal('1000'), selling_price=Decimal('1500'),
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('0.000'),
        )

        self.client.force_authenticate(user=self.magasinier)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, kind, payload, op_id=None):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id or str(uuid4()), 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-10-10T08:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _corps(self, **extra):
        corps = {
            'warehouse': str(self.warehouse.id),
            'notes': '',
            'items': [{
                'product': str(self.produit.id),
                'quantity_requested': '24',
                'notes': '',
            }],
        }
        corps.update(extra)
        return corps

    # ------------------------------------------------------------------ nom libre

    def test_un_magasinier_cree_une_requisition_PAR_LE_JOURNAL(self):
        """
        Le contrôle qui attrape le nom libre : sur le code d'origine le verdict
        était `rejected` avec « name '…' is not defined ».
        """
        rid = str(uuid4())
        r = self._journal('requisition.create', self._corps(id=rid))
        verdict = r.data['results'][0]
        self.assertEqual(
            verdict['verdict'], 'applied',
            f"Refus inattendu : {verdict.get('errors')}",
        )
        self.assertTrue(Requisition.objects.filter(organization=self.org).exists())

    # ------------------------------------------------- l'identifiant du terminal

    def test_l_identifiant_du_TERMINAL_est_celui_de_la_piece(self):
        """
        Sans cela, le terminal ne peut plus désigner ce qu'il vient de créer.
        """
        rid = str(uuid4())
        self._journal('requisition.create', self._corps(id=rid))
        self.assertTrue(
            Requisition.objects.filter(id=rid, organization=self.org).exists(),
            "La réquisition est née sous un AUTRE identifiant : le terminal ne "
            "peut plus ni ouvrir sa fiche ni la faire avancer.",
        )

    def test_une_transition_mise_en_file_JUSTE_APRES_la_creation_aboutit(self):
        """
        Le cas réel : le magasinier crée puis soumet sans réseau, et les deux
        actes partent dans le même lot.
        """
        rid = str(uuid4())
        self._journal('requisition.create', self._corps(id=rid))
        r = self._journal('requisition.submit', {'requisition': rid, 'notes': ''})
        verdict = r.data['results'][0]
        self.assertEqual(
            verdict['verdict'], 'applied',
            f"Refus inattendu : {verdict.get('errors')}",
        )
        self.assertEqual(
            Requisition.objects.get(id=rid).status, Requisition.Status.SUBMITTED
        )

    def test_la_reference_du_terminal_est_REPRISE(self):
        """
        Le bordereau se dicte et se classe : le remplacer ferait que le papier
        du magasinier ne désigne plus rien.
        """
        rid = str(uuid4())
        self._journal(
            'requisition.create',
            self._corps(id=rid, reference='REQ-20261010-K7QM-0001'),
        )
        self.assertEqual(
            Requisition.objects.get(id=rid).reference, 'REQ-20261010-K7QM-0001'
        )

    # ------------------------------------------------------------- l'instantané

    def test_le_stock_et_le_seuil_sont_RELEVES_jamais_envoyes(self):
        """
        Le client n'envoie que l'article et la quantité. Laisser saisir
        l'instantané permettrait d'écrire une pénurie qui n'existe pas, sur un
        document qui sert à engager de l'argent.
        """
        rid = str(uuid4())
        corps = self._corps(id=rid)
        # Le terminal ESSAIE d'écrire un attendu fantaisiste : il doit être jeté.
        corps['items'][0]['stock_at_request'] = '999'
        corps['items'][0]['reorder_point_at_request'] = '999'
        self._journal('requisition.create', corps)

        ligne = Requisition.objects.get(id=rid).items.first()
        self.assertEqual(ligne.stock_at_request, Decimal('0.000'))
        self.assertEqual(ligne.reorder_point_at_request, Decimal('12.000'))

    # ------------------------------------------------------------- le périmètre

    def test_un_depot_hors_perimetre_est_REFUSE(self):
        """
        La garde d'entrepôt, par le même chemin que la vue : un refus LÈVE, il
        ne rend jamais un ensemble vide.
        """
        r = self._journal(
            'requisition.create',
            self._corps(id=str(uuid4()), warehouse=str(self.depot_ferme.id)),
        )
        self.assertIn(r.data['results'][0]['verdict'], ('rejected', 'blocked'))
        self.assertFalse(
            Requisition.objects.filter(warehouse=self.depot_ferme).exists()
        )

    # --------------------------------------------------------------- le droit

    def test_un_magasinier_ne_peut_PAS_approuver(self):
        """
        Approuver engage de l'argent : `requisitions.approve` ne va pas au
        magasinier. Le verdict est `blocked`, donc l'opération est CONSERVÉE et
        repartira dès qu'un droit sera accordé.
        """
        rid = str(uuid4())
        self._journal('requisition.create', self._corps(id=rid))
        self._journal('requisition.submit', {'requisition': rid, 'notes': ''})

        r = self._journal('requisition.approve', {'requisition': rid, 'notes': ''})
        self.assertEqual(r.data['results'][0]['verdict'], 'blocked')
        self.assertEqual(
            Requisition.objects.get(id=rid).status, Requisition.Status.SUBMITTED
        )

    # ------------------------------------------------------------- idempotence

    def test_le_renvoi_du_meme_acte_ne_cree_PAS_une_seconde_piece(self):
        """
        C'est ce qui rend le renvoi sûr après une coupure survenue APRÈS que le
        serveur ait validé, mais avant que la réponse n'arrive.
        """
        rid, op = str(uuid4()), str(uuid4())
        self._journal('requisition.create', self._corps(id=rid), op_id=op)
        r = self._journal('requisition.create', self._corps(id=rid), op_id=op)

        self.assertEqual(r.data['results'][0]['verdict'], 'duplicate')
        self.assertEqual(Requisition.objects.filter(organization=self.org).count(), 1)
