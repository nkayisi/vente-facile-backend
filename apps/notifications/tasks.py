"""
Celery tasks for notifications and alerts.
"""
import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def _expiring_quantity(batch):
    """
    Restant d'un lot qui périme, en unité de détail nommée.

    Un lot suit une date de péremption et non un emballage : rien n'y indique
    combien de ses unités sont encore scellées. Annoncer des contenants ferait
    donc dire au lot ce qu'il ne sait pas.
    """
    from apps.inventory.packaging import PackagingService

    return PackagingService.format_base_total(batch.product, batch.quantity)


def _lecture_du_rayon(rayon):
    """
    Le stock d'UN rayon dans ses propres termes.

    ⚠ La ventilation n'est rendue que si RIEN n'est réservé. Le total qui
    déclenche l'alerte est un DISPONIBLE (quantité moins réservé) alors que les
    deux compteurs portent la quantité brute : les présenter ensemble
    laisserait croire que 4 casiers sont libres alors qu'un est réservé. Sinon
    on nomme l'unité de détail, ce qui reste lisible sans rien affirmer de faux.
    """
    from apps.inventory.packaging import PackagingService

    produit = rayon.product
    if PackagingService.factor(produit) is None:
        return PackagingService.format_base_total(produit, rayon.quantity)
    if (rayon.reserved_quantity or 0) > 0:
        return PackagingService.format_base_total(produit, rayon.quantity)
    return PackagingService.format_split(
        produit, rayon.package_quantity or 0, rayon.loose_quantity or 0
    )


def _servir_par_etablissement(par_organisation, quoi, resoudre=None):
    """
    Sert chaque établissement À PART, et dit jusqu'où la tournée est allée.

    ┌──────────────────────────────────────────────────────────────────────────────┐
    │ AUCUNE BOUCLE DE BATTEMENT N'EST ENVELOPPÉE, ET AUCUNE N'EST NUE.           │
    │                                                                              │
    │ C'est la doctrine du journal de synchronisation - « aucun `atomic`           │
    │ n'enveloppe la boucle, sinon une opération fautive condamne les autres » -   │
    │ appliquée aux battements. Une organisation qui lève ne doit pas priver       │
    │ d'alerte tout le parc qui vient après elle.                                 │
    │                                                                              │
    │ ⚠ UN SEUL CORPS, parce que deux battements l'écrivaient déjà et qu'un        │
    │   troisième allait suivre. Trois isolations écrites séparément finiraient    │
    │   par ne plus isoler les mêmes choses, et la divergence ne se verrait que    │
    │   sur le téléphone d'un marchand qui n'a pas été prévenu.                    │
    └──────────────────────────────────────────────────────────────────────────────┘

    ⚠ `SoftTimeLimitExceeded` PASSE, et c'est tout le sens d'une limite douce.
    L'avaler ici ravalerait la levée que `_pousser` vient de rétablir, et la
    tâche irait au SIGKILL de la limite dure - worker mort, tournée à moitié
    faite, et RIEN dans le journal pour dire jusqu'où. On journalise donc
    COMBIEN d'établissements sur combien ont été servis : c'est la différence
    entre « un worker est mort » et « la tournée a été écourtée, et voici où ».
    """
    from apps.notifications.services import alerter_en_lot

    resoudre = resoudre or {}
    etablissements = set(par_organisation) | set(resoudre)
    servis = 0
    for organisation in etablissements:
        try:
            alerter_en_lot(
                organisation,
                par_organisation.get(organisation, []),
                resoudre=resoudre.get(organisation, []),
            )
        except SoftTimeLimitExceeded:
            logger.warning(
                "Battement %s interrompu : %d établissement(s) sur %d servis.",
                quoi, servis, len(etablissements),
            )
            raise
        except Exception:  # noqa: BLE001 - voir l'encadré
            logger.exception(
                "Alertes de %s impossibles pour %s ; la tournée continue.",
                quoi, organisation,
            )
            continue
        servis += 1
    return servis


# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ LE RAPPEL EST UNE TÂCHE, ET C'EST CE QUI SORT LE RÉSEAU DES VERROUS.        │
# │                                                                              │
# │ `sync/operations.py` ouvre un `atomic` autour de CHAQUE gestionnaire - §5.5  │
# │ l'exige, la trace et l'effet sont inséparables. Tout `atomic` de service n'y │
# │ est donc qu'un POINT DE SAUVEGARDE : les deux `select_for_update` de         │
# │ `_ecrire_mouvement` (`Stock`, puis `Product`) tenaient jusqu'au commit       │
# │ EXTÉRIEUR, et un POST de dix secondes tournait dedans. Toute vente           │
# │ concurrente du même article attendait, sur un gunicorn à deux workers.       │
# │                                                                              │
# │ Aucune scission de service ne pouvait corriger cela : l'enveloppe non        │
# │ atomique de `create_stock_movement` est ELLE AUSSI dans ce `atomic`. Huit    │
# │ fonctions préviennent, et chacune mentait sur l'un de ses deux chemins.      │
# │ `notifications.services._pousser` les rend toutes honnêtes d'un coup, en     │
# │ enfilant ICI par `transaction.on_commit`.                                    │
# └──────────────────────────────────────────────────────────────────────────────┘
@shared_task(soft_time_limit=60, time_limit=90, ignore_result=True)
def pousser_le_rappel(ids):
    """
    Fait sonner les téléphones. Le RAPPEL seul, jamais la notification.

    ⚠ `ignore_result=True` : `CELERY_RESULT_BACKEND` est `django-db`, et cette
    tâche part à CHAQUE alerte. Une ligne de résultat par rappel remplirait
    `django_celery_results_taskresult`, table qu'aucune purge ne visite -
    arbitrage déjà rendu pour `poll_moko_pending_payments`.

    ⚠ SES PROPRES BORNES, parce que c'est elle qui fait le réseau désormais.
    Soixante secondes couvrent largement `ceil(n / 100)` lots à `DELAI`, l'arrêt
    au premier lot refusé bornant la série.

    ⚠ ON RELIT PAR IDENTIFIANT, et c'est le serializer qui l'impose
    (`CELERY_TASK_SERIALIZER = 'json'`) : un objet Django ne traverse pas. Une
    ligne disparue entre-temps fait simplement un rappel de moins - le push est
    un RAPPEL, l'information est en base.
    """
    from apps.notifications.models import Notification
    from apps.notifications.push import pousser_notifications

    return pousser_notifications(list(Notification.objects.filter(id__in=ids)))


