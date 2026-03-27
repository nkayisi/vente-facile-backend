from rest_framework import permissions


class IsPlatformAdmin(permissions.BasePermission):
    """
    Only allows access to platform administrators (is_staff=True).
    """
    message = "Accès réservé aux administrateurs de la plateforme."

    def has_permission(self, request, view):
        return (
            request.user
            and request.user.is_authenticated
            and request.user.is_staff
        )
