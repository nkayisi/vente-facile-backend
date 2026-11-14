"""
Les règles de l'administration de la plateforme, en un seul endroit.

Trois services, et chacun répond à une question :
- `PlatformPermissionService` : que peut faire ce compte sur la plateforme ?
- `AuditLogService` : qu'a-t-il fait ?
- `PlatformAdminService` : qui peut accorder ou retirer ce pouvoir, et comment ?

⚠ `ATOMIC_REQUESTS` EST À `False` DANS CE PROJET, et c'est délibéré (le journal
de synchronisation en dépend). Toute écriture en plusieurs étapes porte donc son
propre `transaction.atomic` : un acte et sa ligne d'audit naissent ensemble, ou
aucun des deux ne naît.
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from apps.core.adresse import adresse_client
from .models import AdminActionLog, PlatformAdminProfile, PlatformRole
from .permissions_catalog import PLATFORM_PERMISSION_CATALOG


class RefusAdministration(APIException):
    """
    Un refus de règle, rendu en 400 avec une PHRASE dans `detail`.

    Une `ValidationError` rendrait `{"detail": ["…"]}`, une liste, là où le
    back-office lit une chaîne : le message s'afficherait entre crochets, ou pas
    du tout.
    """

    status_code = 400
    default_detail = "Action refusée."
    default_code = 'platform_admin_refused'


# =============================================================================
# QUI ADMINISTRE
# =============================================================================

def profil_admin(user):
    """
    Le profil ACTIF de ce compte, ou `None`.

    Mémoïsé sur l'instance : la permission, la vue et le sérialiseur le
    demandent tous au cours d'une même requête, et un `OneToOne` relu trois fois
    est trois requêtes pour une réponse qui ne bouge pas.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if not hasattr(user, '_profil_admin_plateforme'):
        try:
            profil = user.platform_admin_profile
        except PlatformAdminProfile.DoesNotExist:
            profil = None
        user._profil_admin_plateforme = profil if (profil and profil.is_active) else None
    return user._profil_admin_plateforme


def oublier_profil(user):
    """
    Efface ce que l'instance croit savoir de son profil.

    ⚠ DEUX caches, et il faut les deux : notre mémo, et celui que Django pose sur
    la relation inverse. Un profil relu par une autre requête (`select_for_update`)
    est un autre objet ; sans cette purge, l'instance garderait l'ancien - actif
    après un retrait, sans ses extras après un changement.
    """
    user.__dict__.pop('_profil_admin_plateforme', None)
    user._state.fields_cache.pop('platform_admin_profile', None)


