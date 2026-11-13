"""
Les exports de l'administration : listes et journal, aux trois formats du socle.

┌──────────────────────────────────────────────────────────────────────────────┐
│ UN DOCUMENT DE L'ÉDITEUR N'A PAS D'ÉTABLISSEMENT ÉMETTEUR.                  │
│                                                                              │
│ Le socle d'export (`apps/core/exports.py`) imprime en tête l'identité de     │
│ l'organisation qui émet le document. Ici, c'est la plateforme : une identité │
│ sans adresse ni RCCM, que `organization_identity` sait rendre vide. Surtout  │
│ PAS l'organisation d'un marchand, qui ferait passer une liste de tous les    │
│ comptes pour une pièce de sa boutique.                                       │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ L'EXPORT PORTE LE PÉRIMÈTRE FILTRÉ, JAMAIS LA PAGE AFFICHÉE : c'est la règle
  de `ExportableListMixin`, qui passe le queryset déjà filtré à ces fonctions.

⚠ UN MONTANT S'ÉCRIT DANS SA MONNAIE (`currency_field`), et aucun total ne
  mêle deux devises : la synthèse ventile.
"""
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError as DjangoValidationError

from apps.core.exports import (
    KIND_DATETIME, KIND_MONEY, KIND_NUMBER, KIND_TEXT, ReportColumn, ReportSpec,
    currency_decimals, format_number,
)

ZERO = Decimal('0')

#: L'émetteur des documents de l'administration.
IDENTITE_PLATEFORME = SimpleNamespace(
    name='Vente Facile - administration de la plateforme',
    address='', city='', phone='', email='', rccm='', id_nat='', tax_id='', logo=None,
)


def filtres_de_la_requete(request, libelles):
    """
    Les filtres de l'écran, en couples « intitulé : valeur » pour l'en-tête.

    ⚠ UN FILTRE ABSENT S'ÉCRIT « Tous », il ne disparaît pas : un lecteur doit
      distinguer « pas de filtre » d'un filtre oublié dans l'en-tête.
    """
    couples = []
    recherche = (request.query_params.get('search') or '').strip()
    if recherche:
        couples.append(('Recherche', recherche))
    for cle, (intitule, traduire) in libelles.items():
        brut = (request.query_params.get(cle) or '').strip()
        couples.append((intitule, traduire(brut) if brut else 'Tous'))
    return tuple(couples)


def _nom_de(modele, attribut='name'):
    """
    Traduit l'identifiant d'un filtre en ce qu'un lecteur reconnaît.

    ⚠ Un en-tête qui écrit « Établissement : 3f2a9c1e-… » ne dit à personne quel
      périmètre le fichier couvre. Un identifiant introuvable reste écrit tel
      quel plutôt que de faire échouer le document.
    """
    def traduire(brut):
        try:
            objet = modele._base_manager.filter(pk=brut).first()
        except (ValueError, TypeError, DjangoValidationError):
            objet = None
        return str(getattr(objet, attribut, '') or brut) if objet else brut
    return traduire


def _oui_non(actif, oui='Actif', non='Inactif'):
    return oui if actif else non


def _synthese_par_devise(lignes, champ_montant):
    """`[(libellé, valeur)]` : un total par devise, jamais une somme entre elles."""
    cumuls = defaultdict(lambda: ZERO)
    for ligne in lignes:
        if ligne.get(champ_montant) is not None:
            cumuls[ligne['currency']] += ligne[champ_montant]
    return [
        (f'Total {code}', format_number(total, currency_decimals(code)))
        for code, total in sorted(cumuls.items())
    ]


def _spec_monetaire(*, title, columns, rows, champ_montant, filters, summary_tete, vide):
    """Le spec d'une liste dont chaque ligne porte un montant dans SA devise."""
    devises = {r['currency'] for r in rows if r.get('currency')}
    seule = next(iter(devises)) if len(devises) == 1 else None
    sous_titre = ''
    if len(devises) > 1:
        sous_titre = (
            f"ATTENTION : plusieurs devises ({', '.join(sorted(devises))}), "
            "les totaux ne sont pas convertis"
        )
    return ReportSpec(
        title=title,
        organization=IDENTITE_PLATEFORME,
        columns=columns,
        rows=rows,
        subtitle=sous_titre,
        filters_applied=filters,
        summary=tuple(summary_tete + _synthese_par_devise(rows, champ_montant)),
        group_totals=(champ_montant,) if len(devises) <= 1 else (),
        currency_field='currency' if len(devises) > 1 else None,
        currency=seule or 'USD',
        landscape_mode=True,
        empty_message=vide,
    )


