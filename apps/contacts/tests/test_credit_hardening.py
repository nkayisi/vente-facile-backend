"""
Tests des garde-fous de la vente à crédit.

Ils couvrent les chemins qui contournaient le service de dette
(``apps.contacts.services``, seul point d'écriture du solde) et qui laissaient
donc le solde du client diverger de la somme de ses factures ouvertes :

- la limite de crédit n'était jamais franchie en test, sa branche n'était pas
  exercée ;
- rien n'interdisait le crédit à un client donné ;
- un devis converti créait une facture due sans inscrire la dette, mais son
  règlement la décrémentait quand même : le client devenait créditeur ;
- un retour de marchandise laissait la dette entière ;
- une avance n'était jamais consommée par la vente suivante ;
- la suppression et le PATCH d'une vente pouvaient effacer une dette sans
  toucher le solde.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework import status

from apps.contacts import services as contacts_services
from apps.contacts.models import CustomerTransaction
from apps.sales.models import PaymentMethod, Quotation, QuotationItem, Sale, SaleReturn
from apps.settings.models import Currency, OrganizationCurrency

from .test_debt_flows import _DebtBaseTest


class _CreditBaseTest(_DebtBaseTest):
    """Ajoute l'invariant central, à revérifier après chaque scénario."""

    def assert_debt_matches_open_invoices(self, currency='CDF'):
        open_due = sum(
            (s.amount_due for s in Sale.objects.filter(
                customer=self.customer,
                currency=currency,
                status__in=['pending', 'partially_paid'],
            )),
            Decimal('0.00'),
        )
        balance = self._balance(currency)
        # Un solde négatif est une avance : aucune facture ne la porte.
        self.assertEqual(max(balance, Decimal('0.00')), open_due)


class CreditLimitTests(_CreditBaseTest):
    """La branche de dépassement de limite, jamais exercée jusqu'ici."""

    def test_sale_beyond_credit_limit_is_rejected(self):
        self.customer.credit_limit = Decimal('1500.00')
        self.customer.save(update_fields=['credit_limit'])

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            {
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
                'payments': [],
            },
            format='json', **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assertFalse(Sale.objects.filter(customer=self.customer).exists())

    def test_zero_credit_limit_means_unlimited(self):
        """0 = pas de plafond. L'interdiction passe par `allow_credit`."""
        self.customer.credit_limit = Decimal('0.00')
        self.customer.save(update_fields=['credit_limit'])

        self._create_sale()

        self.assertEqual(self._balance(), Decimal('2000.00'))


class AllowCreditTests(_CreditBaseTest):
    """Autorisation de crédit, indépendante du plafond."""

    def test_customer_without_allow_credit_is_rejected(self):
        self.customer.allow_credit = False
        self.customer.save(update_fields=['allow_credit'])

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            {
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
                'payments': [],
            },
            format='json', **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertEqual(self._balance(), Decimal('0.00'))

    def test_new_customers_may_buy_on_credit_by_default(self):
        self.assertTrue(self.customer.allow_credit)
        self._create_sale()
        self.assertEqual(self._balance(), Decimal('2000.00'))

    def test_partially_paid_retail_sale_also_needs_the_authorisation(self):
        """Un reste à payer est une dette, quel que soit le `sale_type`."""
        self.customer.allow_credit = False
        self.customer.save(update_fields=['allow_credit'])

        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/sales/',
            {
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
                'payments': [{
                    'payment_method': str(self.payment_method.id),
                    'amount': '800.00',
                }],
            },
            format='json', **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertEqual(self._balance(), Decimal('0.00'))


