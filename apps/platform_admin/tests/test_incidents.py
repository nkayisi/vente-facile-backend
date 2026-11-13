"""
Ce que le support traite aujourd'hui : rangé par urgence, et sans faux positif.

Le faux positif qui compte ici est le RENOUVELLEMENT : `activate_subscription`
expire l'ancien abonnement. Le compter comme « échu » enverrait le support
relancer un marchand qui vient de payer.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone
from rest_framework import status

from apps.organizations.models import Organization, OrganizationMembership
from apps.platform_admin.incidents import tableau_des_incidents
from apps.platform_admin.models import AdminActionLog
from apps.subscriptions.models import Plan, Subscription, SubscriptionPayment

from ._helpers import BaseAdminTest

URL = '/api/v1/platform-admin/incidents/'


class _Base(BaseAdminTest):
    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.get(code='TEST')
        self.maintenant = timezone.now()
        self.sub = Subscription.objects.get(organization=self.org, status='active')

    def autre_org(self, nom):
        return Organization.objects.create(name=nom, slug=nom.lower().replace(' ', '-'))

    def abonner(self, org, *, statut, fin):
        return Subscription.objects.create(
            organization=org, plan=self.plan, status=statut, billing_cycle='monthly',
            price=Decimal('10'), currency='USD',
            current_period_start=fin - timedelta(days=30), current_period_end=fin,
        )


class AbonnementsARisqueTests(_Base):
    def test_le_tri_va_du_plus_grave_au_moins_grave(self):
        self.sub.current_period_end = self.maintenant + timedelta(days=6)
        self.sub.save(update_fields=['current_period_end', 'updated_at'])
        bloquee = self.autre_org('Bloquee')
        self.abonner(bloquee, statut='expired', fin=self.maintenant - timedelta(days=2))
        imminent = self.autre_org('Imminent')
        self.abonner(imminent, statut='active', fin=self.maintenant + timedelta(days=1))

        lignes = tableau_des_incidents(self.maintenant)['subscriptions_at_risk']
        self.assertEqual(
            [(l['organization_name'], l['severity']) for l in lignes],
            [('Bloquee', 'critical'), ('Imminent', 'high'), ('Test Org', 'medium')],
        )
        # Le contact du propriétaire, pour relancer au téléphone.
        self.assertEqual(lignes[2]['owner_email'], 'owner@vf.test')

    def test_un_renouvellement_n_est_pas_un_incident(self):
        """L'ancien abonnement expiré, le nouveau en cours : rien à relancer."""
        self.abonner(self.org, statut='expired', fin=self.maintenant - timedelta(days=3))
        noms = [l['organization_name'] for l in tableau_des_incidents(self.maintenant)['subscriptions_at_risk']]
        self.assertNotIn('Test Org', noms)

    def test_un_echu_ancien_n_est_plus_recuperable(self):
        org = self.autre_org('Ancienne')
        self.abonner(org, statut='expired', fin=self.maintenant - timedelta(days=40))
        noms = [l['organization_name'] for l in tableau_des_incidents(self.maintenant)['subscriptions_at_risk']]
        self.assertNotIn('Ancienne', noms)


class PaiementsTests(_Base):
    def payer(self, *, statut, age, tentatives=0, ref='vf_sub_test'):
        p = SubscriptionPayment.objects.create(
            organization=self.org, amount=Decimal('10'), currency='USD',
            payment_method='mobile_money', status=statut, reference=ref,
            reconcile_attempts=tentatives,
        )
        SubscriptionPayment.objects.filter(pk=p.pk).update(created_at=self.maintenant - age)
        return p

    def test_une_attente_recente_n_est_pas_suspecte(self):
        self.payer(statut='pending', age=timedelta(minutes=5))
        self.assertEqual(tableau_des_incidents(self.maintenant)['counts']['payments_to_review'], 0)

    def test_une_attente_qui_a_resiste_a_la_reconciliation_est_critique(self):
        self.payer(statut='pending', age=timedelta(hours=3), tentatives=2, ref='vf_sub_bloque')
        self.payer(statut='failed', age=timedelta(days=2), ref='vf_sub_echec')
        lignes = tableau_des_incidents(self.maintenant)['payments_to_review']
        self.assertEqual([(l['reference'], l['severity']) for l in lignes],
                         [('vf_sub_bloque', 'critical'), ('vf_sub_echec', 'medium')])

    def test_la_reconciliation_a_la_demande_est_journalisee(self):
        self.payer(statut='pending', age=timedelta(hours=1), ref='vf_sub_attente')
        with mock.patch('apps.subscriptions.moko_client.get_payment_status_v2',
                        return_value=(200, {'status': 'PENDING'})), \
             mock.patch('apps.subscriptions.moko_client.extract_payment_status_v2', return_value='PENDING'), \
             mock.patch('apps.subscriptions.moko_client.is_payment_pending_v2', return_value=True):
            reponse = self.en(self.support).post(
                f'{URL}reconcile/', {'reference': 'vf_sub_attente'}, format='json',
            )
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        self.assertEqual(reponse.data['still_pending'], 1)
        ligne = AdminActionLog.objects.get(action='reconcile_payments')
        self.assertEqual(ligne.target_repr, 'vf_sub_attente')
        self.assertEqual(ligne.actor, self.support)


class SansProprietaireTests(_Base):
    def test_une_boutique_sans_proprietaire_actif_est_signalee(self):
        OrganizationMembership.objects.filter(organization=self.org, role='owner').update(is_active=False)
        orgs = tableau_des_incidents(self.maintenant)['organizations_without_owner']
        self.assertEqual([o['organization_name'] for o in orgs], ['Test Org'])

    def test_l_ecran_est_reserve_a_l_administration(self):
        self.assertEqual(self.en(self.owner).get(URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.en(self.support).get(URL).status_code, status.HTTP_200_OK)