# --------------------------------------------------------------- établissements

def organisations(request, queryset):
    from apps.organizations.models import Organization

    types = dict(Organization.BusinessType.choices)
    rows = [
        {
            'name': o.name,
            'business_type': types.get(o.business_type, o.business_type),
            'country': o.country or '',
            'city': o.city or '',
            'phone': o.phone or '',
            'email': o.email or '',
            'currency': o.currency,
            'members': getattr(o, 'nb_membres', None),
            'status': _oui_non(o.is_active, 'Active', 'Suspendue'),
            'created_at': o.created_at,
        }
        for o in queryset
    ]
    return ReportSpec(
        title='Établissements',
        organization=IDENTITE_PLATEFORME,
        columns=[
            ReportColumn(key='name', header='Établissement', width=46),
            ReportColumn(key='business_type', header='Type', width=24),
            ReportColumn(key='country', header='Pays', width=24),
            ReportColumn(key='city', header='Ville', width=24),
            ReportColumn(key='phone', header='Téléphone', width=28),
            ReportColumn(key='email', header='Courriel', width=40),
            ReportColumn(key='currency', header='Devise', width=14),
            ReportColumn(key='members', header='Membres', kind=KIND_NUMBER, width=16),
            ReportColumn(key='status', header='Statut', width=18),
            ReportColumn(key='created_at', header='Créé le', kind=KIND_DATETIME, width=28),
        ],
        rows=rows,
        filters_applied=filtres_de_la_requete(request, {
            'is_active': ('Statut', lambda v: 'Actifs' if v.lower() == 'true' else 'Suspendus'),
            'business_type': ('Type', lambda v: types.get(v, v)),
            'country': ('Pays', str),
        }),
        summary=(('Établissements', str(len(rows))),),
        landscape_mode=True,
        empty_message='Aucun établissement ne correspond aux critères retenus.',
    )


# --------------------------------------------------------------------- comptes

def comptes(request, queryset):
    rows = [
        {
            'email': u.email,
            'name': u.full_name or '',
            'phone': u.phone or '',
            'status': _oui_non(u.is_active),
            'staff': 'Oui' if u.is_staff else '',
            'date_joined': u.date_joined,
            'last_login': u.last_login,
        }
        for u in queryset
    ]
    return ReportSpec(
        title='Comptes',
        organization=IDENTITE_PLATEFORME,
        columns=[
            ReportColumn(key='email', header='Courriel', width=50),
            ReportColumn(key='name', header='Nom', width=40),
            ReportColumn(key='phone', header='Téléphone', width=28),
            ReportColumn(key='status', header='Statut', width=18),
            ReportColumn(key='staff', header='Administration', width=22),
            ReportColumn(key='date_joined', header='Inscrit le', kind=KIND_DATETIME, width=28),
            ReportColumn(key='last_login', header='Dernière connexion', kind=KIND_DATETIME, width=30),
        ],
        rows=rows,
        filters_applied=filtres_de_la_requete(request, {
            'is_active': ('Statut', lambda v: 'Actifs' if v.lower() == 'true' else 'Inactifs'),
            'is_staff': ('Administration', lambda v: 'Oui' if v.lower() == 'true' else 'Non'),
        }),
        summary=(('Comptes', str(len(rows))),),
        landscape_mode=True,
        empty_message='Aucun compte ne correspond aux critères retenus.',
    )


# ------------------------------------------------------------------ abonnements

def abonnements(request, queryset):
    from apps.organizations.models import Organization
    from apps.subscriptions.models import Plan, Subscription

    statuts = dict(Subscription.Status.choices)
    rows = [
        {
            'organization': s.organization.name,
            'plan': s.plan.name if s.plan_id else '',
            'status': statuts.get(s.status, s.status),
            'billing_cycle': s.get_billing_cycle_display(),
            'currency': s.currency or (s.plan.currency.code if s.plan_id else ''),
            'price': s.price,
            'current_period_end': s.current_period_end,
        }
        for s in queryset.select_related('organization', 'plan__currency')
    ]
    return _spec_monetaire(
        title='Abonnements',
        columns=[
            ReportColumn(key='organization', header='Établissement', width=46),
            ReportColumn(key='plan', header='Plan', width=28),
            ReportColumn(key='status', header='Statut', width=22),
            ReportColumn(key='billing_cycle', header='Cycle', width=20),
            ReportColumn(key='currency', header='Devise', width=14),
            ReportColumn(key='price', header='Prix', kind=KIND_MONEY, width=24),
            ReportColumn(key='current_period_end', header='Échéance', kind=KIND_DATETIME, width=28),
        ],
        rows=rows,
        champ_montant='price',
        filters=filtres_de_la_requete(request, {
            'status': ('Statut', lambda v: statuts.get(v, v)),
            'billing_cycle': ('Cycle', lambda v: dict(Plan.BillingCycle.choices).get(v, v)),
            'plan': ('Plan', _nom_de(Plan)),
            'organization': ('Établissement', _nom_de(Organization)),
        }),
        summary_tete=[('Abonnements', str(len(rows)))],
        vide='Aucun abonnement ne correspond aux critères retenus.',
    )


