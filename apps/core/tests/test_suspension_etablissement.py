"""
Un établissement suspendu par Vente Facile : tout est fermé, sauf la poussée du journal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `Organization.is_active` N'AVAIT AUCUN EFFET POUR LE MARCHAND.              │
│                                                                              │
│ L'administration le basculait, le dialogue le confirmait, et rien ne le     │
│ lisait : ni la connexion, ni `IsTenantMember`, ni le tirage. Un             │
│ établissement « suspendu » continuait de vendre, de lire et de tirer.       │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Les rôles sont BORNÉS (caissier, gérant) : la règle du dépôt, faute de quoi
le propriétaire sortirait en amont de la moitié des chemins éprouvés.
"""
from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework import status
from rest_framework.test import APITestCase

from apps.core.exceptions import EtablissementSuspendu
from apps.organizations.models import Organization, OrganizationMembership
from apps.platform_admin.models import PlatformRole
from apps.platform_admin.services import PlatformAdminService
from apps.sales.tests._helpers import make_org_with_users, make_user
from apps.sync.operations import SyncOperationsView

MESSAGE = "Cet établissement est suspendu par Vente Facile. Contactez le support."


class _Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.__dict__.update(make_org_with_users())

    def suspendre(self, org=None):
        org = org or self.org
        org.is_active = False
        org.save(update_fields=['is_active', 'updated_at'])

    def en(self, user, org=None):
        self.client.force_authenticate(user=user)
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str((org or self.org).pk))
        return self.client

    def assert_suspendu(self, reponse):
        self.assertEqual(reponse.status_code, status.HTTP_403_FORBIDDEN, getattr(reponse, 'data', None))
        self.assertEqual(reponse.data['code'], 'organization_suspended')
        self.assertEqual(reponse.data['detail'], MESSAGE)


class AccesTests(_Base):
    def test_les_ecrans_de_l_etablissement_sont_fermes(self):
        self.suspendre()
        client = self.en(self.cashier_a)
        for url in ('/api/v1/sales/', '/api/v1/products/', '/api/v1/users/me/permissions/'):
            with self.subTest(url=url):
                self.assert_suspendu(client.get(url))

    def test_une_ecriture_est_fermee_aussi(self):
        self.suspendre()
        self.assert_suspendu(self.en(self.manager).post('/api/v1/customers/', {'name': 'X'}, format='json'))

    def test_le_tirage_est_ferme(self):
        self.suspendre()
        self.assert_suspendu(self.en(self.cashier_a).get('/api/v1/sync/pull/manifest/'))

    def test_la_poussee_du_journal_passe(self):
        """Les ventes encaissées hors ligne doivent pouvoir arriver : sinon la suspension les perd."""
        self.suspendre()
        reponse = self.en(self.cashier_a).post('/api/v1/sync/operations/', {'operations': []}, format='json')
        # La vue a été ATTEINTE : elle refuse un lot vide pour sa propre raison.
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST, reponse.data)
        self.assertEqual(reponse.data['code'], 'no_operations')

    def test_la_liste_garde_l_etablissement_et_dit_qu_il_est_suspendu(self):
        """C'est ce qui permet au back-office d'afficher l'écran de suspension."""
        self.suspendre()
        self.client.force_authenticate(user=self.manager)
        liste = self.client.get('/api/v1/organizations/')
        self.assertEqual(liste.status_code, status.HTTP_200_OK)
        donnees = liste.data['results'] if isinstance(liste.data, dict) else liste.data
        self.assertEqual([(o['id'], o['is_active']) for o in donnees], [(str(self.org.pk), False)])
        self.assert_suspendu(self.client.get(f'/api/v1/organizations/{self.org.pk}/'))
        self.assert_suspendu(self.client.get(f'/api/v1/organizations/{self.org.pk}/dashboard/'))

    def test_une_autre_boutique_du_meme_compte_reste_ouverte(self):
        autre = Organization.objects.create(name='Autre boutique', slug='autre-boutique-ouverte')
        OrganizationMembership.objects.create(user=self.manager, organization=autre, role='owner', is_active=True)
        self.suspendre()
        self.client.force_authenticate(user=self.manager)
        self.assertEqual(self.client.get(f'/api/v1/organizations/{autre.pk}/').status_code, status.HTTP_200_OK)

    def test_la_reactivation_rend_l_acces(self):
        self.suspendre()
        self.org.is_active = True
        self.org.save(update_fields=['is_active', 'updated_at'])
        self.assertEqual(self.en(self.cashier_a).get('/api/v1/sales/').status_code, status.HTTP_200_OK)


