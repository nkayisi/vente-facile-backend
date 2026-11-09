"""
Tâches Celery de l'app Settings.

⚠ CE MODULE EST VIDE DEPUIS QUE LA FIDÉLITÉ SE COMPTE EN ARGENT, et c'est
  délibéré. Il portait `expire_loyalty_points`, qui lisait
  `LoyaltyProgram.points_expiry_days` ; l'expiration a disparu avec les points -
  une cagnotte en argent ne se périme pas sans qu'on l'ait demandé, et c'était
  l'un des réglages dont le marchand ne pouvait pas prévoir l'effet.

  Le fichier reste : `autodiscover_tasks()` l'importe, et le supprimer pour le
  recréer à la première tâche suivante ferait perdre cette note.
"""