def est_admin_plateforme(user):
    """
    Vrai pour un profil actif, ou pour un superutilisateur.

    ⚠ LE SUPERUTILISATEUR PASSE TOUJOURS, et ce n'est pas une facilité : c'est le
    compte créé par `createsuperuser`, avant toute migration et tout profil. Sans
    cette porte, une erreur dans les profils enfermerait dehors le seul compte
    capable de la réparer.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if not user.is_active:
        return False
    if user.is_superuser:
        return True
    return profil_admin(user) is not None


def role_de(user):
    """Le rôle à AFFICHER : un superutilisateur sans profil vaut super-admin."""
    profil = profil_admin(user)
    if profil is not None:
        return profil.role
    if user is not None and getattr(user, 'is_superuser', False):
        return PlatformRole.SUPER_ADMIN
    return None


class PlatformPermissionService:
    """
    Les permissions d'un compte sur la plateforme : son rôle, plus ses extras.

    Miroir de `apps.core.services.PermissionService`, pour le même motif : une
    permission se lit dans une table déclarée, jamais dans un `if role == ...`
    éparpillé dans les vues.
    """

    #: Ce que le support peut faire. Tout ce qui n'est pas listé lui est refusé.
    #:
    #: Il consulte TOUT, et agit sur ce qui se défait : réactiver une organisation,
    #: prolonger un abonnement, rattraper un paiement. Ce qui lui est refusé ne se
    #: défait pas, ou change les règles de la plateforme pour tout le monde :
    #: annuler un abonnement, toucher aux tarifs, nommer un administrateur,
    #: poser un seuil de mise à jour qui peut bloquer tout le parc.
    SUPPORT = frozenset({
        'organizations.view',
        'organizations.toggle_active',
        'users.view',
        'users.toggle_active',
        # ⚠ Contrepartie ASSUMÉE (décision du 2026-11-13) : le support connaît le
        # mot de passe qu'il dicte, donc il peut se connecter en écriture, ce que
        # la prise en main lui refuse. Le changement obligatoire à la prochaine
        # connexion, le motif et le journal d'audit en sont les garde-fous. Un
        # administrateur de la plateforme, lui, ne se réinitialise que par
        # `platform_admins.manage` (`reinitialiser_mot_de_passe`).
        'users.reset_password',
        'platform_admins.view',
        'plans.view',
        'subscriptions.view',
        'subscriptions.extend',
        'app_release.view',
        'audit_log.view',
        'incidents.view',
        'incidents.reconcile_payments',
        'metrics.view',
        'impersonation.start',
        # Le support écrit les notes : c'est lui qui décroche.
        'support_notes.view',
        'support_notes.create',
        # ⚠ PAS `exports.run` : un export emporte la liste de tous les marchands
        # hors de l'application, ce qu'aucune consultation ne fait.
    })

    ROLE_PERMISSIONS = {
        PlatformRole.SUPPORT: SUPPORT,
        PlatformRole.SUPER_ADMIN: PLATFORM_PERMISSION_CATALOG,
    }

    @classmethod
    def get_effective_permissions(cls, user):
        """
        Les permissions effectives, ou un ensemble vide.

        ⚠ Un extra absent du catalogue est IGNORÉ : une faute de frappe dans un
        profil ne doit pas ouvrir une action qui n'existe pas, et ne doit pas non
        plus faire lever toute la requête.
        """
        if not est_admin_plateforme(user):
            return frozenset()
        if user.is_superuser and profil_admin(user) is None:
            return PLATFORM_PERMISSION_CATALOG
        profil = profil_admin(user)
        base = cls.ROLE_PERMISSIONS.get(profil.role, frozenset())
        extras = {p for p in (profil.extra_permissions or []) if p in PLATFORM_PERMISSION_CATALOG}
        return frozenset(base | extras)

    @classmethod
    def has(cls, user, code):
        return code in cls.get_effective_permissions(user)


# =============================================================================
# CE QU'IL A FAIT
# =============================================================================

class AuditLogService:
    """L'unique écrivain du journal d'audit."""

    @staticmethod
    def record(*, actor, action, target_type, target_id='', target_repr='',
               before=None, after=None, reason='', request=None):
        """
        Inscrit un acte.

        ⚠ À appeler DANS la transaction de l'acte : écrite après coup, la ligne
        décrirait un acte dont le commit a pu échouer ; écrite avant, elle
        survivrait à un acte annulé.
        """
        return AdminActionLog.objects.create(
            actor=actor if getattr(actor, 'pk', None) else None,
            actor_email=getattr(actor, 'email', '') or '',
            action=action,
            target_type=target_type,
            target_id=str(target_id or ''),
            target_repr=(target_repr or '')[:255],
            before=before or {},
            after=after or {},
            reason=reason or '',
            ip_address=adresse_client(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') if request is not None else ''),
        )


# =============================================================================
# QUI PEUT ACCORDER CE POUVOIR
# =============================================================================

def _comptes_capables_de_tout(exclure_user_id=None):
    """
    Le nombre de comptes ACTIFS qui pourraient encore nommer un administrateur.

    Un superutilisateur compte, un super-admin actif aussi. C'est la borne qui
    interdit de retirer le dernier : sans elle, la plateforme n'aurait plus
    personne pour réparer l'erreur.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()

    supers = User.objects.filter(is_active=True, is_superuser=True)
    profils = PlatformAdminProfile.objects.filter(
        is_active=True, role=PlatformRole.SUPER_ADMIN, user__is_active=True,
    )
    if exclure_user_id is not None:
        supers = supers.exclude(pk=exclure_user_id)
        profils = profils.exclude(user_id=exclure_user_id)
    ids = set(supers.values_list('pk', flat=True)) | set(profils.values_list('user_id', flat=True))
    return len(ids)


def _instantane(profil):
    if profil is None:
        return {}
    return {
        'role': profil.role,
        'is_active': profil.is_active,
        'extra_permissions': list(profil.extra_permissions or []),
    }


