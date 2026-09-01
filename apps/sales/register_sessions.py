"""
Ouverture et clôture d'une session de caisse.

Le corps vit ici et non dans la vue parce que DEUX surfaces la déclenchent : le
back-office par `RegisterSessionViewSet.close`, et le terminal mobile par le
journal d'opérations. C'est le même partage qu'aux lots 6 à 8, et la même
raison : deux corps auraient fini par diverger sur l'arithmétique du tiroir.

**LE SOLDE EST PAR DEVISE, ET NE SE SOMME JAMAIS.** Un tiroir contient des
billets de plusieurs devises ; les additionner donnerait un nombre qui ne
correspond à aucune liasse. Chaque devise a son fonds d'ouverture, ses entrées,
ses sorties, son comptage et son écart.

**Une note est OBLIGATOIRE dès qu'un écart est non nul**, dans n'importe quelle
devise. Un écart sans explication est exactement la ligne qu'un contrôle
regarde en premier, et le caissier est la seule personne à pouvoir la donner.
"""
from decimal import Decimal

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone
from apps.core.clock import maintenant


class TransitionRefusee(Exception):
    """Refus métier déterministe : à ne JAMAIS réessayer."""


class CaisseIntrouvable(TransitionRefusee):
    """Caisse inexistante, désactivée, ou hors du périmètre du membre."""


class SessionDejaOuverte(TransitionRefusee):
    """
    Une session est déjà ouverte sur cette caisse.

    Porte QUI l'a ouverte et QUAND : c'est le refus le plus probable et le plus
    coûteux d'une ouverture hors ligne, puisqu'il emporte avec lui toutes les
    ventes qui s'y rattachaient. Un message qui se contenterait de « déjà
    ouverte » laisserait le caissier sans rien pour comprendre.
    """

    def __init__(self, session):
        self.session = session
        qui = (
            session.opened_by.full_name if session.opened_by else "un autre utilisateur"
        )
        super().__init__(
            f"Une session est déjà ouverte sur {session.register.name}, "
            f"par {qui} le {session.opened_at:%d/%m à %H:%M}."
        )


class NoteRequise(TransitionRefusee):
    """
    Écart constaté sans explication.

    Exception DISTINCTE parce que la réponse l'est : l'endpoint rend
    ``{'notes': ...}`` et non ``{'error': ...}``, contrat déjà publié et sur
    lequel le back-office branche son message de champ. Les confondre ferait
    disparaître l'erreur sous le formulaire, là où personne ne la lit.
    """


