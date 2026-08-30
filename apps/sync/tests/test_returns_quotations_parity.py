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
