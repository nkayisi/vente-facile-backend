"""
Le contrat de parité, tenu par balayage plutôt que par vigilance.

┌──────────────────────────────────────────────────────────────────────────────┐
│ « ACTION NON LISTÉE = ACCÈS REFUSÉ » EST UN PIÈGE SILENCIEUX.                │
│                                                                              │
│ `HasPermission` refuse toute action absente d'`action_permissions`. C'est la │
│ bonne règle - une action oubliée doit se fermer, pas s'ouvrir - mais elle    │
│ n'émet AUCUN signal au développeur : la vue existe, la route répond, et      │
│ c'est 403. Le front n'affiche rien, personne ne cherche un bug là où il n'y  │
│ a pas d'erreur.                                                              │
│                                                                              │
│ Le dépôt l'a payé trois fois : `product_supplies`, en 403 pour tous les      │
│ rôles alors que le frontend l'appelait ; `locked_products`, qui rendait le   │
│ verrou d'inventaire INERTE sur le web et a fait refuser une vente déjà       │
│ encaissée et imprimée ; et côté terminal `can("sales.refund")`, un code      │
│ inexistant qui faisait simplement disparaître un bouton.                     │
│                                                                              │
│ Ce test balaie les routes RÉELLEMENT enregistrées, pas une liste tenue à la  │
│ main : une action ajoutée sans permission fait échouer la suite en la        │
│ nommant, le jour où elle est écrite.                                         │
└──────────────────────────────────────────────────────────────────────────────┘

Ce qu'il ne couvre PAS, et qui relève d'autres fichiers : que la permission
déclarée soit la BONNE (`test_operation_permissions.py` la croise acte par acte
avec le journal), et que l'EFFET soit partagé entre la vue et le journal (les
fichiers `test_*_parity.py`).
"""
from django.test import SimpleTestCase
from django.urls import get_resolver, URLPattern, URLResolver

from apps.core.api_permissions import HasPermission


def _vues_enregistrees():
    """
    Les couples (classe de vue, actions) réellement servis par les URLs.

    On lit le résolveur plutôt qu'un inventaire de modules : ce qui compte est
    ce qu'un client peut ATTEINDRE. Une action non routée ne refuse personne,
    et une vue routée sans permission refuse tout le monde.

    DRF pose `cls` et `actions` sur la fonction rendue par `as_view()` ; les
    vues qui n'en portent pas (APIView simples, vues Django) sortent d'elles
    mêmes de ce balayage, n'ayant pas d'`action`.

    Une action dont la MÉTHODE HTTP est retirée par `http_method_names` est
    écartée : elle répond 405 avant toute permission, ce qui est une fermeture
    explicite et lisible, pas un oubli. C'est le cas de `DeviceViewSet`, qui
    n'accepte ni PUT ni DELETE.
    """
    trouvees = {}

    def descendre(patterns):
        for p in patterns:
            if isinstance(p, URLResolver):
                descendre(p.url_patterns)
            elif isinstance(p, URLPattern):
                callback = p.callback
                cls = getattr(callback, 'cls', None)
                actions = getattr(callback, 'actions', None)
                if cls is None or not actions:
                    continue
                autorisees = [m.lower() for m in getattr(cls, 'http_method_names', [])]
                trouvees.setdefault(cls, set()).update(
                    action for methode, action in actions.items()
                    if methode.lower() in autorisees
                )

    descendre(get_resolver().url_patterns)
    return trouvees


class ContratDesActionsTests(SimpleTestCase):
    """Balayage statique : ni base de données, ni requête."""

    def test_le_balayage_trouve_bien_des_vues(self):
        """
        Un test de balayage qui ne balaie rien PASSE, et ne prouve rien.

        C'est exactement le défaut qu'a connu `test_users_scope` sur le nom de
        la table pivot : chercher au mauvais endroit rend une liste vide, et
        une liste vide satisfait toutes les assertions qui suivent.
        """
        vues = _vues_enregistrees()
        self.assertGreater(len(vues), 30, "Le résolveur n'a presque rien rendu.")
        self.assertTrue(
            any(HasPermission in (v.permission_classes or []) for v in vues),
            "Aucune vue ne porte HasPermission : le balayage vise à côté.",
        )

    def test_aucune_action_sans_permission(self):
        """
        Toute action d'une vue gardée par `HasPermission` est DÉCLARÉE.

        `'*'` reste permis : c'est une déclaration explicite d'ouverture, elle
        se lit et se relit. L'absence, elle, ne se lit nulle part.
        """
        manquantes = []
        for vue, actions in sorted(_vues_enregistrees().items(), key=lambda kv: kv[0].__name__):
            if HasPermission not in (vue.permission_classes or []):
                continue
            declarees = getattr(vue, 'action_permissions', None)
            if not declarees:
                # Pas de table du tout : `HasPermission` laisse alors passer
                # (voir sa docstring). C'est un autre régime, pas un oubli.
                continue
            for action in sorted(actions):
                if action not in declarees:
                    manquantes.append(f"{vue.__name__}.{action}")

        self.assertEqual(
            manquantes, [],
            "Actions routées mais absentes d'`action_permissions`, donc en 403 "
            f"pour TOUS les rôles, sans erreur ni journal : {manquantes}",
        )
