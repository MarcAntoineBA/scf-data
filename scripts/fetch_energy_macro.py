"""Fetcher Macro/Énergie — 3 indicateurs de l'onglet Indicateur (catégorie Macro).

Toutes les composantes sont recalculées à partir de sources publiques auditables
(FRED St. Louis Fed + EIA U.S. Energy Information Administration), avec le maximum
d'historique disponible.

  1. WTI + 321 Crack Spread  (window.__WTI_CRACK__)
     - WTI Cushing spot .......... FRED DCOILWTICO  ($/bbl, quotidien 1986+)
     - Essence RBOB NY Harbor .... FRED DGASNYH     ($/gal, quotidien 2006+)
     - Distillat ULSD NY Harbor .. FRED DDFUELNYH   ($/gal, quotidien 2006+)
     - Crack 3:2:1 recalculé chez nous :
         crack = ( 2*essence*42 + 1*distillat*42 − 3*WTI ) / 3      [$/bbl]
       = marge brute de raffinage : 3 barils de brut → 2 essence + 1 distillat.

  2. Strategic Petroleum Reserve  (window.__SPR__)
     - Stocks SPR US ............. EIA WCSSTUS1 (milliers de barils, hebdo 1990+)
     - + variation hebdomadaire (rate of change).

  3. Appétit Retail  (window.__RETAIL_APPETITE__)
     Vanda Research (flux retail actions) est propriétaire/payant → aucun proxy
     gratuit ni auditable. Reconstruction avec deux composantes FRED auditables :
     - Margin Debt FINRA/Z.1 ..... FRED BOGZ1FL663067003Q ($M, trimestriel 1945+)
       = actions achetées à crédit = levier spéculatif retail. YoY = flux d'appétit.
     - Retail Money Market Funds . FRED WRMFNS   ($Md, hebdo)
       = cash retail « sur la touche » = appétit inverse (risk-off quand ça monte).

Sortie : ~/Desktop/Site_Crypto_Finance/energy_macro_cache.js
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fred_helpers import fetch_fred

OUT_JS = os.path.expanduser("~/Desktop/Site_Crypto_Finance/energy_macro_cache.js")
OUT_PATHS = [
    os.path.expanduser("~/Library/Caches/site_crypto_finance/energy_macro_cache.js"),
    OUT_JS,
]

# EIA : clé gratuite (https://www.eia.gov/opendata/register.php).
# Ordre de résolution : env EIA_API_KEY → fichier ~/.eia_api_key → DEMO_KEY.
# DEMO_KEY est partagée mondialement et renvoie souvent 429 : déposer sa propre
# clé dans ~/.eia_api_key (une ligne) suffit à fiabiliser TOUS les fetchers EIA.
def _eia_key():
    k = os.environ.get("EIA_API_KEY")
    if k:
        return k.strip()
    p = os.path.expanduser("~/.eia_api_key")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            pass
    return "DEMO_KEY"


EIA_API_KEY = _eia_key()


# ─────────────────────────────────────────────────────────────────────────────
def _downsample(pairs, max_points=6000):
    """Downsample linéaire d'une liste [(date, ...vals)] en préservant extrémités."""
    n = len(pairs)
    if n <= max_points:
        return pairs
    step = n / max_points
    out = []
    for i in range(max_points):
        idx = min(n - 1, int(round(i * step)))
        out.append(pairs[idx])
    if out[-1] != pairs[-1]:
        out[-1] = pairs[-1]
    return out


