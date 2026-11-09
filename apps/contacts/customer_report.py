"""
Le rapport d'un client sur une PÉRIODE : ses achats, sa dette, sa cagnotte.

┌──────────────────────────────────────────────────────────────────────────────┐
│ AUCUN ÉCRAN NE RENDAIT LE COMPTE D'UN CLIENT SUR UNE PÉRIODE.                │
│                                                                              │
│ Sa fiche montre ses ventes et ses écritures, jamais un rapport BORNÉ dans le │
│ temps - et nulle part sa fidélité GAGNÉE contre celle déjà UTILISÉE. Le      │
│ marchand qui veut savoir ce qu'un client lui a rapporté ce mois-ci devait    │
│ additionner sa liste de ventes à la main.                                    │
└──────────────────────────────────────────────────────────────────────────────┘

⚠ LA CAGNOTTE DISPONIBLE N'A PAS DE PÉRIODE, et c'est la seule valeur du
  rapport dans ce cas : c'est un solde d'AUJOURD'HUI. L'écran doit le DIRE,
  sinon elle se lit comme un chiffre de la fenêtre choisie - et le marchand
  croirait que son client a gagné cette somme ce mois-ci.

⚠ LES BORNES SONT LOCALES. Entre 23 h et minuit UTC il est déjà le lendemain à
  Kinshasa : une date nue comparée à un horodatage perdrait la première heure du
  jour. `date_to` est INCLUSIVE, comme partout ailleurs dans ce dépôt.
"""
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.cashbook.services import primary_sum
from apps.inventory.filters import day_bounds
from apps.settings.services import CurrencyService


#: Deux décimales, comme tout montant de ce projet.
#:
#: ⚠ `primary_sum` REND LA PRÉCISION DE SON PRODUIT, pas celle d'un montant :
#:   relevé sur le vrai serveur, un chiffre d'affaires sortait
#:   « 303791.08000000000000 ». Sans arrondi il s'imprimerait tel quel dans la
#:   synthèse du PDF - les moteurs d'export formatent les COLONNES par leur
#:   `kind`, jamais les couples de la synthèse, qui sont des chaînes déjà faites.
CENTIME = Decimal('0.01')


def _sou(montant) -> str:
    """Un montant, en chaîne, à deux décimales."""
    return str((montant or Decimal('0')).quantize(CENTIME))


def _fenetre(date_from, date_to):
    """Les deux bornes aware d'une période, `date_to` incluse."""
    debut = day_bounds(date_from)[0] if date_from else None
    fin = day_bounds(date_to)[1] if date_to else None
    return debut, fin


def _ventes_du_client(customer, request, debut, fin):
    """
    Les ventes du client, dans le périmètre du demandeur.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ LES DEUX PIÈCES SONT CELLES DE `/sales/`, ET C'EST TOUT L'ENJEU.         │
    │                                                                          │
    │ `SaleViewSet` applique exactement deux choses : le périmètre ENTREPÔT    │
    │ (`WarehouseScopedQuerysetMixin`, `warehouse_scope_field='warehouse_id'`, │
    │ `include_null=True`) et une borne par CRÉATEUR quand le demandeur n'a    │
    │ pas `sales.view_all`. On les reprend toutes deux, telles quelles.        │
    │                                                                          │
    │ ⚠ ET SURTOUT PAS `restrict_visibility_for_request` : elle exprime la     │
    │   même intention par le RÔLE et non par la PERMISSION, et les deux       │
    │   divergent sur un cas réel - un caissier à qui le marchand accorde      │
    │   `sales.view_all` par `extra_permissions`. `/sales/` lui montre alors   │
    │   les ventes de son dépôt ; la fonction par rôle continue de le borner   │
    │   à ses propres ventes. Les deux listes vivent sur la MÊME page de la    │
    │   fiche client : la divergence s'y lirait comme deux chiffres            │
    │   contradictoires. Elle ne borne pas non plus un caissier par entrepôt,  │
    │   là que `/sales/` le fait.                                              │
    └──────────────────────────────────────────────────────────────────────────┘

    C'est le correctif adjacent de ce lot : `CustomerViewSet.sales` ne bornait
    RIEN. Le corps est partagé pour que la liste et le rapport ne puissent plus
    se contredire.
    """
    from apps.core.api_permissions import has_perm_code
    from apps.core.warehouse_scope import (
        filter_queryset_by_related_warehouse,
        get_membership_for_request,
    )
    from apps.sales.models import Sale

    qs = Sale.objects.filter(customer=customer, is_deleted=False)

    membership = get_membership_for_request(request)
    if membership:
        qs = filter_queryset_by_related_warehouse(
            qs, membership, 'warehouse_id',
            # ⚠ `include_null=True`, comme `SaleViewSet` : les ventes anciennes
            #   n'ont pas d'entrepôt, et les exclure ferait disparaître du
            #   rapport ce que la liste au-dessus montre.
            include_null=True,
        )
    if not has_perm_code(request, 'sales.view_all'):
        qs = qs.filter(sold_by=request.user)

    if debut:
        qs = qs.filter(sale_date__gte=debut)
    if fin:
        qs = qs.filter(sale_date__lte=fin)
    return qs