class TerminalTests(_Base):
    def test_le_reveil_du_terminal_repond_et_dit_que_l_etablissement_est_suspendu(self):
        """
        ⚠ 200 et non 403 : un refus ici ferait perdre sa session au terminal, donc
        sa file d'opérations. Il apprend la suspension, et s'arrête de lui-même.
        """
        self.client.force_authenticate(user=self.cashier_a)
        jeton = self.client.post(
            '/api/v1/auth/devices/enroll/',
            {'name': 'Caisse 1', 'platform': 'android', 'model': 'NB55', 'os_version': '11', 'app_version': '1.0.0'},
            format='json', HTTP_X_ORGANIZATION_ID=str(self.org.pk),
        ).data['device_token']
        self.suspendre()
        self.client.force_authenticate(user=None)
        reveil = self.client.post('/api/v1/auth/devices/session/', {'device_token': jeton}, format='json')
        self.assertEqual(reveil.status_code, status.HTTP_200_OK, reveil.data)
        self.assertFalse(reveil.data['organization']['is_active'])

    def test_un_terminal_ne_s_enrole_pas_dans_un_etablissement_suspendu(self):
        self.suspendre()
        reponse = self.en(self.cashier_a).post(
            '/api/v1/auth/devices/enroll/',
            {'name': 'Caisse 2', 'platform': 'android', 'model': 'NB55', 'os_version': '11', 'app_version': '1.0.0'},
            format='json',
        )
        self.assert_suspendu(reponse)


class ConnexionTests(_Base):
    PORTES = ('/api/v1/auth/token/', '/api/v1/auth/login/')

    def connecter(self, email, porte):
        self.client.force_authenticate(user=None)
        return self.client.post(porte, {'email': email, 'password': 'pw12345!'}, format='json')

    def test_un_compte_dont_tous_les_etablissements_sont_suspendus_est_refuse_avec_le_message(self):
        self.suspendre()
        for porte in self.PORTES:
            with self.subTest(porte=porte):
                self.assert_suspendu(self.connecter('a@vf.test', porte))

    def test_une_seule_boutique_ouverte_suffit(self):
        autre = Organization.objects.create(name='Autre boutique', slug='autre-boutique-connexion')
        OrganizationMembership.objects.create(user=self.cashier_a, organization=autre, role='cashier', is_active=True)
        self.suspendre()
        for porte in self.PORTES:
            with self.subTest(porte=porte):
                self.assertEqual(self.connecter('a@vf.test', porte).status_code, status.HTTP_200_OK)

    def test_un_compte_sans_boutique_entre_pour_creer_la_sienne(self):
        make_user('neuf@vf.test')
        self.assertEqual(self.connecter('neuf@vf.test', self.PORTES[0]).status_code, status.HTTP_200_OK)

    def test_un_administrateur_de_la_plateforme_entre_toujours(self):
        racine = make_user('racine@vf.test')
        racine.is_superuser = True
        racine.save(update_fields=['is_superuser', 'updated_at'])
        admin = make_user('support@vf.test')
        PlatformAdminService.grant(admin, PlatformRole.SUPPORT, granted_by=racine)
        OrganizationMembership.objects.create(user=admin, organization=self.org, role='manager', is_active=True)
        self.suspendre()
        self.assertEqual(self.connecter('support@vf.test', self.PORTES[0]).status_code, status.HTTP_200_OK)


class ContratDesVuesToleranteTests(SimpleTestCase):
    """
    ⚠ Une vue tolérante de plus serait une porte ouverte sur un établissement
    suspendu, et rien ne le signalerait : on parcourt le routeur réel.
    """

    def vues(self):
        def parcourir(motifs):
            for motif in motifs:
                if isinstance(motif, URLResolver):
                    yield from parcourir(motif.url_patterns)
                elif isinstance(motif, URLPattern):
                    cible = getattr(motif.callback, 'cls', None) or getattr(motif.callback, 'view_class', None)
                    if cible is not None:
                        yield cible

        return set(parcourir(get_resolver().url_patterns))

    def test_seule_la_poussee_du_journal_tolere_la_suspension(self):
        vues = self.vues()
        self.assertGreater(len(vues), 50, "Le balayage ne trouve plus les vues.")
        tolerantes = {v for v in vues if getattr(v, 'tolere_etablissement_suspendu', False)}
        self.assertEqual(tolerantes, {SyncOperationsView})

    def test_le_refus_porte_le_message_nomme(self):
        refus = EtablissementSuspendu()
        self.assertEqual(refus.payload['code'], 'organization_suspended')
        self.assertEqual(refus.status_code, 403)