def close_register_session(session, user, donnees=None, ip=None, agent=None):
    """
    Ferme la session et rend l'objet.

    ``donnees`` porte le comptage manuel : ``counted_balance`` (devise
    principale, compatibilité) et/ou ``counted_balances`` (une entrée par
    devise). Absent, le solde attendu fait foi.
    """
    from apps.cashbook.models import CashMovement

    from .models import Payment, RegisterSessionCurrencyBalance

    donnees = donnees or {}

    if session.status != 'open':
        raise TransitionRefusee("Cette session est déjà fermée")

    notes_input = (donnees.get('notes') or '').strip()

    from apps.cashbook.models import CashMovement
    from .models import RegisterSessionCurrencyBalance

    primary = session.organization.currency or 'CDF'
    TWO = Decimal('0.01')

    # Fonds d'ouverture PAR DEVISE : depuis les currency_balances de la session
    # si présents (nouvelles sessions), sinon la devise principale = scalaire.
    opening_by_ccy = {
        cb.currency: cb.opening_balance
        for cb in session.currency_balances.all()
    }
    if not opening_by_ccy:
        opening_by_ccy = {primary: session.opening_balance}

    # Entrées espèces par devise = somme des règlements cash (montant remis)
    # des ventes de la session.
    cash_in_rows = Payment.objects.filter(
        sale__session=session,
        payment_method__method_type='cash',
        status='completed',
    ).values('currency').annotate(
        total=Sum(Coalesce('tendered_amount', 'amount'))
    )
    cash_in_by_ccy = {
        (r['currency'] or primary): (r['total'] or Decimal('0.00'))
        for r in cash_in_rows
    }

    # Sorties espèces par devise = mouvements cash 'out' rattachés à la session
    # (dépenses, monnaie rendue, retraits). payment_method NULL = espèces comptoir.
    cash_out_rows = CashMovement.objects.filter(
        session=session,
        direction='out',
        is_cancelled=False,
    ).filter(
        Q(payment_method__isnull=True) | Q(payment_method__method_type='cash')
    ).values('currency').annotate(total=Sum('amount'))
    cash_out_by_ccy = {
        (r['currency'] or primary): (r['total'] or Decimal('0.00'))
        for r in cash_out_rows
    }

    # Comptage manuel par devise (+ compat scalaire = devise principale).
    counted_by_ccy = {}
    if donnees.get('counted_balance') is not None:
        counted_by_ccy[primary] = Decimal(donnees['counted_balance'])
    for item in donnees.get('counted_balances') or []:
        counted_by_ccy[item['currency']] = Decimal(item['amount'])

    currencies = (
        set(opening_by_ccy) | set(cash_in_by_ccy)
        | set(cash_out_by_ccy) | set(counted_by_ccy)
    )

    rows = []
    any_diff = False
    for ccy in sorted(currencies):
        opening = opening_by_ccy.get(ccy, Decimal('0.00'))
        cin = cash_in_by_ccy.get(ccy, Decimal('0.00'))
        cout = cash_out_by_ccy.get(ccy, Decimal('0.00'))
        expected = (opening + cin - cout).quantize(TWO)
        counted = counted_by_ccy.get(ccy)
        if counted is not None:
            counted = counted.quantize(TWO)
            diff = (counted - expected).quantize(TWO)
        else:
            diff = Decimal('0.00')
        if diff != 0:
            any_diff = True
        rows.append({
            'currency': ccy, 'opening_balance': opening,
            'expected_balance': expected, 'counted_balance': counted,
            'difference': diff,
        })

    # Notes obligatoires si un écart est non nul dans une devise.
    if any_diff and not notes_input:
        raise NoteRequise(
            "Une note explicative est obligatoire lorsque le comptage diffère du "
            "solde attendu."
        )

    # Persister les soldes par devise.
    for row in rows:
        RegisterSessionCurrencyBalance.objects.update_or_create(
            session=session, currency=row['currency'],
            defaults={
                'organization': session.organization,
                'opening_balance': row['opening_balance'],
                'expected_balance': row['expected_balance'],
                'counted_balance': row['counted_balance'],
                'difference': row['difference'],
            },
        )

    # Renseigner les champs scalaires (devise principale) pour compat ascendante.
    primary_row = next((r for r in rows if r['currency'] == primary), None)
    if primary_row is None:
        primary_row = {
            'opening_balance': session.opening_balance,
            'expected_balance': session.opening_balance,
            'counted_balance': None, 'difference': Decimal('0.00'),
        }
    session.expected_balance = primary_row['expected_balance']
    session.counted_balance = primary_row['counted_balance']
    session.difference = primary_row['difference']
    session.closing_balance = (
        primary_row['counted_balance'] if primary_row['counted_balance'] is not None
        else primary_row['expected_balance']
    )
    session.closed_by = user
    # Heure de la CLÔTURE au comptoir : le Z se tire à la fermeture, souvent
    # avant que le réseau ne revienne, et le papier porte ce jour-là.
    session.closed_at = maintenant()
    session.status = 'closed'
    session.notes = notes_input
    session.save()

    # Audit log de la fermeture - détail par devise inclus.
    from apps.users.models import UserActivity

    closed_by_other = session.opened_by_id != user.id
    UserActivity.objects.create(
        user=user,
        organization=session.organization,
        action=UserActivity.ActionType.UPDATE,
        resource_type='register_session',
        resource_id=str(session.id),
        details={
            'event': 'session_closed',
            'register_id': str(session.register_id),
            'opened_by_id': str(session.opened_by_id),
            'closed_by_id': str(user.id),
            'closed_by_other_user': closed_by_other,
            'currency_balances': [
                {
                    'currency': r['currency'],
                    'opening_balance': str(r['opening_balance']),
                    'expected_balance': str(r['expected_balance']),
                    'counted_balance': str(r['counted_balance']) if r['counted_balance'] is not None else None,
                    'difference': str(r['difference']),
                }
                for r in rows
            ],
            'notes': notes_input,
        },
        ip_address=ip,
        # `agent or ''` et non `agent` : `user_agent` est une colonne NON NULLE
        # (`blank=True`, donc vide et non nul). La VUE la remplit depuis
        # l'en-tête HTTP ; le JOURNAL n'a pas de navigateur et passait `None`,
        # ce qui levait `IntegrityError` et faisait refuser la clôture.
        #
        # Conséquence relevée sur l'émulateur : Z imprimé sous son numéro
        # définitif, tiroir compté dans les deux devises, et le serveur refusant.
        # AUCUNE clôture venue d'un terminal n'a jamais abouti. `ip_address`,
        # lui, est nullable : un acte sans requête n'a pas d'adresse, et en
        # inventer une serait pire que de n'en pas mettre.
        user_agent=agent or '',
    )

    return session


# ---------------------------------------------------------------------------
# OUVERTURE
# ---------------------------------------------------------------------------


