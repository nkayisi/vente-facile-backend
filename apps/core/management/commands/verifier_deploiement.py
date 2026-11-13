"""
Ce qu'un déploiement doit avoir laissé derrière lui, vérifié en une commande.

┌──────────────────────────────────────────────────────────────────────────────┐
│ LE 9 NOVEMBRE, UNE MIGRATION A ÉCHOUÉ EN PRODUCTION, ET PERSONNE NE S'EST   │
│ PLUS CONNECTÉ.                                                               │
│                                                                              │
│ `entrypoint.sh` est en `set -e`, donc le serveur n'a pas démarré du tout :   │
│ l'échec était bruyant, et c'est ce qui l'a rendu réparable en une heure.     │
│ Le défaut qu'on redoute ici est l'inverse - une migration qui PASSE et       │
│ laisse un état faux : un `is_staff` sans profil d'administration est         │
│ enfermé dehors le lendemain, sans qu'aucune erreur ne l'annonce.            │
│                                                                              │
│ Cette commande se lance sur la COPIE de la base avant de déployer            │
│ (`scripts/repetition_migrations.sh`), puis sur la vraie juste après. Elle    │
│ n'écrit RIEN : on peut la relancer autant de fois qu'on veut.               │
└──────────────────────────────────────────────────────────────────────────────┘

Trois niveaux, et seul le premier fait échouer la commande :

- **ERREUR** : un état qui empêche quelqu'un de travailler. Code de sortie 1.
- **AVERTISSEMENT** : un réglage qui fausse un journal sans rien bloquer.
- **OK** : ce qui a été vérifié, nommé, pour qu'on sache ce que la commande a
  regardé - une vérification muette ne se distingue pas d'une vérification
  qui n'a pas eu lieu.
"""
from dataclasses import dataclass

from django.apps import apps as registre
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.questioner import NonInteractiveMigrationQuestioner
from django.db.migrations.state import ProjectState
from django.utils import translation

ERREUR = 'ERREUR'
AVERTISSEMENT = 'AVERTISSEMENT'
OK = 'OK'

#: La tâche que `platform_admin/0003` pose en base. Sans elle, les journaux
#: grossissent sans borne, et rien ne le signale avant que le disque soit plein.
TACHE_DE_PURGE = 'purger_journaux_administration'


@dataclass(frozen=True)
class Constat:
    niveau: str
    rubrique: str
    message: str


def migrations_en_attente():
    """Les migrations connues du code et absentes de la base."""
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        noms = ', '.join(f'{m.app_label}.{m.name}' for m, _arriere in plan)
        return [Constat(ERREUR, 'migrations', f"{len(plan)} migration(s) non appliquée(s) : {noms}")]
    return [Constat(OK, 'migrations', "Toutes les migrations sont appliquées.")]


def modeles_sans_migration():
    """
    Un modèle modifié sans migration qui l'accompagne.

    C'est `makemigrations --check`, sans son effet de bord : on n'écrit rien et
    on ne quitte pas le processus. Un tel écart ne casse rien au déploiement -
    il casse le SUIVANT, qui découvrira une migration à créer sur la machine
    de production.
    """
    # ⚠ LES TRADUCTIONS SONT COUPÉES, comme `makemigrations` le fait par son
    #   décorateur `@no_translations`. Sous la langue du projet, chaque
    #   `verbose_name` de Django sort traduit et diffère de celui enregistré
    #   dans les migrations : la première écriture annonçait ainsi sept
    #   applications « modifiées » (admin, auth, sessions…) sur une base
    #   parfaitement à jour - un garde-fou qui crie sur du sain finit ignoré.
    with translation.override(None):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        detecteur = MigrationAutodetector(
            loader.project_state(),
            ProjectState.from_apps(registre),
            NonInteractiveMigrationQuestioner(specified_apps=set(), dry_run=True),
        )
        changements = detecteur.changes(graph=loader.graph)
    if changements:
        return [Constat(
            ERREUR, 'migrations',
            "Modèles modifiés sans migration : " + ', '.join(sorted(changements)),
        )]
    return [Constat(OK, 'migrations', "Aucun modèle modifié sans sa migration.")]