# ─── 1. WTI + 321 Crack Spread ───────────────────────────────────────────────
def build_wti_crack():
    print("[energy] fetch WTI / gasoline / distillate ...", file=sys.stderr)
    wti = fetch_fred("DCOILWTICO", start="1986-01-01")   # $/bbl
    gas = fetch_fred("DGASNYH", start="1986-01-01")       # $/gal RBOB
    dis = fetch_fred("DDFUELNYH", start="1986-01-01")     # $/gal ULSD
    if not wti or not gas or not dis:
        print("[energy] WTI/crack: une série manque", file=sys.stderr)
        return None
    wti_m = dict(zip(wti["dates"], wti["values"]))
    gas_m = dict(zip(gas["dates"], gas["values"]))
    dis_m = dict(zip(dis["dates"], dis["values"]))

    rows = []          # (date, wti, crack)
    wti_only = []      # (date, wti) — pour l'historique WTI long (1986+)
    for d in sorted(wti_m):
        w = wti_m[d]
        wti_only.append((d, round(w, 2)))
        g = gas_m.get(d)
        s = dis_m.get(d)
        if g is None or s is None:
            continue
        crack = (2 * g * 42 + 1 * s * 42 - 3 * w) / 3.0
        rows.append((d, round(w, 2), round(crack, 2)))
    if not rows:
        return None

    cur_d, cur_w, cur_c = rows[-1]
    # variation crack sur ~1 mois (21 séances)
    chg_txt = ""
    if len(rows) >= 22:
        prev_c = rows[-22][2]
        dd = cur_c - prev_c
        chg_txt = f"{dd:+.1f}$ 1m"
    # tone : marge de raffinage. Repères : <10 faible, 10-25 normal, >25 tendue.
    if cur_c >= 30:
        tone, label = "warn", "Marge raffinage très tendue"
    elif cur_c >= 18:
        tone, label = "pos", "Marge raffinage solide"
    elif cur_c >= 10:
        tone, label = "eq", "Marge raffinage normale"
    else:
        tone, label = "neg", "Marge raffinage comprimée"

    # Résolution quotidienne complète (les deux courbes WTI + crack denses).
    hist = [{"d": d, "w": w, "c": c} for d, w, c in _downsample(rows, 6000)]
    return {
        "current": {
            "date": cur_d, "wti": cur_w, "crack": round(cur_c, 2),
            "tone": tone, "label": label, "chg_txt": chg_txt,
        },
        "history": hist,
        "n_obs": len(rows),
        "first_date": rows[0][0],
        "wti_first_date": wti_only[0][0],
        "source_url": "https://fred.stlouisfed.org/series/DCOILWTICO",
    }


# ─── 2. Strategic Petroleum Reserve (EIA) ────────────────────────────────────
def _eia_spr():
    """EIA WCSSTUS1 — stocks SPR (milliers de barils), hebdomadaire depuis 1990."""
    all_rows = {}
    offset = 0
    for _ in range(20):  # pagination 5000 lignes max
        params = {
            "api_key": EIA_API_KEY,
            "frequency": "weekly",
            "data[0]": "value",
            "facets[series][]": "WCSSTUS1",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": 5000,
        }
        url = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/?" + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(url, headers={"User-Agent": "SiteCryptoFinance"})
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        data = d.get("response", {}).get("data", [])
        if not data:
            break
        for row in data:
            v = row.get("value")
            if v is None:
                continue
            all_rows[row["period"]] = float(v)
        if len(data) < 5000:
            break
        offset += 5000
    return sorted(all_rows.items())  # [(date, thousand_bbl)]