class PlatformAdminService:
    """
    Accorder, changer, retirer un rôle de la plateforme.

    ⚠ `is_staff` SUIT, et c'est le seul endroit qui l'écrit. Le laisser à un
    signal ferait d'un changement de pouvoir un effet de bord invisible ; le
    laisser aux vues le ferait diverger du profil à la première vue oubliée.
    """

    @staticmethod
    @transaction.atomic
    def grant(user, role, *, granted_by, extra_permissions=None, notes='', request=None):
        """Accorde ou change le rôle de `user`. Rend le profil."""
        if role not in PlatformRole.values:
            raise RefusAdministration("Rôle inconnu.")
        if granted_by is not None and granted_by.pk == user.pk:
            raise RefusAdministration("Vous ne pouvez pas modifier votre propre rôle.")
        if not user.is_active:
            raise RefusAdministration(
                "Ce compte est désactivé : réactivez-le avant de lui confier un rôle."
            )
        extras = sorted({p for p in (extra_permissions or []) if p in PLATFORM_PERMISSION_CATALOG})

        profil = (
            PlatformAdminProfile.objects.select_for_update()
            .filter(user=user).first()
        )
        avant = _instantane(profil)

        # Rétrograder le dernier super-admin enfermerait la plateforme dehors.
        if (
            profil is not None and profil.is_active
            and profil.role == PlatformRole.SUPER_ADMIN
            and role != PlatformRole.SUPER_ADMIN
            and not user.is_superuser
            and _comptes_capables_de_tout(exclure_user_id=user.pk) == 0
        ):
            raise RefusAdministration(
                "C'est le dernier super-administrateur : nommez-en un autre avant de le rétrograder."
            )

        if profil is None:
            profil = PlatformAdminProfile(user=user)
            action = AdminActionLog.Action.GRANT_ROLE
        elif not profil.is_active:
            action = AdminActionLog.Action.GRANT_ROLE
        else:
            action = AdminActionLog.Action.CHANGE_ROLE

        profil.role = role
        profil.is_active = True
        profil.extra_permissions = extras
        profil.notes = notes or profil.notes
        if action == AdminActionLog.Action.GRANT_ROLE:
            profil.granted_by = granted_by
            profil.granted_at = timezone.now()
            profil.revoked_by = None
            profil.revoked_at = None
        profil.save()

        if not user.is_staff:
            user.is_staff = True
            user.save(update_fields=['is_staff', 'updated_at'])
        oublier_profil(user)

        AuditLogService.record(
            actor=granted_by, action=action,
            target_type=AdminActionLog.Target.PLATFORM_ADMIN,
            target_id=user.pk, target_repr=user.email,
            before=avant, after=_instantane(profil), reason=notes, request=request,
        )
        return profil

    @staticmethod
    @transaction.atomic
    def revoke(user, *, revoked_by, reason='', request=None):
        """Retire le rôle. Le profil reste, désactivé : c'est la trace."""
        if revoked_by is not None and revoked_by.pk == user.pk:
            raise RefusAdministration("Vous ne pouvez pas retirer votre propre rôle.")

        profil = (
            PlatformAdminProfile.objects.select_for_update()
            .filter(user=user, is_active=True).first()
        )
        if profil is None:
            raise RefusAdministration("Ce compte n'a pas de rôle sur la plateforme.")
        if (
            profil.role == PlatformRole.SUPER_ADMIN
            and not user.is_superuser
            and _comptes_capables_de_tout(exclure_user_id=user.pk) == 0
        ):
            raise RefusAdministration(
                "C'est le dernier super-administrateur : nommez-en un autre avant de lui retirer son rôle."
            )

        avant = _instantane(profil)
        profil.is_active = False
        profil.revoked_by = revoked_by
        profil.revoked_at = timezone.now()
        profil.save()

        # ⚠ Un superutilisateur garde `is_staff` : Django l'exige pour l'admin, et
        # son accès ne vient de toute façon pas de ce profil.
        if user.is_staff and not user.is_superuser:
            user.is_staff = False
            user.save(update_fields=['is_staff', 'updated_at'])
        oublier_profil(user)

        AuditLogService.record(
            actor=revoked_by, action=AdminActionLog.Action.REVOKE_ROLE,
            target_type=AdminActionLog.Target.PLATFORM_ADMIN,
            target_id=user.pk, target_repr=user.email,
            before=avant, after=_instantane(profil), reason=reason, request=request,
        )
        # ⚠ Le pouvoir retiré emporte les prises en main qu'il avait ouvertes :
        # sans cela, le support garderait les minutes qui leur restent.
        from .impersonation import fermer_sessions

        fermer_sessions(impersonator=user, acteur=revoked_by, raison=reason, request=request)
        return profil


# =============================================================================
# UN MOT DE PASSE OUBLIÉ
# =============================================================================

