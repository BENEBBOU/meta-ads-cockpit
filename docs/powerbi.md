# Tableau de bord Power BI

`export_powerbi.py` transforme l'entrepôt DuckDB en un **schéma en étoile** que Power BI
Desktop lit sans connecteur particulier. Ce document décrit le modèle, les mesures DAX
et les pages proposées.

Power BI Desktop est gratuit sur Windows (Microsoft Store ou
<https://powerbi.microsoft.com/desktop>). Aucune licence Pro n'est nécessaire pour
un tableau de bord local ; la licence ne sert qu'à publier sur le service en ligne.

## 1. Produire les données

```bash
python export_powerbi.py               # entrepôt réel, ~2 min (bootstrap des segments)
python export_powerbi.py --skip-stats  # sans les analyses statistiques, quelques secondes
python export_powerbi.py --csv         # ajoute un CSV par table, pour d'autres outils
```

Le script écrit **un classeur Excel**, `export/powerbi/meta_ads_powerbi.xlsx`, avec une
feuille par table.

Excel plutôt que CSV, pour une raison concrète : un CSV est du texte, et Power BI le lit
avec les paramètres régionaux de sa **langue d'affichage**. Sur une installation
française, `21.16` devient silencieusement 2116 et `36323.0` devient 363230 — sans
message d'erreur, et le réglage « Paramètres régionaux » du fichier ne corrige pas les
étapes de typage déjà enregistrées. Un classeur transporte les types réels, donc le
problème ne peut pas se poser. Pour la même raison, les identifiants Meta (17 à 18
chiffres) sont écrits en **texte** : Excel stocke les nombres en virgule flottante,
exacte jusqu'à 15 chiffres seulement, et un identifiant altéré casse les relations
sans prévenir.

| Feuille | Grain | Lignes (compte réel) |
|---|---|---|
| `dim_date` | un jour | 1 036 |
| `dim_campaign` | une campagne (+ `event_id` selon la convention hashtag / numéro en tête) | 106 |
| `dim_ad` | une annonce (+ ensemble, campagne, type de créative, vidéo ou non, longueur du texte) | 378 |
| `fact_daily` | annonce × jour | 3 759 |
| `fact_age_gender` | annonce × jour × âge × genre | 44 775 |
| `fact_placement` | annonce × jour × plateforme × position × appareil | 91 108 |
| `fact_region` | annonce × jour × région | 7 012 |
| `fact_hourly` | campagne × jour × heure | 25 556 |
| `stat_segments` | un segment par dimension : indice, IC bootstrap, valeur q (FDR) | 58 |

Toutes les tables de faits portent les mêmes mesures brutes : `impressions`, `reach`,
`clicks`, `spend`, `link_click`, `landing_page_view`, `view_content`, `add_to_cart`,
`initiate_checkout`, `purchase`, `purchase_value`, `video_view`, `post_engagement`.

## 2. Charger dans Power BI Desktop

**Obtenir les données → Classeur Excel**, choisir `meta_ads_powerbi.xlsx`. Dans le
navigateur, cocher **« Sélectionner plusieurs éléments »**, puis les feuilles voulues,
et cliquer **Charger** — surtout pas « Transformer les données » : les types sont déjà
corrects, il n'y a rien à retoucher.

Pour une première prise en main, `dim_date`, `dim_campaign`, `dim_ad` et `fact_daily`
suffisent à construire la page Vue d'ensemble.

Contrôle immédiat après le chargement : une carte affichant la mesure `Dépense` doit
donner **44 017 $**, et `Checkouts` **9 670** — les chiffres de la page d'accueil de
l'application Streamlit. S'ils diffèrent, c'est un problème de typage, pas d'analyse.

## 3. Modèle (vue Modèle)

Relations à créer, toutes en **1 → \*** avec filtrage dans une seule direction :

```
dim_date[date]            → fact_daily[date], fact_age_gender[date], fact_placement[date],
                            fact_region[date], fact_hourly[date]
dim_ad[ad_id]             → fact_daily[ad_id], fact_age_gender[ad_id], fact_placement[ad_id],
                            fact_region[ad_id]
dim_campaign[campaign_id] → dim_ad[campaign_id], fact_hourly[campaign_id]
```

`stat_segments` reste isolée : elle porte des résultats déjà agrégés, qu'aucun filtre du
modèle ne doit recalculer.

Power BI propose des relations automatiquement au chargement. Vérifier la liste dans
**Gérer les relations** et supprimer celles qui font doublon — en particulier
`fact_daily[campaign_id] → dim_campaign[campaign_id]`, qui crée un second chemin vers
les faits alors que `dim_campaign → dim_ad → fact_daily` existe déjà. Power BI la
désactive de lui-même ; autant l'enlever.

Marquer `dim_date` comme **table de dates** (clic droit sur la table dans le volet
Données → Marquer comme table de dates → colonne `date`) pour activer l'intelligence
temporelle. Masquer les colonnes `campaign_id` et `ad_id` des tables de faits pour que
les utilisateurs filtrent par les dimensions.

## 4. Mesures DAX

Clic droit sur `fact_daily` → **Nouvelle mesure**, une par formule :

```dax
Dépense = SUM ( fact_daily[spend] )
Impressions = SUM ( fact_daily[impressions] )
Clics lien = SUM ( fact_daily[link_click] )
Checkouts = SUM ( fact_daily[initiate_checkout] )
Achats = SUM ( fact_daily[purchase] )

CTR = DIVIDE ( [Clics lien], [Impressions] )
CPM = DIVIDE ( [Dépense], [Impressions] ) * 1000
Coût par checkout = DIVIDE ( [Dépense], [Checkouts] )
Checkouts par dollar = DIVIDE ( [Checkouts], [Dépense] )
Taux checkout → achat = DIVIDE ( [Achats], [Checkouts] )

-- Indice de performance d'un segment par rapport au reste du compte
-- (même définition que analysis/segments.py : taux du segment / taux hors segment)
Taux segment = DIVIDE ( SUM ( fact_age_gender[initiate_checkout] ), SUM ( fact_age_gender[spend] ) )
Taux reste du compte =
    VAR Conv = CALCULATE ( SUM ( fact_age_gender[initiate_checkout] ), ALLSELECTED ( fact_age_gender ) ) - SUM ( fact_age_gender[initiate_checkout] )
    VAR Spend = CALCULATE ( SUM ( fact_age_gender[spend] ), ALLSELECTED ( fact_age_gender ) ) - SUM ( fact_age_gender[spend] )
    RETURN DIVIDE ( Conv, Spend )
Indice segment = DIVIDE ( [Taux segment], [Taux reste du compte] )

-- Intelligence temporelle
Dépense mois précédent = CALCULATE ( [Dépense], DATEADD ( dim_date[date], -1, MONTH ) )
Variation dépense = DIVIDE ( [Dépense] - [Dépense mois précédent], [Dépense mois précédent] )
Dépense cumulée = CALCULATE ( [Dépense], DATESYTD ( dim_date[date] ) )

-- Fenêtre précoce : dépense et checkouts des N premiers jours d'une annonce
Jours depuis lancement = DATEDIFF ( RELATED ( dim_ad[first_day] ), fact_daily[date], DAY )  -- colonne calculée
Checkouts J+2 = CALCULATE ( [Checkouts], fact_daily[Jours depuis lancement] <= 1 )
Dépense J+2 = CALCULATE ( [Dépense], fact_daily[Jours depuis lancement] <= 1 )
Règle J+2 = DIVIDE ( [Checkouts J+2], [Dépense J+2] )
```

L'indice DAX est un indice *descriptif* : il reproduit le point estimé mais pas
l'intervalle de confiance ni la correction pour tests multiples, qui exigent un
bootstrap. C'est pour cela que la feuille `stat_segments` est fournie : ses colonnes
`rate_ci_low`, `rate_ci_high`, `q_value` et `significant` viennent du code Python et
se visualisent telles quelles (barres d'erreur, mise en forme conditionnelle).

## 5. Pages proposées

| Page | Visuels | Filtres |
|---|---|---|
| **Vue d'ensemble** | 4 cartes (Dépense, Checkouts, Coût par checkout, Taux checkout → achat) ; courbe Dépense et Checkouts par mois ; tableau par `event_id` avec mise en forme conditionnelle sur le coût par checkout | Segment de dates, campagne |
| **Entonnoir** | Entonnoir Impressions → Clics → Vues de page → Panier → Checkout → Achat ; carte « Taux checkout → achat » en rouge sous 40 % | Mois |
| **Emplacements** | Barres horizontales Coût par checkout par `publisher_platform` / `platform_position` ; nuage de points Dépense × Coût par checkout, taille = checkouts | Plateforme, appareil |
| **Segments** | Barres d'erreur (`rate_ci_low` / `rate_ci_high`) depuis `stat_segments`, couleur = `significant` ; matrice âge × genre avec `Indice segment` | Dimension |
| **Heures et jours** | Matrice `hour` × `day_name` (fact_hourly ⋈ dim_date) colorée par Checkouts par dollar | Campagne |
| **Créatives** | Tableau `dim_ad` avec `is_video`, `object_type`, `cta`, CTR et Checkouts par dollar ; nuage de points CTR × Checkouts par dollar, couleur = `is_video` — l'inversion attention / conversion du rapport | Campagne |

## 6. Rafraîchissement

`refresh_all.py` ne relance pas l'export. Pour un tableau de bord toujours à jour,
ajouter une étape dans `refresh.bat` :

```bat
python export_powerbi.py --skip-stats
```

puis, dans Power BI Desktop, **Accueil → Actualiser** : le classeur est simplement relu,
le modèle et les visuels sont conservés. Sur le service Power BI, une *passerelle de
données* locale permet d'automatiser cette actualisation.

## 7. Confidentialité

`export/` est exclu du dépôt Git (`.gitignore`). Le classeur contient les noms de
campagnes réels : ne pas le partager ni publier le `.pbix` en dehors de l'entreprise.
Pour une démonstration publique, exporter depuis le jeu synthétique :

```bash
set BACKFILL_DATA_DIR=data_sample
python export_powerbi.py --out export/powerbi_sample
```
