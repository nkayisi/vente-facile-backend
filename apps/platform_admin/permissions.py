"""
Les gardes de l'administration de la plateforme.

Deux étages, comme côté marchand (`IsTenantMember` puis `HasPermission`) :
`IsPlatformAdmin` dit si l'on entre, `HasPlatformPermission` ce qu'on y fait.
"""
from rest_framework import permissions

from apps.core.api_permissions import DENY

from .services import PlatformPermissionService, est_admin_plateforme


class IsPlatformAdmin(permissions.BasePermission):
    """
    Un profil d'administrateur ACTIF, ou un superutilisateur.

    ⚠ `is_staff` SEUL NE SUFFIT PLUS. Il se pose aussi depuis l'admin Django, sans
    passer par l'octroi d'un rôle ni par le journal d'audit : le laisser ouvrir la
    porte rendrait le rôle facultatif, c'est-à-dire décoratif.
    """

    message = "Accès réservé aux administrateurs de la plateforme."

    def has_permission(self, request, view):
        return est_admin_plateforme(getattr(request, 'user', None))


class HasPlatformPermission(permissions.BasePermission):
    """
    Permission granulaire, lue dans `view.action_permissions`.

    Mêmes règles que `apps.core.api_permissions.HasPermission`, et pour les mêmes
    raisons :
    - une action ABSENTE de la table est refusée : une action oubliée doit se
      fermer, pas s'ouvrir ;
    - `DENY` déclare une fermeture voulue, pour qu'on la distingue d'un oubli ;
    - une liste ou un tuple vaut « l'une de ces permissions ».

    ⚠ Une `APIView` sans `action` lit la clé de sa MÉTHODE HTTP en minuscules
    (`get`, `patch`…). Sans cela, ces vues passeraient toujours, faute d'action
    à chercher dans la table.
    """

    message = "Votre rôle sur la plateforme ne permet pas cette action."

    def has_permission(self, request, view):
        # ⚠ UNE MÉTHODE FERMÉE N'EST PAS UNE PERMISSION MANQUANTE. DRF contrôle les
        # permissions AVANT de répondre 405 : sans ce passage, un `DELETE` fermé
        # par `http_method_names` répondait « rôle insuffisant » au super-admin
        # lui-même, et on chercherait un droit là où il n'y a qu'une méthode
        # retirée. Laisser passer ne peut rien ouvrir : DRF ne distribue que les
        # méthodes autorisées ET pourvues d'un gestionnaire, et répond 405 au reste.
        methode = request.method.lower()
        if methode not in view.http_method_names or getattr(view, methode, None) is None:
            return True

        table = getattr(view, 'action_permissions', None)
        if not table:
            return False
        action = getattr(view, 'action', None) or request.method.lower()
        requise = table.get(action)
        if requise is None or requise == DENY:
            return False
        effectives = PlatformPermissionService.get_effective_permissions(request.user)
        if isinstance(requise, (list, tuple, set, frozenset)):
            return any(p in effectives for p in requise)
        return requise in effectives