def build_spr():
    print("[energy] fetch SPR (EIA WCSSTUS1) ...", file=sys.stderr)
    try:
        series = _eia_spr()
    except Exception as e:
        print(f"[energy] SPR EIA échec: {e}", file=sys.stderr)
        return None
    if not series:
        return None
    rows = []  # (date, value_kbbl, chg_kbbl)
    prev = None
    for d, v in series:
        chg = None if prev is None else round(v - prev, 0)
        rows.append((d, v, chg))
        prev = v

    cur_d, cur_v, cur_chg = rows[-1]
    cur_mbbl = cur_v / 1000.0  # millions de barils
    peak = max(v for _, v, _ in rows) / 1000.0
    drawdown_pct = (cur_mbbl / peak - 1) * 100 if peak else 0
    # variation 4 semaines
    chg_txt = ""
    if len(rows) >= 5:
        d4 = (cur_v - rows[-5][1]) / 1000.0
        chg_txt = f"{d4:+.1f} Mb 4sem"
    if drawdown_pct <= -40:
        tone, label = "neg", "Réserve fortement ponctionnée"
    elif drawdown_pct <= -15:
        tone, label = "warn", "Réserve en repli marqué"
    elif cur_chg is not None and cur_chg > 0:
        tone, label = "pos", "Réserve en reconstitution"
    else:
        tone, label = "eq", "Réserve stable"

    hist = [{"d": d, "u": round(v / 1000.0, 2),
             "c": (round(c / 1000.0, 3) if c is not None else None)}
            for d, v, c in _downsample(rows, 3000)]
    return {
        "current": {
            "date": cur_d,
            "mbbl": round(cur_mbbl, 1),
            "kbbl": cur_v,
            "chg_txt": chg_txt,
            "chg_week": (round(cur_chg / 1000.0, 3) if cur_chg is not None else None),
            "peak_mbbl": round(peak, 1),
            "drawdown_pct": round(drawdown_pct, 1),
            "tone": tone, "label": label,
        },
        "history": hist,
        "n_obs": len(rows),
        "first_date": rows[0][0],
        "source_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s=WCSSTUS1&f=W",
    }


# ─── 3. Appétit Retail (Margin Debt + Retail MMF) ────────────────────────────
# Source PRIMAIRE de la dette de marge : FINRA, fichier officiel des
# « Margin Statistics » (mensuel, 1997-01+, $M). C'est LA série que tout le
# monde cite ; publiée ~3-4 semaines après la fin du mois.
#   https://www.finra.org/rules-guidance/key-topics/margin-accounts/margin-statistics
# Le lien porte « 2021-03 » dans son chemin mais le fichier est réécrit sur
# place à chaque publication mensuelle — c'est bien la version courante.
#
# Le fallback FRED BOGZ1FL663067003Q (Z.1 de la Fed) est TRIMESTRIEL et publié
# avec ~5 mois de retard : il ne sert qu'à ne pas laisser le panneau vide si
# FINRA est injoignable. Les deux séries ne sont PAS au même niveau (périmètre
# comptable différent : $1 502 Md FINRA vs $622 Md Z.1 début 2026), donc on ne
# les concatène jamais — on bascule de l'une à l'autre en le disant à l'écran.
FINRA_MARGIN_URL = "https://www.finra.org/sites/default/files/2021-03/margin-statistics.xlsx"


def fetch_finra_margin(timeout=30, max_retries=3):
    """Dette de marge FINRA mensuelle → [(YYYY-MM-01, $Md)] triée, ou None.

    Le .xlsx est un OOXML minimal (colonne A = 'YYYY-MM' en inlineStr,
    colonne B = debit balances en $M) : on le lit avec zipfile + regex plutôt
    que d'ajouter openpyxl en dépendance.
    """
    import zipfile
    import io

    last_err = None
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                FINRA_MARGIN_URL,
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                blob = resp.read()
            z = zipfile.ZipFile(io.BytesIO(blob))
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", errors="ignore")
            rows = []
            for r in re.findall(r"<row[^>]*>(.*?)</row>", sheet, re.S):
                ym = re.search(r'r="A\d+"[^>]*><is><t>(\d{4})-(\d{2})</t>', r)
                dv = re.search(r'r="B\d+"[^>]*><v>([\d.]+)</v>', r)
                if not (ym and dv):
                    continue          # ligne d'en-tête / cellule vide
                rows.append((f"{ym.group(1)}-{ym.group(2)}-01",
                             round(float(dv.group(1)) / 1000.0, 1)))   # $M → $Md
            rows.sort()
            if len(rows) < 120:
                raise ValueError(f"FINRA: seulement {len(rows)} lignes lues")
            return rows
        except Exception as e:   # noqa: BLE001 — on veut TOUT rattraper pour le fallback
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
    print(f"[energy] FINRA margin KO ({last_err}) → fallback FRED Z.1", file=sys.stderr)
    return None


