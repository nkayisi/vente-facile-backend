"""
Les modèles propres à l'administration de la plateforme.

┌──────────────────────────────────────────────────────────────────────────────┐
│ `is_staff` NE DIT PLUS QUI ADMINISTRE LA PLATEFORME.                        │
│                                                                              │
│ Un seul booléen donnait jusqu'ici un pouvoir TOTAL : supprimer une           │
│ organisation, promouvoir n'importe qui, changer les tarifs. Il ne pouvait    │
│ pas distinguer le support - qui consulte, prolonge, réconcilie - de celui    │
│ qui décide des prix. Le rôle vit désormais ici, avec qui l'a accordé et      │
│ quand.                                                                       │
│                                                                              │
│ `is_staff` RESTE, et c'est délibéré : c'est la garde générique de Django,    │
│ et le back-office route encore ses comptes dessus. Il devient DÉRIVÉ, écrit  │
│ par `PlatformAdminService` et par lui seul - jamais par un signal, pour que  │
│ chaque changement de pouvoir soit un geste visible dans le journal d'audit.  │
└──────────────────────────────────────────────────────────────────────────────┘

Modèle séparé plutôt que champ sur `User` : `User` est le modèle le plus lu du
dépôt (tirage compris), et un besoin qui ne concerne qu'une app n'a pas à
peser sur ses migrations.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel, UUIDModel


class PlatformRole(models.TextChoices):
    """
    Deux rôles, et pas davantage tant que le besoin n'est pas mesuré.

    ⚠ `support` n'est PAS une lecture seule : il prolonge un abonnement,
    réconcilie un paiement, prend la main sur un compte pour diagnostiquer.
    Ce qui lui est refusé est ce qui ne se défait pas ou qui change les règles
    de la plateforme : tarifs, attribution des rôles, porte de mise à jour.
    """

    SUPPORT = 'support', 'Support'
    SUPER_ADMIN = 'super_admin', 'Super administrateur'


class PlatformAdminProfile(TimeStampedModel, UUIDModel):
    """
    Le pouvoir d'un compte sur la plateforme, et son histoire.

    Un profil RÉVOQUÉ n'est pas supprimé : `is_active` passe à faux et
    `revoked_by` / `revoked_at` disent qui et quand. Supprimer la ligne
    effacerait la seule trace de qui avait ce pouvoir, et à partir de quand.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='platform_admin_profile',
    )
    role = models.CharField(max_length=20, choices=PlatformRole.choices)
    is_active = models.BooleanField(default=True, db_index=True)
    # Permissions ajoutées au rôle, comme `OrganizationMembership.extra_permissions`.
    extra_permissions = models.JSONField(default=list, blank=True)

    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='platform_roles_granted',
    )
    granted_at = models.DateTimeField(default=timezone.now)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='platform_roles_revoked',
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'platform_admin_profiles'
        verbose_name = 'Administrateur plateforme'
        verbose_name_plural = 'Administrateurs plateforme'

    def __str__(self):
        etat = '' if self.is_active else ' (révoqué)'
        return f'{self.user.email} - {self.get_role_display()}{etat}'


