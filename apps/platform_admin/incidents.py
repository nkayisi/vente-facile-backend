"""
Ce que le support doit traiter AUJOURD'HUI, toutes organisations confondues.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN ÉCRAN DE TRAVAIL, PAS UN RAPPORT.                                         │
│                                                                              │
│ Chaque ligne appelle un geste : relancer un marchand dont l'abonnement       │
│ tombe dans trois jours, réconcilier un paiement Mobile Money prélevé mais    │
│ jamais activé, retrouver un propriétaire pour une boutique qui n'en a plus.  │
│ Les lignes sont rangées par urgence, la plus grave en tête.                  │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ CROSS-TENANT INTENTIONNEL, sur chaque requête de ce module : c'est la raison
d'être de l'administration de la plateforme, et la convention de
`apps/core/managers.py` veut que ce soit écrit là où ça se lit.

⚠ ON N'ÉCRIT RIEN ICI. Le chemin d'activation d'un paiement Moko est celui de
l'argent encaissé, et il a sa propre doctrine (« l'argent encaissé prime »). Un
paiement bloqué se REPÈRE à ses traces de réconciliation (`reconcile_attempts`,
`last_reconciled_at`) ; la raison d'un blocage s'obtient en le réconciliant, ce
que l'action dédiée fait, à la demande et journalisée.
"""
from datetime import timedelta

from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from apps.organizations.models import Organization, OrganizationMembership
from apps.subscriptions.models import Subscription, SubscriptionPayment

#: Un abonnement qui s'achève dans ce délai est à relancer.
RELANCE_JOURS = 7
#: Un abonnement échu depuis moins longtemps reste « récupérable ».
RECUPERABLE_JOURS = 14
#: Un paiement Mobile Money encore en attente après ce délai est suspect :
#: l'opérateur confirme en quelques minutes.
ATTENTE_SUSPECTE = timedelta(minutes=30)
#: Les échecs plus anciens n'appellent plus de geste.
ECHECS_RECENTS = timedelta(days=30)
#: Une liste plus longue ne se traite pas en une séance ; le décompte, lui, est exact.
PLAFOND = 50

CRITIQUE, HAUTE, MOYENNE = 'critical', 'high', 'medium'
_RANG = {CRITIQUE: 0, HAUTE: 1, MOYENNE: 2}

STATUTS_EN_COURS = (
    Subscription.Status.TRIAL, Subscription.Status.ACTIVE, Subscription.Status.PAST_DUE,
)


def _proprietaires(org_ids):
    """`{org_id: (nom, email, téléphone)}` du premier propriétaire actif, en une requête."""
    sortie = {}
    lignes = (
        OrganizationMembership.objects  # cross-tenant intentionnel
        .filter(organization_id__in=org_ids, role=OrganizationMembership.Role.OWNER, is_active=True)
        .select_related('user').order_by('joined_at')
    )
    for m in lignes:
        sortie.setdefault(m.organization_id, (m.user.full_name, m.user.email, m.user.phone))
    return sortie


def _contact(org, proprietaires):
    nom, email, tel = proprietaires.get(org.id, (None, None, None))
    return {
        'owner_name': nom,
        'owner_email': email,
        # Le téléphone de la boutique quand le propriétaire n'en a pas : on relance
        # au téléphone, et c'est la seule information qui permette de le faire.
        'phone': tel or org.phone or None,
    }


def abonnements_a_risque(maintenant):
    """
    Trois cas, du plus grave au moins grave :
    - en retard de paiement (`past_due`) : la période de grâce court ;
    - échu depuis peu, sans abonnement de remplacement : la boutique est bloquée ;
    - en cours mais s'achevant dans `RELANCE_JOURS` jours.
    """
    horizon = maintenant + timedelta(days=RELANCE_JOURS)
    depuis = maintenant - timedelta(days=RECUPERABLE_JOURS)

    remplace = Subscription.objects.filter(
        organization_id=OuterRef('organization_id'), status__in=STATUTS_EN_COURS,
        current_period_end__gt=maintenant,
    )
    qs = (
        Subscription.objects  # cross-tenant intentionnel
        .select_related('organization', 'plan')
        .filter(organization__is_deleted=False)
        .filter(
            Q(status=Subscription.Status.PAST_DUE)
            | Q(status__in=(Subscription.Status.TRIAL, Subscription.Status.ACTIVE),
                current_period_end__lte=horizon)
            | Q(status=Subscription.Status.EXPIRED, current_period_end__gte=depuis)
        )
        .annotate(remplace=Exists(remplace))
    )

    lignes = []
    for sub in qs:
        if sub.status == Subscription.Status.EXPIRED and sub.remplace:
            # Renouvelé : `activate_subscription` expire l'ancien. Rien à faire.
            continue
        jours = (sub.current_period_end - maintenant).total_seconds() / 86400
        if sub.status in (Subscription.Status.PAST_DUE, Subscription.Status.EXPIRED) or jours <= 0:
            gravite = CRITIQUE
            motif = 'En retard de paiement' if sub.status == Subscription.Status.PAST_DUE else 'Échu, boutique bloquée'
        elif jours <= 3:
            gravite = HAUTE
            motif = "Fin d'essai imminente" if sub.status == Subscription.Status.TRIAL else 'Échéance imminente'
        else:
            gravite = MOYENNE
            motif = "Fin d'essai proche" if sub.status == Subscription.Status.TRIAL else 'Échéance proche'
        lignes.append((sub, gravite, motif, jours))

    lignes.sort(key=lambda t: (_RANG[t[1]], t[0].current_period_end))
    proprietaires = _proprietaires({sub.organization_id for sub, *_ in lignes[:PLAFOND]})
    return len(lignes), [
        {
            'subscription_id': str(sub.id),
            'organization_id': str(sub.organization_id),
            'organization_name': sub.organization.name,
            'plan_name': sub.plan.name,
            'status': sub.status,
            'status_display': sub.get_status_display(),
            'current_period_end': sub.current_period_end,
            'days_remaining': int(jours) if jours > 0 else 0,
            'days_overdue': int(-jours) if jours < 0 else 0,
            'severity': gravite,
            'reason': motif,
            **_contact(sub.organization, proprietaires),
        }
        for sub, gravite, motif, jours in lignes[:PLAFOND]
    ]