# -------------------------------------------------------------------- paiements

def paiements(request, queryset):
    from apps.organizations.models import Organization
    from apps.subscriptions.models import SubscriptionPayment

    statuts = dict(SubscriptionPayment.Status.choices)
    moyens = dict(SubscriptionPayment.PaymentMethod.choices)
    rows = [
        {
            'date': p.paid_at or p.created_at,
            'organization': p.organization.name,
            'plan': p.subscription.plan.name if p.subscription_id and p.subscription.plan_id else '',
            'method': moyens.get(p.payment_method, p.payment_method),
            'status': statuts.get(p.status, p.status),
            'reference': p.reference or '',
            'currency': p.currency or 'USD',
            # ⚠ Seul un règlement ENCAISSÉ entre dans le total : un paiement
            #   échoué ou en attente n'est pas de l'argent reçu.
            'amount': p.amount,
            'encaisse': p.amount if p.status == SubscriptionPayment.Status.COMPLETED else None,
        }
        for p in queryset.select_related('organization', 'subscription__plan')
    ]
    return _spec_monetaire(
        title="Paiements d'abonnement",
        columns=[
            ReportColumn(key='date', header='Date', kind=KIND_DATETIME, width=28),
            ReportColumn(key='organization', header='Établissement', width=44),
            ReportColumn(key='plan', header='Plan', width=24),
            ReportColumn(key='method', header='Moyen', width=24),
            ReportColumn(key='status', header='Statut', width=20),
            ReportColumn(key='reference', header='Référence', width=36),
            ReportColumn(key='currency', header='Devise', width=14),
            ReportColumn(key='amount', header='Montant', kind=KIND_MONEY, width=24),
        ],
        rows=rows,
        champ_montant='encaisse',
        filters=filtres_de_la_requete(request, {
            'status': ('Statut', lambda v: statuts.get(v, v)),
            'payment_method': ('Moyen', lambda v: moyens.get(v, v)),
            'organization': ('Établissement', _nom_de(Organization)),
            'date_from': ('Depuis le', str),
            'date_to': ("Jusqu'au", str),
        }),
        summary_tete=[('Paiements', str(len(rows)))],
        vide='Aucun paiement ne correspond aux critères retenus.',
    )


# ---------------------------------------------------------------------- journal

def journal(request, queryset):
    from apps.users.models import User

    from .models import AdminActionLog

    actions = dict(AdminActionLog.Action.choices)
    cibles = dict(AdminActionLog.Target.choices)
    rows = [
        {
            'date': a.created_at,
            'actor': a.actor_email or '',
            'action': actions.get(a.action, a.action),
            'target_type': cibles.get(a.target_type, a.target_type),
            'target': a.target_repr or '',
            'reason': a.reason or '',
            'ip': a.ip_address or '',
        }
        for a in queryset
    ]
    return ReportSpec(
        title="Journal d'audit",
        organization=IDENTITE_PLATEFORME,
        columns=[
            ReportColumn(key='date', header='Date', kind=KIND_DATETIME, width=28),
            ReportColumn(key='actor', header='Auteur', width=40),
            ReportColumn(key='action', header='Acte', width=40),
            ReportColumn(key='target_type', header='Cible', width=26),
            ReportColumn(key='target', header='Sur', width=44),
            ReportColumn(key='reason', header='Motif', width=50),
            ReportColumn(key='ip', header='Adresse', width=26),
        ],
        rows=rows,
        filters_applied=filtres_de_la_requete(request, {
            'actor': ('Auteur', _nom_de(User, 'email')),
            'action': ('Acte', lambda v: actions.get(v, v)),
            'target_type': ('Cible', lambda v: cibles.get(v, v)),
            'date_from': ('Depuis le', str),
            'date_to': ("Jusqu'au", str),
        }),
        summary=(('Actes', str(len(rows))),),
        landscape_mode=True,
        empty_message='Aucun acte ne correspond aux critères retenus.',
    )