def build_customer_report(customer, request, *, date_from=None, date_to=None) -> dict:
    """Le rapport complet, prêt à servir ou à exporter."""
    organization = customer.organization
    debut, fin = _fenetre(date_from, date_to)
    ventes = _ventes_du_client(customer, request, debut, fin)

    # ⚠ CONVERTI AU TAUX FIGÉ SUR CHAQUE VENTE, jamais celui du jour : un
    #   rapport dont les chiffres d'hier bougent avec le cours n'est pas
    #   relisable, et le marchand ne saurait pas lequel croire.
    chiffres = ventes.aggregate(
        count=Count('id'),
        revenu=primary_sum('total'),
        encaisse=primary_sum('amount_paid'),
        du=primary_sum('amount_due'),
    )
    nb = chiffres['count'] or 0
    revenu = chiffres['revenu'] or Decimal('0')

    articles = (
        ventes.aggregate(n=Sum('items__quantity'))['n'] or Decimal('0')
    )

    # La ventilation par devise : jamais sommée nativement, c'est la règle de
    # tout le dépôt. `primary_total` sert à comparer, `native_total` à relire.
    par_devise = []
    for ligne in (
        ventes.values('currency')
        .annotate(
            native_total=Sum('total'),
            primary_total=primary_sum('total'),
            count=Count('id'),
        )
        .order_by('-primary_total')
    ):
        par_devise.append({
            'currency': ligne['currency'],
            'native_total': _sou(ligne['native_total']),
            'primary_total': _sou(ligne['primary_total']),
            'count': ligne['count'],
        })

    return {
        'customer': {
            'id': str(customer.id),
            'name': customer.name,
            'code': customer.code,
            'phone': customer.phone or '',
        },
        'period': {
            'date_from': str(date_from) if date_from else None,
            'date_to': str(date_to) if date_to else None,
            # L'arrêté : ce qui n'a PAS de période le dit par cette date.
            'as_of': str(timezone.localdate()),
        },
        'currency': CurrencyService.primary_code(organization),
        'sales': {
            'count': nb,
            'revenue': _sou(revenu),
            # ⚠ UNE MOYENNE BRUTE EST PIRE QU'UNE SOMME BRUTE : un panier moyen
            #   calculé sur des factures en francs et en dollars ne ressemble à
            #   rien. On divise le revenu CONVERTI par le nombre de ventes.
            'average_basket': _sou((revenu / nb) if nb else Decimal('0')),
            'items_sold': str(articles),
            'collected': _sou(chiffres['encaisse']),
            'outstanding': _sou(chiffres['du']),
        },
        'by_currency': par_devise,
        'debt': _dette(customer),
        'loyalty': _fidelite(customer, debut, fin),
    }


