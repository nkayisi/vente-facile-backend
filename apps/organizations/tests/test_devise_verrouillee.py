"""
La devise principale d'un établissement se choisit à la création, et ne change plus.

┌──────────────────────────────────────────────────────────────────────────────┐
│ ELLE SE CHANGEAIT PAR QUATRE PORTES, ET UNE SEULE ÉTAIT FERMÉE.             │
│                                                                              │
│ `set_primary` refusait déjà. Mais `PATCH /organizations/{id}/` écrivait     │
│ `Organization.currency` sans toucher la ligne `is_primary`, et la création  │
│ comme la modification d'une `OrganizationCurrency` avec `is_primary`         │
│ rétrogradaient l'ancienne principale sans toucher `Organization.currency`.  │
│ Dans les trois cas, les deux autorités divergeaient : les ventes nouvelles   │
│ prenaient un code, l'affichage et les décimales un autre - et toutes les     │
│ ventes passées, au taux figé de l'ancienne principale, se lisaient dans une  │
│ monnaie qui n'était plus la leur.                                            │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ Le PROPRIÉTAIRE, et ce n'est pas une facilité : la fiche de l'établissement ne
se modifie que par lui (`IsTenantAdmin`), et `settings.manage` n'est accordé qu'à
lui. Aucun rôle plus faible n'a le droit d'écrire ici - un gérant reçoit 403
avant même que la règle de la devise soit lue. `EtablissementDeLaRequeteTests`
éprouve, lui, un compte membre de DEUX établissements.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrganizationMembership
from apps.sales.tests._helpers import make_org_with_users
from apps.settings.models import Currency, OrganizationCurrency


class _Base(APITestCase):
    def setUp(self):
        self.ctx = make_org_with_users()
        self.org = self.ctx['org']
        self.cdf = Currency.objects.get_or_create(
            code='CDF', defaults={'name': 'Franc Congolais', 'symbol': 'FC'}
        )[0]
        self.usd = Currency.objects.get_or_create(
            code='USD', defaults={'name': 'Dollar', 'symbol': '$', 'decimal_places': 2}
        )[0]
        self.eur = Currency.objects.get_or_create(
            code='EUR', defaults={'name': 'Euro', 'symbol': '€', 'decimal_places': 2}
        )[0]
        self.org.currency = 'CDF'
        self.org.save(update_fields=['currency'])
        self.principale = OrganizationCurrency.objects.create(
            organization=self.org, currency=self.cdf, is_primary=True,
            exchange_rate=Decimal('1'),
        )
        self.secondaire = OrganizationCurrency.objects.create(
            organization=self.org, currency=self.usd, is_primary=False,
            exchange_rate=Decimal('2800'),
        )
        self.client.force_authenticate(self.ctx['owner'])
        self.client.credentials(HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def _relire(self):
        self.org.refresh_from_db()
        self.principale.refresh_from_db()
        self.secondaire.refresh_from_db()


class EtablissementTests(_Base):
    def test_le_patch_refuse_une_autre_devise(self):
        r = self.client.patch(f'/api/v1/organizations/{self.org.id}/', {'currency': 'USD'})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('currency', r.data)
        self._relire()
        self.assertEqual(self.org.currency, 'CDF')

    def test_renvoyer_la_meme_devise_n_est_pas_un_refus(self):
        # Un formulaire qui renvoie tout ce qu'il a lu ne doit pas échouer.
        r = self.client.patch(
            f'/api/v1/organizations/{self.org.id}/', {'currency': 'CDF', 'city': 'Goma'}
        )
        self.assertEqual(r.status_code, 200, r.data)
        self._relire()
        self.assertEqual(self.org.city, 'Goma')

    def test_le_reste_de_la_fiche_se_modifie(self):
        r = self.client.patch(f'/api/v1/organizations/{self.org.id}/', {'country': 'Cameroun'})
        self.assertEqual(r.status_code, 200, r.data)
        self._relire()
        self.assertEqual(self.org.country, 'Cameroun')


class DevisesDeLEtablissementTests(_Base):
    URL = '/api/v1/settings/organization-currencies/'

    def test_une_secondaire_ne_devient_pas_principale(self):
        r = self.client.patch(f'{self.URL}{self.secondaire.id}/', {'is_primary': True})
        self.assertEqual(r.status_code, 400, r.data)
        self._relire()
        self.assertTrue(self.principale.is_primary)
        self.assertFalse(self.secondaire.is_primary)

    def test_la_principale_ne_se_retrograde_pas(self):
        r = self.client.patch(f'{self.URL}{self.principale.id}/', {'is_primary': False})
        self.assertEqual(r.status_code, 400, r.data)
        self._relire()
        self.assertTrue(self.principale.is_primary)

    def test_la_principale_ne_se_desactive_pas(self):
        r = self.client.patch(f'{self.URL}{self.principale.id}/', {'is_active': False})
        self.assertEqual(r.status_code, 400, r.data)
        self._relire()
        self.assertTrue(self.principale.is_active)

    def test_le_taux_de_la_principale_reste_un(self):
        r = self.client.patch(f'{self.URL}{self.principale.id}/', {'exchange_rate': '3'})
        self.assertEqual(r.status_code, 400, r.data)
        self._relire()
        self.assertEqual(self.principale.exchange_rate, Decimal('1'))

    def test_une_secondaire_se_regle_toujours(self):
        # Contrôle : on ferme la principale, pas la gestion des devises.
        r = self.client.patch(
            f'{self.URL}{self.secondaire.id}/', {'exchange_rate': '2900', 'is_active': False}
        )
        self.assertEqual(r.status_code, 200, r.data)
        self._relire()
        self.assertEqual(self.secondaire.exchange_rate, Decimal('2900'))
        self.assertFalse(self.secondaire.is_active)

    def test_on_ne_cree_pas_une_seconde_principale(self):
        r = self.client.post(
            self.URL, {'currency': self.eur.id, 'is_primary': True, 'exchange_rate': '3000'}
        )
        self.assertEqual(r.status_code, 400, r.data)
        self._relire()
        self.assertTrue(self.principale.is_primary)
        self.assertFalse(
            OrganizationCurrency.objects.filter(organization=self.org, currency=self.eur).exists()
        )

    def test_une_secondaire_s_ajoute(self):
        r = self.client.post(self.URL, {'currency': self.eur.id, 'exchange_rate': '3000'})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(
            OrganizationCurrency.objects.filter(
                organization=self.org, currency=self.eur, is_primary=False
            ).exists()
        )


class EtablissementDeLaRequeteTests(_Base):
    """
    ⚠ LA DEVISE S'AJOUTAIT À L'ÉTABLISSEMENT « ACTIF » DU COMPTE, PAS À CELUI
      DE LA REQUÊTE.

    `OrganizationCurrencyCreateSerializer` lisait `user.active_organization` :
    un propriétaire de deux boutiques qui ajoutait le dollar à la seconde, en-tête
    compris, l'ajoutait à la première. Le défaut exact déjà corrigé sur le
    programme de fidélité, resté ouvert ici.
    """

    def setUp(self):
        super().setUp()
        self.autre = Organization.objects.create(name='Autre boutique', slug='autre-boutique')
        OrganizationMembership.objects.create(
            user=self.ctx['owner'], organization=self.autre,
            role=OrganizationMembership.Role.OWNER, is_active=True,
        )
        owner = self.ctx['owner']
        owner.active_organization = self.autre
        owner.save(update_fields=['active_organization'])

    def test_la_devise_va_a_l_etablissement_de_l_en_tete(self):
        r = self.client.post(
            '/api/v1/settings/organization-currencies/',
            {'currency': self.eur.id, 'exchange_rate': '3000'},
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(
            OrganizationCurrency.objects.filter(organization=self.org, currency=self.eur).exists()
        )
        self.assertFalse(
            OrganizationCurrency.objects.filter(organization=self.autre).exists()
        )

    def test_le_doublon_se_juge_dans_l_etablissement_de_l_en_tete(self):
        # Le dollar existe dans CET établissement : refus, même si l'établissement
        # « actif » du compte ne l'a pas.
        r = self.client.post(
            '/api/v1/settings/organization-currencies/',
            {'currency': self.usd.id, 'exchange_rate': '2800'},
        )
        self.assertEqual(r.status_code, 400, r.data)
