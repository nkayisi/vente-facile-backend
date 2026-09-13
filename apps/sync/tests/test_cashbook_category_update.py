"""
Modification d'une catégorie de caisse par le journal, à parité avec la vue.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE SERIALIZER EST LE `Create`, ET C'EST L'INVERSE DES PRODUITS.             │
│                                                                              │
│ `category.update` impose `CategoryDetailSerializer` parce que le             │
│ `CategoryCreateSerializer` des PRODUITS n'exclut pas la fiche modifiée de    │
│ son contrôle d'unicité. Recopier ce motif ici serait le défaut :             │
│ `IncomeCategoryCreateSerializer.validate` porte DÉJÀ son                     │
│ `exclude(pk=self.instance.pk)`, et c'est lui que                             │
│ `IncomeCategoryViewSet.get_serializer_class` retient pour `partial_update`.  │
│                                                                              │
│ Deux tests tiennent la frontière, dans les deux sens :                       │
│  - `test_une_categorie_garde_son_propre_nom` échoue si l'on prend le         │
│    `CategoryCreateSerializer` des produits par analogie ;                    │
│  - `test_un_nom_deja_pris_est_refuse_par_un_message_de_champ` échoue si l'on │
│    passe au `…DetailSerializer`, qui n'a AUCUN `validate()` : le doublon     │
│    lèverait alors un `IntegrityError` non rattrapé, et le verdict porterait  │
│    le texte brut de PostgreSQL.                                              │
└──────────────────────────────────────────────────────────────────────────────┘

**Le rôle est BORNÉ, jamais `owner`.** `cashbook.manage_categories` ne va qu'au
propriétaire et au GÉRANT (`PermissionService.ROLE_PERMISSIONS`) : on prend le
gérant pour les cas passants, et le CAISSIER pour démontrer le `blocked` - il a
`cashbook.create_expense` mais pas la gestion des rubriques, et c'est ce qui
prouve que l'acte n'hérite pas d'une permission par accident.
"""
from rest_framework import status
from rest_framework.test import APITestCase

from apps.cashbook.models import ExpenseCategory, IncomeCategory
from apps.organizations.models import Organization
from apps.sales.tests._helpers import make_org_with_users

OPERATIONS = '/api/v1/sync/operations/'


