"""
Tests de la cagnotte de fidélité : crédit et emploi sur facture.

Couvre les dysfonctionnements signalés en production :
- le cumul ne se faisait pas pour chaque client ;
- impossible de régler une facture, partiellement ou totalement, avec la cagnotte.

⚠ CE FICHIER A ÉTÉ PORTÉ, PAS RÉÉCRIT, lors de la bascule points → argent. Les
  montants y valent DIX FOIS les points d'avant, parce que le programme de la
  fixture était réglé à `point_value = 10` : une vente de 2 000 rapportait
  2 points, c'est-à-dire 20 en valeur, et c'est 20 que la cagnotte porte
  désormais. `earn_percent = 1 %` donne exactement le même argent que l'ancien
  barème « 1 point par 1 000, 1 point = 10 ».
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.contacts.models import Customer
from apps.inventory.models import Stock
from apps.products.models import Product
from apps.sales.models import Payment, RegisterSession, Sale, SaleReturn, SaleReturnItem
from apps.settings.models import (
    Currency, CustomerLoyalty, LoyaltyProgram, LoyaltyTransaction, OrganizationCurrency,
)
from apps.settings.services import LoyaltyService

from ._helpers import make_org_with_users, make_cash_payment_method


class _LoyaltyBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)
        self.payment_method = make_cash_payment_method(self.org)
        self.session = RegisterSession.objects.create(
            organization=self.org, register=self.register,
            opened_by=self.cashier_a, opening_balance=Decimal('0'),
            status='open',
        )
        self.product = Product.objects.create(
            organization=self.org, name='Produit', sku='P1',
            selling_price=Decimal('2000.00'), cost_price=Decimal('1500.00'),
            track_inventory=True, allow_negative_stock=False, is_active=True,
        )
        Stock.objects.create(
            organization=self.org, product=self.product, warehouse=self.warehouse,
            quantity=Decimal('100.000'), avg_cost=Decimal('1500.00'),
        )
        self.customer = Customer.objects.create(
            organization=self.org, name='Client', code='C1', phone='0900000000',
            credit_limit=Decimal('1000000'),
        )
        # 1 % de la facture reversé dans la cagnotte, en devise principale.
        self.program = LoyaltyProgram.objects.create(
            organization=self.org, is_active=True,
            earn_percent=Decimal('1.00'),
        )
        self.loyalty = CustomerLoyalty.objects.create(
            organization=self.org, customer=self.customer, balance=0,
        )

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _set_redemption_ceiling(self, percent):
        """
        Règle la part de facture réglable avec la cagnotte pour ce programme.

        Il n'existe pas de « sans plafond » : `MAX_REDEMPTION_PERCENT_CEILING`
        borne durement la configuration à 70 %, une facture garde toujours une
        part à encaisser en monnaie. Les tests qui veulent isoler un autre cap
        (le reste dû, le total) montent donc au maximum autorisé, pas à 100.
        """
        self.program.max_redemption_percent = Decimal(percent)
        self.program.save(update_fields=['max_redemption_percent'])

    def _create_sale(self, *, sale_type='credit', unit_price='2000.00', payments=None):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': sale_type,
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': payments or [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp.data['id']


class AccumulationTests(_LoyaltyBaseTest):
    """Le cumul doit se faire pour chaque client, dans toutes les devises."""

    def test_completed_sale_credits_the_wallet(self):
        self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 20)
        self.assertEqual(self.loyalty.total_earned, 20)

    def test_credit_sale_settled_from_customer_page_credits_the_wallet(self):
        """
        La cause racine du « pas de cumul » : une vente à crédit soldée depuis
        la fiche client ne passait jamais `completed`, donc ne créditait rien.
        Les deux bugs n'en faisaient qu'un.
        """
        sale_id = self._create_sale()
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 0, "Rien avant le règlement.")

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            {'amount': '2000.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.assertEqual(Sale.objects.get(pk=sale_id).status, 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 20)

    def test_le_credit_s_accumule_de_vente_en_vente(self):
        for _ in range(3):
            self._create_sale(sale_type='retail', payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '2000.00',
            }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 60)

    def test_award_is_idempotent_per_sale(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        sale = Sale.objects.get(pk=sale_id)

        LoyaltyService.award_for_sale(sale, user=self.cashier_a)
        LoyaltyService.award_for_sale(sale, user=self.cashier_a)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 20)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=LoyaltyTransaction.TransactionType.EARN,
            ).count(), 1,
        )


class _MultiCurrencyLoyaltyTest(_LoyaltyBaseTest):
    """Organisation en CDF principal, avec l'USD activé à 2 800."""

    def setUp(self):
        super().setUp()
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC'},
        )
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'US Dollar', 'symbol': '$'},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1'), 'is_active': True},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=usd,
            defaults={'is_primary': False, 'exchange_rate': Decimal('2800'), 'is_active': True},
        )