class AdminActionLog(TimeStampedModel, UUIDModel):
    """
    Qui a fait quoi, sur quoi, et pourquoi, depuis l'administration.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LA CIBLE EST DU TEXTE, PAS UNE CLÉ ÉTRANGÈRE.                            │
    │                                                                          │
    │ Un journal d'audit doit survivre à ce qu'il décrit. Une FK en CASCADE   │
    │ ferait disparaître la trace avec l'organisation ; en SET_NULL elle       │
    │ laisserait une ligne qui ne dit plus sur QUOI. `target_repr` fige ce    │
    │ que la cible s'appelait au moment de l'acte : c'est ce qu'un lecteur     │
    │ cherche six mois plus tard.                                              │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ JAMAIS ÉCRIT PAR UN SIGNAL. Chaque ligne naît d'un appel explicite à
    `AuditLogService.record`, dans la même transaction que l'acte : un journal
    implicite est invisible en revue, et un journal écrit hors de la
    transaction peut décrire un acte qui n'a pas eu lieu.
    """

    class Action(models.TextChoices):
        CREATE = 'create', 'Création'
        UPDATE = 'update', 'Modification'
        TOGGLE_ACTIVE = 'toggle_active', 'Activation / désactivation'
        GRANT_ROLE = 'grant_role', "Octroi d'un rôle plateforme"
        CHANGE_ROLE = 'change_role', "Changement de rôle plateforme"
        REVOKE_ROLE = 'revoke_role', "Retrait d'un rôle plateforme"
        ACTIVATE_SUBSCRIPTION = 'activate_subscription', "Activation d'abonnement"
        EXTEND_SUBSCRIPTION = 'extend_subscription', "Prolongation d'abonnement"
        CANCEL_SUBSCRIPTION = 'cancel_subscription', "Annulation d'abonnement"
        UPDATE_APP_RELEASE = 'update_app_release', 'Porte de mise à jour modifiée'
        RECONCILE_PAYMENTS = 'reconcile_payments', 'Réconciliation des paiements'
        IMPERSONATE_START = 'impersonate_start', 'Début de prise en main'
        IMPERSONATE_END = 'impersonate_end', 'Fin de prise en main'

    class Target(models.TextChoices):
        ORGANIZATION = 'organization', 'Organisation'
        USER = 'user', 'Utilisateur'
        PLAN = 'plan', 'Plan'
        SUBSCRIPTION = 'subscription', 'Abonnement'
        PLATFORM_ADMIN = 'platform_admin', 'Administrateur plateforme'
        APP_RELEASE = 'app_release', 'Porte de mise à jour'
        PAYMENT = 'payment', 'Paiement'

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='platform_actions',
    )
    # Figé au moment de l'acte : l'acteur peut être supprimé ou renommé.
    actor_email = models.CharField(max_length=254, blank=True)
    action = models.CharField(max_length=30, choices=Action.choices, db_index=True)
    target_type = models.CharField(max_length=30, choices=Target.choices)
    target_id = models.CharField(max_length=64, blank=True)
    target_repr = models.CharField(max_length=255, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        db_table = 'platform_admin_action_logs'
        ordering = ['-created_at']
        verbose_name = "Action d'administration"
        verbose_name_plural = "Actions d'administration"
        indexes = [
            models.Index(fields=['actor', 'created_at']),
            models.Index(fields=['target_type', 'target_id']),
            models.Index(fields=['action', 'created_at']),
        ]

    def __str__(self):
        return f'{self.actor_email or "?"} · {self.get_action_display()} · {self.target_repr}'


class ImpersonationSession(TimeStampedModel, UUIDModel):
    """
    Une prise en main : un administrateur voit l'application comme un marchand.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LECTURE SEULE, COURTE, ET TRACÉE DES DEUX CÔTÉS.                         │
    │                                                                          │
    │ - Le jeton ne vit que `DUREE_MINUTES` et n'a PAS de jeton de             │
    │   rafraîchissement : il ne peut ni se prolonger ni survivre à son        │
    │   échéance.                                                              │
    │ - Aucune écriture ne passe sous ce jeton (voir                           │
    │   `apps.core.authentication`) : on regarde, on ne vend pas au nom de     │
    │   quelqu'un.                                                             │
    │ - Le journal de la plateforme ET celui de l'organisation la portent : le │
    │   marchand doit pouvoir savoir que quelqu'un a regardé ses chiffres.     │
    └──────────────────────────────────────────────────────────────────────────┘
    """

    DUREE_MINUTES = 20

    class EndReason(models.TextChoices):
        MANUAL = 'manual', 'Terminée'
        EXPIRED = 'expired', 'Expirée'
        REPLACED = 'replaced', 'Remplacée par une autre'
        REVOKED = 'revoked', 'Révoquée'

    impersonator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='impersonation_sessions_started',
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='impersonation_sessions_received',
    )
    organization = models.ForeignKey(
        'organizations.Organization', on_delete=models.CASCADE,
        related_name='impersonation_sessions',
    )
    reason = models.TextField()
    jti = models.CharField(max_length=255, unique=True)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=20, choices=EndReason.choices, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)

    class Meta:
        db_table = 'platform_admin_impersonation_sessions'
        ordering = ['-started_at']
        indexes = [
            models.Index(fields=['impersonator', 'started_at']),
            models.Index(fields=['target_user', 'started_at']),
            models.Index(fields=['organization', 'started_at']),
        ]

    def __str__(self):
        return f'{self.impersonator_id} → {self.target_user_id} ({self.organization_id})'

    @property
    def is_live(self):
        """Ouverte, et pas encore échue. C'est la seule question que pose l'authentification."""
        return self.ended_at is None and self.expires_at > timezone.now()