def paiements_a_traiter(maintenant):
    """
    Les règlements Mobile Money dont le sort n'est pas tranché, ou l'est mal.

    ⚠ UN ÉCHEC N'EST PAS FORCÉMENT UN ÉCHEC : l'ancienne logique de sondage
    marquait `failed` des paiements que l'opérateur avait encaissés, et la
    réconciliation les reprend. Ils restent donc ici, à côté des attentes.
    """
    qs = (
        SubscriptionPayment.objects  # cross-tenant intentionnel
        .select_related('organization')
        .filter(payment_method=SubscriptionPayment.PaymentMethod.MOBILE_MONEY)
        .filter(
            Q(status=SubscriptionPayment.Status.PENDING, created_at__lte=maintenant - ATTENTE_SUSPECTE)
            | Q(status=SubscriptionPayment.Status.FAILED, created_at__gte=maintenant - ECHECS_RECENTS)
        )
        .order_by('status', 'created_at')
    )
    total = qs.count()
    paiements = list(qs[:PLAFOND])
    proprietaires = _proprietaires({p.organization_id for p in paiements})
    sortie = []
    for p in paiements:
        attente = p.status == SubscriptionPayment.Status.PENDING
        sortie.append({
            'payment_id': str(p.id),
            'reference': p.reference,
            'organization_id': str(p.organization_id),
            'organization_name': p.organization.name,
            'amount': str(p.amount),
            'currency': p.currency,
            'status': p.status,
            'status_display': p.get_status_display(),
            'created_at': p.created_at,
            'age_minutes': int((maintenant - p.created_at).total_seconds() // 60),
            'reconcile_attempts': p.reconcile_attempts,
            'last_reconciled_at': p.last_reconciled_at,
            'plan_id': (p.metadata or {}).get('plan_id'),
            'billing_cycle': (p.metadata or {}).get('billing_cycle'),
            # Une attente qui a déjà résisté à une réconciliation est la plus
            # grave : l'argent peut être parti sans que rien ne s'active.
            'severity': CRITIQUE if attente and p.reconcile_attempts else (HAUTE if attente else MOYENNE),
            **_contact(p.organization, proprietaires),
        })
    sortie.sort(key=lambda d: (_RANG[d['severity']], d['created_at']))
    return total, sortie


def organisations_sans_proprietaire():
    """
    Les boutiques ACTIVES dont plus personne ne peut régler l'abonnement.

    Le paiement est réservé au propriétaire : sans lui, la boutique s'arrêtera à
    sa prochaine échéance et aucun de ses membres ne pourra l'éviter.
    """
    proprietaire = OrganizationMembership.objects.filter(
        organization_id=OuterRef('pk'), role=OrganizationMembership.Role.OWNER,
        is_active=True, user__is_active=True,
    )
    qs = (
        Organization.objects  # cross-tenant intentionnel
        .filter(is_deleted=False, is_active=True)
        .annotate(a_un_proprietaire=Exists(proprietaire))
        .filter(a_un_proprietaire=False)
        .order_by('name')
    )
    total = qs.count()
    return total, [
        {
            'organization_id': str(o.id),
            'organization_name': o.name,
            'phone': o.phone or None,
            'email': o.email or None,
            'created_at': o.created_at,
            'severity': HAUTE,
        }
        for o in qs[:PLAFOND]
    ]


def tableau_des_incidents(maintenant=None):
    maintenant = maintenant or timezone.now()
    n_abos, abos = abonnements_a_risque(maintenant)
    n_paie, paie = paiements_a_traiter(maintenant)
    n_orgs, orgs = organisations_sans_proprietaire()
    return {
        'generated_at': maintenant,
        'counts': {
            'subscriptions_at_risk': n_abos,
            'payments_to_review': n_paie,
            'organizations_without_owner': n_orgs,
            'critical': (
                sum(1 for a in abos if a['severity'] == CRITIQUE)
                + sum(1 for p in paie if p['severity'] == CRITIQUE)
            ),
        },
        'subscriptions_at_risk': abos,
        'payments_to_review': paie,
        'organizations_without_owner': orgs,
        'limit': PLAFOND,
    }
