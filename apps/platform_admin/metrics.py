"""
Les chiffres récurrents de la plateforme : MRR, ARPU, attrition.

┌──────────────────────────────────────────────────────────────────────────────┐
│ CE QUI ÉTAIT AFFICHÉ COMME « REVENU MENSUEL » N'EN ÉTAIT PAS UN.             │
│                                                                              │
│ Le tableau de bord sommait les paiements ENCAISSÉS ce mois-ci. Un            │
│ abonnement annuel réglé en janvier faisait de janvier un mois record et des  │
│ onze suivants des creux, alors que la plateforme gagnait exactement la même  │
│ chose chaque mois. Le MRR ramène chaque abonnement à son équivalent mensuel. │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ ON RECONSTRUIT, ON N'INSTANTANÉISE PAS. Un abonnement porte sa période
(`current_period_start` / `current_period_end`) et sa date d'annulation : on sait
donc exactement s'il couvrait une date donnée. Une table d'instantanés
quotidiens et sa tâche planifiée n'apporteraient que des trous - les jours où la
tâche n'a pas tourné - pour une information qu'on recalcule sans perte.
Seule réserve, assumée : une prolongation réécrit `current_period_end` en place,
donc l'historique la voit comme si elle avait toujours été là. Elle ne fait
qu'ALLONGER une période, jamais en inventer une.

⚠ JAMAIS DE SOMME INTER-DEVISES. Un plan est libellé dans sa devise ; le MRR se
rend par devise, et le chiffre « principal » est celui de la devise qui porte le
plus d'abonnés payants, nommée à côté. Additionner des francs et des dollars
rendrait un nombre qui n'existe pas.
"""
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone

from apps.subscriptions.models import Plan, Subscription

#: Mois couverts par un prix, selon le cycle de facturation.
MOIS_PAR_CYCLE = {
    Plan.BillingCycle.MONTHLY: 1,
    Plan.BillingCycle.QUARTERLY: 3,
    Plan.BillingCycle.YEARLY: 12,
}

#: Les statuts d'un abonnement qui a été PAYANT à un moment de sa période.
#: L'essai n'y est pas : il ne rapporte rien, le compter gonflerait le MRR.
STATUTS_PAYANTS_EN_COURS = (Subscription.Status.ACTIVE, Subscription.Status.PAST_DUE)

CENTIME = Decimal('0.01')


def equivalent_mensuel(prix, cycle):
    """Le prix d'une période ramené à un mois."""
    mois = MOIS_PAR_CYCLE.get(cycle, 1)
    return (Decimal(prix or 0) / Decimal(mois))


def _arrondi(valeur):
    return Decimal(valeur).quantize(CENTIME, rounding=ROUND_HALF_UP)


def _couvre(ligne, instant):
    """
    Cet abonnement payant était-il en vigueur à cet instant ?

    ⚠ Un abonnement ANNULÉ cesse de compter à son annulation, pas à son échéance :
    `cancel` coupe l'accès sur-le-champ, et le compter jusqu'au bout de la
    période annoncerait un revenu que la plateforme ne touche plus.
    """
    if ligne['current_period_start'] is None or ligne['current_period_end'] is None:
        return False
    if not (ligne['current_period_start'] <= instant < ligne['current_period_end']):
        return False
    annule = ligne['cancelled_at']
    return annule is None or annule > instant


def _lignes_payantes(depuis):
    """
    Les abonnements PAYANTS dont la période touche la fenêtre.

    Une seule requête, en valeurs : le nombre d'abonnements de la plateforme se
    compte en centaines, et tout le calcul se fait ensuite en mémoire.
    """
    return list(
        Subscription.objects
        .exclude(status=Subscription.Status.TRIAL)
        .filter(price__gt=0, current_period_end__gt=depuis)
        .values(
            'organization_id', 'price', 'billing_cycle', 'currency', 'status',
            'current_period_start', 'current_period_end', 'cancelled_at',
        )
    )


def _etat_a(lignes, instant):
    """MRR par devise et organisations payantes, à un instant."""
    mrr = defaultdict(Decimal)
    payants = defaultdict(set)
    for ligne in lignes:
        if _couvre(ligne, instant):
            devise = ligne['currency'] or '?'
            mrr[devise] += equivalent_mensuel(ligne['price'], ligne['billing_cycle'])
            payants[devise].add(ligne['organization_id'])
    return mrr, payants