# ┌──────────────────────────────────────────────────────────────────────────────┐
# │ UNE TÂCHE QUI PRÉVIENT DOIT POUVOIR ÊTRE INTERROMPUE.                       │
# │                                                                              │
# │ Sans borne de tâche, un service injoignable retient un worker indéfiniment  │
# │ - et ce battement parcourt TOUT le parc. C'est la borne que                 │
# │ `poll_moko_pending_payments` porte déjà, pour la même raison.                │
# │                                                                              │
# │ ⚠ ET LA BORNE NE VAUT QUE SI PERSONNE N'AVALE SA LEVÉE.                     │
# │   `SoftTimeLimitExceeded` descend de `CeleryError`, donc d'`Exception` : le  │
# │   `except Exception` de `_pousser` la prenait, et aucune de ces tâches ne    │
# │   pouvait s'arrêter - elles allaient au SIGKILL de la limite dure, worker    │
# │   mort, tournée à moitié faite. Le défaut était d'ailleurs bien plus vieux   │
# │   que ces décorateurs : `CELERY_TASK_SOFT_TIME_LIMIT = 120` est posé dans    │
# │   les réglages depuis toujours.                                              │
# └──────────────────────────────────────────────────────────────────────────────┘
@shared_task(soft_time_limit=240, time_limit=300)
def check_low_stock_alerts():
    """
    Prévient des rayons sous leur seuil, PAR ENTREPÔT.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ L'ALERTE ÉTAIT AGRÉGÉE SUR TOUTE L'ORGANISATION, DONC INADRESSABLE.     │
    │                                                                          │
    │ Elle sommait les dépôts et ne nommait aucun : impossible, alors, de      │
    │ tenir la règle de périmètre - un gérant doit être alerté pour SES        │
    │ dépôts. Ou bien il recevait des ruptures d'un magasin où il ne met pas   │
    │ les pieds, ou bien il ne recevait rien. Et le chiffre lui-même mentait   │
    │ par omission : « 3 casiers » tous dépôts confondus ne dit pas que la     │
    │ boutique est vide pendant que la réserve est pleine, ce qui est          │
    │ exactement le cas qu'un réassort doit traiter.                           │
    │                                                                          │
    │ L'alerte porte désormais son entrepôt (`data.warehouse_id`), et c'est    │
    │ lui que `notifications.services.destinataires` oppose.                   │
    └──────────────────────────────────────────────────────────────────────────┘

    ⚠ **UN RAYON QUI SE VIDE EST UN FAIT NOUVEAU.** La déduplication couvrait
    `LOW_STOCK` et `OUT_OF_STOCK` ensemble - juste pour ne pas répéter la même
    alerte à chaque battement, faux pour une AGGRAVATION : un produit déjà
    signalé « bas » qui tombe à zéro ne produisait plus rien, et le marchand
    n'apprenait jamais la rupture. La déduplication est désormais par (rayon,
    TYPE), et le passage de l'un à l'autre résout l'alerte précédente.
    """
    from apps.inventory.models import Stock
    from apps.inventory.packaging import PackagingService
    from apps.notifications.models import Alert

    # Le critère est celui de `StockFilter` et de `lignes_proposees` : suivi,
    # seuil réel, quantité au-dessous. Trois conditions, et il faut les trois.
    rayons = list(
        Stock.objects
        .filter(
            product__is_active=True,
            product__is_deleted=False,
            product__track_inventory=True,
            product__reorder_point__gt=0,
            quantity__lte=F('product__reorder_point'),
        )
        .select_related(
            'organization', 'warehouse',
            'product', 'product__unit', 'product__packaging_unit',
        )
    )
    if not rayons:
        return

    def _type(rayon):
        return (
            Alert.AlertType.OUT_OF_STOCK if rayon.quantity <= 0
            else Alert.AlertType.LOW_STOCK
        )

    # Les alertes ACTIVES de ces rayons, en une requête. La clé est le couple
    # (rayon, type) : c'est ce qui laisse passer l'aggravation.
    actives = {}
    for a in Alert.objects.filter(
        alert_type__in=[Alert.AlertType.LOW_STOCK, Alert.AlertType.OUT_OF_STOCK],
        resource_type='stock',
        resource_id__in=[r.id for r in rayons],
        status=Alert.Status.ACTIVE,
    ):
        actives.setdefault(a.resource_id, []).append(a)

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ ON GROUPE PAR ÉTABLISSEMENT, ET CE N'EST PAS UNE OPTIMISATION.           │
    # │                                                                          │
    # │ Cette boucle appelait `alerter` PAR RAYON. Chaque appel résolvait les     │
    # │ destinataires - une requête d'appartenance plus son préchargement -,      │
    # │ contrôlait l'idempotence PAR DESTINATAIRE, interrogeait les appareils, et │
    # │ faisait un `urlopen` bloquant à dix secondes dès qu'un jeton existait.   │
    # │ Cinquante établissements, cent rayons sous seuil, cinq membres : environ  │
    # │ quarante-cinq mille requêtes et jusqu'à cinq mille appels HTTP           │
    # │ séquentiels, dans un battement unique, sur le VPS à deux cœurs.          │
    # │                                                                          │
    # │ ⚠ LE VOLUME D'ALERTES NE CHANGE PAS. Une alerte par (produit, entrepôt)  │
    # │   est le choix délibéré de T14 - « l'alerte porte désormais son dépôt,   │
    # │   et c'est lui que `destinataires` oppose ». On réduit le COÛT, jamais   │
    # │   la granularité.                                                        │
    # └──────────────────────────────────────────────────────────────────────────┘
    # ⚠ LA RÉSOLUTION EST GROUPÉE PAR ÉTABLISSEMENT, pas pour tout le parc :
    #   elle part dans le MÊME `atomic` que les créations de son organisation.
    a_resoudre = {}
    par_organisation = {}

    for rayon in rayons:
        genre = _type(rayon)
        deja = actives.get(rayon.id, [])
        if any(a.alert_type == genre for a in deja):
            continue

        # ⚠ L'ANCIENNE ALERTE SE RÉSOUT, elle ne s'empile pas : un rayon vide
        # n'est plus « bas », et laisser les deux actives ferait compter deux
        # fois le même rayon dans tout décompte d'alertes.
        a_resoudre.setdefault(rayon.organization, []).extend(
            a.id for a in deja if a.alert_type != genre
        )

        produit = rayon.product
        stock_text = _lecture_du_rayon(rayon)
        reorder_text = PackagingService.format_base_total(
            produit, produit.reorder_point
        )
        vide = genre == Alert.AlertType.OUT_OF_STOCK

        par_organisation.setdefault(rayon.organization, []).append(dict(
            alert_type=genre,
            severity=Alert.Severity.CRITICAL if vide else Alert.Severity.HIGH,
            title=(
                f"Rupture : {produit.name} ({rayon.warehouse.name})" if vide
                else f"Stock bas : {produit.name} ({rayon.warehouse.name})"
            ),
            message=(
                f"{produit.name} (SKU : {produit.sku}) est à {stock_text} "
                f"dans {rayon.warehouse.name}. "
                f"Le seuil de réapprovisionnement est de {reorder_text}."
            ),
            resource_type='stock',
            resource_id=rayon.id,
            data={
                'product_id': str(produit.id),
                'product_name': produit.name,
                # ⚠ C'EST CE CHAMP QUI REND L'ALERTE ADRESSABLE. Sans lui, le
                # service de livraison ne peut pas la borner à un dépôt.
                'warehouse_id': str(rayon.warehouse_id),
                'warehouse_name': rayon.warehouse.name,
                'current_stock': float(rayon.quantity),
                'current_stock_display': stock_text,
                'reorder_point': float(produit.reorder_point),
            },
            action_url='/dashboard/stock/stock-levels',
            action_label='Voir le rayon',
        ))

    # ┌──────────────────────────────────────────────────────────────────────────┐
    # │ CHAQUE ÉTABLISSEMENT EST ISOLÉ, ET LA RÉSOLUTION PART AVEC SES          │
    # │ CRÉATIONS.                                                               │
    # │                                                                          │
    # │ La résolution était une écriture PARK-WIDE, posée AVANT la boucle. Une   │
    # │ organisation qui levait laissait toutes les suivantes avec leur ancienne │
    # │ alerte RÉSOLUE et aucune remplaçante : le marchand ne voyait plus rien   │
    # │ pour un rayon réellement vide, ce qui est pire que l'alerte périmée      │
    # │ qu'on venait de retirer.                                                 │
    # │                                                                          │
    # │ Et la boucle n'avait AUCUNE isolation : c'est la doctrine du journal de  │
    # │ synchronisation, « aucun `atomic` n'enveloppe la boucle, sinon une       │
    # │ opération fautive condamne les autres ».                                 │
    # └──────────────────────────────────────────────────────────────────────────┘
    _servir_par_etablissement(par_organisation, "bas-de-stock", a_resoudre)


