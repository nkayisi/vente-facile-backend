"""
Trace des opérations reçues d'un terminal.

Sert deux fins, indissociables.

**L'idempotence.** Le réseau peut lâcher entre le moment où le serveur valide
une vente et celui où le client reçoit la réponse. Le client renverra alors la
même opération, et sans trace le serveur créerait une seconde vente : le
marchand encaisserait deux fois, le stock sortirait deux fois. L'identifiant
d'opération EST la clé primaire, donc le rejeu est structurellement impossible.

**L'enquête.** Une opération refusée est une pièce à conviction : elle dit ce
que le terminal a tenté, quand, et pourquoi le serveur a dit non. Elle ne se
purge jamais.
"""
import uuid

from django.db import models


class SyncOperation(models.Model):
    """Une intention métier envoyée par un terminal."""

    class Verdict(models.TextChoices):
        #: Appliquée : le point de sauvegarde a été validé.
        APPLIED = 'applied', 'Appliquée'
        #: Déjà connue : on rejoue le résultat, sans rien réécrire.
        DUPLICATE = 'duplicate', 'Doublon'
        #: Refus métier déterministe. JAMAIS réessayée.
        REJECTED = 'rejected', 'Refusée'
        #: Transitoire. Le client réessaiera, rien n'a été écrit.
        RETRY = 'retry', 'À réessayer'
        #: Porte fermée : abonnement expiré, permission manquante.
        BLOCKED = 'blocked', 'Bloquée'

    #: L'identifiant vient du CLIENT, et c'est la clé primaire : c'est ce qui
    #: rend le renvoi sûr après une coupure au pire moment.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    organization = models.ForeignKey(
        'organizations.Organization', on_delete=models.CASCADE,
        related_name='sync_operations',
    )
    device = models.ForeignKey(
        'users.Device', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='operations',
    )
    user = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, related_name='+',
    )

    #: `sale.create`, `payment.add`, `expense.create`…
    kind = models.CharField(max_length=64, db_index=True)
    #: Compteur strictement croissant par appareil : l'ordre d'émission.
    seq = models.BigIntegerField()

    payload = models.JSONField()
    result = models.JSONField(null=True, blank=True)
    error = models.JSONField(null=True, blank=True)

    verdict = models.CharField(max_length=12, choices=Verdict.choices)
    http_status = models.IntegerField(null=True, blank=True)

    #: Horodatage du terminal. Indicatif : son horloge peut dériver, ce n'est
    #: pas lui qui ordonne quoi que ce soit côté serveur.
    occurred_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = 'sync_operations'
        ordering = ['received_at']
        indexes = [
            models.Index(fields=['organization', 'device', 'received_at']),
            models.Index(fields=['organization', 'verdict']),
        ]

    def __str__(self):
        return f'{self.kind} {self.id} -> {self.verdict}'

    @property
    def is_settled(self):
        """
        Vrai quand le verdict est définitif.

        `retry` ne l'est pas : rien n'a été écrit, l'opération repassera. Le
        distinguer est ce qui évite de rejouer un résultat qui n'existe pas.
        """
        return self.verdict in (self.Verdict.APPLIED, self.Verdict.REJECTED)
