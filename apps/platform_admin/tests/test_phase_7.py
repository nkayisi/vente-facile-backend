"""
Phase 7 de l'administration : notes du support, recherche globale, paiements, exports.

Les rôles éprouvés sont les deux qui existent : le SUPPORT (qui écrit les notes et
consulte tout, mais n'exporte pas) et le SUPER-ADMINISTRATEUR (qui exporte).
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from apps.platform_admin.models import SupportNote
from apps.platform_admin.services import PlatformPermissionService
from apps.platform_admin.tests._helpers import BaseAdminTest
from apps.sales.tests._helpers import make_user
from apps.platform_admin.models import PlatformRole
from apps.platform_admin.services import PlatformAdminService
from apps.subscriptions.models import Subscription, SubscriptionPayment

BASE = '/api/v1/platform-admin'


class NotesDuSupportTests(BaseAdminTest):
    URL = f'{BASE}/support-notes/'

    def _note(self, qui, corps='A payé par virement, en attente de rapprochement.'):
        return self.en(qui).post(
            self.URL, {'organization': str(self.org.id), 'body': corps}, format='json'
        )

    def test_le_support_ecrit_et_lit_une_note(self):
        r = self._note(self.support)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['author_email'], 'support@pa.test')
        liste = self.en(self.support).get(self.URL, {'organization': str(self.org.id)})
        self.assertEqual(liste.status_code, 200, liste.data)
        lignes = liste.data.get('results', liste.data)
        self.assertEqual([n['body'] for n in lignes], ['A payé par virement, en attente de rapprochement.'])

    def test_une_note_vide_ne_dit_rien(self):
        r = self._note(self.support, corps='   ')
        self.assertEqual(r.status_code, 400, r.data)

    def test_seul_l_auteur_corrige_le_texte(self):
        note = self._note(self.support).data
        autre = make_user('support2@pa.test', 'Sup', 'Deux')
        PlatformAdminService.grant(autre, PlatformRole.SUPPORT, granted_by=self.racine)
        r = self.en(autre).patch(f"{self.URL}{note['id']}/", {'body': 'Réécrite'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(SupportNote.objects.get(pk=note['id']).body,
                         'A payé par virement, en attente de rapprochement.')
        # L'auteur, lui, corrige.
        r = self.en(self.support).patch(f"{self.URL}{note['id']}/", {'body': 'Corrigée'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_un_collegue_epingle_sans_toucher_au_texte(self):
        note = self._note(self.support).data
        r = self.en(self.super_admin).patch(f"{self.URL}{note['id']}/", {'is_pinned': True}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(SupportNote.objects.get(pk=note['id']).is_pinned)

    def test_une_note_ne_se_supprime_pas(self):
        note = self._note(self.support).data
        r = self.en(self.super_admin).delete(f"{self.URL}{note['id']}/")
        self.assertEqual(r.status_code, 405)
        self.assertTrue(SupportNote.objects.filter(pk=note['id']).exists())

    def test_l_epinglee_passe_en_tete(self):
        self._note(self.support, 'Ancienne')
        epinglee = self._note(self.support, 'Importante').data
        self._note(self.support, 'Récente')
        self.en(self.support).patch(f"{self.URL}{epinglee['id']}/", {'is_pinned': True}, format='json')
        lignes = self.en(self.support).get(self.URL, {'organization': str(self.org.id)}).data
        lignes = lignes.get('results', lignes)
        self.assertEqual(lignes[0]['body'], 'Importante')

    def test_un_marchand_ne_lit_aucune_note(self):
        self._note(self.support)
        r = self.en(self.owner).get(self.URL, {'organization': str(self.org.id)})
        self.assertEqual(r.status_code, 403)


class RechercheTests(BaseAdminTest):
    URL = f'{BASE}/search/'

    def test_moins_de_deux_caracteres_ne_cherche_rien(self):
        r = self.en(self.support).get(self.URL, {'q': 'T'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(r.data['organizations'])

    def test_trouve_un_etablissement_et_un_compte(self):
        r = self.en(self.support).get(self.URL, {'q': 'test org'})
        self.assertEqual([o['name'] for o in r.data['organizations']], ['Test Org'])
        r = self.en(self.support).get(self.URL, {'q': 'manager@vf'})
        self.assertEqual([u['detail'] for u in r.data['users']], ['manager@vf.test'])

    def test_un_groupe_que_l_on_ne_peut_pas_lire_est_absent(self):
        # ⚠ ABSENT (`None`), pas vide : vide se lirait « aucun résultat ».
        with mock.patch.object(
            PlatformPermissionService, 'get_effective_permissions',
            return_value=frozenset({'organizations.view'}),
        ):
            r = self.en(self.support).get(self.URL, {'q': 'test'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNotNone(r.data['organizations'])
        self.assertIsNone(r.data['users'])
        self.assertIsNone(r.data['payments'])


class _AvecPaiements(BaseAdminTest):
    def setUp(self):
        super().setUp()
        abonnement = Subscription.objects.filter(organization=self.org).first()
        self.encaisse = SubscriptionPayment.objects.create(
            organization=self.org, subscription=abonnement, amount=Decimal('10.00'),
            currency='USD', payment_method='mobile_money', status='completed',
            reference='vf_sub_encaisse', paid_at=timezone.now(),
        )
        self.echoue = SubscriptionPayment.objects.create(
            organization=self.org, subscription=abonnement, amount=Decimal('10.00'),
            currency='USD', payment_method='mobile_money', status='failed',
            reference='vf_sub_echoue',
        )


class PaiementsTests(_AvecPaiements):
    URL = f'{BASE}/subscription-payments/'

    def test_le_support_lit_les_paiements(self):
        r = self.en(self.support).get(self.URL)
        self.assertEqual(r.status_code, 200, r.data)
        lignes = r.data.get('results', r.data)
        self.assertEqual({p['reference'] for p in lignes}, {'vf_sub_encaisse', 'vf_sub_echoue'})

    def test_filtre_par_statut(self):
        r = self.en(self.support).get(self.URL, {'status': 'failed'})
        lignes = r.data.get('results', r.data)
        self.assertEqual([p['reference'] for p in lignes], ['vf_sub_echoue'])

    def test_la_borne_de_fin_est_incluse(self):
        aujourd_hui = timezone.localdate().isoformat()
        r = self.en(self.support).get(self.URL, {'date_to': aujourd_hui})
        self.assertEqual(len(r.data.get('results', r.data)), 2)
        hier = (timezone.localdate() - timedelta(days=1)).isoformat()
        r = self.en(self.support).get(self.URL, {'date_to': hier})
        self.assertEqual(len(r.data.get('results', r.data)), 0)

    def test_un_paiement_ne_se_modifie_pas_d_ici(self):
        r = self.en(self.super_admin).patch(f'{self.URL}{self.encaisse.id}/', {'status': 'failed'}, format='json')
        self.assertEqual(r.status_code, 405)

    def test_la_recherche_trouve_un_paiement_par_sa_reference(self):
        r = self.en(self.support).get(f'{BASE}/search/', {'q': 'vf_sub_enc'})
        self.assertEqual([p['name'] for p in r.data['payments']], ['vf_sub_encaisse'])


class ExportsTests(_AvecPaiements):
    LISTES = {
        'organizations': 'Test Org',
        'users': 'manager@vf.test',
        'subscriptions': 'Test Org',
        'subscription-payments': 'vf_sub_encaisse',
        'audit-log': None,
    }

    def test_le_super_administrateur_exporte_les_cinq_listes(self):
        for liste, attendu in self.LISTES.items():
            with self.subTest(liste=liste):
                r = self.en(self.super_admin).get(f'{BASE}/{liste}/export/', {'export_format': 'csv'})
                self.assertEqual(r.status_code, 200, getattr(r, 'data', None))
                contenu = r.content.decode('utf-8-sig')
                self.assertIn('Vente Facile - administration de la plateforme', contenu)
                if attendu:
                    self.assertIn(attendu, contenu)

    def test_le_support_n_exporte_pas(self):
        # ⚠ Un fichier emporte la liste de TOUS les marchands hors de
        #   l'application, ce qu'aucune consultation ne fait.
        for liste in self.LISTES:
            with self.subTest(liste=liste):
                r = self.en(self.support).get(f'{BASE}/{liste}/export/', {'export_format': 'csv'})
                self.assertEqual(r.status_code, 403)

    def test_l_export_porte_le_perimetre_filtre(self):
        r = self.en(self.super_admin).get(
            f'{BASE}/subscription-payments/export/', {'export_format': 'csv', 'status': 'failed'}
        )
        contenu = r.content.decode('utf-8-sig')
        self.assertIn('vf_sub_echoue', contenu)
        self.assertNotIn('vf_sub_encaisse', contenu)

    def test_seul_l_encaisse_entre_dans_le_total(self):
        r = self.en(self.super_admin).get(
            f'{BASE}/subscription-payments/export/', {'export_format': 'csv'}
        )
        contenu = r.content.decode('utf-8-sig')
        # Dix dollars encaissés, dix échoués : le total n'en compte que dix.
        self.assertIn('Total USD', contenu)
        ligne = next(l for l in contenu.splitlines() if l.startswith('Total USD'))
        self.assertIn('10', ligne)
        self.assertNotIn('20', ligne)

    def test_les_trois_formats(self):
        for fmt in ('pdf', 'xlsx', 'csv'):
            with self.subTest(fmt=fmt):
                r = self.en(self.super_admin).get(f'{BASE}/audit-log/export/', {'export_format': fmt})
                self.assertEqual(r.status_code, 200)

    def test_l_en_tete_nomme_le_filtre_au_lieu_de_son_identifiant(self):
        # ⚠ « Établissement : 3f2a9c1e-… » ne dirait à personne quel périmètre
        #   le fichier couvre.
        r = self.en(self.super_admin).get(
            f'{BASE}/subscription-payments/export/',
            {'export_format': 'csv', 'organization': str(self.org.id)},
        )
        contenu = r.content.decode('utf-8-sig')
        ligne = next(l for l in contenu.splitlines() if l.startswith('Établissement'))
        self.assertIn('Test Org', ligne)
        self.assertNotIn(str(self.org.id), contenu)