@shared_task(soft_time_limit=240, time_limit=300)
def check_expiring_products():
    """
    Alerte sur les lots qui approchent de leur péremption, PAR ÉTABLISSEMENT.

    ┌──────────────────────────────────────────────────────────────────────────┐
    │ ELLE APPELAIT `alerter` PAR LOT, ET C'EST LE PROFIL QUE SON VOISIN      │
    │ DÉCLARE CORRIGÉ.                                                         │
    │                                                                          │
    │ Chaque appel rejouait `membres_actifs` et son préchargement, construisait │
    │ un `PerimetreAlertes` par membre, contrôlait l'idempotence par           │
    │ destinataire et enfilait un envoi - plus un `EXISTS` par lot. L'encadré  │
    │ de `check_low_stock_alerts` dit cela corrigé « par établissement » : le  │
    │ battement VOISIN, dans le même fichier, le payait encore en entier.      │
    │                                                                          │
    │ Même forme que lui, donc : un `resource_id__in` pour les alertes déjà    │
    │ actives, un lot par organisation, et la même isolation - une             │
    │ organisation qui lève ne condamne pas les suivantes.                      │
    └──────────────────────────────────────────────────────────────────────────┘
    """
    from django.conf import settings

    from apps.inventory.models import StockBatch
    from apps.notifications.models import Alert

    warning_days = settings.VENTE_FACILE.get('EXPIRY_WARNING_DAYS', 30)
    aujourdhui = timezone.localdate()
    warning_date = aujourdhui + timedelta(days=warning_days)

    lots = list(
        StockBatch.objects.filter(
            expiry_date__lte=warning_date,
            expiry_date__gte=aujourdhui,
            quantity__gt=0,
        ).select_related(
            'product', 'product__unit', 'organization', 'warehouse'
        )
    )
    if not lots:
        return

    # Les alertes ACTIVES de ces lots, en UNE requête - et non un `EXISTS` par
    # lot, qui était la moitié du coût.
    deja = set(
        Alert.objects.filter(
            alert_type=Alert.AlertType.EXPIRING_PRODUCT,
            resource_type='stock_batch',
            resource_id__in=[lot.id for lot in lots],
            status=Alert.Status.ACTIVE,
        ).values_list('resource_id', flat=True)
    )

    par_organisation = {}
    for lot in lots:
        if lot.id in deja:
            continue
        jours = (lot.expiry_date - aujourdhui).days
        par_organisation.setdefault(lot.organization, []).append(dict(
            alert_type=Alert.AlertType.EXPIRING_PRODUCT,
            severity=(
                Alert.Severity.HIGH if jours <= 7 else Alert.Severity.MEDIUM
            ),
            title=f"Produit bientôt périmé: {lot.product.name}",
            message=(
                f"Le lot {lot.batch_number} du produit {lot.product.name} "
                f"expire le {lot.expiry_date}. "
                f"Quantité: {_expiring_quantity(lot)}"
            ),
            resource_type='stock_batch',
            resource_id=lot.id,
            data={
                'batch_id': str(lot.id),
                'product_name': lot.product.name,
                'batch_number': lot.batch_number,
                'expiry_date': str(lot.expiry_date),
                'quantity': float(lot.quantity),
                # ⚠ UN LOT PORTE SON DÉPÔT : c'est ce qui rend l'alerte
                # adressable. Sans ce champ, la péremption d'un lot de la
                # réserve irait aussi au gérant de la boutique d'en face.
                'warehouse_id': str(lot.warehouse_id),
                'warehouse_name': lot.warehouse.name,
            },
            action_url='/dashboard/stock/stock-levels',
            action_label='Voir le lot',
        ))

    _servir_par_etablissement(par_organisation, "péremption")