class SecondaryCurrencyAccumulationTests(_MultiCurrencyLoyaltyTest):
    """Le barème est en devise principale : une facture en USD doit être convertie."""

    def test_usd_invoice_awards_a_converted_credit(self):
        """
        50 USD = 140 000 CDF ⇒ 1 400 de cagnotte. L'ancien code comparait 50 à
        un barème de 1 000 CDF et n'attribuait rien.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '50.00',
                'currency': 'USD',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 1400)


class InvoiceRedemptionTests(_LoyaltyBaseTest):
    """Régler une facture émise avec la cagnotte, partiellement ou totalement."""

    def setUp(self):
        super().setUp()
        self.loyalty.balance = 5000
        self.loyalty.save(update_fields=['balance'])

    def test_la_cagnotte_regle_une_partie_de_la_facture(self):
        """500 de cagnotte imputés sur une facture de 2 000."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 500}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'partially_paid')
        self.assertEqual(sale.amount_paid, Decimal('500.00'))
        self.assertEqual(sale.amount_due, Decimal('1500.00'))
        # Le total de la facture émise ne bouge pas.
        self.assertEqual(sale.total, Decimal('2000.00'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 4500)

    def test_la_cagnotte_ne_solde_jamais_toute_la_facture(self):
        """
        Même au plafond maximal, une facture garde une part à encaisser.

        Le client a de quoi couvrir les 2 000 avec sa cagnotte : seuls 1 400
        passent, la borne dure de 70 % retenant le reste.
        """
        self._set_redemption_ceiling('70.00')
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 2000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1400.00'))
        self.assertEqual(sale.amount_due, Decimal('600.00'))
        self.assertEqual(sale.status, 'partially_paid')
        self.loyalty.refresh_from_db()
        # 1 400 consommés ; la vente n'étant pas soldée, rien n'est gagné.
        self.assertEqual(self.loyalty.balance, 3600)

    def test_la_cagnotte_est_bornee_par_le_reste_du(self):
        """
        On ne peut pas payer plus que ce qui reste dû.

        Il faut d'abord encaisser en monnaie pour que le reste dû (400) passe
        SOUS l'enveloppe de cagnotte (1 400) : sinon c'est le plafond qui mord
        et ce test ne mesurerait plus rien.
        """
        self._set_redemption_ceiling('70.00')
        sale_id = self._create_sale(payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '1600.00',
        }])

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 5000}, format='json', **self._headers(),
        )

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.assertEqual(sale.status, 'completed')
        self.loyalty.refresh_from_db()
        # 400 consommés seulement, + 20 gagnés à la clôture.
        self.assertEqual(self.loyalty.balance, 4620)

    def test_deux_reglements_successifs_en_cagnotte_sont_permis(self):
        """Une facture peut être réglée avec la cagnotte en plusieurs fois."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        for _ in range(2):
            resp = self.client.post(
                f'/api/v1/sales/{sale_id}/add-payment/',
                {'loyalty_amount_used': 500}, format='json', **self._headers(),
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 4000)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                sale=sale, transaction_type=LoyaltyTransaction.TransactionType.REDEEM,
            ).count(), 2,
        )

    def test_un_reglement_en_cagnotte_ne_touche_pas_le_tiroir(self):
        """La cagnotte n'est pas de l'argent physique : rien au tiroir."""
        from apps.cashbook.models import CashMovement

        sale_id = self._create_sale()
        before = CashMovement.objects.filter(organization=self.org).count()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 500}, format='json', **self._headers(),
        )

        self.assertEqual(CashMovement.objects.filter(organization=self.org).count(), before)
        self.assertTrue(
            Payment.objects.filter(
                sale_id=sale_id, payment_method__method_type='loyalty',
            ).exists()
        )

    def test_un_reglement_en_cagnotte_reduit_la_dette_du_client(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 500}, format='json', **self._headers(),
        )

        from apps.contacts import services as contacts_services
        self.assertEqual(
            contacts_services.get_balance(self.customer, 'CDF'), Decimal('1500.00'),
        )

    def test_une_petite_part_de_cagnotte_est_acceptee(self):
        """
        ⚠ CE TEST AFFIRMAIT L'INVERSE : il exigeait un refus sous
        `min_points_to_redeem`. Ce minimum existait parce qu'un point n'avait
        pas de valeur lisible ; un montant en a une, et refuser 50 FC de remise
        « parce que c'est trop peu » n'a aucun sens au comptoir. Le minimum a
        disparu avec les points.
        """
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 50}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(Sale.objects.get(pk=sale_id).amount_paid, Decimal('50.00'))


class DebtRedemptionTests(_LoyaltyBaseTest):
    """Employer la cagnotte sur la dette globale du client."""

    def setUp(self):
        super().setUp()
        self.loyalty.balance = 5000
        self.loyalty.save(update_fields=['balance'])

    def test_la_cagnotte_eponge_la_dette_globale(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/use-loyalty/',
            {'amount': 1000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 4000)

    def test_customer_without_debt_is_rejected(self):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/use-loyalty/',
            {'amount': 1000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ReversalCounterTests(_LoyaltyBaseTest):
    """Les compteurs à vie doivent être corrigés du bon côté."""

    def test_annuler_reprend_le_gain_sans_gonfler_les_compteurs(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.total_earned, 20)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 0)
        self.assertEqual(self.loyalty.total_earned, 0)

    def test_annuler_une_utilisation_corrige_le_bon_compteur(self):
        """
        Annuler une utilisation doit réduire `total_used`, pas gonfler
        `total_earned` comme le faisait l'ancien code.
        """
        self.loyalty.balance = 5000
        self.loyalty.save(update_fields=['balance'])

        sale_id = self._create_sale()
        self.client.force_authenticate(user=self.cashier_a)
        self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 500}, format='json', **self._headers(),
        )
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.total_used, 500)
        earned_before = self.loyalty.total_earned

        self.client.force_authenticate(user=self.manager)
        self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 5000)
        self.assertEqual(self.loyalty.total_used, 0)
        self.assertEqual(
            self.loyalty.total_earned, earned_before,
            "Annuler une utilisation ne doit pas gonfler la cagnotte gagnée.",
        )


class ReturnReversalTests(_LoyaltyBaseTest):
    """Un retour total doit reprendre la cagnotte, comme une annulation."""

    def test_un_retour_total_reprend_la_cagnotte(self):
        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])
        sale = Sale.objects.get(pk=sale_id)
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 20)

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post('/api/v1/sale-returns/', {
            'original_sale': str(sale.id),
            'return_type': 'full',
            'reason': 'Produit abîmé',
            'items': [{
                'original_item': str(sale.items.first().id),
                'quantity': '1',
                'unit_price': '2000.00',
                'total': '2000.00',
                'restock': True,
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        sale_return = SaleReturn.objects.get(original_sale=sale)
        resp = self.client.post(
            f'/api/v1/sale-returns/{sale_return.id}/approve/',
            {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 0)


class ReceiptExposureTests(_LoyaltyBaseTest):
    """
    Ce que le reçu doit lire. Le POS rejouait le barème lui-même, et annonçait
    dix fois ce qui était réellement crédité. Le serveur est la seule source.
    """

    def _detail(self, sale_id):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(f'/api/v1/sales/{sale_id}/', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_le_gain_correspond_au_registre(self):
        sale_id = self._create_sale(sale_type='retail', unit_price='5000.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5000.00',
        }])

        data = self._detail(sale_id)
        self.loyalty.refresh_from_db()

        # 1 % de 5 000 = 50. ⚠ LE MONTANT VOYAGE EN CHAÎNE, comme tout montant
        # de la plateforme : c'est `_money_out` qui le fixe, et le comparer à un
        # nombre passerait pour une égalité là où le type a changé.
        self.assertEqual(data['loyalty_earned'], '50.00')
        self.assertEqual(Decimal(data['loyalty_earned']), self.loyalty.total_earned)
        self.assertEqual(Decimal(data['loyalty_balance']), self.loyalty.balance)
        self.assertTrue(data['loyalty_program_active'])

    def test_le_pourcentage_est_continu(self):
        """
        ⚠ CE TEST OPPOSAIT LE POURCENTAGE AU BARÈME PAR TRANCHES. Le barème a
        disparu : il ne reste qu'un pourcentage, et il est CONTINU - une facture
        de 5 500 rapporte 55, pas 50. C'était l'intérêt du pourcentage, et c'est
        désormais la seule règle.
        """
        sale_id = self._create_sale(sale_type='retail', unit_price='5500.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5500.00',
        }])

        self.loyalty.refresh_from_db()
        self.assertEqual(self._detail(sale_id)['loyalty_earned'], '55.00')
        self.assertEqual(self.loyalty.balance, Decimal('55.00'))

    def test_la_cagnotte_utilisee_est_exposee(self):
        self.loyalty.balance = 1000
        self.loyalty.total_earned = 1000
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'loyalty_amount_used': 500,
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '1500.00',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 500 de cagnotte en remise sur 2 000.
        self.assertEqual(Decimal(resp.data['loyalty_redemption_amount']), Decimal('500.00'))
        self.assertEqual(resp.data['loyalty_used'], '500.00')

        data = self._detail(resp.data['id'])
        self.assertEqual(data['loyalty_used'], '500.00')
        # La fiche de vente et le reçu réimprimé isolent la remise fidélité de
        # la remise commerciale : sans ce montant sur le détail, la déduction
        # se fondrait dans `discount_amount` et disparaîtrait de la facture.
        self.assertEqual(
            Decimal(data['loyalty_redemption_amount']), Decimal('500.00')
        )
        self.assertEqual(Decimal(data['discount_amount']), Decimal('500.00'))

    def test_la_remise_fidelite_se_distingue_de_la_remise_commerciale(self):
        """
        `discount_amount` cumule les deux : la facture doit pouvoir les séparer.

        Sans `loyalty_redemption_amount` exposé à part, l'écran et le reçu
        afficheraient « Remise 800 » sans dire que 500 viennent de la cagnotte.
        """
        self.loyalty.balance = 1000
        self.loyalty.total_earned = 1000
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'global_discount_amount': '300.00',
            'loyalty_amount_used': 500,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        data = self._detail(resp.data['id'])
        loyalty = Decimal(data['loyalty_redemption_amount'])
        total_discount = Decimal(data['discount_amount'])
        self.assertEqual(loyalty, Decimal('500.00'))
        self.assertEqual(total_discount, Decimal('800.00'))
        # Ce que la facture affiche sur la ligne « Remise » commerciale.
        self.assertEqual(total_discount - loyalty, Decimal('300.00'))
        self.assertEqual(Decimal(data['total']), Decimal('1200.00'))

    def test_annulation_remet_les_compteurs_du_recu_a_zero(self):
        sale_id = self._create_sale(sale_type='retail', unit_price='5000.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '5000.00',
        }])
        self.assertEqual(self._detail(sale_id)['loyalty_earned'], '50.00')

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/',
            {'reason': 'test'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        # Un reçu réimprimé après annulation ne doit plus annoncer de gain.
        self.assertEqual(self._detail(sale_id)['loyalty_earned'], '0.00')

    def test_sans_programme_actif_le_recu_le_sait(self):
        self.program.is_active = False
        self.program.save()

        sale_id = self._create_sale(sale_type='retail', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '2000.00',
        }])

        data = self._detail(sale_id)
        self.assertFalse(data['loyalty_program_active'])
        self.assertEqual(data['loyalty_earned'], '0.00')


class CustomerLoyaltyEndpointTests(_LoyaltyBaseTest):
    """
    Le POS lit le solde via `?customer=`. L'endpoint était paginé alors que le
    client attendait un tableau : il recevait donc toujours `null`, ce qui
    masquait tout le panneau fidélité et affichait un solde nul sur la fiche.
    """

    def test_reponse_non_paginee(self):
        self.loyalty.balance = 42
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(
            f'/api/v1/settings/customer-loyalty/?customer={self.customer.id}',
            **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertIsInstance(resp.data, list)
        self.assertEqual(len(resp.data), 1)
        self.assertEqual(Decimal(resp.data[0]['balance']), Decimal('42.00'))


class TenderCurrencyIndependenceTests(_MultiCurrencyLoyaltyTest):
    """
    La cagnotte se calcule sur la FACTURE, jamais sur ce qui est remis.

    Le montant perçu peut être encaissé dans une autre devise (une facture en
    CDF réglée en dollars, la monnaie rendue dans une troisième). Rien de tout
    cela ne change ce que le client a acheté : le pourcentage s'applique au
    total de la facture, ramené en devise principale.
    """

    def _sell(self, *, unit_price, payments, currency=None, change_currency=None):
        self.client.force_authenticate(user=self.cashier_a)
        payload = {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': payments,
        }
        if currency:
            payload['currency'] = currency
        if change_currency:
            payload['change_currency'] = change_currency
        resp = self.client.post('/api/v1/sales/', payload, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp

    def test_facture_en_principale_reglee_en_devise_etrangere(self):
        """2 800 CDF encaissés en 1 USD : 28 de cagnotte, comme en CDF."""
        resp = self._sell(unit_price='2800.00', payments=[{
            'payment_method': str(self.payment_method.id),
            'amount': '1.00',
            'currency': 'USD',
        }])

        self.assertEqual(resp.data['currency'], 'CDF')
        self.assertEqual(Decimal(resp.data['total']), Decimal('2800.00'))
        self.assertEqual(resp.data['status'], 'completed')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 28)
        self.assertEqual(resp.data['loyalty_earned'], '28.00')

    def test_reglement_fractionne_deux_devises(self):
        """1 400 CDF + 0,5 USD sur une facture de 2 800 CDF : toujours 28 de cagnotte."""
        resp = self._sell(unit_price='2800.00', payments=[
            {'payment_method': str(self.payment_method.id), 'amount': '1400.00'},
            {
                'payment_method': str(self.payment_method.id),
                'amount': '0.50',
                'currency': 'USD',
            },
        ])

        self.assertEqual(resp.data['status'], 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 28)

    def test_monnaie_rendue_en_devise_etrangere_ne_change_rien(self):
        """
        Surpaiement rendu en USD : la monnaie sort du tiroir, elle ne retire
        aucun point. Le client a bien acheté pour 2 800 CDF.
        """
        resp = self._sell(
            unit_price='2800.00',
            payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '5000.00',
            }],
            change_currency='USD',
        )

        self.assertEqual(resp.data['change_currency'], 'USD')
        self.assertGreater(Decimal(resp.data['change_amount']), Decimal('0'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 28)

    def test_facture_a_credit_soldee_plus_tard_en_devise_etrangere(self):
        """
        Le règlement différé emprunte le même chemin : la facture reste
        l'assiette, quelle que soit la devise du versement.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2800.00',
            }],
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale_id = resp.data['id']

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 0, "Rien avant le règlement.")

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {
                'payment_method': str(self.payment_method.id),
                'amount': '1.00',
                'currency': 'USD',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(resp.data['status'], 'completed')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 28)
        self.assertEqual(resp.data['loyalty_earned'], '28.00')

    def test_facture_en_usd_reglee_en_cdf(self):
        """
        Symétrique du cas précédent : facture de 50 USD réglée en 140 000 CDF.
        L'assiette reste la facture ramenée en principale, soit 1 400.
        """
        resp = self._sell(
            unit_price='50.00',
            currency='USD',
            payments=[{
                'payment_method': str(self.payment_method.id),
                'amount': '140000.00',
                'currency': 'CDF',
            }],
        )

        self.assertEqual(resp.data['currency'], 'USD')
        self.assertEqual(resp.data['status'], 'completed')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 1400)


