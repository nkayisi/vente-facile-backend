"""
Parité de la MODIFICATION des référentiels entre le back-office et le terminal.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `CategoryDetailSerializer`, JAMAIS `CategoryCreateSerializer`.              │
│                                                                              │
│ Le serializer de création vérifie l'unicité du nom SANS exclure la fiche     │
│ modifiée : il n'a aucun `exclude(pk=self.instance.pk)`. L'employer en        │
│ modification ferait refuser une catégorie pour SON PROPRE nom, à chaque      │
│ envoi qui porte `name`, c'est-à-dire toujours. Le refus serait déterministe  │
│ et inconditionnel, donc une quarantaine, découverte hors ligne bien plus     │
│ tard. `test_une_categorie_garde_son_propre_nom_quand_le_parent_bouge` est    │
│ le test qui l'attrape.                                                       │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`.** `categories.edit` ne va qu'au
propriétaire et au gérant : on prend le GÉRANT. `products.edit` va aussi au
MAGASINIER : on le prend pour les marques et les unités.

Le magasinier n'est pas un détail de style : il a `products.edit` mais **ni
`products.create` ni `categories.edit`**. La même authentification doit donc
être ACCEPTÉE sur `brand.update` et REFUSÉE sur `category.update`. C'est la
seule paire qui prouve que les deux actes ne partagent pas une permission par
accident.
"""
from decimal import Decimal

from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization, OrganizationMembership
from apps.products.models import Brand, Category, Unit
from apps.sales.tests._helpers import make_org_with_users, make_user

OPERATIONS = '/api/v1/sync/operations/'


class _ReferentielBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.magasinier = make_user('stock@vf.test', 'Sto', 'Ck')
        OrganizationMembership.objects.create(
            user=self.magasinier, organization=self.org,
            role=OrganizationMembership.Role.STOCK_KEEPER, is_active=True,
        )

        self.boissons = Category.objects.create(
            organization=self.org, name='Boissons', slug='boissons',
        )
        self.sodas = Category.objects.create(
            organization=self.org, name='Sodas', slug='sodas', parent=self.boissons,
        )
        self.epicerie = Category.objects.create(
            organization=self.org, name='Épicerie', slug='epicerie',
        )
        self.marque = Brand.objects.create(
            organization=self.org, name='Coca-Cola', slug='coca-cola',
        )
        self.unite = Unit.objects.create(
            organization=self.org, name='Bouteille', symbol='btl',
            conversion_factor=Decimal('1.0000'),
        )

        self.client.force_authenticate(user=self.manager)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, kind, payload, op_id='11111111-1111-4111-8111-111111111111'):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-09-09T09:00:00Z',
                'payload': payload,
            }]},
            format='json', **self._headers(),
        )

    def _verdict(self, reponse, attendu='applied'):
        self.assertEqual(reponse.status_code, status.HTTP_200_OK, reponse.data)
        verdict = reponse.data['results'][0]
        self.assertEqual(
            verdict['verdict'], attendu,
            f"verdict={verdict['verdict']} errors={verdict.get('errors')}",
        )
        return verdict