class QuotationConversionTests(_CreditBaseTest):
    """Un devis converti est une facture due : elle doit porter une dette."""

    def _convert_quotation(self, total='2000.00'):
        quotation = Quotation.objects.create(
            organization=self.org,
            reference='DEV-1',
            customer=self.customer,
            status='sent',
            subtotal=Decimal(total),
            total=Decimal(total),
            valid_until=(timezone.now() + timedelta(days=30)).date(),
            created_by=self.manager,
        )
        QuotationItem.objects.create(
            quotation=quotation,
            organization=self.org,
            product=self.product,
            quantity=Decimal('1.000'),
            unit_price=Decimal(total),
            total=Decimal(total),
        )
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/quotations/{quotation.id}/convert/',
            {'warehouse': str(self.warehouse.id)},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data['sale_id']

    def test_conversion_records_the_debt(self):
        self._convert_quotation()

        self.assertEqual(self._balance(), Decimal('2000.00'))
        self.assert_debt_matches_open_invoices()

    def test_settling_a_converted_quotation_does_not_credit_the_customer(self):
        """Le bug de crédit fantôme : le solde ne doit pas passer sous zéro."""
        sale_id = self._convert_quotation()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/add-payment/',
            {'payment_method': str(self.payment_method.id), 'amount': '2000.00'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assert_debt_matches_open_invoices()


class AdvanceConsumptionTests(_CreditBaseTest):
    """Une avance doit éteindre la facture suivante, pas seulement le solde."""

    def _record_advance(self, amount='3000.00'):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-advance/',
            {'amount': amount}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

    def test_advance_settles_the_next_invoice(self):
        self._record_advance('3000.00')
        sale_id = self._create_sale()

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(sale.status, 'completed')
        # 3000 d'avance - 2000 de facture = 1000 encore créditeur.
        self.assertEqual(self._balance(), Decimal('-1000.00'))
        self.assert_debt_matches_open_invoices()

    def test_partial_advance_leaves_the_remainder_due(self):
        self._record_advance('1200.00')
        sale_id = self._create_sale()

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_due, Decimal('800.00'))
        self.assertEqual(sale.status, 'partially_paid')
        self.assertEqual(self._balance(), Decimal('800.00'))
        self.assert_debt_matches_open_invoices()

    def test_consuming_an_advance_is_not_a_new_cash_movement(self):
        """L'argent est entré au tiroir quand l'avance a été enregistrée."""
        from apps.cashbook.models import CashMovement

        self._record_advance('3000.00')
        movements_before = CashMovement.objects.filter(organization=self.org).count()

        self._create_sale()

        self.assertEqual(
            CashMovement.objects.filter(organization=self.org).count(),
            movements_before,
        )


