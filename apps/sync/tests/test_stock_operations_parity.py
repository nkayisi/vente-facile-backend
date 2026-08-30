"""
Parité des opérations de STOCK entre le back-office et le terminal.

Les transitions d'un transfert (approuver, expédier, réceptionner, annuler) et
d'un ajustement (approuver, rejeter) étaient écrites DANS les vues, sur environ
330 lignes. Les rejouer depuis le journal aurait demandé de les réécrire, et
c'est exactement ainsi que la dette client avait divergé avant le lot 6. Elles
vivent désormais dans `inventory.services`, et les deux surfaces les appellent.

Le point le plus important de ce fichier : **un refus de transition est
DÉTERMINISTE**. Il doit devenir verdict `rejected`, jamais `retry`. Réessayer
un transfert déjà expédié le réexpédierait, et le stock sortirait deux fois.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.inventory.models import (
    Stock, StockAdjustment, StockAdjustmentItem, StockMovement,
    StockTransfer, StockTransferItem, Warehouse,
)
from apps.products.models import Product
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _StockBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.autre_entrepot = Warehouse.objects.create(
            organization=self.org, name='Dépôt 2', code='D2',
        )
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True,
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

    def _transfert(self, quantite='10'):
        return {
            'source_warehouse': str(self.warehouse.id),
            'destination_warehouse': str(self.autre_entrepot.id),
            'items': [{
                'product': str(self.produit.id),
                'quantity_requested': quantite,
            }],
        }


class TransfertParityTests(_StockBaseTest):
    def _etat(self, transfert):
        transfert.refresh_from_db()
        source = Stock.objects.get(
            product=self.produit, warehouse=transfert.source_warehouse,
        )
        destination = Stock.objects.filter(
            product=self.produit, warehouse=transfert.destination_warehouse,
        ).first()
        return {
            'statut': transfert.status,
            'source': source.quantity,
            'destination': destination.quantity if destination else None,
            'mouvements': StockMovement.objects.filter(
                reference_id=transfert.id,
            ).count(),
            'lignes': list(
                transfert.items.order_by('id').values_list(
                    'quantity_shipped', 'quantity_received',
                )
            ),
        }

    def test_the_whole_transfer_run_leaves_the_same_state_by_both_paths(self):
        """Créer, expédier, réceptionner : le chemin complet, des deux côtés."""
        web = self.client.post(
            '/api/v1/stock-transfers/', self._transfert(), format='json',
            **self._headers(),
        )
        self.assertEqual(web.status_code, status.HTTP_201_CREATED, web.data)
        # `StockTransferCreateSerializer` ne rend pas l'identifiant : on relit
        # le dernier créé plutôt que d'ajouter un champ pour le test.
        t_web = StockTransfer.objects.latest('created_at')
        for chemin in ('ship', 'receive'):
            r = self.client.post(
                f'/api/v1/stock-transfers/{t_web.id}/{chemin}/', {},
                format='json', **self._headers(),
            )
            self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        etat_web = self._etat(t_web)

        # Remise à l'état initial pour comparer sur les mêmes stocks.
        Stock.objects.filter(warehouse=self.warehouse).update(
            quantity=Decimal('100.000')
        )
        Stock.objects.filter(warehouse=self.autre_entrepot).delete()

        verdict = self._verdict(self._send(
            'stock_transfer.create', self._transfert(),
            'eeee1111-0000-4000-8000-000000000001',
        ))
        t_mob = StockTransfer.objects.get(id=verdict['server_ids']['stock_transfer'])
        for i, kind in enumerate(('stock_transfer.ship', 'stock_transfer.receive')):
            self._verdict(self._send(
                kind, {'transfer': str(t_mob.id)},
                f'eeee1111-0000-4000-8000-00000000000{i + 2}',
            ))

        self.assertEqual(etat_web, self._etat(t_mob))

    def test_the_client_identifier_is_kept(self):
        local = 'aaaaaaaa-1111-4000-8000-000000000001'
        verdict = self._verdict(self._send(
            'stock_transfer.create', {'id': local, **self._transfert()},
            'eeee1111-0000-4000-8000-000000000010',
        ))
        self.assertEqual(verdict['server_ids']['stock_transfer'], local)

    def test_shipping_twice_is_REJECTED_never_retried(self):
        """
        Le test le plus important du fichier. Un `retry` sur un transfert déjà
        expédié sortirait le stock une seconde fois.
        """
        verdict = self._verdict(self._send(
            'stock_transfer.create', self._transfert(),
            'eeee1111-0000-4000-8000-000000000020',
        ))
        transfert = verdict['server_ids']['stock_transfer']
        self._verdict(self._send(
            'stock_transfer.ship', {'transfer': transfert},
            'eeee1111-0000-4000-8000-000000000021',
        ))
        # Seconde expédition, avec un AUTRE identifiant d'opération : ce n'est
        # pas un doublon d'idempotence, c'est un ordre réellement impossible.
        refus = self._verdict(self._send(
            'stock_transfer.ship', {'transfer': transfert},
            'eeee1111-0000-4000-8000-000000000022',
        ), attendu='rejected')
        self.assertIn('expédié', str(refus.get('errors', '')).lower() + str(refus))

        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal('90.000'))

    def test_cancelling_a_shipped_transfer_gives_the_stock_back(self):
        verdict = self._verdict(self._send(
            'stock_transfer.create', self._transfert(),
            'eeee1111-0000-4000-8000-000000000030',
        ))
        transfert = verdict['server_ids']['stock_transfer']
        self._verdict(self._send(
            'stock_transfer.ship', {'transfer': transfert},
            'eeee1111-0000-4000-8000-000000000031',
        ))
        self._verdict(self._send(
            'stock_transfer.cancel', {'transfer': transfert},
            'eeee1111-0000-4000-8000-000000000032',
        ))
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        self.assertEqual(stock.quantity, Decimal('100.000'))

    def test_an_unknown_transfer_is_rejected(self):
        self._verdict(self._send(
            'stock_transfer.ship',
            {'transfer': '00000000-0000-4000-8000-000000000000'},
            'eeee1111-0000-4000-8000-000000000040',
        ), attendu='rejected')


class AjustementParityTests(_StockBaseTest):
    def _corps(self, quantite='95'):
        return {
            'warehouse': str(self.warehouse.id),
            'adjustment_type': 'count',
            'reason': 'Comptage',
            'items': [{
                'product': str(self.produit.id),
                'quantity_expected': '100',
                'quantity_counted': quantite,
            }],
        }

    def test_an_adjustment_applies_the_same_way_by_both_paths(self):
        web = self.client.post(
            '/api/v1/stock-adjustments/', self._corps(), format='json',
            **self._headers(),
        )
        self.assertEqual(web.status_code, status.HTTP_201_CREATED, web.data)
        a_web = StockAdjustment.objects.latest('created_at')
        r = self.client.post(
            f'/api/v1/stock-adjustments/{a_web.id}/approve/', {},
            format='json', **self._headers(),
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.data)
        quantite_web = Stock.objects.get(
            product=self.produit, warehouse=self.warehouse,
        ).quantity

        Stock.objects.filter(warehouse=self.warehouse).update(
            quantity=Decimal('100.000')
        )

        verdict = self._verdict(self._send(
            'stock_adjustment.create', self._corps(),
            'ffff1111-0000-4000-8000-000000000001',
        ))
        self._verdict(self._send(
            'stock_adjustment.approve',
            {'adjustment': verdict['server_ids']['stock_adjustment']},
            'ffff1111-0000-4000-8000-000000000002',
        ))
        quantite_mob = Stock.objects.get(
            product=self.produit, warehouse=self.warehouse,
        ).quantity

        self.assertEqual(quantite_web, quantite_mob)

    def test_approving_twice_is_rejected(self):
        verdict = self._verdict(self._send(
            'stock_adjustment.create', self._corps(),
            'ffff1111-0000-4000-8000-000000000010',
        ))
        ajustement = verdict['server_ids']['stock_adjustment']
        self._verdict(self._send(
            'stock_adjustment.approve', {'adjustment': ajustement},
            'ffff1111-0000-4000-8000-000000000011',
        ))
        self._verdict(self._send(
            'stock_adjustment.approve', {'adjustment': ajustement},
            'ffff1111-0000-4000-8000-000000000012',
        ), attendu='rejected')


class DeconditionnementTests(_StockBaseTest):
    def test_unpacking_by_both_paths_opens_the_same_number_of_packages(self):
        self.produit.selling_mode = 'both'
        self.produit.units_per_package = 12
        self.produit.save()
        # Cent unités, entièrement SCELLÉES : sans les deux compteurs, le stock
        # est incohérent et le service reporte l'écart sur le vrac, ce qui ôte
        # au test tout ce qu'il prétend mesurer.
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        stock.quantity = Decimal('120.000')
        stock.package_quantity = Decimal('10.000')
        stock.loose_quantity = Decimal('0.000')
        stock.save()

        web = self.client.post(
            f'/api/v1/stocks/{stock.id}/unpack/', {'packages': 2},
            format='json', **self._headers(),
        )
        self.assertEqual(web.status_code, status.HTTP_200_OK, web.data)
        stock.refresh_from_db()
        vrac_web = stock.loose_quantity

        stock.quantity = Decimal('120.000')
        stock.package_quantity = Decimal('10.000')
        stock.loose_quantity = Decimal('0.000')
        stock.save()

        self._verdict(self._send(
            'stock.unpack', {'stock': str(stock.id), 'packages': 2},
            'aaaa2222-0000-4000-8000-000000000001',
        ))
        stock.refresh_from_db()
        self.assertEqual(vrac_web, stock.loose_quantity)

    def test_unpacking_a_product_without_packaging_is_rejected(self):
        stock = Stock.objects.get(product=self.produit, warehouse=self.warehouse)
        self._verdict(self._send(
            'stock.unpack', {'stock': str(stock.id), 'packages': 1},
            'aaaa2222-0000-4000-8000-000000000010',
        ), attendu='rejected')
