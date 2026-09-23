# Tableau de bord Power BI

`export_powerbi.py` transforme l'entrepôt DuckDB en un **schéma en étoile** (fichiers
CSV dans `export/powerbi/`) que Power BI Desktop lit sans connecteur particulier.
Ce document décrit le modèle, les mesures DAX et les pages proposées.

Power BI Desktop est gratuit sur Windows (Microsoft Store ou
<https://powerbi.microsoft.com/desktop>). Aucune licence Pro n'est nécessaire pour
un tableau de bord local ; la licence ne sert qu'à publier sur le service en ligne.

## 1. Produire les données

```bash
python export_powerbi.py               # entrepôt réel, ~2 min (bootstrap des segments)
python export_powerbi.py --skip-stats  # sans les analyses statistiques, quelques secondes
```

| Fichier | Grain | Lignes (compte réel) |
|---|---|---|
| `dim_date.csv` | un jour | 1 036 |
| `dim_campaign.csv` | une campagne (+ `event_id` selon la convention hashtag / numéro en tête) | 106 |
| `dim_ad.csv` | une annonce (+ ensemble, campagne, type de créative, vidéo ou non, longueur du texte) | 378 |
| `fact_daily.csv` | annonce × jour | 3 759 |
| `fact_age_gender.csv` | annonce × jour × âge × genre | 44 775 |
| `fact_placement.csv` | annonce × jour × plateforme × position × appareil | 91 108 |
| `fact_region.csv` | annonce × jour × région | 7 012 |
| `fact_hourly.csv` | campagne × jour × heure | 25 556 |
| `stat_segments.csv` | un segment par dimension : indice, IC bootstrap, valeur q (FDR) | 58 |

Toutes les tables de faits portent les mêmes mesures brutes : `impressions`, `reach`,
`clicks`, `spend`, `link_click`, `landing_page_view`, `view_content`, `add_to_cart`,
`initiate_checkout`, `purchase`, `purchase_value`, `video_view`, `post_engagement`.

## 2. Charger dans Power BI Desktop

1. **Obtenir les données → Texte/CSV**, un fichier à la fois (ou **Dossier** puis
   « Combiner » n'est *pas* recommandé : les schémas diffèrent d'un fichier à l'autre).
2. Dans Power Query, vérifier que `date` est typée *Date* (pas *Date/Heure*) et que les
   identifiants `campaign_id`, `ad_id`, `adset_id` sont typés **Texte** — ce sont des
   entiers à 17 chiffres, Power BI les arrondirait en nombre décimal.
3. **Fermer et appliquer**.

> **Séparateur décimal.** Les CSV utilisent le point (`3.40`), comme tout export
> technique. Power BI les interprète selon les *paramètres régionaux du fichier*,
> hérités de Windows. Si les colonnes numériques arrivent en texte ou multipliées
> par cent, aller dans **Fichier → Options → Fichier actuel → Paramètres
> régionaux** et choisir **Anglais (États-Unis)**, puis actualiser. (Sur ce poste,
> la culture Windows est déjà `en-US` : rien à changer.)

## 3. Modèle (vue Modèle)

Relations à créer, toutes en **1 → \*** avec filtrage dans une seule direction :

```
dim_date[date]            → fact_daily[date], fact_age_gender[date], fact_placement[date],
                            fact_region[date], fact_hourly[date]
dim_ad[ad_id]             → fact_daily[ad_id], fact_age_gender[ad_id], fact_placement[ad_id],
                            fact_region[ad_id]
dim_campaign[campaign_id] → dim_ad[campaign_id], fact_hourly[campaign_id]
```

Marquer `dim_date` comme **table de dates** (Outils de table → Marquer comme table de
dates) pour activer l'intelligence temporelle. Masquer les colonnes `campaign_id` et
`ad_id` des tables de faits pour que les utilisateurs filtrent par les dimensions.

## 4. Mesures DAX

Créer une table `Mesures` vide (Entrer des données) et y placer :

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
bootstrap. C'est pour cela que `stat_segments.csv` est fourni : ses colonnes
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

puis, dans Power BI Desktop, **Accueil → Actualiser** (les requêtes pointent sur le
dossier, les fichiers sont simplement relus). Sur le service Power BI, une
*passerelle de données* locale permet d'automatiser cette actualisation.

## 7. Confidentialité

`export/` est exclu du dépôt Git (`.gitignore`). Les CSV contiennent les noms de
campagnes réels : ne pas partager le dossier ni publier le `.pbix` en dehors de
l'entreprise. Pour une démonstration publique, exporter depuis le jeu synthétique :

```bash
set BACKFILL_DATA_DIR=data_sample
python export_powerbi.py --out export/powerbi_sample
```
