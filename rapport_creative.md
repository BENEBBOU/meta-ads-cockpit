# Analyse des créatives publicitaires

260 annonces avec créative et diffusion suffisante · 89 campagnes · 12 variables de texte, 7 d'image, 2 de format.

## CTR — l'attention gagnée

### Associations univariées (Spearman, corrigées FDR)

```
     feature   n    rho  q_value sig  intra intra_ok
  caps_ratio 260 -0.346 2.12e-07 oui -0.129         
aspect_ratio 260  0.329  5.7e-07 oui  0.414      oui
    is_video 260  0.298 6.79e-06 oui  0.416      oui
     is_reel 260  0.259 0.000125 oui  0.374      oui
  saturation 260 -0.226  0.00103 oui -0.184      oui
colorfulness 260 -0.208  0.00258 oui -0.213      oui
  n_question 260  0.185  0.00838 oui  0.168      oui
    contrast 260 -0.177   0.0104 oui -0.245      oui
    has_date 260 -0.176   0.0104 oui -0.243      oui
   has_price 260  0.169    0.013 oui  0.029         
```

_11 variable(s) sur 21 survivent au contrôle du taux de fausses découvertes ; 11 tiennent encore une fois l'effet campagne retiré (colonne `intra_ok`)._

### Modèle multivarié (validation croisée groupée par campagne)

```
  logistic             AUC 0.697   IC 95% [0.630, 0.759]   n=260
  gradient_boosting    AUC 0.662   IC 95% [0.593, 0.728]   n=260
  (21 variables, 5 plis)
```

## Checkouts par dollar — la conversion obtenue

### Associations univariées (Spearman, corrigées FDR)

```
     feature   n    rho q_value sig  intra intra_ok
  caps_ratio 260  0.239 0.00208 oui -0.083         
    contrast 260  0.218 0.00297 oui  0.205      oui
colorfulness 260  0.217 0.00297 oui  0.136         
   has_price 260 -0.209 0.00359 oui  0.119         
  brightness 260  0.195 0.00672 oui  0.106         
     n_emoji 260 -0.155  0.0438 oui -0.226      oui
  dark_ratio 260 -0.150  0.0456 oui -0.079         
     is_reel 260 -0.143  0.0524     -0.180      oui
aspect_ratio 260 -0.142  0.0524     -0.199      oui
    is_video 260 -0.135  0.0621     -0.199      oui
```

_7 variable(s) sur 21 survivent au contrôle du taux de fausses découvertes ; 5 tiennent encore une fois l'effet campagne retiré (colonne `intra_ok`)._

### Modèle multivarié (validation croisée groupée par campagne)

```
  logistic             AUC 0.603   IC 95% [0.534, 0.669]   n=260
  gradient_boosting    AUC 0.616   IC 95% [0.547, 0.677]   n=260
  (21 variables, 5 plis)
```