class _CategorieCaisseBaseTest(APITestCase):
    def setUp(self):
        ctx = make_org_with_users()
        self.__dict__.update(ctx)

        self.apport = IncomeCategory.objects.create(
            organization=self.org, name='Apport de fonds', color='#10B981',
        )
        self.subvention = IncomeCategory.objects.create(
            organization=self.org, name='Subvention', color='#10B981',
        )
        self.carburant = ExpenseCategory.objects.create(
            organization=self.org, name='Carburan', color='#6B7280',
        )
        self.loyer = ExpenseCategory.objects.create(
            organization=self.org, name='Loyer', color='#6B7280',
        )

        self.client.force_authenticate(user=self.manager)

    def _headers(self):
        return {'HTTP_X_ORGANIZATION_ID': str(self.org.id)}

    def _journal(self, kind, payload, op_id='22222222-2222-4222-8222-222222222222'):
        return self.client.post(
            OPERATIONS,
            {'operations': [{
                'operation_id': op_id, 'kind': kind, 'seq': 1,
                'depends_on': [], 'occurred_at': '2026-09-11T09:00:00Z',
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


class ModificationDeCategorieCaisseTests(_CategorieCaisseBaseTest):
    def test_une_categorie_de_depense_est_renommee_par_le_journal(self):
        """Le motif de la demande : « Carburan » se corrige en « Carburant »."""
        self._verdict(self._journal('expense_category.update', {
            'id': str(self.carburant.id), 'name': 'Carburant',
        }))
        self.carburant.refresh_from_db()
        self.assertEqual(self.carburant.name, 'Carburant')

    def test_un_type_d_entree_est_renomme_par_le_journal(self):
        self._verdict(self._journal('income_category.update', {
            'id': str(self.apport.id), 'name': 'Apport du propriétaire',
        }))
        self.apport.refresh_from_db()
        self.assertEqual(self.apport.name, 'Apport du propriétaire')

    def test_la_couleur_et_la_description_suivent(self):
        self._verdict(self._journal('expense_category.update', {
            'id': str(self.loyer.id),
            'name': 'Loyer', 'color': '#EF4444', 'description': 'Bail mensuel',
        }))
        self.loyer.refresh_from_db()
        self.assertEqual(self.loyer.color, '#EF4444')
        self.assertEqual(self.loyer.description, 'Bail mensuel')

    def test_une_categorie_garde_son_propre_nom(self):
        """
        ⚠ LE TEST QUI ATTRAPE LE SERIALIZER DES PRODUITS.

        Renvoyer `name` inchangé en modifiant autre chose est le cas ORDINAIRE
        d'un formulaire qui poste tous ses champs. Avec un serializer dont le
        contrôle d'unicité n'exclut pas la fiche, la catégorie se refuserait
        elle-même - refus déterministe, donc quarantaine, à chaque envoi.
        """
        self._verdict(self._journal('expense_category.update', {
            'id': str(self.loyer.id), 'name': 'Loyer', 'color': '#F59E0B',
        }))
        self.loyer.refresh_from_db()
        self.assertEqual(self.loyer.name, 'Loyer')
        self.assertEqual(self.loyer.color, '#F59E0B')

    def test_un_nom_deja_pris_est_refuse_par_un_message_de_champ(self):
        """
        ⚠ LE TEST QUI ATTRAPE LE `…DetailSerializer`.

        Il n'a aucun `validate()` : le doublon passerait le serializer et
        lèverait un `IntegrityError` sur la contrainte. On exige donc un refus
        DÉTERMINISTE portant une erreur sur le CHAMP `name`, et non du texte
        brut de PostgreSQL que l'écran ne saurait pas placer.
        """
        verdict = self._verdict(
            self._journal('expense_category.update', {
                'id': str(self.carburant.id), 'name': 'Loyer',
            }),
            attendu='rejected',
        )
        erreurs = verdict.get('errors') or {}
        self.assertIn('name', str(erreurs), erreurs)
        self.assertNotIn('duplicate key', str(erreurs).lower(), erreurs)
        self.carburant.refresh_from_db()
        self.assertEqual(self.carburant.name, 'Carburan')

    def test_le_doublon_est_insensible_a_la_casse(self):
        """`__iexact`, comme la vue - et comme le `toLowerCase` du terminal."""
        self._verdict(
            self._journal('income_category.update', {
                'id': str(self.apport.id), 'name': 'SUBVENTION',
            }),
            attendu='rejected',
        )

    def test_desactiver_retire_la_rubrique_sans_toucher_a_l_historique(self):
        """
        `is_active` est le levier retenu À LA PLACE de la suppression.

        `ExpenseCategory` est `PROTECT`-référencée et aucune des deux tables
        n'émet de pierre tombale au tirage : une suppression n'atteindrait
        jamais un terminal. Désactiver, si.
        """
        self._verdict(self._journal('expense_category.update', {
            'id': str(self.loyer.id), 'is_active': False,
        }))
        self.loyer.refresh_from_db()
        self.assertFalse(self.loyer.is_active)
        # La fiche existe toujours : l'historique la garde.
        self.assertTrue(ExpenseCategory.objects.filter(pk=self.loyer.pk).exists())

    def test_reactiver_est_possible(self):
        self.loyer.is_active = False
        self.loyer.save(update_fields=['is_active'])
        self._verdict(self._journal('expense_category.update', {
            'id': str(self.loyer.id), 'is_active': True,
        }))
        self.loyer.refresh_from_db()
        self.assertTrue(self.loyer.is_active)

    def test_une_categorie_d_une_autre_organisation_est_rejetee(self):
        """
        Refus DÉTERMINISTE, jamais `retry` : la fiche n'apparaîtra pas d'ici la
        prochaine synchronisation, et la réessayer serait la marteler.
        """
        autre = Organization.objects.create(name='Autre', slug='autre')
        etrangere = ExpenseCategory.objects.create(organization=autre, name='Ailleurs')
        self._verdict(
            self._journal('expense_category.update', {
                'id': str(etrangere.id), 'name': 'Volée',
            }),
            attendu='rejected',
        )
        etrangere.refresh_from_db()
        self.assertEqual(etrangere.name, 'Ailleurs')

    def test_un_identifiant_absent_est_rejete(self):
        self._verdict(
            self._journal('expense_category.update', {'name': 'Sans cible'}),
            attendu='rejected',
        )


class DroitDeGererLesRubriquesTests(_CategorieCaisseBaseTest):
    def test_un_caissier_est_bloque(self):
        """
        Le caissier a `cashbook.create_expense` mais PAS
        `cashbook.manage_categories`. Le verdict est `blocked` et non
        `rejected` : l'opération est CONSERVÉE et repartira seule dès qu'un
        gérant accordera le droit.
        """
        self.client.force_authenticate(user=self.cashier_a)
        self._verdict(
            self._journal('expense_category.update', {
                'id': str(self.loyer.id), 'name': 'Loyer et charges',
            }),
            attendu='blocked',
        )
        self.loyer.refresh_from_db()
        self.assertEqual(self.loyer.name, 'Loyer')
