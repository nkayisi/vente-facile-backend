"""Validation d'un code de devise, partagée par tous les points d'écriture.

⚠ LA TABLE `Currency` EST LA SOURCE DE VÉRITÉ, PAS UNE CONSTANTE PYTHON.

Elle l'est déjà partout ailleurs : `CurrencyService.resolve` la consulte pour ses
appelants, et `currency_decimals` / `currency_symbol` en tirent décimales et
symbole. Une constante créerait une SECONDE autorité, qui se contredirait dans
les deux sens : un code qu'elle autorise mais que la table ignore passerait la
validation puis tomberait sur le repli de `OrganizationService`, et un code actif
en table mais absent d'elle serait refusé alors que tous les chemins de LECTURE
l'honorent. Conséquence voulue : ouvrir un pays nouveau est une ligne en base.

⚠ PAS DE `ChoiceField`, ET C'EST LA RAISON D'ÊTRE DE CE MODULE. Ses choix sont
évalués À L'IMPORT du module : une requête vers `currencies` au chargement
casserait `manage.py migrate` sur base vierge, la table n'existant pas encore.
C'est donc `CharField` plus un `validate_<champ>` qui délègue ici.

⚠ PAS `validators=[valider_code_devise]` NON PLUS. DRF JETTE la valeur de retour
d'un validateur de cette liste : la normalisation en majuscules serait perdue, et
« xaf » entrerait en base tel quel. Seul `validate_<champ>` rend la valeur.

Ce module vit à part des serializers et des services pour la même raison que
`apps/sales/sale_validation.py` et
`apps/organizations/membership_warehouse_validation.py` : éviter les cycles
d'import entre les deux.
"""
from rest_framework import serializers


def valider_code_devise(value: str) -> str:
    """Rend le code normalisé en majuscules, ou lève si la plateforme l'ignore.

    ⚠ Le message est VOLONTAIREMENT distinct de celui de `CurrencyService.resolve`
    (« La devise 'X' n'est pas activée pour cette organisation. ») : les deux
    répondent à deux questions différentes - la plateforme connaît-elle cette
    monnaie, contre cette boutique l'a-t-elle activée - et les confondre enverrait
    le marchand chercher un réglage qui n'est pas en cause.
    """
    from apps.settings.models import Currency

    code = (value or '').strip().upper()
    if not Currency.objects.filter(code=code, is_active=True).exists():
        raise serializers.ValidationError(
            f"La devise '{code}' n'est pas prise en charge."
        )
    return code