@shared_task(soft_time_limit=240, time_limit=300)
def check_customer_payment_due():
    """
    Alerte sur les créances clients : échéance proche, échéance dépassée, et
    limite de crédit bientôt atteinte.

    ⚠ ELLE N'AVAIT AUCUNE BORNE DE TÂCHE, contre la règle que l'encadré de ce
    fichier écrit juste au-dessus - et ses TROIS boucles appelaient `alerter`
    par facture puis par client, donc avec le coût complet à chaque tour. Le
    battement voisin déclarait ce profil corrigé ; celui-ci le payait encore.

    Les trois types d'alerte (`payment_due`, `payment_overdue`, `credit_limit`)
    étaient déclarés dans `Alert.AlertType` depuis l'origine mais **aucune tâche
    ne les produisait** : `Sale.due_date` était stocké et exposé sans que rien ne
    le lise.

    Idempotence : une alerte ACTIVE existante pour la même ressource n'est pas
    recréée. Une facture passe de `payment_due` à `payment_overdue` quand
    l'échéance tombe ; l'alerte d'échéance proche est alors résolue pour ne pas
    laisser deux alertes concurrentes sur la même facture.
    """
    from django.conf import settings

    from apps.contacts.models import Customer
    from apps.notifications.models import Alert
    from apps.sales.models import Sale

    today = timezone.localdate()
    warning_days = settings.VENTE_FACILE.get('PAYMENT_DUE_WARNING_DAYS', 3)
    warning_date = today + timedelta(days=warning_days)

    open_invoices = Sale.objects.filter(
        status__in=[Sale.Status.PENDING, Sale.Status.PARTIALLY_PAID],
        amount_due__gt=0,
        customer__isnull=False,
        due_date__isnull=False,
    ).select_related('customer', 'organization')

    # ⚠ LES ALERTES ACTIVES EN UNE REQUÊTE PAR GENRE, et non un `EXISTS` par
    #   ligne : c'est lui qui multipliait le coût de ces trois boucles.
    #
    # ⚠ ELLE REND LES IDENTIFIANTS, PAS SEULEMENT LA PRÉSENCE. Une première
    #   écriture rendait un `set` de ressources, et la résolution devait alors
    #   rechercher les identifiants d'alerte PAR FACTURE - un N+1 réintroduit
    #   dans le correctif qui venait de le retirer.
    def _actives(genre, resource_type, ids):
        par_ressource = {}
        for ressource, alerte in Alert.objects.filter(
            alert_type=genre,
            resource_type=resource_type,
            resource_id__in=ids,
            status=Alert.Status.ACTIVE,
        ).values_list('resource_id', 'id'):
            par_ressource.setdefault(ressource, []).append(alerte)
        return par_ressource

    echues = list(open_invoices.filter(due_date__lt=today))
    proches = list(
        open_invoices.filter(due_date__gte=today, due_date__lte=warning_date)
    )
    ids_factures = [v.id for v in echues + proches]
    deja_en_retard = _actives(
        Alert.AlertType.PAYMENT_OVERDUE, 'sale', ids_factures
    )
    deja_proches = _actives(Alert.AlertType.PAYMENT_DUE, 'sale', ids_factures)

    par_organisation = {}
    a_resoudre = {}

    for sale in echues:
        days_late = (today - sale.due_date).days

        # L'échéance est passée : l'alerte « bientôt dû » n'a plus d'objet.
        #
        # ⚠ ELLE SE RÉSOUT AVEC SA REMPLAÇANTE, dans le même `atomic`, et elle
        #   pose enfin `resolved_at` - le chemin bas-de-stock le posait, celui-ci
        #   non : le même état s'écrivait de deux façons.
        perimees = deja_proches.get(sale.id)
        if perimees:
            a_resoudre.setdefault(sale.organization, []).extend(perimees)

        if sale.id in deja_en_retard:
            continue

        par_organisation.setdefault(sale.organization, []).append(dict(
            alert_type=Alert.AlertType.PAYMENT_OVERDUE,
            severity=(
                Alert.Severity.HIGH if days_late > 7 else Alert.Severity.MEDIUM
            ),
            title=f"Paiement en retard : {sale.customer.name}",
            message=(
                f"La facture {sale.reference} est échue depuis {days_late} jour(s). "
                f"Reste à payer : {sale.amount_due} {sale.currency}."
            ),
            resource_type='sale',
            resource_id=sale.id,
            data={
                'sale_id': str(sale.id),
                'sale_reference': sale.reference,
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                'due_date': str(sale.due_date),
                'days_late': days_late,
                # Montant ET devise : une créance de 50 USD n'est pas une
                # créance de 50 CDF.
                'amount_due': str(sale.amount_due),
                'currency': sale.currency,
            },
        ))

    for sale in proches:
        if sale.id in deja_proches:
            continue

        days_left = (sale.due_date - today).days
        par_organisation.setdefault(sale.organization, []).append(dict(
            alert_type=Alert.AlertType.PAYMENT_DUE,
            severity=Alert.Severity.LOW,
            title=f"Échéance proche : {sale.customer.name}",
            message=(
                f"La facture {sale.reference} arrive à échéance "
                f"{'aujourd’hui' if days_left == 0 else f'dans {days_left} jour(s)'}. "
                f"Reste à payer : {sale.amount_due} {sale.currency}."
            ),
            resource_type='sale',
            resource_id=sale.id,
            data={
                'sale_id': str(sale.id),
                'sale_reference': sale.reference,
                'customer_id': str(sale.customer_id),
                'customer_name': sale.customer.name,
                'due_date': str(sale.due_date),
                'days_left': days_left,
                'amount_due': str(sale.amount_due),
                'currency': sale.currency,
            },
        ))

    # Limite de crédit bientôt atteinte. `current_balance` et `credit_limit` sont
    # tous deux exprimés en devise principale : la comparaison est homogène.
    # Une limite à 0 signifie « illimité », il n'y a rien à surveiller.
    threshold_percent = settings.VENTE_FACILE.get('CREDIT_LIMIT_WARNING_PERCENT', 80)
    near_limit = Customer.objects.filter(
        credit_limit__gt=0,
        current_balance__gt=0,
    ).filter(
        current_balance__gte=F('credit_limit') * threshold_percent / 100,
    ).select_related('organization')

    clients = list(near_limit)
    deja_au_plafond = _actives(
        Alert.AlertType.CREDIT_LIMIT, 'customer', [c.id for c in clients]
    )

    for customer in clients:
        if customer.id in deja_au_plafond:
            continue

        used_percent = int(customer.current_balance / customer.credit_limit * 100)
        par_organisation.setdefault(customer.organization, []).append(dict(
            alert_type=Alert.AlertType.CREDIT_LIMIT,
            severity=(
                Alert.Severity.HIGH if used_percent >= 100 else Alert.Severity.MEDIUM
            ),
            title=f"Limite de crédit : {customer.name}",
            message=(
                f"{customer.name} utilise {used_percent} % de sa limite de crédit "
                f"({customer.current_balance} sur {customer.credit_limit})."
            ),
            resource_type='customer',
            resource_id=customer.id,
            data={
                'customer_id': str(customer.id),
                'customer_name': customer.name,
                'current_balance': str(customer.current_balance),
                'credit_limit': str(customer.credit_limit),
                'used_percent': used_percent,
            },
        ))

    # Les trois genres partent ENSEMBLE, par établissement : une facture échue,
    # une échéance proche et un plafond atteint concernent le même marchand, et
    # les servir en trois tournées paierait trois fois son périmètre.
    _servir_par_etablissement(par_organisation, "créances", a_resoudre)