def _dette(customer) -> list:
    """Ce que le client doit AUJOURD'HUI, par devise. Hors période."""
    lignes = []
    for solde in customer.balances.all():
        # ⚠ LE CHAMP S'APPELLE `amount`, pas `balance` : `CustomerBalance` porte
        #   une ligne par devise, et `CustomerLoyalty.balance` - l'autre modèle
        #   de ce lot - porte bien ce nom. Les confondre lève un
        #   `AttributeError` qui ressort en 500.
        montant = solde.amount or Decimal('0')
        if montant == 0:
            continue
        lignes.append({
            'currency': solde.currency,
            'amount': _sou(montant),
            # Dette et avance COEXISTENT chez le même client dès que les devises
            # diffèrent : trancher sur un seul signe tairait la moitié de la
            # situation.
            'kind': 'debt' if montant > 0 else 'advance',
        })
    return lignes


def _fidelite(customer, debut, fin) -> dict:
    """
    La cagnotte : ce qui a été GAGNÉ et UTILISÉ sur la période, et le solde.

    ⚠ `created_at` PORTE L'HEURE DE L'ACTE (`apps/core/clock.py`), pas celle de
      son enregistrement : une vente encaissée hors ligne se range au bon jour.
    """
    from apps.settings.models import CustomerLoyalty, LoyaltyTransaction

    # ⚠ `LoyaltyTransaction` N'A PAS DE FK VERS LE CLIENT : elle passe par son
    #   REGISTRE (`customer_loyalty`). Filtrer sur `customer` lève un
    #   `FieldError`, donc un 500.
    ecritures = LoyaltyTransaction.objects.filter(
        customer_loyalty__customer=customer
    )
    if debut:
        ecritures = ecritures.filter(created_at__gte=debut)
    if fin:
        ecritures = ecritures.filter(created_at__lte=fin)

    cumuls = ecritures.aggregate(
        # Le signe fait foi : un crédit est positif, un débit négatif. Compter
        # sur le `transaction_type` obligerait à tenir une liste de types en
        # phase avec l'énumération, et `EXPIRE` y dort déjà sans producteur.
        gagne=Sum('amount', filter=Q(amount__gt=0)),
        utilise=Sum('amount', filter=Q(amount__lt=0)),
        nombre=Count('id'),
    )

    registre = CustomerLoyalty.objects.filter(customer=customer).first()
    return {
        'earned': _sou(cumuls['gagne']),
        # Rendu POSITIF : « utilisée : 500 » se lit, « -500 » fait hésiter.
        'used': _sou(abs(cumuls['utilise'] or Decimal('0'))),
        'entries': cumuls['nombre'] or 0,
        # ⚠ HORS PÉRIODE, et l'écran doit le dire : c'est le solde d'aujourd'hui.
        'balance': _sou(registre.balance if registre else Decimal('0')),
        'total_earned': _sou(registre.total_earned if registre else Decimal('0')),
        'total_used': _sou(registre.total_used if registre else Decimal('0')),
        'has_program': registre is not None,
    }


