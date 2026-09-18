#!/usr/bin/env python3
"""Publish the analysis as a dashboard inside a Google Spreadsheet.

    python publish_to_sheets.py --sheet-id <ID>
    python publish_to_sheets.py --sheet-id <ID> --credentials ../google_service_account.json

Why Python computes and Sheets only displays: the statistics that make this
analysis trustworthy — cluster bootstrap, false-discovery-rate control,
within-campaign centring — cannot be expressed as spreadsheet formulas. The
script therefore writes computed values, and is idempotent: dashboard tabs are
dropped and rebuilt on every run, so re-running after a fresh extraction simply
refreshes them.

Raw data tabs (ads_daily, ads_by_placement, ...) are never touched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb
import pandas as pd

from analysis.segments import SEGMENT_SPECS, analyse
from meta_backfill.config import ConfigError, load_settings

# Dashboard tabs are owned by this script and rebuilt each run. Anything else
# in the workbook is left alone.
MANAGED = ["Synthèse", "Entonnoir", "Emplacements", "Démographie", "Horaire", "Créatives"]

HEADER_BG = {"red": 0.12, "green": 0.16, "blue": 0.20}
BAND_BG = {"red": 0.965, "green": 0.973, "blue": 0.980}
GOOD_BG = {"red": 0.85, "green": 0.93, "blue": 0.87}
GOOD_FG = {"red": 0.11, "green": 0.37, "blue": 0.20}

# Number formats, one per column index. Without them every column reads as a
# bare float and the reader has to guess whether 3.55 is dollars, a ratio or a
# count.
MONEY0 = {"type": "CURRENCY", "pattern": "#,##0\\ \"$\""}
MONEY2 = {"type": "CURRENCY", "pattern": "#,##0.00\\ \"$\""}
INT = {"type": "NUMBER", "pattern": "#,##0"}
PCT1 = {"type": "PERCENT", "pattern": "0.0%"}
DEC2 = {"type": "NUMBER", "pattern": "0.00"}
DEC3 = {"type": "NUMBER", "pattern": "0.000"}
# Explicit sign: a creative effect of -0.20 must not be mistaken for +0.20.
SIGNED3 = {"type": "NUMBER", "pattern": "+0.000;−0.000;0.000"}
QVAL = {"type": "NUMBER", "pattern": "0.0000"}

COLUMN_FORMATS: dict[str, dict[int, dict]] = {
    "Entonnoir":    {1: INT, 2: PCT1},
    "Emplacements": {1: MONEY0, 2: INT, 3: MONEY2, 4: MONEY2, 5: MONEY2,
                     6: DEC2, 7: QVAL},
    "Démographie":  {1: MONEY0, 2: INT, 3: MONEY2, 4: MONEY2, 5: MONEY2,
                     6: DEC2, 7: QVAL},
    "Horaire":      {1: MONEY0, 2: INT, 3: MONEY2, 4: MONEY2, 5: MONEY2,
                     6: DEC2, 7: QVAL},
    "Créatives":    {1: SIGNED3, 2: SIGNED3},
}

# Columns holding "oui" markers, highlighted green.
FLAG_COLUMNS: dict[str, list[int]] = {
    "Emplacements": [8], "Démographie": [8], "Horaire": [8],
    "Créatives": [3, 4],
}

# Performance-index columns get a red-white-green scale centred on 1.0.
INDEX_COLUMN: dict[str, int] = {"Emplacements": 6, "Démographie": 6, "Horaire": 6}


def build_service(credentials_path: Path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        str(credentials_path), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------
# Data assembly
# --------------------------------------------------------------------------

def funnel_table(con) -> pd.DataFrame:
    row = con.sql(
        """
        SELECT SUM(impressions) AS imp, SUM(link_click) AS lc,
               SUM(landing_page_view) AS lpv, SUM(add_to_cart) AS atc,
               SUM(initiate_checkout) AS ic, SUM(purchase) AS p
        FROM ads_daily WHERE spend > 0
        """
    ).df().iloc[0]

    stages = [
        ("Impressions", row.imp), ("Clics sur lien", row.lc),
        ("Vues de page", row.lpv), ("Ajouts au panier", row.atc),
        ("Paiements initiés", row.ic), ("Achats", row.p),
    ]
    out = []
    for i, (name, value) in enumerate(stages):
        prev = stages[i - 1][1] if i else None
        rate = (value / prev * 100) if prev else None
        # Stocké comme ratio, pas comme 6.3 : le format POURCENTAGE de Sheets
        # multiplie par 100 à l'affichage, et la valeur reste juste si on la
        # réutilise dans un calcul.
        out.append({"Étape": name, "Volume": int(value or 0),
                    "Taux de passage": round(rate / 100, 5) if rate is not None else ""})
    return pd.DataFrame(out)


def segment_table(con, spec_name: str, label: str) -> pd.DataFrame:
    """Ranked segments with the statistics that justify acting on them."""
    result = analyse(con, SEGMENT_SPECS[spec_name], n_boot=1000)
    if result.frame.empty:
        return pd.DataFrame()
    f = result.frame
    return pd.DataFrame({
        label: f["segment"],
        "Dépense ($)": f["spend"].round(0),
        "Conversions": f["conversions"].round(0),
        "Coût / conv. ($)": f["cost_per_conv"].round(2),
        "IC bas": f["cpc_ci_low"].round(2),
        "IC haut": f["cpc_ci_high"].round(2),
        "Indice": f["perf_index"].round(2),
        "q": f["q_value"].map(lambda v: round(v, 4)),
        "Significatif": f["significant"].map({True: "oui", False: ""}),
    })


def creative_table(con, settings) -> pd.DataFrame:
    """The attention/conversion inversion, within-campaign."""
    from analysis.creative import build_dataset, univariate_associations

    creatives = settings.data_dir / "creatives.parquet"
    thumbs = settings.data_dir / "thumbnails"
    if not creatives.exists():
        return pd.DataFrame()

    df = build_dataset(con, creatives, thumbs)
    ctr = univariate_associations(df, "ctr").set_index("feature")
    icr = univariate_associations(df, "icr").set_index("feature")

    labels = {
        "is_video": "Vidéo", "aspect_ratio": "Format vertical", "is_reel": "Reel",
        "contrast": "Contraste", "colorfulness": "Colorfulness",
        "saturation": "Saturation", "has_date": "Mention de date",
        "n_emoji": "Nombre d'emojis", "n_question": "Question",
        "caps_ratio": "Proportion de majuscules",
    }
    rows = []
    for key, name in labels.items():
        if key not in ctr.index or key not in icr.index:
            continue
        rows.append({
            "Caractéristique": name,
            "Effet sur le CTR": round(float(ctr.loc[key, "rho_within"]), 3),
            "Effet sur la conversion": round(float(icr.loc[key, "rho_within"]), 3),
            "Robuste (CTR)": "oui" if bool(ctr.loc[key, "holds_within"]) else "",
            "Robuste (conv.)": "oui" if bool(icr.loc[key, "holds_within"]) else "",
        })
    return pd.DataFrame(rows).sort_values("Effet sur le CTR", ascending=False)


def summary_rows(con, funnel: pd.DataFrame) -> list[list]:
    t = con.sql(
        """
        SELECT COUNT(DISTINCT ad_id) AS ads, COUNT(DISTINCT campaign_id) AS camps,
               COUNT(DISTINCT date) AS days, ROUND(SUM(spend), 2) AS spend,
               SUM(initiate_checkout) AS ic, MIN(date) AS d0, MAX(date) AS d1
        FROM ads_daily WHERE spend > 0
        """
    ).df().iloc[0]

    checkout = int(funnel.loc[funnel["Étape"] == "Paiements initiés", "Volume"].iloc[0])
    purchase = int(funnel.loc[funnel["Étape"] == "Achats", "Volume"].iloc[0])
    rate = purchase / checkout * 100 if checkout else 0

    return [
        ["PÉRIMÈTRE", ""],
        ["Publicités", int(t.ads)],
        ["Campagnes", int(t.camps)],
        ["Jours avec diffusion", int(t.days)],
        ["Période", f"{t.d0.date()} → {t.d1.date()}"],
        ["Dépense totale ($)", float(t.spend)],
        ["Paiements initiés", int(t.ic or 0)],
        ["", ""],
        ["CONSTATS", ""],
        ["1. Pixel Purchase défaillant",
         f"{rate:.2f}% de passage paiement→achat (norme : 40-70%)"],
        ["   Conséquence", "Meta optimise à l'aveugle sur tout le compte"],
        ["", ""],
        ["2. Audience Network", "Coût par conversion très supérieur à la moyenne"],
        ["   Action", "Désactiver l'emplacement"],
        ["", ""],
        ["3. Attention ≠ conversion",
         "Les formats qui captent les clics convertissent moins"],
        ["   Conséquence", "Optimiser sur le CTR sélectionne contre la conversion"],
        ["", ""],
        ["MÉTHODE", ""],
        ["Inférence", "Bootstrap par grappes (rééchantillonnage des annonces)"],
        ["Multiplicité", "Correction Benjamini-Hochberg (FDR 5%)"],
        ["Confondants", "Centrage intra-campagne systématique"],
        ["Réserve", "Données observationnelles — un test A/B reste nécessaire"],
    ]


# --------------------------------------------------------------------------
# Sheets writing
# --------------------------------------------------------------------------

def reset_tabs(svc, sheet_id: str) -> dict[str, int]:
    """Drop and recreate the managed tabs. Returns tab title -> sheetId."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)").execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    requests = [{"deleteSheet": {"sheetId": existing[t]}} for t in MANAGED if t in existing]
    requests += [{"addSheet": {"properties": {"title": t, "index": i}}}
                 for i, t in enumerate(MANAGED)]
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests}).execute()

    meta = svc.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties(title,sheetId)").execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}