def _devise_principale(payants, mrr):
    if not payants and not mrr:
        return None
    devises = set(payants) | set(mrr)
    return max(devises, key=lambda d: (len(payants.get(d, ())), mrr.get(d, Decimal(0)), d))


class PlatformMetricsService:
    """Le calcul, en un seul endroit, servi au tableau de bord et à l'écran dédié."""

    @staticmethod
    def instantane(maintenant=None):
        """Les chiffres d'aujourd'hui, et l'attrition des trente derniers jours."""
        maintenant = maintenant or timezone.now()
        il_y_a_30j = maintenant - timedelta(days=30)
        lignes = _lignes_payantes(il_y_a_30j - timedelta(days=1))

        mrr, payants = _etat_a(lignes, maintenant)
        _, payants_avant = _etat_a(lignes, il_y_a_30j)

        orgs_maintenant = set().union(*payants.values()) if payants else set()
        orgs_avant = set().union(*payants_avant.values()) if payants_avant else set()
        perdues = orgs_avant - orgs_maintenant
        gagnees = orgs_maintenant - orgs_avant

        devise = _devise_principale(payants, mrr)
        mrr_principal = mrr.get(devise, Decimal(0)) if devise else Decimal(0)
        nb_principal = len(payants.get(devise, ())) if devise else 0

        en_essai = (
            Subscription.objects.filter(
                status=Subscription.Status.TRIAL, current_period_end__gt=maintenant,
            ).values('organization_id').distinct().count()
        )

        return {
            'currency': devise,
            'multi_currency': len(mrr) > 1,
            'mrr': str(_arrondi(mrr_principal)),
            'mrr_by_currency': [
                {
                    'currency': d,
                    'mrr': str(_arrondi(v)),
                    'paying_organizations': len(payants.get(d, ())),
                }
                for d, v in sorted(mrr.items(), key=lambda kv: -len(payants.get(kv[0], ())))
            ],
            # ⚠ L'ARPU se calcule DANS une devise : diviser un MRR en dollars par
            # des organisations qui paient en francs ne veut rien dire.
            'arpu': str(_arrondi(mrr_principal / nb_principal)) if nb_principal else '0.00',
            'paying_organizations': len(orgs_maintenant),
            'trialing_organizations': en_essai,
            'new_paying_30d': len(gagnees),
            'churned_30d': len(perdues),
            # `None` quand il n'y avait personne à perdre : « 0 % » affirmerait une
            # fidélité parfaite là où il n'y a simplement rien à mesurer.
            'churn_rate_30d': (
                str(_arrondi(Decimal(len(perdues)) * 100 / Decimal(len(orgs_avant))))
                if orgs_avant else None
            ),
        }

    @staticmethod
    def serie_mensuelle(mois=12, maintenant=None):
        """
        Le MRR à la fin de chaque mois, le mois courant compté à aujourd'hui.

        ⚠ LES FINS DE MOIS SONT LOCALES : à minuit UTC il est déjà une heure du
        matin à Kinshasa, et un abonnement qui s'achève le dernier soir du mois
        serait compté dans le mauvais.
        """
        mois = max(1, min(int(mois), 36))
        maintenant = maintenant or timezone.now()
        local = timezone.localtime(maintenant)

        # Le premier jour de chaque mois de la fenêtre, du plus ancien au courant.
        debuts = []
        annee, m = local.year, local.month
        for _ in range(mois):
            debuts.append((annee, m))
            m -= 1
            if m == 0:
                m, annee = 12, annee - 1
        debuts.reverse()

        def premier(a, mm):
            return timezone.make_aware(datetime.combine(datetime(a, mm, 1).date(), time.min))

        depuis = premier(*debuts[0])
        lignes = _lignes_payantes(depuis - timedelta(days=1))

        serie = []
        precedent = None
        for a, mm in debuts:
            suivant = (a + (mm // 12), mm % 12 + 1)
            fin = premier(*suivant) - timedelta(microseconds=1)
            instant = min(fin, maintenant)
            mrr, payants = _etat_a(lignes, instant)
            orgs = set().union(*payants.values()) if payants else set()
            serie.append({
                'month': f'{a:04d}-{mm:02d}',
                'mrr_by_currency': {d: str(_arrondi(v)) for d, v in mrr.items()},
                'paying_organizations': len(orgs),
                'new': len(orgs - precedent) if precedent is not None else None,
                'churned': len(precedent - orgs) if precedent is not None else None,
            })
            precedent = orgs
        return serie