# ⚠ ELLE N'AVAIT AUCUNE BORNE, contre la règle de l'encadré ci-dessus. Elle
#   parcourt tout le parc et enfile un rappel par établissement : sans borne,
#   elle retombait sur `CELERY_TASK_TIME_LIMIT`, soit TRENTE MINUTES de worker
#   retenu.
@shared_task(soft_time_limit=240, time_limit=300)
def check_subscription_expiry():
    """Check for subscriptions nearing expiry."""
    from apps.subscriptions.models import Subscription
    from apps.notifications.models import Alert
    from apps.notifications.services import alerter
    
    warning_date = timezone.now() + timedelta(days=7)
    
    expiring_subscriptions = Subscription.objects.filter(
        status=Subscription.Status.ACTIVE,
        current_period_end__lte=warning_date,
        current_period_end__gte=timezone.now()
    ).select_related('organization', 'plan')
    
    for subscription in expiring_subscriptions:
        existing_alert = Alert.objects.filter(
            organization=subscription.organization,
            alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING,
            status=Alert.Status.ACTIVE
        ).exists()
        
        if not existing_alert:
            days_remaining = (subscription.current_period_end - timezone.now()).days
            
            alerter(
                subscription.organization,
                alert_type=Alert.AlertType.SUBSCRIPTION_EXPIRING,
                severity=Alert.Severity.CRITICAL if days_remaining <= 3 else Alert.Severity.HIGH,
                title="Abonnement expire bientôt",
                message=f"Votre abonnement {subscription.plan.name} expire dans {days_remaining} jours. "
                       f"Renouvelez pour éviter une interruption de service.",
                data={
                    'subscription_id': str(subscription.id),
                    'plan_name': subscription.plan.name,
                    'expiry_date': str(subscription.current_period_end)
                },
                # ⚠ LA BOUCLE ÉCRITE À LA MAIN DISPARAÎT, et c'est le sens de
                # ce lot. Elle cherchait `role='owner'` : le fan-out rend la
                # même chose en opposant `subscription.view`, que le catalogue
                # n'accorde qu'au propriétaire. La différence est qu'un marchand
                # qui accorde ce droit par `extra_permissions` est désormais
                # servi, et que c'était ici la SEULE alerte livrée sur sept.
                action_url='/dashboard/subscription',
                action_label='Renouveler',
            )


# ⚠ Même raison : une purge qui balaie tout le parc doit pouvoir être
#   interrompue. Elle ne prévient personne, donc elle n'a pas de réseau à
#   attendre - la borne y est plus courte.
@shared_task(soft_time_limit=120, time_limit=180)
def cleanup_old_notifications():
    """Delete notifications older than 30 days."""
    from apps.notifications.models import Notification
    
    cutoff_date = timezone.now() - timedelta(days=30)
    
    deleted_count, _ = Notification.objects.filter(
        created_at__lt=cutoff_date,
        is_read=True
    ).delete()
    
    return f"Deleted {deleted_count} old notifications"
