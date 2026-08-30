"""
Clôture d'une session de caisse.

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


class TransitionRefusee(Exception):
    """Refus métier déterministe : à ne JAMAIS réessayer."""


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
    session.closed_at = timezone.now()
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
        user_agent=agent,
    )

    return session
