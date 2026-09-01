"""
Parité des RETOURS et des DEVIS.

Ni l'un ni l'autre n'avait d'écran, nulle part : le terminal crée la référence.
Ces fonctions sont donc le seul chemin d'écriture, et le web les emprunte aussi.

Le test le plus important est celui de la DETTE : un devis converti est une
facture émise et non payée. Sans `register_sale_debt`, la facture était retenue
par `open_credit_sales` alors qu'aucune dette n'était inscrite - son règlement
décrémentait un solde jamais incrémenté, et rendait le client artificiellement
créditeur. C'est le défaut corrigé à la session 2026-08-24, et il ne doit pas
revenir par la porte du journal.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts import services as contacts_services
from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import Quotation, Sale, SaleReturn
from apps.sales.tests._helpers import make_cash_payment_method, make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _BaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        make_cash_payment_method(self.org)
        self.produit = Product.objects.create(
            organization=self.org, name='Article', slug='article', sku='A1',
            selling_price=Decimal('1000.00'), cost_price=Decimal('700.00'),
            track_inventory=True, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.produit, warehouse=self.warehouse,
            quantity=Decimal('50.000'), avg_cost=Decimal('700.00'),
        )
        self.acheteur = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='09',
            credit_limit=Decimal('0'),
        )
        # Une vente au POS exige une session OUVERTE sur la caisse : c'est la
        # garde que le lot 4 a posée, et elle vaut ici aussi.
        from apps.sales.models import RegisterSession
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register, opened_by=self.owner,
            opening_balance=Decimal('0'), status='open',
        )
        self.client.force_authenticate(user=self.owner)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _send(self, kind, payload, op_id):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1, 'depends_on': [],
                'occurred_at': '2026-08-30T09:00:00Z', 'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(verdict['verdict'], attendu, verdict.get('errors'))
        return verdict

    def _corps_devis(self):
        return {
            'customer': str(self.acheteur.id),
            # `valid_until` est OBLIGATOIRE : un devis sans date de validité
            # n'expire jamais, et le serveur refuse d'en créer.
            'valid_until': '2026-12-31',
            'items': [{
                'product': str(self.produit.id),
                'quantity': '2',
                'unit_price': '1000.00',
            }],
        }


class DevisTests(_BaseTest):
    def test_converting_a_quotation_REGISTERS_THE_DEBT(self):
        """
        Le test le plus important du fichier : sans la dette, le règlement
        suivant rendrait le client créditeur sans qu'il ait jamais payé.
        """
        verdict = self._verdict(self._send(
            'quotation.create', self._corps_devis(),
            'dddd4444-0000-4000-8000-000000000001',
        ))
        devis = verdict['server_ids']['quotation']

        avant = contacts_services.get_balance(
            self.acheteur, self.org.currency or 'CDF'
        )
        verdict = self._verdict(self._send(
            'quotation.convert',
            {'quotation': devis, 'warehouse': str(self.warehouse.id)},
            'dddd4444-0000-4000-8000-000000000002',
        ))

        vente = Sale.objects.get(id=verdict['server_ids']['sale'])
        self.assertEqual(vente.status, 'pending')
        self.assertEqual(vente.amount_due, Decimal('2000.00'))

        self.acheteur.refresh_from_db()
        apres = contacts_services.get_balance(self.acheteur, vente.currency)
        self.assertEqual(apres - avant, Decimal('2000.00'))

    def test_converting_twice_is_REJECTED(self):
        verdict = self._verdict(self._send(
            'quotation.create', self._corps_devis(),
            'dddd4444-0000-4000-8000-000000000010',
        ))
        devis = verdict['server_ids']['quotation']
        self._verdict(self._send(
            'quotation.convert', {'quotation': devis},
            'dddd4444-0000-4000-8000-000000000011',
        ))
        self._verdict(self._send(
            'quotation.convert', {'quotation': devis},
            'dddd4444-0000-4000-8000-000000000012',
        ), attendu='rejected')

    def test_converting_without_enough_stock_is_rejected_and_writes_nothing(self):
        Stock.objects.filter(warehouse=self.warehouse).update(quantity=Decimal('1.000'))
        verdict = self._verdict(self._send(
            'quotation.create', self._corps_devis(),
            'dddd4444-0000-4000-8000-000000000020',
        ))
        devis = verdict['server_ids']['quotation']
        self._verdict(self._send(
            'quotation.convert',
            {'quotation': devis, 'warehouse': str(self.warehouse.id)},
            'dddd4444-0000-4000-8000-000000000021',
        ), attendu='rejected')

        # Le contrôle de stock passe AVANT toute écriture : un devis converti
        # sur une vente impossible à servir serait pire qu'un refus.
        self.assertEqual(Quotation.objects.get(id=devis).status, 'draft')
        self.assertFalse(Sale.objects.filter(customer=self.acheteur).exists())

    def test_the_client_identifier_is_kept(self):
        local = 'eeee4444-1111-4000-8000-000000000001'
        verdict = self._verdict(self._send(
            'quotation.create', {'id': local, **self._corps_devis()},
            'dddd4444-0000-4000-8000-000000000030',
        ))
        self.assertEqual(verdict['server_ids']['quotation'], local)


class RetourTests(_BaseTest):
    def _vente_due(self):
        corps = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            # CRÉDIT : une vente comptant exige un règlement. Ici on veut
            # justement une facture DUE, pour que le retour ait une dette à
            # éteindre.
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.acheteur.id),
            'items': [{
                'product': str(self.produit.id),
                'quantity': '2',
                'unit_price': '1000.00',
            }],
            'payments': [],
        }
        reponse = self.client.post(
            '/api/v1/sales/', corps, format='json', **self._headers()
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        return Sale.objects.get(id=reponse.data['id'])

    def _creer_retour(self, vente, quantite, op_id):
        # Une ligne de retour DÉSIGNE la ligne de vente d'origine : sans elle,
        # rien ne dit ce qui est rendu, ni à quel prix il avait été vendu.
        ligne = vente.items.first()
        return self._verdict(self._send(
            'sale_return.create',
            {
                'original_sale': str(vente.id),
                'warehouse': str(self.warehouse.id),
                'reason': 'Article défectueux',
                'items': [{
                    'original_item': str(ligne.id),
                    'product': str(self.produit.id),
                    'quantity': quantite,
                    'unit_price': '1000.00',
                    'total': str(Decimal(quantite) * Decimal('1000.00')),
                }],
            },
            op_id,
        ))['server_ids']['sale_return']

    def test_approving_a_return_settles_the_debt_before_refunding_cash(self):
        """
        Un retour sur une facture encore due ÉTEINT D'ABORD la dette. Sans cet
        ordre, le client rendait le produit ET continuait de devoir la totalité,
        pendant qu'on lui remboursait en espèces de l'argent jamais encaissé.
        """
        vente = self._vente_due()
        self.assertGreater(vente.amount_due, Decimal('0.00'))

        retour = self._creer_retour(vente, '2', 'ffff4444-0000-4000-8000-000000000001')
        self._verdict(self._send(
            'sale_return.approve', {'sale_return': retour},
            'ffff4444-0000-4000-8000-000000000002',
        ))

        vente.refresh_from_db()
        self.assertEqual(vente.amount_due, Decimal('0.00'))
        self.assertEqual(SaleReturn.objects.get(id=retour).status, 'completed')

    def test_approving_twice_is_rejected(self):
        vente = self._vente_due()
        retour = self._creer_retour(vente, '1', 'ffff4444-0000-4000-8000-000000000010')
        self._verdict(self._send(
            'sale_return.approve', {'sale_return': retour},
            'ffff4444-0000-4000-8000-000000000011',
        ))
        self._verdict(self._send(
            'sale_return.approve', {'sale_return': retour},
            'ffff4444-0000-4000-8000-000000000012',
        ), attendu='rejected')

    def test_a_rejected_return_puts_nothing_back(self):
        vente = self._vente_due()
        avant = Stock.objects.get(
            product=self.produit, warehouse=self.warehouse,
        ).quantity
        retour = self._creer_retour(vente, '1', 'ffff4444-0000-4000-8000-000000000020')
        self._verdict(self._send(
            'sale_return.reject', {'sale_return': retour},
            'ffff4444-0000-4000-8000-000000000021',
        ))
        apres = Stock.objects.get(
            product=self.produit, warehouse=self.warehouse,
        ).quantity
        self.assertEqual(avant, apres)
        self.assertEqual(SaleReturn.objects.get(id=retour).status, 'rejected')


class RetourEnRolBorneTests(_BaseTest):
    """
    Le retour vu par un rôle BORNÉ, et non par le propriétaire.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ C'EST LE RÔLE QUI RÉVÈLE LE DÉFAUT, PAS LE SCÉNARIO.                     │
    │                                                                          │
    │ Toutes les vérifications du lot 11 ont été faites en propriétaire, pour  │
    │ qui `accessible_warehouse_ids` sort en amont. Sous un rôle borné, le     │
    │ handler refusait TOUT retour : `SaleReturnCreateSerializer` ne porte pas │
    │ `warehouse` dans ses champs (un retour n'a pas d'entrepôt à lui, il      │
    │ hérite de celui de sa vente), donc `validated_data.get('warehouse')`     │
    │ valait toujours `None`, et `assert_warehouse_allowed_for_request` répond │
    │ « Un entrepôt est requis pour votre compte » à quiconque n'est pas       │
    │ propriétaire.                                                            │
    │                                                                          │
    │ Le handler avait ajouté une règle que la VUE n'a pas : `SaleReturnViewSet`│
    │ n'appelle jamais cette assertion. Le périmètre est déjà vérifié, au bon  │
    │ endroit, sur la vente d'origine (`SaleReturnCreateSerializer.validate`). │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    def setUp(self):
        super().setUp()
        # Le gérant est le rôle le plus faible qui porte `sale_returns.create`.
        self.client.force_authenticate(user=self.manager)

    def _vente_due(self):
        corps = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.acheteur.id),
            'items': [{
                'product': str(self.produit.id),
                'quantity': '2',
                'unit_price': '1000.00',
            }],
            'payments': [],
        }
        reponse = self.client.post(
            '/api/v1/sales/', corps, format='json', **self._headers()
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)
        return Sale.objects.get(id=reponse.data['id'])

    def test_un_gerant_peut_creer_un_retour_depuis_son_terminal(self):
        vente = self._vente_due()
        ligne = vente.items.first()
        verdict = self._verdict(self._send(
            'sale_return.create',
            {
                'original_sale': str(vente.id),
                'reason': 'Article défectueux',
                'items': [{
                    'original_item': str(ligne.id),
                    'product': str(self.produit.id),
                    'quantity': '1',
                    'unit_price': '1000.00',
                    'total': '1000.00',
                }],
            },
            '66666666-6666-4666-8666-666666666666',
        ))
        self.assertEqual(SaleReturn.objects.count(), 1)
        self.assertIn('sale_return', verdict['server_ids'])
        # L'AUTEUR : le back-office le pose par `AuditMixin`, les handlers le
        # laissaient nul. « Qui a créé ce retour » est une question qu'on pose
        # toujours après coup, et à laquelle un champ vide ne répond pas.
        retour = SaleReturn.objects.get()
        self.assertEqual(retour.created_by_id, self.manager.id)

    def test_une_vente_hors_perimetre_reste_refusee(self):
        """
        La suppression du contrôle en trop ne doit pas ouvrir la porte.

        Le vrai périmètre vit dans le serializer, sur la vente d'ORIGINE : un
        retour sur une vente d'un entrepôt qu'on ne couvre pas reste refusé.
        """
        from apps.inventory.models import Warehouse

        vente = self._vente_due()
        ailleurs = Warehouse.objects.create(
            organization=self.org, name='Dépôt 2', code='D2',
        )
        Sale.objects.filter(id=vente.id).update(warehouse=ailleurs)

        ligne = vente.items.first()
        self._verdict(
            self._send(
                'sale_return.create',
                {
                    'original_sale': str(vente.id),
                    'reason': 'Article défectueux',
                    'items': [{
                        'original_item': str(ligne.id),
                        'product': str(self.produit.id),
                        'quantity': '1',
                        'unit_price': '1000.00',
                        'total': '1000.00',
                    }],
                },
                '77777777-7777-4777-8777-777777777777',
            ),
            'rejected',
        )
        self.assertEqual(SaleReturn.objects.count(), 0)
