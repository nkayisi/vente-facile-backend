"""
Toute action de l'administration est DÉCLARÉE, et tout code qu'elle cite EXISTE.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UNE ACTION OUBLIÉE NE LÈVE RIEN : ELLE RÉPOND 403, À TOUT LE MONDE.         │
│                                                                              │
│ `HasPlatformPermission` refuse ce qui n'est pas dans `action_permissions`.  │
│ C'est la bonne règle - une action oubliée se ferme - mais elle ne signale    │
│ rien : le bouton répond « rôle insuffisant » au super-admin lui-même, et     │
│ personne ne cherche un défaut là où il n'y a pas d'erreur. Même contrat que  │
│ `apps/sync/tests/test_parity_contract.py` côté marchand.                     │
└──────────────────────────────────────────────────────────────────────────────┘
"""
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver

from apps.core.api_permissions import DENY
from apps.platform_admin.permissions import HasPlatformPermission
from apps.platform_admin.permissions_catalog import PLATFORM_PERMISSION_CATALOG
from apps.platform_admin.services import PlatformPermissionService


def _vues_du_module():
    """`{classe: actions routées}` pour toute vue de `apps.platform_admin`."""
    trouvees = {}

    def descendre(patterns):
        for p in patterns:
            if isinstance(p, URLResolver):
                descendre(p.url_patterns)
            elif isinstance(p, URLPattern):
                cls = getattr(p.callback, 'view_class', None) or getattr(p.callback, 'cls', None)
                if cls is None or not cls.__module__.startswith('apps.platform_admin'):
                    continue
                autorisees = {m.lower() for m in getattr(cls, 'http_method_names', [])}
                actions = getattr(p.callback, 'actions', None)
                if actions:
                    noms = {a for m, a in actions.items() if m.lower() in autorisees}
                else:
                    # Une APIView : sa clé est la méthode HTTP.
                    noms = {
                        m for m in autorisees
                        if m not in ('options', 'head') and hasattr(cls, m)
                    }
                trouvees.setdefault(cls, set()).update(noms)

    descendre(get_resolver().url_patterns)
    return trouvees


def _codes(valeur):
    if isinstance(valeur, (list, tuple, set, frozenset)):
        return set(valeur)
    return {valeur}


class ContratTests(SimpleTestCase):
    def test_le_balayage_trouve_bien_des_vues(self):
        """Un balayage qui ne balaie rien passe, et ne prouve rien."""
        self.assertGreaterEqual(len(_vues_du_module()), 9)

    def test_toute_vue_porte_la_permission_granulaire(self):
        """
        Deux exceptions, nommées :
        - `AdminMeView` dit à l'administrateur ce qu'il peut faire, il faut donc
          qu'il puisse la lire ;
        - `AdminImpersonationEndView` est appelée avec le jeton de PRISE EN MAIN,
          dont le porteur est le marchand : c'est la seule écriture qu'il puisse
          faire, et elle ne sert qu'à sortir.
        """
        sans = sorted(
            cls.__name__ for cls in _vues_du_module()
            if HasPlatformPermission not in (cls.permission_classes or [])
        )
        self.assertEqual(sans, ['AdminImpersonationEndView', 'AdminMeView'])

    def test_toute_action_routee_est_declaree(self):
        manquantes = []
        for cls, actions in _vues_du_module().items():
            if HasPlatformPermission not in (cls.permission_classes or []):
                continue
            table = getattr(cls, 'action_permissions', {}) or {}
            for nom in sorted(actions):
                if nom not in table:
                    manquantes.append(f'{cls.__name__}.{nom}')
        self.assertEqual(manquantes, [], "Actions routées sans permission déclarée : elles répondent 403 à tous.")

    def test_tout_code_cite_existe_au_catalogue(self):
        inconnus = []
        for cls in _vues_du_module():
            for action, valeur in (getattr(cls, 'action_permissions', {}) or {}).items():
                for code in _codes(valeur):
                    if code != DENY and code not in PLATFORM_PERMISSION_CATALOG:
                        inconnus.append(f'{cls.__name__}.{action} → {code}')
        self.assertEqual(inconnus, [])

    def test_tout_code_du_catalogue_est_porte_par_un_role(self):
        """Une permission que personne ne porte ferme son action pour tous."""
        portees = set().union(*PlatformPermissionService.ROLE_PERMISSIONS.values())
        self.assertEqual(sorted(PLATFORM_PERMISSION_CATALOG - portees), [])

    def test_le_support_ne_porte_que_des_codes_du_catalogue(self):
        self.assertEqual(sorted(PlatformPermissionService.SUPPORT - PLATFORM_PERMISSION_CATALOG), [])

    def test_aucune_suppression_n_est_routee(self):
        """
        `DELETE` exécutait un vrai DELETE SQL, en cascade. Il est fermé par
        `http_method_names`, pas seulement par la table : une table se corrige
        d'une ligne, une méthode retirée ne se rouvre pas par mégarde.
        """
        interdites = {'destroy', 'update', 'delete', 'put'}
        ouvertes = sorted(
            f'{cls.__name__}.{nom}'
            for cls, actions in _vues_du_module().items()
            for nom in actions & interdites
        )
        self.assertEqual(ouvertes, [])
