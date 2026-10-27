"""L'inscription accepte TOUTE devise active de la plateforme.

Ce que ces tests protègent : un marchand de Douala, de Dakar ou de Kigali doit
pouvoir créer sa boutique dans SA monnaie. Le serializer portait un
`ChoiceField(choices=[CDF, USD])` pendant que le sélecteur des deux clients en
offrait vingt et une : choisir le franc CFA rendait 400, l'écran affichait
« « XAF » n'est pas un choix valide », et l'inscription était impossible.

⚠ CES TESTS ITÈRENT LA TABLE, ILS NE RECOPIENT PAS DE LISTE.
La table `Currency` est la source de vérité du serveur ; un test qui figerait les
vingt et un codes en dur serait une vingt-deuxième liste à tenir en phase, et il
faudrait penser à l'étendre le jour où un pays s'ouvre. Ici, ajouter une ligne en
base étend la couverture toute seule.
"""
from django.core.cache import cache
from rest_framework import status
from rest_framework.test import APITestCase

from apps.organizations.models import Organization
from apps.settings.models import Currency, OrganizationCurrency
from apps.users.models import User
from apps.users.serializers import RegisterWithOrganizationSerializer


def _corps(**extra):
    """Corps d'inscription valide, celui que les deux clients envoient."""
    corps = {
        'email': 'marchand@test.vf',
        'password': 'Bonjour@2026',
        'password_confirm': 'Bonjour@2026',
        'first_name': 'Awa',
        'last_name': 'Mbarga',
        'phone': '+237600000000',
        'organization_name': 'Boutique de Douala',
        'organization_phone': '+237600000001',
        'business_type': 'boutique',
        'country': 'Cameroun',
    }
    corps.update(extra)
    return corps


class DevisesAcceptesALInscriptionTests(APITestCase):
    """Le serializer, code par code."""

    def setUp(self):
        # ⚠ `CACHES` n'est LocMem que si DEBUG, donc Redis en test, et
        # l'`AnonRateThrottle` à 100/h PERSISTE d'un `pytest` à l'autre : sans
        # purge, un test paie pour les exécutions précédentes et échoue en 429
        # sans rapport avec ce qu'il vérifie.
        cache.clear()

    def test_toute_devise_active_est_acceptee(self):
        """⚠ C'est LE test du lot : il boucle sur la table, pas sur une liste.

        Il passe par le serializer et non par HTTP, pour deux raisons : vingt et un
        POST useraient le quota anonyme que `setUp` vient de purger, et ce qui
        s'éprouve ici est la VALIDATION, pas le trajet complet (couvert une fois
        plus bas).
        """
        codes = list(
            Currency.objects.filter(is_active=True)
            .order_by('code')
            .values_list('code', flat=True)
        )
        # Un balayage qui ne balaie rien passe au vert et ne prouve rien.
        self.assertGreaterEqual(
            len(codes), 21,
            "Le référentiel de devises est incomplet : la migration "
            "settings/0004 en pose 21."
        )

        refuses = []
        for code in codes:
            s = RegisterWithOrganizationSerializer(data=_corps(currency=code))
            s.is_valid()
            if 'currency' in s.errors:
                refuses.append((code, str(s.errors['currency'][0])))

        self.assertEqual(refuses, [], f"Devises actives refusées : {refuses}")

    def test_la_casse_et_les_espaces_sont_normalises(self):
        """« xaf » entre en base en « XAF », sinon deux codes désignent la même monnaie."""
        s = RegisterWithOrganizationSerializer(data=_corps(currency='  xaf '))
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['currency'], 'XAF')

    def test_un_code_inconnu_est_refuse(self):
        s = RegisterWithOrganizationSerializer(data=_corps(currency='ZZZ'))
        s.is_valid()
        self.assertIn('currency', s.errors)
        self.assertEqual(
            str(s.errors['currency'][0]),
            "La devise 'ZZZ' n'est pas prise en charge.",
        )

    def test_une_devise_desactivee_est_refusee(self):
        """⚠ C'est ce test qui rend `is_active` porteur.

        Sans lui, le filtre pourrait tomber à la seule présence en table, et une
        devise retirée du catalogue resterait inscriptible - alors qu'aucun écran
        ne l'offre plus.
        """
        Currency.objects.filter(code='XAF').update(is_active=False)
        s = RegisterWithOrganizationSerializer(data=_corps(currency='XAF'))
        s.is_valid()
        self.assertIn('currency', s.errors)


class InscriptionEnFrancCFATests(APITestCase):
    """Le trajet complet, une fois, et ce qu'il écrit en base."""

    def setUp(self):
        cache.clear()

    def test_une_boutique_en_xaf_se_cree_de_bout_en_bout(self):
        avant = Currency.objects.count()

        reponse = self.client.post(
            '/api/v1/auth/register-with-organization/',
            _corps(currency='XAF'),
            format='json',
        )
        self.assertEqual(reponse.status_code, status.HTTP_201_CREATED, reponse.data)

        org = Organization.objects.get(name='Boutique de Douala')
        self.assertEqual(org.currency, 'XAF')

        principale = OrganizationCurrency.objects.select_related('currency').get(
            organization=org, is_primary=True
        )
        self.assertEqual(principale.currency.code, 'XAF')
        self.assertEqual(principale.exchange_rate, 1)
        # Le franc CFA n'a pas de décimale : c'est ce chiffre qui décide de ce
        # qu'un ticket imprime.
        self.assertEqual(principale.currency.decimal_places, 0)
        self.assertEqual(principale.currency.symbol, 'FCFA')

        # ⚠ Preuve DIRECTE que le repli fabricant n'a pas tiré. Il insérait une
        # `Currency` au symbole et aux décimales faux pour tout code inconnu, et
        # l'établissement restait faux à vie.
        self.assertEqual(Currency.objects.count(), avant)

        self.assertTrue(User.objects.filter(email='marchand@test.vf').exists())

    def test_un_code_inconnu_n_est_jamais_fabrique(self):
        """⚠ C'est CE test qui garde le refus de fabriquer, pas le cas XAF.

        Sur une devise qui EST en table, le repli n'est pas atteint : son
        compteur ne prouve donc qu'une non-régression. Le seul chemin qui
        l'atteignait est un code inconnu, et c'est ici qu'on l'éprouve - le
        serializer refuse, et rien n'est écrit : ni organisation, ni compte, ni
        `Currency` au symbole et aux décimales faux.
        """
        avant_devises = Currency.objects.count()
        avant_comptes = User.objects.count()

        reponse = self.client.post(
            '/api/v1/auth/register-with-organization/',
            _corps(currency='ZZZ'),
            format='json',
        )

        # Le marchand lit une phrase, pas une erreur de choix.
        self.assertEqual(reponse.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(reponse.data['currency'][0]),
            "La devise 'ZZZ' n'est pas prise en charge.",
        )
        self.assertEqual(
            Currency.objects.count(), avant_devises,
            "Une devise fantôme a été fabriquée : l'établissement serait faux à vie.",
        )
        self.assertFalse(Organization.objects.filter(name='Boutique de Douala').exists())
        self.assertEqual(User.objects.count(), avant_comptes)