class ReturnDebtTests(_CreditBaseTest):
    """Un retour sur facture due éteint la dette avant de sortir de l'argent."""

    def _approve_return(self, sale_id):
        original_item = Sale.objects.get(pk=sale_id).items.first()
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            '/api/v1/sale-returns/',
            {
                'original_sale': str(sale_id),
                'return_type': 'full',
                'reason': 'Marchandise défectueuse',
                'items': [{
                    'original_item': str(original_item.id),
                    'quantity': '1',
                    'unit_price': '2000.00',
                    'total': '2000.00',
                }],
            },
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        # Le serializer de création n'expose pas `id` : on relit le retour, dont
        # `refund_amount` est calculé côté serveur depuis les lignes.
        sale_return = SaleReturn.objects.get(original_sale_id=sale_id)
        self.assertEqual(sale_return.refund_amount, Decimal('2000.00'))

        resp = self.client.post(
            f'/api/v1/sale-returns/{sale_return.id}/approve/',
            {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return sale_return

    def test_return_clears_the_outstanding_debt(self):
        sale_id = self._create_sale()
        self.assertEqual(self._balance(), Decimal('2000.00'))

        self._approve_return(sale_id)

        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assertEqual(Sale.objects.get(pk=sale_id).amount_due, Decimal('0.00'))
        self.assert_debt_matches_open_invoices()
        self.assertTrue(
            CustomerTransaction.objects.filter(
                customer=self.customer,
                transaction_type=CustomerTransaction.TransactionType.REFUND,
            ).exists()
        )

    def test_return_on_a_due_invoice_does_not_refund_cash(self):
        """Le marchand n'a rien encaissé : il n'a rien à rendre en espèces."""
        from apps.cashbook.models import CashMovement

        sale_id = self._create_sale()
        movements_before = CashMovement.objects.filter(organization=self.org).count()

        self._approve_return(sale_id)

        self.assertEqual(
            CashMovement.objects.filter(organization=self.org).count(),
            movements_before,
        )


class SaleMutationGuardTests(_CreditBaseTest):
    """Ni la suppression ni le PATCH ne doivent pouvoir effacer une dette."""

    def test_deleting_a_sale_with_outstanding_debt_is_refused(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.delete(
            f'/api/v1/sales/{sale_id}/', **self._headers(),
        )

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertEqual(self._balance(), Decimal('2000.00'))
        self.assert_debt_matches_open_invoices()

    def test_patch_cannot_rewrite_amounts_or_status(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.patch(
            f'/api/v1/sales/{sale_id}/',
            {
                'amount_due': '0.00',
                'amount_paid': '2000.00',
                'status': 'completed',
                'total': '1.00',
            },
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.amount_due, Decimal('2000.00'))
        self.assertEqual(sale.amount_paid, Decimal('0.00'))
        self.assertEqual(sale.total, Decimal('2000.00'))
        self.assertEqual(sale.status, 'pending')
        self.assert_debt_matches_open_invoices()

    def test_patch_still_accepts_annotations(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.patch(
            f'/api/v1/sales/{sale_id}/',
            {'notes': 'À rappeler vendredi', 'due_date': '2026-09-30'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.notes, 'À rappeler vendredi')
        self.assertEqual(str(sale.due_date), '2026-09-30')


class CancelledSaleTests(_CreditBaseTest):
    """Une vente annulée ne doit plus rien devoir."""

    def test_cancel_zeroes_amount_due(self):
        sale_id = self._create_sale()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/sales/{sale_id}/cancel/', {}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        sale = Sale.objects.get(pk=sale_id)
        self.assertEqual(sale.status, 'cancelled')
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(self._balance(), Decimal('0.00'))


class LoyaltySettlementTests(_CreditBaseTest):
    """Régler en points ne doit pas faire entrer d'argent au tiroir."""

    def test_redeeming_points_without_a_loyalty_method_creates_no_cash_income(self):
        from apps.cashbook.models import CashMovement
        from apps.settings.models import CustomerLoyalty, LoyaltyProgram

        # Aucune PaymentMethod de type `loyalty` dans l'organisation : c'est le
        # cas qui faisait retomber le repli sur `cash`.
        self.assertFalse(
            PaymentMethod.objects.filter(
                organization=self.org, method_type=PaymentMethod.MethodType.LOYALTY,
            ).exists()
        )

        LoyaltyProgram.objects.create(
            organization=self.org, is_active=True,
            points_calculation_type=LoyaltyProgram.PointsCalculationType.FIXED_PER_AMOUNT,
            points_per_unit=1,
            amount_per_unit=Decimal('1000.00'),
            point_value=Decimal('10.00'),
            min_points_to_redeem=10,
        )
        self._create_sale()
        loyalty, _ = CustomerLoyalty.objects.get_or_create(
            organization=self.org, customer=self.customer,
        )
        loyalty.add_points(50)

        movements_before = CashMovement.objects.filter(
            organization=self.org, direction='in',
        ).count()

        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/redeem-points/',
            {'points': '50'}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.assertEqual(
            CashMovement.objects.filter(
                organization=self.org, direction='in',
            ).count(),
            movements_before,
        )


class AdvanceOnExistingDebtTests(_CreditBaseTest):
    """
    De l'argent remis par un client qui doit déjà est un RÈGLEMENT.

    `record-advance` déplaçait le solde sans toucher aucune facture : appelé sur
    un client endetté, il faisait diverger le solde de la somme des `amount_due`
    ouverts. L'action impute désormais comme `record-payment`.
    """

    def _record_advance(self, amount):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-advance/',
            {'amount': amount}, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_advance_on_a_debtor_settles_the_invoice(self):
        sale_id = self._create_sale()

        data = self._record_advance('2000.00')

        self.assertEqual(Sale.objects.get(pk=sale_id).amount_due, Decimal('0.00'))
        self.assertEqual(self._balance(), Decimal('0.00'))
        self.assert_debt_matches_open_invoices()
        self.assertIn('VT', data['settled_invoices'][0])

    def test_surplus_beyond_the_debt_becomes_an_advance(self):
        self._create_sale()

        self._record_advance('3000.00')

        # 2000 imputés sur la facture, 1000 restants en avance.
        self.assertEqual(self._balance(), Decimal('-1000.00'))
        self.assert_debt_matches_open_invoices()

    def test_advance_without_debt_still_credits_the_customer(self):
        """Le cas d'origine doit continuer de fonctionner."""
        self._record_advance('3000.00')

        self.assertEqual(self._balance(), Decimal('-3000.00'))
        self.assert_debt_matches_open_invoices()


class AdjustBalanceTests(_CreditBaseTest):
    """`adjust-balance` n'avait aucune couverture HTTP."""

    def _adjust(self, amount, notes='Régularisation'):
        self.client.force_authenticate(user=self.manager)
        return self.client.post(
            f'/api/v1/customers/{self.customer.id}/adjust-balance/',
            {'amount': amount, 'notes': notes},
            format='json', **self._headers(),
        )

    def test_positive_amount_increases_the_debt_without_touching_the_till(self):
        from apps.cashbook.models import CashMovement

        movements_before = CashMovement.objects.filter(organization=self.org).count()

        resp = self._adjust('500.00')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(self._balance(), Decimal('500.00'))
        # Alourdir une dette n'encaisse rien.
        self.assertEqual(
            CashMovement.objects.filter(organization=self.org).count(),
            movements_before,
        )

    def test_negative_amount_reduces_the_debt_and_enters_the_till(self):
        from apps.cashbook.models import CashMovement

        self._create_sale()
        movements_before = CashMovement.objects.filter(organization=self.org).count()

        resp = self._adjust('-500.00')

        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        self.assertEqual(self._balance(), Decimal('1500.00'))
        # Réduire une dette, c'est de l'argent reçu : il entre au tiroir.
        self.assertEqual(
            CashMovement.objects.filter(organization=self.org).count(),
            movements_before + 1,
        )

    def test_response_carries_the_recomputed_balance(self):
        resp = self._adjust('500.00')

        # Ces trois clés sont le contrat que consomme la fiche client.
        self.assertIsNotNone(resp.data['transaction'])
        self.assertEqual(resp.data['new_balance'], '500.00')
        self.assertIn('CDF', resp.data['balances'])

    def test_adjustment_beyond_the_credit_limit_is_refused(self):
        self.customer.credit_limit = Decimal('100.00')
        self.customer.save(update_fields=['credit_limit'])

        resp = self._adjust('500.00')

        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertEqual(self._balance(), Decimal('0.00'))


class CurrencyReadabilityTests(_CreditBaseTest):
    """
    Un caissier doit pouvoir lire les devises de son organisation.

    Le POS en dépend entièrement pour formater, convertir et rendre la monnaie,
    or `list` exigeait `settings.view`, absent du rôle caissier : tous les taux
    valaient 1 et les symboles étaient remplacés par les codes.
    """

    def test_cashier_can_read_organization_currencies(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.get(
            '/api/v1/settings/organization-currencies/', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

    def test_cashier_cannot_change_a_rate(self):
        self.client.force_authenticate(user=self.cashier_a)
        resp = self.client.post(
            '/api/v1/settings/organization-currencies/',
            {'currency_code': 'USD', 'exchange_rate': '2800'},
            format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN, resp.content)


class CrossCurrencySettlementTests(_CreditBaseTest):
    """
    Un client qui doit en USD peut payer en francs congolais.

    La devise du règlement servait à la fois à choisir les factures visées et à
    exprimer l'argent remis. Choisir CDF sur un client ne devant qu'en USD ne
    trouvait donc aucune facture : le montant partait en avance CDF pendant que
    la dette USD restait entière. La modale de paiement d'une facture acceptait
    pourtant déjà ce cas.
    """

    def setUp(self):
        super().setUp()
        usd, _ = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'US Dollar', 'symbol': '$'},
        )
        cdf, _ = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC'},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=cdf,
            defaults={'is_primary': True, 'exchange_rate': Decimal('1'), 'is_active': True},
        )
        OrganizationCurrency.objects.update_or_create(
            organization=self.org, currency=usd,
            defaults={'is_primary': False, 'exchange_rate': Decimal('2800'), 'is_active': True},
        )

    def _usd_sale(self, total='50.00'):
        """Facture de 50 USD, laissée entièrement due."""
        sale_id = self._create_sale(unit_price=total)
        sale = Sale.objects.get(pk=sale_id)
        sale.currency = 'USD'
        sale.exchange_rate = Decimal('2800')
        sale.save(update_fields=['currency', 'exchange_rate'])
        # La dette suit la facture : on la réinscrit dans la bonne devise.
        contacts_services.settle_debt(self.customer, Decimal(total), currency='CDF')
        contacts_services.apply_debt(
            self.customer, Decimal(total), currency='USD', sale=sale,
        )
        return sale

    def _record_payment(self, payload):
        self.client.force_authenticate(user=self.manager)
        resp = self.client.post(
            f'/api/v1/customers/{self.customer.id}/record-payment/',
            payload, format='json', **self._headers(),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        return resp.data

    def test_cdf_cash_settles_a_usd_invoice(self):
        sale = self._usd_sale('50.00')

        # 50 USD = 140 000 CDF au taux de 2800.
        data = self._record_payment({
            'amount': '140000.00',
            'currency': 'CDF',
            'settle_currency': 'USD',
        })

        sale.refresh_from_db()
        self.assertEqual(sale.amount_due, Decimal('0.00'))
        self.assertEqual(sale.status, 'completed')
        self.assertEqual(self._balance('USD'), Decimal('0.00'))
        self.assertEqual(data['settled_invoices'], [sale.reference])
        # Rien ne part en avance : le règlement couvrait exactement la dette.
        self.assertEqual(Decimal(data['advance_amount']), Decimal('0.00'))

    def test_the_till_receives_the_currency_actually_handed_over(self):
        """L'argent entré au tiroir est du CDF, pas de l'USD."""
        from apps.cashbook.models import CashMovement

        self._usd_sale('50.00')

        self._record_payment({
            'amount': '140000.00',
            'currency': 'CDF',
            'settle_currency': 'USD',
        })

        movement = CashMovement.objects.filter(
            organization=self.org, direction='in',
        ).latest('created_at')
        self.assertEqual(movement.currency, 'CDF')

    def test_partial_cdf_payment_leaves_the_rest_due_in_usd(self):
        sale = self._usd_sale('50.00')

        # 70 000 CDF = 25 USD.
        self._record_payment({
            'amount': '70000.00',
            'currency': 'CDF',
            'settle_currency': 'USD',
        })

        sale.refresh_from_db()
        self.assertEqual(sale.amount_due, Decimal('25.00'))
        self.assertEqual(self._balance('USD'), Decimal('25.00'))
        self.assert_debt_matches_open_invoices('USD')

    def test_surplus_becomes_an_advance_in_the_currency_received(self):
        self._usd_sale('50.00')

        data = self._record_payment({
            'amount': '168000.00',   # 60 USD
            'currency': 'CDF',
            'settle_currency': 'USD',
        })

        self.assertEqual(self._balance('USD'), Decimal('0.00'))
        # Le reliquat est de l'argent physiquement détenu : il reste en CDF.
        self.assertEqual(Decimal(data['advance_amount']), Decimal('28000.00'))
        self.assertEqual(self._balance('CDF'), Decimal('-28000.00'))

    def test_omitting_settle_currency_keeps_the_previous_behaviour(self):
        """Les appelants qui ne connaissent pas le champ ne changent pas de sens."""
        self._usd_sale('50.00')

        data = self._record_payment({'amount': '10000.00', 'currency': 'CDF'})

        # Aucune facture en CDF : tout part en avance, comme avant.
        self.assertEqual(data['settled_invoices'], [])
        self.assertEqual(self._balance('CDF'), Decimal('-10000.00'))
        self.assertEqual(self._balance('USD'), Decimal('50.00'))