def _yoy_series(rows, lag):
    """[(date, val)] → [(date, YoY %)] avec un décalage de `lag` observations."""
    out = []
    for i in range(lag, len(rows)):
        prev = rows[i - lag][1]
        if prev:
            out.append((rows[i][0], round((rows[i][1] / prev - 1) * 100, 1)))
    return out


def build_retail():
    print("[energy] fetch Margin Debt (FINRA) + Retail MMF (FRED) ...", file=sys.stderr)
    margin_rows = fetch_finra_margin()
    if margin_rows:
        margin_src, margin_freq, margin_lag = "FINRA", "mensuel", 12
        margin_src_url = ("https://www.finra.org/rules-guidance/key-topics/"
                          "margin-accounts/margin-statistics")
        margin_src_label = "FINRA · Margin Statistics (mensuel)"
    else:
        fred = fetch_fred("BOGZ1FL663067003Q", start="1990-01-01")  # $M trimestriel
        margin_rows = [(d, round(v / 1000.0, 1))
                       for d, v in zip(fred["dates"], fred["values"])] if fred else []
        margin_src, margin_freq, margin_lag = "FRED Z.1", "trimestriel", 4
        margin_src_url = "https://fred.stlouisfed.org/series/BOGZ1FL663067003Q"
        margin_src_label = "FRED BOGZ1FL663067003Q (trimestriel · repli)"

    mmf = fetch_fred("WRMFNS", start="1990-01-01")                # $Md hebdo
    if not margin_rows and not mmf:
        return None

    mmf_rows = []
    if mmf and mmf.get("dates"):
        for d, v in zip(mmf["dates"], mmf["values"]):
            mmf_rows.append((d, round(v, 1)))  # $Md

    # YoY = le signal d'appétit. Percentile sur tout l'historique disponible :
    # les seuils fixes seuls sont trompeurs (médiane FINRA depuis 1998 = +14%).
    yoy_rows = _yoy_series(margin_rows, margin_lag)
    margin_yoy = yoy_rows[-1][1] if yoy_rows else None
    yoy_pct = None
    if yoy_rows and len(yoy_rows) >= 24:
        vals = sorted(y for _, y in yoy_rows)
        yoy_pct = round(100.0 * sum(1 for v in vals if v < margin_yoy) / len(vals))

    # tone : seuils recalibrés sur la distribution réelle FINRA 1998+ (les
    # anciens seuils, hérités du trimestriel Z.1, déclenchaient « surchauffe »
    # une observation sur deux). Repères = percentiles 90 / 50 / 25.
    if margin_yoy is None:
        tone, label = "eq", "—"
    elif margin_yoy >= 40:
        tone, label = "warn", "Levier retail en surchauffe"
    elif margin_yoy >= 15:
        tone, label = "pos", "Appétit retail en hausse"
    elif margin_yoy >= -5:
        tone, label = "eq", "Appétit retail stable"
    else:
        tone, label = "neg", "Désendettement retail (risk-off)"

    chg_txt = f"{margin_yoy:+.1f}% YoY" if margin_yoy is not None else ""
    cur_date = (margin_rows[-1][0] if margin_rows else (mmf_rows[-1][0] if mmf_rows else "—"))
    return {
        "current": {
            "date": cur_date,
            "margin_bn": (margin_rows[-1][1] if margin_rows else None),
            "margin_yoy": margin_yoy,
            "margin_yoy_pct": yoy_pct,
            "mmf_bn": (mmf_rows[-1][1] if mmf_rows else None),
            "mmf_date": (mmf_rows[-1][0] if mmf_rows else None),
            "tone": tone, "label": label, "chg_txt": chg_txt,
        },
        "margin_hist": [{"d": d, "v": v} for d, v in _downsample(margin_rows, 3000)],
        "mmf_hist": [{"d": d, "v": v} for d, v in _downsample(mmf_rows, 3000)],
        "margin_first_date": (margin_rows[0][0] if margin_rows else None),
        "mmf_first_date": (mmf_rows[0][0] if mmf_rows else None),
        "margin_src": margin_src,
        "margin_src_label": margin_src_label,
        "margin_freq": margin_freq,
        "margin_lag": margin_lag,
        "source_url": margin_src_url,
    }