def reinitialiser_mot_de_passe(cible, mot_de_passe, *, acteur, motif, request=None):
    """
    Pose un mot de passe TEMPORAIRE, que le titulaire devra changer à sa prochaine
    connexion, et coupe tout ce qui était ouvert sous l'ancien.

    Rend `(sessions_fermees, terminaux_revoques)`.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ « TOUT COUPER » (décision du 2026-11-13), ET IL FAUT LES TROIS PIÈCES.   │
    │                                                                          │
    │ 1. Les jetons de RAFRAÎCHISSEMENT vont sur liste noire : le back-office  │
    │    ne peut plus prolonger sa session.                                    │
    │ 2. `sessions_revoked_at` tue les jetons d'ACCÈS déjà émis, qui vivraient │
    │    sinon encore trente minutes (`apps.core.authentication`).             │
    │ 3. Les TERMINAUX sont révoqués : un jeton d'appareil redonne des JWT     │
    │    sans mot de passe, pendant trente jours. Révoquer n'efface RIEN sur   │
    │    le téléphone : ses ventes en file partent au réenrôlement.            │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ LE MOT DE PASSE N'EST ÉCRIT NULLE PART : ni au journal, ni dans la réponse,
    ni dans un message d'erreur. Seule son empreinte atteint la base.
    """
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    from apps.users.models import Device, User, UserActivity

    if cible.pk == getattr(acteur, 'pk', None):
        raise RefusAdministration(
            "C'est votre propre compte : changez votre mot de passe depuis votre profil."
        )
    if not cible.is_active:
        raise RefusAdministration(
            "Ce compte est désactivé : réactivez-le d'abord, puis réinitialisez son mot de passe."
        )
    # ⚠ Même garde que la désactivation : sans elle, le support poserait le mot
    # de passe d'un super-administrateur, et entrerait avec ses pouvoirs.
    est_admin = cible.is_superuser or profil_admin(cible) is not None
    if est_admin and not PlatformPermissionService.has(acteur, 'platform_admins.manage'):
        raise RefusAdministration(
            "Ce compte administre la plateforme : seul un super-administrateur peut "
            "réinitialiser son mot de passe."
        )

    maintenant = timezone.now()
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=cible.pk)
        user.set_password(mot_de_passe)
        user.must_change_password = True
        user.sessions_revoked_at = maintenant
        # ⚠ `updated_at` EXPLICITE : `User` n'hérite pas de `TimeStampedModel`.
        user.save(update_fields=[
            'password', 'must_change_password', 'sessions_revoked_at', 'updated_at',
        ])

        en_cours = list(
            OutstandingToken.objects
            .filter(user=user, expires_at__gt=maintenant, blacklistedtoken__isnull=True)
        )
        BlacklistedToken.objects.bulk_create(
            [BlacklistedToken(token=jeton) for jeton in en_cours], ignore_conflicts=True,
        )

        terminaux = list(Device.objects.filter(user=user, revoked_at__isnull=True))
        for terminal in terminaux:
            terminal.revoke(by=acteur)

        AuditLogService.record(
            actor=acteur, action=AdminActionLog.Action.RESET_PASSWORD,
            target_type=AdminActionLog.Target.USER,
            target_id=user.pk, target_repr=user.email,
            after={'sessions_fermees': len(en_cours), 'terminaux_revoques': len(terminaux)},
            reason=motif, request=request,
        )
        # ⚠ « Tout couper » vaut aussi pour les prises en main que CE compte avait
        # ouvertes : un jeton de prise en main échappe à `sessions_revoked_at`
        # (`apps.core.authentication`), et un administrateur dont le mot de passe
        # a fuité garderait sinon les minutes qui leur restent.
        if est_admin:
            from .impersonation import fermer_sessions

            fermer_sessions(impersonator=user, acteur=acteur, raison=motif, request=request)
        # Dans l'historique du TITULAIRE : c'est son mot de passe, et il doit
        # pouvoir lire qu'on l'a changé pour lui.
        UserActivity.objects.create(
            user=user,
            action=UserActivity.ActionType.UPDATE,
            resource_type='password',
            details={'method': 'admin_reset'},
            ip_address=adresse_client(request),
            user_agent=(request.META.get('HTTP_USER_AGENT', '') if request is not None else ''),
        )
    cible.must_change_password = True
    cible.sessions_revoked_at = maintenant
    return len(en_cours), len(terminaux)
