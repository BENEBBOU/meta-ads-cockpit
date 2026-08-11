# Détection précoce des annonces sous-performantes

Variables mesurées sur les jours 1–2, cible sur les jours 3+. Séparation temporelle au 2026-08-01.

## Données

```
annonces éligibles      174
entraînement (< 2026-08-01)  137
test (>= 2026-08-01)         37
seuil 'bonne annonce'   0.1556 checkouts/$ (médiane entraînement)
part de positifs (test) 43%
```

## Performance hors échantillon

```
AUC modèle (test)       0.598   IC 95% [0.393, 0.794]
AUC checkouts/$ seul    0.586
AUC entraînement        0.857
score de Brier          0.265
```

_Écart entraînement/test de 0.26 : sur-apprentissage résiduel malgré la régularisation. Attendu avec un échantillon de cette taille._

### Validation croisée groupée par campagne

```
annonces évaluées       174 (5 plis)
AUC hors pli            0.726   IC 95% [0.646, 0.802]
AUC checkouts/$ seul    0.779
```

_Chaque annonce sert de cas de test une fois, aucune campagne ne figure des deux côtés d'un pli. Gagne en puissance ce que la séparation temporelle apporte en réalisme : les deux sont nécessaires._

### Coefficients (variables standardisées)

```
  log_icr      +1.187
  log_cpm      -0.609
  log_atcr     +0.513
  log_spend    +0.379
  log_lpvr     -0.361
  is_reel      +0.275
```

### Calibration

```
 n  predicted  observed
10      0.097     0.400
 9      0.247     0.333
 9      0.474     0.333
 9      0.828     0.667
```

## Courbe de décision (test uniquement)

Gain projeté en coupant au jour 2 les annonces les moins bien notées, budget libéré redistribué avec saturation (alpha = 0.8).

```
 cut_fraction  ads_cut  spend_freed  conv_lost  projected_conv  gain_pct
         0.00        0         0.00        0.0          499.00      0.00
         0.10        4       147.71       11.0          507.82      1.77
         0.20        7       153.19       15.0          504.42      1.09
         0.25        9       360.44       58.0          487.79     -2.25
         0.33       12       585.41       85.0          491.16     -1.57
         0.40       15       758.29      112.0          486.72     -2.46
         0.50       18      1030.72      125.0          520.65      4.34
```

_Référence : 499 checkouts pour 3043 $._

_La courbe entière est rapportée volontairement. Choisir le meilleur point de coupe en la lisant réintroduirait exactement le biais de sélection que la séparation temporelle sert à éliminer._

## Limite structurelle : campagne contre créative

```
campagnes retenues (>=3 annonces)  26  (108 annonces)
rho brut (inter + intra)           +0.463  (p=4.6e-07)
rho intra-campagne                 +0.197  (p=0.041)
```

L'essentiel du pouvoir prédictif distingue les campagnes, non les créatives d'une même campagne. **Cet outil sert l'arbitrage de portefeuille, pas la sélection d'un visuel parmi ses frères.**

## Verdict

**Le signal existe.** En validation croisée groupée, l'AUC hors pli atteint 0.73 avec un intervalle [0.65, 0.80] qui exclut nettement le hasard. Les deux premiers jours de diffusion portent bien de l'information sur la suite.

**Mais le modèle n'apporte rien.** La seule variable « checkouts par dollar » atteint 0.78, soit mieux que la régression logistique à 6 variables (0.73). Avec 174 observations et des variables corrélées, le modèle ajoute du bruit plutôt que du signal.

**Recommandation : déployer la règle simple, pas le modèle.** Classer les annonces par checkouts/\$ au jour 2 et signaler le dernier quartile. C'est interprétable, calculable dans un tableur, sans modèle à réentraîner ni à surveiller — et c'est mesurablement plus performant.

**La généralisation temporelle reste non démontrée.** Sur la séparation juillet → août, l'AUC tombe à 0.60 avec un intervalle [0.39, 0.79] qui contient 0,50. Deux lectures possibles, que 37 annonces de test ne permettent pas de départager : soit le signal ne se transporte pas d'un mois à l'autre, soit l'échantillon est trop petit pour le détecter. À trancher lorsque septembre fournira un second jeu de test.

## Réserves

- Échantillon de test de 37 annonces : l'intervalle de confiance sur l'AUC est large et doit être cité avec le point estimé.
- Population filtrée : seules les annonces ayant duré au moins 5 jours sont présentes. Celles coupées plus tôt par le media buyer sont absentes, donc le modèle apprend sur une population déjà triée par le jugement humain.
- La redistribution suppose que le budget libéré se reporte sur les annonces conservées avec des rendements décroissants. C'est une hypothèse, pas une mesure.