def _read_previous():
    """Relit le cache précédent → {'__SPR__': {...}, ...} (vide si illisible).

    GARDE-FOU : l'EIA renvoie régulièrement 429 et FINRA peut être injoignable.
    Sans ça, une seule composante en échec réécrivait `window.__SPR__ = null`
    et effaçait un historique parfaitement valide — le panneau affichait alors
    « cache non chargé » jusqu'au prochain run réussi.
    """
    out = {}
    for path in (OUT_JS, OUT_PATHS[0]):
        try:
            with open(path, encoding="utf-8") as f:
                blob = f.read()
        except Exception:
            continue
        for key in ("__WTI_CRACK__", "__SPR__", "__RETAIL_APPETITE__"):
            if key in out:
                continue
            m = re.search(r"window\.%s = (.*?);\n" % key, blob, re.S)
            if not m:
                continue
            try:
                val = json.loads(m.group(1))
            except Exception:
                continue
            if val:
                out[key] = val
        if len(out) == 3:
            break
    return out


def main():
    print("[energy] start", file=sys.stderr)
    wti_crack = build_wti_crack()
    spr = build_spr()
    retail = build_retail()
    if not any([wti_crack, spr, retail]):
        print("[energy] toutes les séries ont échoué, abandon", file=sys.stderr)
        sys.exit(1)
    # Conserve la dernière valeur connue pour toute composante en échec.
    prev = _read_previous()
    for key, cur in (("__WTI_CRACK__", wti_crack), ("__SPR__", spr),
                     ("__RETAIL_APPETITE__", retail)):
        if cur or key not in prev:
            continue
        print(f"[energy] {key} en échec → conservation du cache précédent "
              f"({prev[key].get('current', {}).get('date', '?')})", file=sys.stderr)
        if key == "__WTI_CRACK__":
            wti_crack = prev[key]
        elif key == "__SPR__":
            spr = prev[key]
        else:
            retail = prev[key]
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # Horodatage embarqué : le panneau affiche la date de FETCH (fraîcheur du
    # cache) séparément de la date de la DONNÉE (vintage de publication).
    if retail:
        retail["generated"] = updated
    js = (
        f"window.__WTI_CRACK__ = {json.dumps(wti_crack, ensure_ascii=False, separators=(',', ':'))};\n"
        f"window.__SPR__ = {json.dumps(spr, ensure_ascii=False, separators=(',', ':'))};\n"
        f"window.__RETAIL_APPETITE__ = {json.dumps(retail, ensure_ascii=False, separators=(',', ':'))};\n"
        f"window.__ENERGY_MACRO_UPDATED__ = {json.dumps(updated)};\n"
    )
    wrote = []
    for outp in OUT_PATHS:
        try:
            os.makedirs(os.path.dirname(outp), exist_ok=True)
            with open(outp, "w", encoding="utf-8") as f:
                f.write(js)
            wrote.append(outp)
        except Exception as e:
            print(f"[energy] écriture échouée {outp}: {e}", file=sys.stderr)
    print(
        "[energy] OK · "
        f"crack={wti_crack['current']['crack'] if wti_crack else 'NA'} "
        f"WTI={wti_crack['current']['wti'] if wti_crack else 'NA'} · "
        f"SPR={spr['current']['mbbl'] if spr else 'NA'}Mb · "
        f"marginYoY={retail['current']['margin_yoy'] if retail else 'NA'} · wrote {len(wrote)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