class ModificationDeCategorieTests(_ReferentielBaseTest):
    def test_une_categorie_est_renommee_par_le_journal(self):
        self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id), 'name': 'Sodas et jus',
        }))
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.name, 'Sodas et jus')

    def test_une_categorie_garde_son_propre_nom_quand_le_parent_bouge(self):
        """
        LE TEST CENTRAL DU LOT.

        Renvoyer le nom ACTUEL en changeant le parent est le cas ordinaire : le
        formulaire poste tous ses champs, pas seulement ceux qui ont bougé. Avec
        `CategoryCreateSerializer`, cette opération est refusée sur « Il existe
        déjà une catégorie avec ce même nom » - la catégorie se heurtant à
        elle-même.
        """
        self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id),
            'name': self.sodas.name,
            'parent': str(self.epicerie.id),
        }))
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.name, 'Sodas')
        self.assertEqual(self.sodas.parent_id, self.epicerie.id)

    def test_une_categorie_est_detachee_par_un_parent_nul(self):
        """
        `parent: None` est la SEULE clé que le terminal envoie à `null`.

        Sous `partial=True`, une clé absente veut dire « ne touche pas » :
        omettre `parent` rendrait impossible de détacher une sous-catégorie pour
        en faire une racine, le geste inverse de celui que ce lot ouvre.
        """
        self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id), 'name': 'Sodas', 'parent': None,
        }))
        self.sodas.refresh_from_db()
        self.assertIsNone(self.sodas.parent_id)

    def test_une_categorie_ne_peut_pas_passer_sous_sa_propre_descendance(self):
        """Refus DÉTERMINISTE : quarantaine, jamais un nouvel essai."""
        verdict = self._journal('category.update', {
            'id': str(self.boissons.id), 'parent': str(self.sodas.id),
        })
        self._verdict(verdict, 'rejected')
        self.boissons.refresh_from_db()
        self.assertIsNone(self.boissons.parent_id)

    def test_une_categorie_supprimee_en_douceur_est_refusee(self):
        self.sodas.soft_delete()
        verdict = self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id), 'name': 'Sodas et jus',
        }), 'rejected')
        self.assertEqual(verdict['errors']['code'], 'not_found')

    def test_une_categorie_dune_autre_organisation_est_invisible(self):
        autre = Organization.objects.create(name='Autre', slug='autre')
        etrangere = Category.objects.create(
            organization=autre, name='Étrangère', slug='etrangere',
        )
        verdict = self._verdict(self._journal('category.update', {
            'id': str(etrangere.id), 'name': 'Volée',
        }), 'rejected')
        self.assertEqual(verdict['errors']['code'], 'not_found')
        etrangere.refresh_from_db()
        self.assertEqual(etrangere.name, 'Étrangère')

    def test_un_nom_deja_pris_est_refuse_a_la_CASSE_pres(self):
        verdict = self._journal('category.update', {
            'id': str(self.sodas.id), 'name': 'boissons',
        })
        self._verdict(verdict, 'rejected')
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.name, 'Sodas')

    def test_desactiver_voyage_SEUL(self):
        """
        Ce test démontre `partial=True`.

        Sans lui, `CategoryDetailSerializer` exigerait `name` et `slug`, et un
        simple basculement d'activité serait refusé pour deux champs qu'on ne
        modifiait pas.
        """
        self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id), 'is_active': False,
        }))
        self.sodas.refresh_from_db()
        self.assertFalse(self.sodas.is_active)
        self.assertEqual(self.sodas.name, 'Sodas')
        self.assertEqual(self.sodas.slug, 'sodas')

    def test_la_meme_operation_deux_fois_est_un_DOUBLON(self):
        corps = {'id': str(self.sodas.id), 'name': 'Sodas et jus'}
        self._verdict(self._journal('category.update', corps))
        self._verdict(self._journal('category.update', corps), 'duplicate')
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.name, 'Sodas et jus')


class ModificationDeMarqueEtUniteTests(_ReferentielBaseTest):
    def test_une_marque_est_renommee_par_un_MAGASINIER(self):
        """`products.edit` va jusqu'au magasinier, contrairement à `categories.edit`."""
        self.client.force_authenticate(user=self.magasinier)
        self._verdict(self._journal('brand.update', {
            'id': str(self.marque.id), 'name': 'Coca-Cola RDC',
        }))
        self.marque.refresh_from_db()
        self.assertEqual(self.marque.name, 'Coca-Cola RDC')

    def test_un_magasinier_ne_peut_PAS_modifier_une_categorie(self):
        """
        Verdict `blocked`, et non `rejected` : conservée, non réessayée, et elle
        repartira SEULE le jour où le droit sera accordé. Cette paire avec le
        test précédent est la seule qui prouve que les deux actes ne partagent
        pas une permission par accident.
        """
        self.client.force_authenticate(user=self.magasinier)
        self._verdict(self._journal('category.update', {
            'id': str(self.sodas.id), 'name': 'Sodas et jus',
        }), 'blocked')
        self.sodas.refresh_from_db()
        self.assertEqual(self.sodas.name, 'Sodas')

    def test_une_unite_renommee_garde_sa_CONVERSION(self):
        self.unite.conversion_factor = Decimal('12.0000')
        self.unite.save(update_fields=['conversion_factor'])
        self._verdict(self._journal('unit.update', {
            'id': str(self.unite.id), 'name': 'Casier', 'symbol': 'csr',
        }))
        self.unite.refresh_from_db()
        self.assertEqual(self.unite.name, 'Casier')
        self.assertEqual(self.unite.symbol, 'csr')
        self.assertEqual(self.unite.conversion_factor, Decimal('12.0000'))
        self.assertIsNone(self.unite.base_unit_id)

    def test_une_unite_REELLEMENT_supprimee_est_refusee(self):
        """
        `Unit` n'a pas de suppression douce, donc `PullTable('units', ...)` n'a
        pas `soft_delete=True`, donc aucune pierre tombale ne descend : une
        unité supprimée dans le back-office SURVIT sur chaque terminal, listée
        et proposée à la création d'article. Le terminal enverra donc des
        `unit.update` sur des unités fantômes, et ce chemin est emprunté pour
        de vrai.
        """
        identifiant = str(self.unite.id)
        self.unite.delete()
        verdict = self._verdict(self._journal('unit.update', {
            'id': identifiant, 'name': 'Casier', 'symbol': 'csr',
        }), 'rejected')
        self.assertEqual(verdict['errors']['code'], 'not_found')

    def test_un_identifiant_absent_est_un_refus_DETERMINISTE(self):
        verdict = self._verdict(
            self._journal('brand.update', {'name': 'Sans cible'}), 'rejected')
        self.assertEqual(verdict['errors']['code'], 'missing_fields')