class RedemptionCurrencyTests(_MultiCurrencyLoyaltyTest):
    """
    L'emploi de la cagnotte traverse les devises sans se perdre.

    La cagnotte est libellée en devise principale ; sur une facture en devise
    secondaire, la remise doit être reconvertie. Le POS restreignait
    l'utilisation aux factures en principale : la cagnotte disparaissait en
    silence, sans message au caissier.
    """

    def test_remise_convertie_dans_la_devise_de_facture(self):
        self.loyalty.balance = 2800
        self.loyalty.total_earned = 2800
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            # 2 800 CDF de cagnotte = 1 USD de remise.
            'loyalty_amount_used': 2800,
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '49.00',
                'currency': 'USD',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('49.00'))
        self.assertEqual(resp.data['status'], 'completed')
        self.assertEqual(resp.data['loyalty_used'], '2800.00')

        self.loyalty.refresh_from_db()
        # 2 800 consommés, puis 49 USD = 137 200 CDF ⇒ 1 372 de gain.
        self.assertEqual(self.loyalty.balance, 1372)


class RedemptionCeilingTests(_LoyaltyBaseTest):
    """
    La cagnotte ne solde jamais toute une facture.

    `max_redemption_percent` borne la part réglable pour qu'il reste
    toujours un montant à encaisser en monnaie. Le plafond porte sur le TOTAL de
    la facture et non sur le reste à payer : sinon des règlements successifs
    grignoteraient la part autorisée sans jamais la franchir en apparence.
    """

    def setUp(self):
        super().setUp()
        # De quoi solder trois fois la facture si rien ne l'en empêchait.
        self.loyalty.balance = 10000
        self.loyalty.save(update_fields=['balance'])

    def test_remise_a_la_creation_plafonnee_a_la_moitie(self):
        """Facture 2 000, plafond 50 % : au plus 1 000 de remise fidélité."""
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'loyalty_amount_used': 10000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1000.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('1000.00'))
        # Il reste à encaisser : c'est tout l'objet du plafond.
        self.assertGreater(Decimal(resp.data['amount_due']), Decimal('0.00'))

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 9000)

    def test_au_plafond_maximal_il_reste_toujours_a_encaisser(self):
        """
        Même réglé au maximum autorisé, le plafond laisse 30 % à payer.

        La borne dure ne se desserre par aucune configuration : c'est la
        garantie qui remplace l'ancien « 100 % = pas de plafond ».
        """
        self._set_redemption_ceiling('70.00')

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'loyalty_amount_used': 10000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('1400.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('600.00'))
        self.assertEqual(Decimal(resp.data['amount_due']), Decimal('600.00'))

    def test_une_valeur_au_dela_de_la_borne_est_ramenee(self):
        """
        Une ligne écrite hors validation ne desserre pas le plafond.

        Le validator ne protège que les écritures API ; `max_redeemable_amount`
        re-borne, sinon un `UPDATE` direct ou une vieille fixture rendrait la
        garantie caduque.
        """
        LoyaltyProgram.objects.filter(pk=self.program.pk).update(
            max_redemption_percent=Decimal('90.00'),
        )
        self.program.refresh_from_db()

        self.assertEqual(
            self.program.max_redeemable_amount(Decimal('2000.00')),
            Decimal('1400.00'),
        )

    def test_l_api_refuse_un_plafond_au_dela_de_la_borne(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.patch(
            f'/api/v1/settings/loyalty-program/{self.program.id}/',
            {'max_redemption_percent': '90.00'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn('max_redemption_percent', resp.data)

    def test_reglement_en_cagnotte_sur_facture_emise_est_plafonne(self):
        """Même règle depuis l'écran des règlements, sinon le POS se contourne."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 10000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))
        self.assertEqual(sale.amount_due, Decimal('1000.00'))
        self.assertEqual(sale.status, 'partially_paid')

    def test_la_remise_de_creation_consomme_l_enveloppe_du_reglement(self):
        """
        Une facture déjà réduite de 50 % n'accepte plus de cagnotte.

        C'est le contournement que le plafond doit fermer : créer la vente avec
        la remise maximale, puis solder le reste en cagnotte depuis la facture.
        """
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '2000.00',
            }],
            'loyalty_amount_used': 10000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        sale_id = resp.data['id']

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 1000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.loyalty.refresh_from_db()
        # Seuls les 1 000 de la remise de création ont été consommés.
        self.assertEqual(self.loyalty.balance, 9000)

    def test_reglements_successifs_ne_depassent_pas_le_plafond(self):
        """Deux fois 500 passent (= 50 %), le troisième est refusé."""
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.cashier_a)
        for _ in range(2):
            resp = self.client.post(
                f'/api/v1/sales/{sale_id}/add-payment/',
                {'loyalty_amount_used': 500}, format='json', **self._headers(),
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'loyalty_amount_used': 500}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('1000.00'))

    def test_le_plafond_ne_touche_pas_l_epongement_de_dette(self):
        """
        La dette globale n'est pas une facture : le plafond n'y a pas cours.

        Un client qui a déjà consommé sa marchandise peut solder ce qu'il doit
        avec sa cagnotte ; il n'y a plus rien à encaisser en échange.
        """
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/use-loyalty/',
            {'amount': 2000}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_paid, Decimal('2000.00'))
        self.assertEqual(sale.amount_due, Decimal('0.00'))


class SecondaryCurrencyCeilingTests(_MultiCurrencyLoyaltyTest):
    """Le plafond se compose avec la conversion de devise sans dériver."""

    def test_plafond_applique_dans_la_devise_de_facture(self):
        self.loyalty.balance = 100000
        self.loyalty.total_earned = 100000
        self.loyalty.save()

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'loyalty_amount_used': 100000,
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 50 % de 50 USD = 25 USD = 70 000 CDF de cagnotte.
        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('25.00')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('25.00'))
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, 30000)


class DegenerateExchangeRateTests(_MultiCurrencyLoyaltyTest):
    """
    Un taux nul sur la vente ne doit pas faire disparaître la cagnotte en silence.

    `Sale.save()` passe par `CurrencyService.resolve` et ne peut pas en produire,
    mais une reprise de données ou une écriture SQL directe le peut. Multiplier
    par zéro ne créditerait rien, sans aucun signal.
    """

    def test_taux_nul_retombe_sur_le_taux_de_l_organisation(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'credit',
            'is_pos': True,
            'customer': str(self.customer.id),
            'currency': 'USD',
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'payments': [],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        sale = Sale.objects.get(pk=resp.data['id'])
        # Écriture directe : on contourne `save()`, comme le ferait une reprise.
        Sale.objects.filter(pk=sale.pk).update(exchange_rate=Decimal('0'))
        sale.refresh_from_db()

        credit = LoyaltyService.credit_for_sale(sale, self.program)

        self.assertEqual(
            credit, 1400, "50 USD valent 140 000 CDF, soit 1 400 de cagnotte.",
        )


class FractionalCreditTests(_MultiCurrencyLoyaltyTest):
    """
    Les centimes de cagnotte ne se perdent pas.

    Cas réel rencontré en production : organisation en USD principal, barème à
    1 %. Une vente de 58 USD valait 0,58 point, tronqué à zéro ; une vente de
    396 USD en valait 3,96, tronqué à 3. Le compteur d'un marchand en devise
    forte ne bougeait donc presque jamais.

    ⚠ LE DÉFAUT NE DISPARAÎT PAS AVEC LES POINTS : 1 % de 58 $ vaut 0,58 $, et
    une cagnotte tronquée à l'unité ne bougerait pas davantage. Ces tests
    gardent donc tout leur sens, au centime près.
    """

    def setUp(self):
        super().setUp()
        # Devise principale USD, barème à 1 % - la configuration du terrain.
        self.org.currency = 'USD'
        self.org.save(update_fields=['currency'])
        OrganizationCurrency.objects.filter(organization=self.org).update(is_primary=False)
        OrganizationCurrency.objects.filter(
            organization=self.org, currency__code='USD',
        ).update(is_primary=True, exchange_rate=Decimal('1'))
        OrganizationCurrency.objects.filter(
            organization=self.org, currency__code='CDF',
        ).update(exchange_rate=Decimal('0.000434782609'))

        from django.core.cache import cache
        cache.clear()

        # 1 % de la facture, en USD : la cagnotte y vaut des dollars.
        self.program.earn_percent = Decimal('1.00')
        self.program.save()

    def _sell(self, unit_price):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': unit_price,
            }],
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': unit_price,
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)
        return resp

    def test_58_usd_credite_zero_virgule_cinquante_huit(self):
        resp = self._sell('58.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, Decimal('0.58'))
        self.assertEqual(resp.data['loyalty_earned'], '0.58')

    def test_396_usd_ne_perd_plus_la_fraction(self):
        resp = self._sell('396.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, Decimal('3.96'))
        self.assertEqual(resp.data['loyalty_earned'], '3.96')

    def test_les_fractions_s_additionnent(self):
        for _ in range(4):
            self._sell('58.00')

        self.loyalty.refresh_from_db()
        # 4 x 0,58 = 2,32 - là où l'ancien calcul laissait le compteur à zéro.
        self.assertEqual(self.loyalty.balance, Decimal('2.32'))
        self.assertEqual(self.loyalty.total_earned, Decimal('2.32'))

    def test_un_montant_voyage_toujours_en_chaine_a_deux_decimales(self):
        """
        ⚠ CE TEST EXIGEAIT UN `int`. C'était la convention de `_points_out` :
        « un solde entier reste un entier à l'écran, 3 pts et non 3.0 pts ».
        La cagnotte est de l'argent, et tout montant de la plateforme voyage en
        CHAÎNE à deux décimales - un solde en CDF à sept chiffres perd ses
        unités dans un nombre JavaScript.
        """
        resp = self._sell('100.00')

        self.assertEqual(resp.data['loyalty_earned'], '1.00')
        self.assertIsInstance(resp.data['loyalty_earned'], str)

    def test_une_cagnotte_fractionnaire_est_utilisable(self):
        """Ce qui est gagné doit pouvoir être dépensé, centimes compris."""
        self._sell('396.00')
        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, Decimal('3.96'))

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post('/api/v1/sales/', {
            'register': str(self.register.id),
            'warehouse': str(self.warehouse.id),
            'sale_type': 'retail',
            'is_pos': True,
            'customer': str(self.customer.id),
            'items': [{
                'product': str(self.product.id),
                'quantity': '1',
                'unit_price': '50.00',
            }],
            'loyalty_amount_used': '3.96',
            'payments': [{
                'payment_method': str(self.payment_method.id),
                'amount': '46.04',
            }],
        }, format='json', **self._headers())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # 3,96 USD de cagnotte en remise sur 50.
        self.assertEqual(
            Decimal(resp.data['loyalty_redemption_amount']), Decimal('3.96')
        )
        self.assertEqual(Decimal(resp.data['total']), Decimal('46.04'))
        self.assertEqual(resp.data['loyalty_used'], '3.96')

    def test_le_pourcentage_ne_tronque_aucune_tranche(self):
        """
        ⚠ CE TEST AFFIRMAIT L'INVERSE : il gardait les PALIERS du barème
        `FIXED_PER_AMOUNT` (« X points pour CHAQUE Y dépensé », une tranche
        entamée ne comptant pas). Ce barème a disparu avec les points - c'était
        le réglage dont le marchand ne pouvait pas prévoir l'effet, et celui
        qui faisait qu'un petit panier ne rapportait rien.

        Le pourcentage est CONTINU : 1 % de 25 USD vaut 0,25 USD, et non zéro.
        """
        self._sell('25.00')

        self.loyalty.refresh_from_db()
        self.assertEqual(self.loyalty.balance, Decimal('0.25'))
