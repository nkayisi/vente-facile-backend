"""
Catalogue canonique des permissions de l'administration PLATEFORME.

⚠ CE N'EST PAS `apps/core/permissions_catalog.py`, et les deux ne se mêlent
jamais. Celui-là décrit ce qu'un membre peut faire DANS une organisation ; celui-ci
ce qu'un administrateur de l'éditeur peut faire SUR toutes. Un même mot y désigne
deux choses différentes : `users.view` du marchand lit son équipe, celui de la
plateforme lit tous les comptes. C'est pourquoi le back-office les interroge par
deux fonctions distinctes (`hasPermission` / `hasPlatformPermission`), et
`pnpm check:permissions` les vérifie chacun contre SON catalogue.

Toute nouvelle permission de la plateforme s'ajoute ici, puis à
`PlatformPermissionService.ROLE_PERMISSIONS` : une permission que personne ne
porte ferme une action pour tout le monde, sans rien signaler.
"""

PLATFORM_PERMISSION_CATALOG = frozenset(
    {
        # Organisations
        "organizations.view",
        "organizations.edit",
        "organizations.toggle_active",
        "organizations.activate_subscription",
        # Comptes
        "users.view",
        "users.create",
        "users.edit",
        "users.toggle_active",
        # Administrateurs de la plateforme
        "platform_admins.view",
        "platform_admins.manage",
        # Tarifs
        "plans.view",
        "plans.create",
        "plans.edit",
        "plans.toggle_active",
        # Abonnements
        "subscriptions.view",
        "subscriptions.create",
        "subscriptions.extend",
        "subscriptions.cancel",
        # Porte de mise à jour du terminal
        "app_release.view",
        "app_release.edit",
        # Modération et suivi
        "audit_log.view",
        "incidents.view",
        "incidents.reconcile_payments",
        "metrics.view",
        # Prise en main d'un compte, pour le support
        "impersonation.start",
        # Arrêter celle d'un autre : réservé au super-administrateur
        "impersonation.end",
    }
)
