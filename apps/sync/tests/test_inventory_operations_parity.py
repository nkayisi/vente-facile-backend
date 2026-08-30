"""
Parité des opérations d'INVENTAIRE et de CATALOGUE.

Compter un rayon est le meilleur usage mobile du produit : on compte debout,
souvent au fond d'un dépôt sans réseau. Les cinq transitions d'une session
doivent donc passer par le journal, et par le MÊME corps que le back-office.

Le test central compare l'état laissé par un cycle complet - démarrer, compter,
soumettre, valider - envoyé par les deux chemins.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import InventoryCount, InventorySession, Stock, StockMovement
from apps.products.models import Brand, Category, Product, Unit
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _InventaireBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _send(self, kind, payload, op_id):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-08-30T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], attendu, verdict.get('errors'))
        return verdict

    def _corps_session(self, nom):
        return {
            'name': nom,
            'warehouse': str(self.warehouse.id),
            'scope_type': 'full',
        }


class CycleInventaireParityTests(_InventaireBaseTest):
    def _etat(self):
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        return {
            'quantite': stock.quantity,
            'mouvements': StockMovement.objects.filter(
                reference_type='inventory_session',
            ).count(),
        }

    def test_a_full_count_leaves_the_same_state_by_both_paths(self):
        # --- chemin web
        web = self.client.post(
            '/api/v1/inventory-sessions/', self._corps_session('Web'),
            format='json', **self._headers(),
        )
        self.assertEqual(web.status_code, status.HTTP_201_CREATED, web.data)
        s_web = InventorySession.objects.latest('created_at')
        for etape in ('start',):
            r = self.client.post(
                f'/api/v1/inventory-sessions/{s_web.id}/{etape}/', {},
                format='json', **self._headers(),
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        ligne = InventoryCount.objects.get(session=s_web)
        for chemin, corps in (
            ('count', {'counts': [{'id': str(ligne.id), 'quantity_counted': '95'}]}),
            ('submit', {}),
            ('validate', {}),
        ):
            r = self.client.post(
                f'/api/v1/inventory-sessions/{s_web.id}/{chemin}/', corps,
                format='json', **self._headers(),
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        etat_web = self._etat()

        # --- remise à l'état initial
        Stock.objects.filter(warehouse=self.warehouse).update(quantity=Decimal('100.000'))
        StockMovement.objects.all().delete()

        # --- chemin mobile
        verdict = self._verdict(self._send(
            'inventory_session.create', self._corps_session('Mobile'),
            'aaaa3333-0000-4000-8000-000000000001',
        ))
        s_mob = verdict['server_ids']['inventory_session']
        self._verdict(self._send(
            'inventory_session.start', {'session': s_mob},
            'aaaa3333-0000-4000-8000-000000000002',
        ))
        ligne_mob = InventoryCount.objects.get(session_id=s_mob)
        self._verdict(self._send(
            'inventory_session.count',
            {'session': s_mob,
             'counts': [{'id': str(ligne_mob.id), 'quantity_counted': '95'}]},
            'aaaa3333-0000-4000-8000-000000000003',
        ))
        self._verdict(self._send(
            'inventory_session.submit', {'session': s_mob},
            'aaaa3333-0000-4000-8000-000000000004',
        ))
        self._verdict(self._send(
            'inventory_session.validate', {'session': s_mob},
            'aaaa3333-0000-4000-8000-000000000005',
        ))

        self.assertEqual(etat_web, self._etat())

    def test_submitting_with_an_uncounted_line_is_REJECTED(self):
        """
        Une session soumise à moitié appliquerait des écarts sur les seuls
        produits regardés, et laisserait croire que les autres sont justes.
        """
        verdict = self._verdict(self._send(
            'inventory_session.create', self._corps_session('Partielle'),
            'aaaa3333-0000-4000-8000-000000000010',
        ))
        session = verdict['server_ids']['inventory_session']
        self._verdict(self._send(
            'inventory_session.start', {'session': session},
            'aaaa3333-0000-4000-8000-000000000011',
        ))
        self._verdict(self._send(
            'inventory_session.submit', {'session': session},
            'aaaa3333-0000-4000-8000-000000000012',
        ), attendu='rejected')

    def test_starting_twice_is_rejected(self):
        verdict = self._verdict(self._send(
            'inventory_session.create', self._corps_session('Double'),
            'aaaa3333-0000-4000-8000-000000000020',
        ))
        session = verdict['server_ids']['inventory_session']
        self._verdict(self._send(
            'inventory_session.start', {'session': session},
            'aaaa3333-0000-4000-8000-000000000021',
        ))
        self._verdict(self._send(
            'inventory_session.start', {'session': session},
            'aaaa3333-0000-4000-8000-000000000022',
        ), attendu='rejected')

    def test_an_unknown_count_line_does_not_sink_the_batch(self):
        """
        Une ligne supprimée entre-temps ne doit pas condamner les autres
        comptages du même envoi : le magasinier a compté, son travail reste.
        """
        verdict = self._verdict(self._send(
            'inventory_session.create', self._corps_session('Ligne absente'),
            'aaaa3333-0000-4000-8000-000000000030',
        ))
        session = verdict['server_ids']['inventory_session']
        self._verdict(self._send(
            'inventory_session.start', {'session': session},
            'aaaa3333-0000-4000-8000-000000000031',
        ))
        ligne = InventoryCount.objects.get(session_id=session)
        self._verdict(self._send(
            'inventory_session.count',
            {'session': session, 'counts': [
                {'id': '00000000-0000-4000-8000-000000000000', 'quantity_counted': '1'},
                {'id': str(ligne.id), 'quantity_counted': '42'},
            ]},
            'aaaa3333-0000-4000-8000-000000000032',
        ))
        ligne.refresh_from_db()
        self.assertTrue(ligne.is_counted)
        self.assertEqual(ligne.quantity_counted, Decimal('42.000'))


class CatalogueParityTests(_InventaireBaseTest):
    def test_a_product_created_by_the_journal_keeps_its_identifier(self):
        local = 'bbbb3333-1111-4000-8000-000000000001'
        verdict = self._verdict(self._send(
            'product.create',
            {'id': local, 'name': 'Nouveau', 'sku': 'NEW-1',
             'selling_price': '1000', 'cost_price': '700'},
            'bbbb3333-0000-4000-8000-000000000001',
        ))
        self.assertEqual(verdict['server_ids']['product'], local)
        self.assertTrue(Product.objects.filter(id=local).exists())

    def test_the_three_reference_tables_are_creatable(self):
        # `slug` est OBLIGATOIRE sur les catégories et les marques, et le
        # serveur ne le dérive pas. L'écran mobile le dérive donc du nom, comme
        # le formulaire web : le déduire ici, dans le gestionnaire, en ferait
        # une règle métier de plus, à tenir en phase avec celle du web.
        for kind, corps, modele in (
            ('category.create', {'name': 'Boissons', 'slug': 'boissons'}, Category),
            ('brand.create', {'name': 'Marque X', 'slug': 'marque-x'}, Brand),
            ('unit.create', {'name': 'Bouteille', 'symbol': 'btl'}, Unit),
        ):
            avant = modele.objects.count()
            self._verdict(self._send(
                kind, corps, f'cccc3333-0000-4000-8000-{abs(hash(kind)) % 10**12:012d}',
            ))
            self.assertEqual(modele.objects.count(), avant + 1, kind)