def administrateurs():
    """
    Chaque compte qui administrait la veille administre encore.

    ⚠ `is_staff` NE SUFFIT PLUS À ENTRER depuis la refonte du back-office : c'est
    le PROFIL qui ouvre l'administration. Un `is_staff` actif sans profil actif
    reçoit 403 sur chaque écran, et son jeton annonce `is_staff: false` au
    back-office - il croit son compte cassé. `platform_admin/0002` en pose un à
    chacun ; cette vérification dit si elle l'a fait, et si quelqu'un a été
    nommé depuis par l'admin Django sans passer par l'écran.
    """
    from apps.platform_admin.models import PlatformAdminProfile, PlatformRole
    from apps.platform_admin.services import _comptes_capables_de_tout

    User = registre.get_model('users', 'User')
    constats = []

    sans_profil = list(
        User.objects.filter(is_staff=True, is_active=True)
        .exclude(is_superuser=True)
        .exclude(platform_admin_profile__is_active=True)
        .order_by('email').values_list('email', flat=True)
    )
    if sans_profil:
        constats.append(Constat(
            ERREUR, 'administrateurs',
            f"{len(sans_profil)} compte(s) is_staff sans profil d'administration "
            f"(refusés partout) : {', '.join(sans_profil)}",
        ))

    # L'inverse n'enferme personne : `is_staff` ne sert plus qu'à l'admin
    # Django. On le nomme quand même, l'écart disant qu'un rôle a été posé par
    # un autre chemin que `PlatformAdminService.grant`.
    profils_sans_staff = list(
        PlatformAdminProfile.objects.filter(is_active=True, user__is_staff=False)
        .order_by('user__email').values_list('user__email', flat=True)
    )
    if profils_sans_staff:
        constats.append(Constat(
            AVERTISSEMENT, 'administrateurs',
            "Profil actif sur un compte sans is_staff : " + ', '.join(profils_sans_staff),
        ))

    capables = _comptes_capables_de_tout()
    actifs = PlatformAdminProfile.objects.filter(is_active=True, user__is_active=True).count()
    if capables == 0:
        # Sans administrateur du tout, c'est une installation neuve, pas une
        # régression. Avec des administrateurs mais aucun capable de nommer,
        # plus personne ne peut réparer une erreur de rôle.
        niveau = ERREUR if actifs else AVERTISSEMENT
        constats.append(Constat(
            niveau, 'administrateurs',
            "Aucun super-administrateur ni superutilisateur actif : personne ne "
            "peut nommer ou retirer un administrateur.",
        ))

    # Le constat OK ne paraît que si rien d'autre n'a été dit : « aucun
    # super-administrateur » suivi de « OK » se contredirait sur deux lignes.
    if not constats:
        super_admins = PlatformAdminProfile.objects.filter(
            is_active=True, role=PlatformRole.SUPER_ADMIN, user__is_active=True,
        ).count()
        constats.append(Constat(
            OK, 'administrateurs',
            f"{actifs} administrateur(s) actif(s), dont {super_admins} super-admin ; "
            f"{capables} compte(s) peuvent nommer.",
        ))
    return constats


def taches_planifiees():
    """La purge des journaux, que seule une migration pose."""
    PeriodicTask = registre.get_model('django_celery_beat', 'PeriodicTask')
    tache = PeriodicTask.objects.filter(name=TACHE_DE_PURGE).first()
    if tache is None:
        return [Constat(
            ERREUR, 'tâches',
            f"La tâche « {TACHE_DE_PURGE} » n'existe pas : les journaux ne seront "
            "jamais purgés.",
        )]
    if not tache.enabled:
        return [Constat(
            AVERTISSEMENT, 'tâches', f"La tâche « {TACHE_DE_PURGE} » est désactivée.",
        )]
    return [Constat(OK, 'tâches', f"La tâche « {TACHE_DE_PURGE} » est planifiée.")]


def configuration():
    """
    Deux réglages qui ne cassent rien, et qui faussent tous les journaux.

    Ils ne valent qu'en production : en développement, ni Next ni un proxy ne
    s'intercalent, et l'adresse lue est déjà la bonne.
    """
    if settings.DEBUG:
        return [Constat(OK, 'configuration', "DEBUG actif : réglages de production non vérifiés.")]

    constats = []
    if not getattr(settings, 'VF_EDGE_KEY', ''):
        constats.append(Constat(
            AVERTISSEMENT, 'configuration',
            "VF_EDGE_KEY est vide : le journal et le verrou de connexion verront "
            "l'adresse du serveur Next, pas celle du navigateur.",
        ))
    if getattr(settings, 'TRUSTED_PROXY_COUNT', 0) < 1:
        constats.append(Constat(
            AVERTISSEMENT, 'configuration',
            "TRUSTED_PROXY_COUNT vaut 0 : derrière un proxy, toutes les requêtes "
            "partagent l'adresse du proxy.",
        ))
    if not constats:
        constats.append(Constat(OK, 'configuration', "Adresse du client : réglages présents."))
    return constats


VERIFICATIONS = (
    migrations_en_attente,
    modeles_sans_migration,
    administrateurs,
    taches_planifiees,
    configuration,
)


def constater():
    """Tous les constats, dans l'ordre où on les lit."""
    resultat = []
    for verification in VERIFICATIONS:
        resultat.extend(verification())
    return resultat


class Command(BaseCommand):
    help = (
        "Vérifie ce qu'un déploiement doit avoir laissé : migrations appliquées, "
        "administrateurs intacts, tâches planifiées. N'écrit rien."
    )

    def handle(self, *args, **options):
        constats = constater()
        styles = {
            ERREUR: self.style.ERROR,
            AVERTISSEMENT: self.style.WARNING,
            OK: self.style.SUCCESS,
        }
        for c in constats:
            self.stdout.write(styles[c.niveau](f"[{c.niveau}] {c.rubrique} : {c.message}"))

        erreurs = [c for c in constats if c.niveau == ERREUR]
        if erreurs:
            raise CommandError(
                f"{len(erreurs)} erreur(s) : ne pas ouvrir le trafic sur cette version."
            )
        self.stdout.write(self.style.SUCCESS("Déploiement conforme."))