def open_register_session(
    organization, register_id, user, *, opening_balance=None,
    opening_balances=None, session_id=None, request=None,
):
    """
    Ouvre une session de caisse. Appelée par la vue ET par le journal.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LE FONDS D'OUVERTURE EST HÉRITÉ PAR DEVISE, ET LE JOURNAL NE LE FAISAIT │
    │ PAS.                                                                     │
    │                                                                          │
    │ Le handler créait la session avec le seul `opening_balance` scalaire et  │
    │ AUCUNE ligne `RegisterSessionCurrencyBalance`. Conséquence directe à la  │
    │ clôture : `close_register_session` retombe sur                           │
    │ `{devise principale: opening_balance}`, donc le tiroir en dollars part   │
    │ de zéro et le Z annonce un écart tous les soirs, sur une caisse ouverte  │
    │ depuis un terminal.                                                      │
    │                                                                          │
    │ Le périmètre entrepôt de la caisse n'était pas vérifié non plus : un     │
    │ caissier pouvait ouvrir la caisse d'un dépôt qui ne lui est pas assigné. │
    └──────────────────────────────────────────────────────────────────────────┘

    ``opening_balance`` est le SCALAIRE, en devise principale, et il compte
    autant que la liste : c'est le seul champ que l'écran d'ouverture du
    terminal envoie, son formulaire ne demandant qu'un montant. Le journal ne
    le transmettait pas, si bien que le fonds compté par le caissier était
    perdu, la session ouvrait à zéro, et le Z du soir annonçait un excédent
    égal au fonds. Même compatibilité que ``counted_balance`` à la clôture, et
    même ordre : le scalaire d'abord, la liste par devise ensuite, pour qu'un
    appelant qui envoie les deux voie sa ventilation l'emporter.

    Lève `CaisseIntrouvable` ou `SessionDejaOuverte`, que chaque surface traduit
    dans SON contrat de réponse : `{'error': …}` en 404/400 pour la vue,
    `OperationRejected` pour le journal. C'est la règle déjà posée pour
    `NoteRequise`.
    """
    from django.db import IntegrityError, transaction

    from apps.core.warehouse_scope import (
        filter_queryset_by_warehouse_ids, get_membership_for_request,
    )

    from .models import Register, RegisterSession, RegisterSessionCurrencyBalance

    caisses = Register.objects.filter(
        id=register_id, organization=organization, is_active=True
    )
    if request is not None:
        membership = get_membership_for_request(request)
        if membership:
            caisses = filter_queryset_by_warehouse_ids(
                caisses, membership, 'warehouse_id'
            )
    caisse = caisses.first()
    if caisse is None:
        raise CaisseIntrouvable("Caisse non trouvée ou inactive")

    principale = organization.currency or 'CDF'
    try:
        with transaction.atomic():
            # Verrou avant le contrôle : sans lui, deux ouvertures simultanées
            # passent toutes deux le test et l'une échoue sur la contrainte.
            ouverte = (
                RegisterSession.objects.select_for_update()
                .filter(register=caisse, status='open')
                .first()
            )
            if ouverte is not None:
                # Rejeu idempotent : le terminal renvoie SA propre ouverture.
                if session_id and str(ouverte.id) == str(session_id):
                    return ouverte
                raise SessionDejaOuverte(ouverte)

            # Le fonds d'ouverture, hérité PAR DEVISE de la dernière clôture.
            herite = {}
            precedente = (
                RegisterSession.objects.filter(register=caisse, status='closed')
                .order_by('-closed_at', '-opened_at')
                .first()
            )
            if precedente is not None:
                for cb in precedente.currency_balances.all():
                    herite[cb.currency] = (
                        cb.counted_balance if cb.counted_balance is not None
                        else (cb.expected_balance or Decimal('0.00'))
                    )
                if not herite:
                    # Session héritée sans ventilation par devise (legacy).
                    herite[principale] = (
                        precedente.counted_balance
                        if precedente.counted_balance is not None
                        else (
                            precedente.closing_balance
                            or precedente.expected_balance
                            or Decimal('0.00')
                        )
                    )

            # Le scalaire vise la devise PRINCIPALE, comme `counted_balance`
            # à la clôture. Il passe avant la liste : une surface qui enverrait
            # les deux a ventilé exprès, et c'est sa ventilation qui gagne.
            if opening_balance is not None:
                herite[principale] = Decimal(str(opening_balance))
            for item in opening_balances or []:
                herite[item['currency']] = Decimal(str(item['amount']))

            session = RegisterSession.objects.create(
                **({'id': session_id} if session_id else {}),
                organization=organization,
                register=caisse,
                opened_by=user,
                opening_balance=herite.get(principale, Decimal('0.00')),
                notes='',
                status='open',
            )
            for devise, montant in herite.items():
                RegisterSessionCurrencyBalance.objects.create(
                    organization=organization,
                    session=session,
                    currency=devise,
                    opening_balance=montant,
                )
    except IntegrityError:
        # Filet de la contrainte d'unicité : une autre requête a gagné la course.
        ouverte = RegisterSession.objects.filter(
            register=caisse, status='open'
        ).first()
        if ouverte is not None:
            raise SessionDejaOuverte(ouverte)
        raise

    return session