def write_values(svc, sheet_id: str, tab: str, values: list[list]) -> None:
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"'{tab}'!A1",
        valueInputOption="RAW", body={"values": values}).execute()


def frame_values(df: pd.DataFrame) -> list[list]:
    if df.empty:
        return [["(aucune donnée)"]]
    body = df.astype(object).where(pd.notna(df), "").values.tolist()
    return [list(df.columns)] + body


def style_table(tab: str, gid: int, n_rows: int, n_cols: int) -> list[dict]:
    """Header, banding, number formats and conditional highlights for one tab."""
    body = {"sheetId": gid, "startRowIndex": 1, "endRowIndex": n_rows + 1,
            "startColumnIndex": 0, "endColumnIndex": n_cols}

    req: list[dict] = [
        # Header row
        {"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n_cols},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BG,
                "verticalAlignment": "MIDDLE",
                "wrapStrategy": "WRAP",
                "textFormat": {"bold": True,
                               "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,verticalAlignment,"
                      "wrapStrategy,textFormat)"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": gid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        # Light rule between rows keeps wide tables readable
        {"updateBorders": {
            "range": body,
            "innerHorizontal": {"style": "SOLID", "width": 1,
                                "color": {"red": 0.88, "green": 0.90, "blue": 0.92}}}},
        {"addBanding": {"bandedRange": {
            "range": body,
            "rowProperties": {"firstBandColor": {"red": 1, "green": 1, "blue": 1},
                              "secondBandColor": BAND_BG}}}},
    ]

    # Numeric columns get an explicit unit.
    for col, fmt in COLUMN_FORMATS.get(tab, {}).items():
        if col >= n_cols:
            continue
        req.append({"repeatCell": {
            "range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": n_rows + 1,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"numberFormat": fmt,
                                           "horizontalAlignment": "RIGHT"}},
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"}})

    # "oui" markers in green, so the eye finds what survived the FDR screen.
    for col in FLAG_COLUMNS.get(tab, []):
        if col >= n_cols:
            continue
        req.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": gid, "startRowIndex": 1, "endRowIndex": n_rows + 1,
                        "startColumnIndex": col, "endColumnIndex": col + 1}],
            "booleanRule": {
                "condition": {"type": "TEXT_EQ",
                              "values": [{"userEnteredValue": "oui"}]},
                "format": {"backgroundColor": GOOD_BG,
                           "textFormat": {"bold": True, "foregroundColor": GOOD_FG}}}}}})

    # Performance index: 1.0 is the account average, so the scale is centred there.
    idx = INDEX_COLUMN.get(tab)
    if idx is not None and idx < n_cols:
        req.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": gid, "startRowIndex": 1, "endRowIndex": n_rows + 1,
                        "startColumnIndex": idx, "endColumnIndex": idx + 1}],
            # MIN/MAX for the extremes rather than fixed bounds: an
            # InterpolationPoint value is parsed in the spreadsheet's locale,
            # and "0.5" is rejected on a French sheet that expects "0,5".
            # The midpoint stays anchored at 1 — the account average — and an
            # integer carries no decimal separator, so it is locale-proof.
            "gradientRule": {
                "minpoint": {"color": {"red": 0.96, "green": 0.80, "blue": 0.76},
                             "type": "MIN"},
                "midpoint": {"color": {"red": 1, "green": 1, "blue": 1},
                             "type": "NUMBER", "value": "1"},
                "maxpoint": {"color": {"red": 0.80, "green": 0.91, "blue": 0.82},
                             "type": "MAX"}}}}})

    req.append({"autoResizeDimensions": {"dimensions": {
        "sheetId": gid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": n_cols}}})
    return req


def style_summary(gid: int, rows: list[list]) -> list[dict]:
    """The synthesis tab is prose, not a table: section titles in bold."""
    req = [
        {"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 280}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {
            "range": {"sheetId": gid, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 520}, "fields": "pixelSize"}},
    ]
    for i, row in enumerate(rows):
        label = str(row[0]) if row else ""
        if label and label == label.upper() and len(label) > 3:
            req.append({"repeatCell": {
                "range": {"sheetId": gid, "startRowIndex": i, "endRowIndex": i + 1,
                          "startColumnIndex": 0, "endColumnIndex": 2},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": HEADER_BG,
                    "textFormat": {"bold": True,
                                   "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
                "fields": "userEnteredFormat(backgroundColor,textFormat)"}})
    return req


def bar_chart(gid: int, title: str, n_rows: int, label_col: int, value_col: int,
              anchor_col: int) -> dict:
    """A column chart anchored to the right of the table."""
    def rng(col):
        # The API expects ChartData -> sourceRange -> sources; omitting the
        # sourceRange level fails with "Unknown name 'sources'".
        return {"sourceRange": {"sources": [{
            "sheetId": gid, "startRowIndex": 0, "endRowIndex": n_rows + 1,
            "startColumnIndex": col, "endColumnIndex": col + 1}]}}
    return {"addChart": {"chart": {
        "spec": {
            "title": title,
            "basicChart": {
                "chartType": "COLUMN", "legendPosition": "NO_LEGEND",
                "domains": [{"domain": rng(label_col)}],
                "series": [{"series": rng(value_col), "targetAxis": "LEFT_AXIS"}],
                "headerCount": 1,
            },
        },
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": gid, "rowIndex": 1, "columnIndex": anchor_col},
            "widthPixels": 620, "heightPixels": 340}},
    }}}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sheet-id", required=True)
    p.add_argument("--credentials", type=Path,
                   default=Path("../google_service_account.json"),
                   help="clé du compte de service ayant accès au classeur")
    args = p.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if not settings.warehouse_path.exists():
        print("Aucun entrepôt — lancer build_warehouse.py.", file=sys.stderr)
        return 1
    if not args.credentials.exists():
        print(f"Clé introuvable : {args.credentials}", file=sys.stderr)
        return 1

    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    svc = build_service(args.credentials)

    print("calcul des tableaux…")
    funnel = funnel_table(con)
    placement = segment_table(con, "placement", "Emplacement")
    demo = segment_table(con, "age_gender", "Segment")
    hourly = segment_table(con, "hourly", "Heure") if "hourly" in SEGMENT_SPECS else pd.DataFrame()
    creative = creative_table(con, settings)

    print("reconstruction des onglets…")
    gids = reset_tabs(svc, args.sheet_id)

    payload = {
        "Synthèse": summary_rows(con, funnel),
        "Entonnoir": frame_values(funnel),
        "Emplacements": frame_values(placement),
        "Démographie": frame_values(demo),
        "Horaire": frame_values(hourly),
        "Créatives": frame_values(creative),
    }
    for tab, values in payload.items():
        write_values(svc, args.sheet_id, tab, values)
        print(f"  {tab:<14} {len(values) - 1:>4} lignes")

    print("mise en forme et graphiques…")
    requests: list[dict] = []
    for tab, values in payload.items():
        gid = gids[tab]
        if tab == "Synthèse":
            requests += style_summary(gid, values)
        else:
            requests += style_table(tab, gid, len(values) - 1, len(values[0]))

    # Charts only where a ranking is worth seeing at a glance.
    if not funnel.empty:
        requests.append(bar_chart(gids["Entonnoir"], "Entonnoir de conversion",
                                  len(funnel), 0, 1, 5))
    if not placement.empty:
        requests.append(bar_chart(gids["Emplacements"], "Coût par conversion",
                                  len(placement), 0, 3, 10))
    if not demo.empty:
        requests.append(bar_chart(gids["Démographie"], "Coût par conversion",
                                  len(demo), 0, 3, 10))
    if not hourly.empty:
        requests.append(bar_chart(gids["Horaire"], "Coût par conversion par heure",
                                  len(hourly), 0, 3, 10))

    svc.spreadsheets().batchUpdate(
        spreadsheetId=args.sheet_id, body={"requests": requests}).execute()

    print(f"\nhttps://docs.google.com/spreadsheets/d/{args.sheet_id}/edit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
