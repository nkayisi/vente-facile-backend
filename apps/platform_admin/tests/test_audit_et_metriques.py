"""
Le journal d'audit, les chiffres récurrents, et la migration qui ouvre la porte.
"""
from datetime import timedelta
from decimal import Decimal

from django.apps import apps as registre
from django.test import TestCase
from django.utils import timezone
from rest_framework import status

from apps.organizations.models import Organization
from apps.platform_admin.metrics import PlatformMetricsService, equivalent_mensuel
from apps.platform_admin.models import AdminActionLog, PlatformAdminProfile
from apps.settings.models import Currency
from apps.subscriptions.models import GlobalConfig, Plan, Subscription, SubscriptionPayment
from apps.sales.tests._helpers import make_user

from ._helpers import BaseAdminTest

BASE = '/api/v1/platform-admin'


class JournalTests(BaseAdminTest):
    def test_un_acte_refuse_n_ecrit_rien(self):
        """La ligne naît DANS la transaction de l'acte : un refus n'en laisse aucune."""
        avant = AdminActionLog.objects.count()
        self.en(self.support).post(f'{BASE}/users/{self.super_admin.pk}/toggle_active/', {'reason': 'Motif de test'}, format='json')
        self.assertEqual(AdminActionLog.objects.count(), avant)

    def test_la_porte_de_mise_a_jour_ne_journalise_que_ce_qui_change(self):
        config = GlobalConfig.get()
        config.android_current_version = '1.3.0'
        config.android_store_url = 'https://play.google.com/store/apps/details?id=com.smizone.app'
        config.save()
        reponse = self.en(self.super_admin).patch(f'{BASE}/app-release/', {
            'android_minimum_version': '1.3.0',
            'android_current_version': '1.3.0',  # inchangé
        }, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        ligne = AdminActionLog.objects.get(action='update_app_release')
        self.assertEqual(ligne.after, {'android_minimum_version': '1.3.0'})
        self.assertEqual(ligne.before, {'android_minimum_version': ''})

    def test_le_journal_se_filtre_et_se_cherche(self):
        self.en(self.super_admin).post(
            f'{BASE}/organizations/{self.org.pk}/toggle_active/', {'reason': 'Fraude signalée'}, format='json',
        )
        client = self.en(self.support)
        par_cible = client.get(f'{BASE}/audit-log/', {'target_type': 'organization'})
        self.assertEqual(par_cible.status_code, status.HTTP_200_OK)
        self.assertEqual(par_cible.data['count'], 1)
        self.assertEqual(par_cible.data['results'][0]['reason'], 'Fraude signalée')
        par_motif = client.get(f'{BASE}/audit-log/', {'search': 'fraude'})
        self.assertEqual(par_motif.data['count'], 1)
        aujourdhui = timezone.localdate().isoformat()
        borne = client.get(f'{BASE}/audit-log/', {'date_from': aujourdhui, 'date_to': aujourdhui, 'target_type': 'organization'})
        self.assertEqual(borne.data['count'], 1)

    def test_le_journal_ne_s_ecrit_pas_par_l_api(self):
        reponse = self.en(self.super_admin).post(f'{BASE}/audit-log/', {}, format='json')
        self.assertEqual(reponse.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_un_marchand_ne_le_lit_pas(self):
        self.assertEqual(self.en(self.owner).get(f'{BASE}/audit-log/').status_code,
                         status.HTTP_403_FORBIDDEN)


class TableauDeBordRevenuTests(BaseAdminTest):
    """
    ⚠ L'ENCAISSÉ NE S'ADDITIONNE JAMAIS D'UNE DEVISE À L'AUTRE.

    Le tableau de bord sommait tous les règlements puis affichait le total sans
    symbole : 10 dollars et 28 000 francs donnaient « 28 010 », un chiffre qui
    ne désigne aucune somme réelle.
    """

    def payer(self, montant, devise, statut='completed', il_y_a=timedelta(days=1)):
        p = SubscriptionPayment.objects.create(
            organization=self.org, amount=Decimal(montant), currency=devise,
            payment_method='mobile_money', status=statut, reference=f'vf_sub_{devise}_{montant}_{statut}',
        )
        SubscriptionPayment.objects.filter(pk=p.pk).update(paid_at=timezone.now() - il_y_a)
        return p

    def test_le_revenu_est_ventile_par_devise_et_jamais_somme(self):
        self.payer('10.00', 'USD')
        self.payer('28000.00', 'CDF')
        self.payer('999.00', 'USD', statut='pending')  # non encaissé : ne compte pas

        reponse = self.en(self.support).get(f'{BASE}/dashboard/')
        self.assertEqual(reponse.status_code, status.HTTP_200_OK)
        par_devise = {r['currency']: r for r in reponse.data['revenue_by_currency']}
        self.assertEqual(set(par_devise), {'USD', 'CDF'})
        self.assertEqual(Decimal(par_devise['USD']['total']), Decimal('10.00'))
        self.assertEqual(Decimal(par_devise['CDF']['total']), Decimal('28000.00'))
        # Aucun scalaire inter-devises ne doit subsister dans la réponse.
        self.assertNotIn('total_revenue', reponse.data)

    def test_la_tendance_garde_la_devise_de_chaque_mois(self):
        self.payer('10.00', 'USD')
        self.payer('28000.00', 'CDF')
        tendance = self.en(self.support).get(f'{BASE}/dashboard/').data['revenue_trend']
        self.assertTrue(tendance, "La tendance doit porter le mois en cours.")
        dernier = tendance[-1]['by_currency']
        self.assertEqual(Decimal(dernier['USD']), Decimal('10.00'))
        self.assertEqual(Decimal(dernier['CDF']), Decimal('28000.00'))


class _BaseMetriques(TestCase):
    def setUp(self):
        self.usd, _ = Currency.objects.get_or_create(code='USD', defaults={'name': 'Dollar', 'symbol': '$'})
        self.cdf, _ = Currency.objects.get_or_create(code='CDF', defaults={'name': 'Franc', 'symbol': 'FC'})
        self.plan = Plan.objects.create(
            name='Standard', code='std', price_monthly=Decimal('10'), price_yearly=Decimal('100'),
            currency=self.usd,
        )
        self.essai = Plan.objects.create(
            name='Essai', code='essai', price_monthly=0, price_yearly=0, currency=self.usd,
        )
        self.maintenant = timezone.now()
        self.n = 0

    def org(self):
        self.n += 1
        return Organization.objects.create(name=f'O{self.n}', slug=f'o{self.n}')

    def sub(self, org, *, prix, cycle='monthly', statut='active', devise='USD', debut=None, fin=None,
            annule=None, plan=None):
        return Subscription.objects.create(
            organization=org, plan=plan or self.plan, status=statut, billing_cycle=cycle,
            price=Decimal(prix), currency=devise,
            current_period_start=debut or self.maintenant - timedelta(days=5),
            current_period_end=fin or self.maintenant + timedelta(days=25),
            cancelled_at=annule,
        )


class MrrTests(_BaseMetriques):
    def test_l_equivalent_mensuel(self):
        self.assertEqual(equivalent_mensuel(Decimal('100'), 'yearly'), Decimal('100') / 12)
        self.assertEqual(equivalent_mensuel(Decimal('30'), 'quarterly'), Decimal('10'))
        self.assertEqual(equivalent_mensuel(Decimal('10'), 'monthly'), Decimal('10'))

    def test_un_annuel_compte_au_douzieme_et_l_essai_ne_compte_pas(self):
        self.sub(self.org(), prix='10')
        self.sub(self.org(), prix='120', cycle='yearly')
        self.sub(self.org(), prix='0', statut='trial', plan=self.essai)
        chiffres = PlatformMetricsService.instantane(self.maintenant)
        self.assertEqual(chiffres['mrr'], '20.00')
        self.assertEqual(chiffres['paying_organizations'], 2)
        self.assertEqual(chiffres['trialing_organizations'], 1)
        self.assertEqual(chiffres['arpu'], '10.00')

    def test_une_annulation_coupe_le_revenu_sur_le_champ(self):
        self.sub(self.org(), prix='10', statut='cancelled', annule=self.maintenant - timedelta(days=1))
        self.assertEqual(PlatformMetricsService.instantane(self.maintenant)['mrr'], '0.00')

    def test_deux_devises_ne_s_additionnent_jamais(self):
        self.sub(self.org(), prix='10')
        self.sub(self.org(), prix='10')
        self.sub(self.org(), prix='28000', devise='CDF')
        chiffres = PlatformMetricsService.instantane(self.maintenant)
        self.assertTrue(chiffres['multi_currency'])
        self.assertEqual(chiffres['currency'], 'USD')
        self.assertEqual(chiffres['mrr'], '20.00')
        par_devise = {d['currency']: d['mrr'] for d in chiffres['mrr_by_currency']}
        self.assertEqual(par_devise, {'USD': '20.00', 'CDF': '28000.00'})

    def test_l_attrition_a_trente_jours(self):
        reste = self.org()
        part = self.org()
        nouveau = self.org()
        debut = self.maintenant - timedelta(days=40)
        self.sub(reste, prix='10', debut=debut, fin=self.maintenant + timedelta(days=20))
        self.sub(part, prix='10', statut='expired', debut=debut, fin=self.maintenant - timedelta(days=10))
        self.sub(nouveau, prix='10', debut=self.maintenant - timedelta(days=3))
        chiffres = PlatformMetricsService.instantane(self.maintenant)
        self.assertEqual(chiffres['churned_30d'], 1)
        self.assertEqual(chiffres['new_paying_30d'], 1)
        self.assertEqual(chiffres['churn_rate_30d'], '50.00')

    def test_sans_payant_l_attrition_n_est_pas_zero_mais_inconnue(self):
        self.assertIsNone(PlatformMetricsService.instantane(self.maintenant)['churn_rate_30d'])

    def test_un_renouvellement_n_est_pas_une_perte(self):
        """`activate_subscription` expire l'ancien abonnement : l'organisation paie toujours."""
        o = self.org()
        self.sub(o, prix='10', statut='expired', debut=self.maintenant - timedelta(days=40),
                 fin=self.maintenant - timedelta(days=2))
        self.sub(o, prix='10', debut=self.maintenant - timedelta(days=2))
        self.assertEqual(PlatformMetricsService.instantane(self.maintenant)['churned_30d'], 0)

    def test_la_serie_couvre_les_mois_demandes(self):
        self.sub(self.org(), prix='10', debut=self.maintenant - timedelta(days=70),
                 fin=self.maintenant + timedelta(days=10))
        serie = PlatformMetricsService.serie_mensuelle(4, self.maintenant)
        self.assertEqual(len(serie), 4)
        self.assertEqual(serie[-1]['month'], timezone.localtime(self.maintenant).strftime('%Y-%m'))
        self.assertEqual(serie[-1]['mrr_by_currency'], {'USD': '10.00'})
        self.assertIsNone(serie[0]['new'])


class MigrationDesStaffTests(TestCase):
    """
    Sans elle, le déploiement enferme dehors toute l'équipe : la porte ne lit plus
    `is_staff`. On appelle la fonction de la migration sur le registre réel.
    """

    def test_tout_staff_existant_devient_super_admin_une_seule_fois(self):
        import importlib
        migration = importlib.import_module(
            'apps.platform_admin.migrations.0002_staff_existants_super_admin'
        )
        staff = make_user('ancien@pa.test')
        staff.is_staff = True
        staff.save(update_fields=['is_staff', 'updated_at'])
        make_user('marchand@pa.test')

        migration.accorder(registre, None)
        migration.accorder(registre, None)  # idempotente

        profils = PlatformAdminProfile.objects.filter(user=staff)
        self.assertEqual(profils.count(), 1)
        self.assertEqual(profils.get().role, 'super_admin')
        self.assertFalse(PlatformAdminProfile.objects.filter(user__email='marchand@pa.test').exists())
        self.assertEqual(AdminActionLog.objects.filter(target_id=str(staff.pk)).count(), 1)