def build_customer_report_spec(rapport, organization):
    """
    La description du document, pour les trois moteurs d'export.

    ⚠ `currency_field` EST POSÉ : le tableau est groupé par devise, et sans lui
      un montant en USD sortirait SANS décimale sur un établissement tenu en
      CDF - les décimales viennent de `Currency.decimal_places`, ligne par ligne.

    ⚠ `grand_total=False` : les devises ne s'additionnent pas. Un total général
      sous un tableau groupé par devise serait un nombre qui n'existe pas.
    """
    from apps.core.exports import (
        KIND_MEASURE,
        KIND_MONEY,
        KIND_TEXT,
        ReportColumn,
        ReportSpec,
        currency_decimals,
        format_number,
    )

    client = rapport['customer']
    periode = rapport['period']
    ventes = rapport['sales']
    fid = rapport['loyalty']
    code = rapport['currency']

    # ⚠ LA COLONNE CONVERTIE EST PRÉ-FORMATÉE, et ce n'est pas une coquetterie.
    #
    #   `currency_field` résout les décimales LIGNE PAR LIGNE : avec lui, un
    #   montant d'argent prend celles de la devise que porte sa ligne. C'est
    #   juste pour `native_total`, qui EST dans cette devise - et faux pour
    #   `primary_total`, qui est toujours en devise principale. Sur un
    #   établissement tenu en CDF (zéro décimale), une ligne en USD aurait
    #   imprimé « Converti (CDF) » à deux décimales ; sur un établissement en
    #   USD, une ligne en francs aurait rendu « 6 303 » pour 6 302,50 - le
    #   « montant faux sur un papier » que `currency_field` existe justement
    #   pour empêcher, par l'autre bout.
    #
    #   Les trois moteurs ne savent pas donner sa devise à UNE colonne, et la
    #   leur apprendre toucherait tous les rapports de la plateforme pour un
    #   seul tableau. On formate donc ici, avec les décimales de la principale,
    #   et la colonne devient du TEXTE rangé à droite. Elle n'est sommée par
    #   personne (`grand_total=False`, aucun `group_totals`), donc rien ne perd
    #   son type de nombre.
    decimales_principale = currency_decimals(code)
    lignes = []
    for d in rapport['by_currency']:
        lignes.append({
            'currency': d['currency'],
            'count': d['count'],
            'native_total': Decimal(d['native_total']),
            'primary_total': format_number(
                Decimal(d['primary_total']), decimales_principale,
            ),
        })

    synthese = [
        ('Client', client['name']),
        ('Ventes', str(ventes['count'])),
        (f"Chiffre d'affaires ({code})", ventes['revenue']),
        (f"Panier moyen ({code})", ventes['average_basket']),
        ('Articles vendus', ventes['items_sold']),
        (f"Encaissé ({code})", ventes['collected']),
        (f"Reste dû ({code})", ventes['outstanding']),
        (f"Cagnotte gagnée ({code})", fid['earned']),
        (f"Cagnotte utilisée ({code})", fid['used']),
        # ⚠ L'ÉTIQUETTE DIT « AUJOURD'HUI », parce que ce chiffre n'a pas de
        #   période : le lire comme un chiffre de la fenêtre serait faux.
        (f"Cagnotte disponible au {periode['as_of']} ({code})", fid['balance']),
    ]

    # ⚠ `filters_applied` ATTEND DES COUPLES, pas les triplets de
    #   `describe_filters` : les trois moteurs le parcourent en
    #   `for label, value in …`, et un triplet y lève
    #   `ValueError: too many values to unpack` - donc un 500 à l'export.
    filtres = [(
        'Période',
        f"du {periode['date_from']} au {periode['date_to']}"
        if periode['date_from'] or periode['date_to']
        else "Tout l'historique",
    )]
    if client['phone']:
        filtres.append(('Téléphone', client['phone']))

    return ReportSpec(
        title=f"Rapport client - {client['name']}",
        subtitle=(
            "Montants convertis en devise principale, au taux figé sur chaque "
            "facture. La cagnotte disponible est un solde d'aujourd'hui."
        ),
        organization=organization,
        currency=code,
        currency_field='currency',
        columns=[
            ReportColumn('currency', 'Devise', kind=KIND_TEXT),
            ReportColumn('count', 'Ventes', kind=KIND_MEASURE),
            ReportColumn('native_total', 'Facturé', kind=KIND_MONEY),
            # Déjà formatée aux décimales de la PRINCIPALE : voir plus haut.
            # `align` est explicite parce que `KIND_TEXT` se range à gauche, et
            # un bloc chiffré ne doit avoir qu'un seul bord.
            ReportColumn(
                'primary_total', f'Converti ({code})',
                kind=KIND_TEXT, align='right',
            ),
        ],
        rows=lignes,
        summary=synthese,
        filters_applied=filtres,
        grand_total=False,
        empty_message="Ce client n'a aucune vente sur cette période.",
    )
