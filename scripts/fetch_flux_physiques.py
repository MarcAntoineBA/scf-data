"""Fetcher Flux Physiques — 3 indicateurs de l'onglet Indicateur (catégorie Macro).

Reproduit, avec des sources publiques auditables et le maximum d'historique,
trois graphiques de flux PHYSIQUES (métal + molécules) qui documentent la
recomposition des flux Chine ↔ US :

  1. Achats d'or de la PBoC        (window.__PBOC_GOLD__)
     - FMI, base IRFCL (International Reserves and Foreign Currency Liquidity),
       déclaration officielle mensuelle de la Banque populaire de Chine.
       · CHN.IRFCLDT1_IRFCL56V_FTO ... volume d'or, onces troy fines
       · CHN.IRFCLDT1_IRFCL56_USD .... or valorisé au marché, USD
       · CHN.IRFCLDT1_IRFCL65_USD .... total des réserves officielles, USD
     - Achat net mensuel recalculé chez nous :
         net_tonnes[m] = (oz[m] − oz[m−1]) × 31,1035 / 1e6
       (1 once troy fine = 31,1035 g ; 1 M oz = 31,1035 t)
     - Couverture : juin 2015 → aujourd'hui (avant 2015 la PBoC ne publiait
       ses réserves d'or que par annonces sporadiques pluriannuelles).

  2. Importations chinoises de brut (window.__CN_CRUDE__)
     - JODI-Oil (Joint Organisations Data Initiative) : base mensuelle où les
       douanes chinoises (General Administration of Customs) déposent leurs
       chiffres. Série CN / CRUDEOIL / TOTIMPSB.
       · KTONS → converti en millions de tonnes (÷1000) = l'unité Bloomberg
       · KBD ... milliers de barils/jour = version corrigée du nombre de jours
     - Couverture : 2002 → aujourd'hui.

  3. Exportations américaines de brut (window.__US_CRUDE_EXP__)
     - EIA (U.S. Energy Information Administration)
       · WCREXUS2 ... hebdomadaire, milliers de barils/jour, depuis fév. 1991
       · MCREXUS2 ... mensuel, milliers de barils/jour (série longue)
     - Moyenne 4 semaines recalculée chez nous (l'hebdo EIA est très bruité).

Sortie : ~/Desktop/Site_Crypto_Finance/flux_physiques_cache.js
Cache brut JODI (évite de retélécharger 25 années à chaque run) :
        ~/Library/Caches/site_crypto_finance/jodi_cn_crude.json
"""
import json
import os
import re
import sys
import csv
import io
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

OUT_JS = os.path.expanduser("~/Desktop/Site_Crypto_Finance/flux_physiques_cache.js")
CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
OUT_PATHS = [
    os.path.join(CACHE_DIR, "flux_physiques_cache.js"),
    OUT_JS,
]
JODI_RAW = os.path.join(CACHE_DIR, "jodi_cn_crude.json")

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

OZ_TO_TONNES = 31.1035 / 1e6  # 1 once troy fine → tonnes
UA = {"User-Agent": "SiteCryptoFinance/1.0 (dashboard perso)"}


def _get(url, timeout=90, headers=None, retries=0):
    """GET avec retry/backoff sur 429 — la clé EIA DEMO_KEY est partagée
    mondialement et rate-limitée ; sans retry, un run sur deux revient vide."""
    h = dict(UA)
    if headers:
        h.update(headers)
    delay = 6
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                print(f"[flux] 429 → retry dans {delay}s", file=sys.stderr)
                time.sleep(delay)
                delay *= 2.5
                continue
            raise


def _first_of_month(period):
    """'2026-M05' ou '2026-05' → '2026-05-01' (parsable par le JS pd())."""
    p = period.replace("-M", "-")
    parts = p.split("-")
    return f"{parts[0]}-{parts[1]}-01"


def _gold_spot():
    """Cours spot de l'once d'or (XAU/USD), gratuit et sans clé.
    api.gold-api.com renvoie {price, updatedAt} et sert un CORS ouvert : le
    navigateur peut donc rafraîchir ce même prix en direct côté client. Ici on
    ne fait que semer une valeur de départ dans le cache (le panneau affiche
    ensuite le vrai temps réel)."""
    try:
        d = json.loads(_get("https://api.gold-api.com/price/XAU", timeout=25))
        px = float(d.get("price"))
        if px > 0:
            return {"price": round(px, 2), "ts": d.get("updatedAt")}
    except Exception as e:
        print(f"[flux] spot or indisponible: {e}", file=sys.stderr)
    return None


# ─── 1. Achats d'or de la PBoC (FMI / IRFCL) ─────────────────────────────────
def build_pboc_gold():
    print("[flux] fetch IMF IRFCL — or PBoC ...", file=sys.stderr)
    inds = [
        "IRFCLDT1_IRFCL56V_FTO",  # volume, onces troy fines
        "IRFCLDT1_IRFCL56_USD",   # or valorisé marché, USD
        "IRFCLDT1_IRFCL65_USD",   # total réserves officielles, USD
    ]
    key = "CHN." + "+".join(inds) + "..M"
    url = f"https://api.imf.org/external/sdmx/2.1/data/IRFCL/{urllib.parse.quote(key, safe='.+')}"
    try:
        xml = _get(url, timeout=120, headers={"Accept": "application/xml"})
    except Exception as e:
        print(f"[flux] IMF IRFCL échec: {e}", file=sys.stderr)
        return None

    # Découpage par bloc <Series ...> ... </Series> pour rattacher chaque Obs
    # à son INDICATOR (le XML SDMX structure-specific met tout en attributs).
    series = {}
    for m in re.finditer(r"<Series\b([^>]*)>(.*?)</Series>", xml, re.S):
        attrs, body = m.group(1), m.group(2)
        ind = re.search(r'INDICATOR="([^"]+)"', attrs)
        if not ind:
            continue
        obs = {}
        for o in re.finditer(r'<Obs\b[^>]*TIME_PERIOD="([^"]+)"[^>]*OBS_VALUE="([^"]+)"', body):
            try:
                obs[o.group(1)] = float(o.group(2))
            except ValueError:
                pass
        if obs:
            series[ind.group(1)] = obs

    oz = series.get("IRFCLDT1_IRFCL56V_FTO") or {}
    gold_usd = series.get("IRFCLDT1_IRFCL56_USD") or {}
    tot_usd = series.get("IRFCLDT1_IRFCL65_USD") or {}
    if len(oz) < 12:
        print(f"[flux] IMF: série or trop courte ({len(oz)} obs)", file=sys.stderr)
        return None

    periods = sorted(oz)
    rows = []  # (date, réserves t, achat net t, part or %, cours implicite $/oz)
    prev = None
    for p in periods:
        res_t = oz[p] * OZ_TO_TONNES
        net = None if prev is None else (oz[p] - prev) * OZ_TO_TONNES
        share = px = None
        g, t = gold_usd.get(p), tot_usd.get(p)
        if g and t:
            share = round(g / t * 100, 2)
        if g and oz[p]:
            # Cours implicite de VALORISATION : ce que le FMI/PBoC retient pour
            # valoriser le stock en fin de mois = or_USD ÷ onces détenues.
            # Ce n'est pas un spot intraday, mais c'est la seule série de prix
            # 100% cohérente avec les tonnages du même tableau (zéro dépendance).
            px = round(g / oz[p], 1)
        rows.append((_first_of_month(p), round(res_t, 1),
                     (None if net is None else round(net, 2)), share, px))
        prev = oz[p]

    last = rows[-1]
    net_now = last[2] or 0.0
    # Cumul 12 mois glissants + série de mois consécutifs d'achat
    nets = [r[2] for r in rows if r[2] is not None]
    buy_12m = round(sum(nets[-12:]), 1)
    streak = 0
    for r in reversed(rows):
        if r[2] is not None and r[2] > 0.05:
            streak += 1
        else:
            break

    if net_now < -0.05:
        tone, label = "neg", "Vente nette déclarée"
    elif net_now <= 0.05:
        tone, label = "eq", "Pause déclarée"
    elif net_now < 2:
        tone, label = "eq", "Achats symboliques"
    elif net_now < 8:
        tone, label = "pos", "Achats réguliers"
    else:
        tone, label = "warn", "Accumulation soutenue"

    chg_txt = f"{net_now:+.1f} t/mois"
    spot = _gold_spot()
    return {
        "current": {
            "date": last[0],
            "net_t": round(net_now, 2),
            "reserves_t": last[1],
            "reserves_moz": round(oz[periods[-1]] / 1e6, 2),
            "share_pct": last[3],
            "gold_px": last[4],          # cours de valorisation du dernier mois
            "spot": (spot or {}).get("price"),    # cours spot au moment du fetch
            "spot_ts": (spot or {}).get("ts"),
            "buy_12m": buy_12m,
            "streak": streak,
            "tone": tone, "label": label, "chg_txt": chg_txt,
        },
        "history": [{"d": d, "r": r, "n": n, "s": s, "g": g} for d, r, n, s, g in rows],
        "n_obs": len(rows),
        "first_date": rows[0][0],
        "source_url": "https://data.imf.org/en/datasets/IMF.STA:IRFCL",
        "series_id": "CHN.IRFCLDT1_IRFCL56V_FTO..M",
    }


# ─── 2. Importations chinoises de brut (JODI-Oil) ────────────────────────────
def _jodi_year(year):
    """CSV annuel JODI (contient les 12 mois). Deux conventions de nommage :
    'YYYY.csv' pour les années closes, 'primaryyearYYYY.csv' pour l'année en cours."""
    base = "https://www.jodidata.org/_resources/files/downloads/oil-data/annual-csv/primary/"
    for name in (f"{year}.csv", f"primaryyear{year}.csv"):
        try:
            txt = _get(base + name, timeout=90)
        except Exception:
            continue
        out = {}
        for r in csv.reader(io.StringIO(txt)):
            # REF_AREA, TIME_PERIOD, ENERGY_PRODUCT, FLOW_BREAKDOWN, UNIT_MEASURE, OBS_VALUE, ASSESSMENT
            if len(r) < 6 or r[0] != "CN" or r[2] != "CRUDEOIL" or r[3] != "TOTIMPSB":
                continue
            if r[4] not in ("KTONS", "KBD"):
                continue
            try:
                v = float(r[5])
            except ValueError:
                continue
            out.setdefault(r[1], {})[r[4]] = v
        if out:
            return out
    return {}


def build_cn_crude():
    print("[flux] fetch JODI — imports brut Chine ...", file=sys.stderr)
    cache = {}
    if os.path.exists(JODI_RAW):
        try:
            with open(JODI_RAW, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    now_year = datetime.now(timezone.utc).year
    years = list(range(2002, now_year + 1))
    # Les années closes sont figées → on ne retélécharge que ce qui manque, plus
    # l'année en cours et la précédente (JODI révise sur ~12 mois glissants).
    refresh = {now_year, now_year - 1}
    fetched = 0
    for y in years:
        k = str(y)
        if k in cache and y not in refresh:
            continue
        got = _jodi_year(y)
        if got:
            cache[k] = got
            fetched += 1
        elif k not in cache:
            print(f"[flux] JODI {y} indisponible", file=sys.stderr)
    if fetched:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = JODI_RAW + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, JODI_RAW)
        except Exception as e:
            print(f"[flux] cache JODI non écrit: {e}", file=sys.stderr)

    merged = {}
    for k, months in cache.items():
        merged.update(months)
    periods = sorted(p for p, v in merged.items() if "KTONS" in v)
    if len(periods) < 24:
        print(f"[flux] JODI: série trop courte ({len(periods)})", file=sys.stderr)
        return None

    rows = []  # (date, Mt, kb/j)
    for p in periods:
        v = merged[p]
        mt = round(v["KTONS"] / 1000.0, 2)
        kbd = round(v.get("KBD", 0.0), 0) or None
        rows.append((_first_of_month(p), mt, kbd))

    last = rows[-1]
    by_date = {d: (mt, kbd) for d, mt, kbd in rows}
    # YoY sur le même mois (neutralise la saisonnalité ET le nombre de jours)
    y, m, _ = last[0].split("-")
    prev_key = f"{int(y)-1}-{m}-01"
    yoy = None
    if prev_key in by_date and by_date[prev_key][0]:
        yoy = round((last[1] / by_date[prev_key][0] - 1) * 100, 1)
    ma3 = round(sum(r[1] for r in rows[-3:]) / min(3, len(rows)), 2)
    # Percentile du niveau kb/j sur 5 ans (60 mois) — « où on se situe »
    win = [r[2] for r in rows[-60:] if r[2]]
    pct_5y = None
    if last[2] and len(win) >= 24:
        pct_5y = round(sum(1 for v in win if v <= last[2]) / len(win) * 100, 0)

    if yoy is None:
        tone, label = "eq", "Données incomplètes"
    elif yoy <= -15:
        tone, label = "neg", "Effondrement des importations"
    elif yoy <= -5:
        tone, label = "warn", "Importations en repli"
    elif yoy < 5:
        tone, label = "eq", "Importations stables"
    else:
        tone, label = "pos", "Importations en hausse"

    return {
        "current": {
            "date": last[0], "mt": last[1], "kbd": last[2],
            "yoy_pct": yoy, "ma3_mt": ma3, "pct_5y": pct_5y,
            "tone": tone, "label": label,
            "chg_txt": (f"{yoy:+.1f}% a/a" if yoy is not None else ""),
        },
        "history": [{"d": d, "mt": mt, "kbd": kbd} for d, mt, kbd in rows],
        "n_obs": len(rows),
        "first_date": rows[0][0],
        "source_url": "https://www.jodidata.org/oil/database/data-downloads.aspx",
        "series_id": "CN · CRUDEOIL · TOTIMPSB",
    }


# ─── 3. Exportations américaines de brut (EIA) ───────────────────────────────
def _eia_series(route, series_id, frequency):
    rows = {}
    offset = 0
    for _ in range(10):  # pagination 5000 lignes max
        params = {
            "api_key": EIA_API_KEY,
            "frequency": frequency,
            "data[0]": "value",
            "facets[series][]": series_id,
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": 5000,
        }
        url = f"https://api.eia.gov/v2/{route}/data/?" + urllib.parse.urlencode(params, doseq=True)
        try:
            d = json.loads(_get(url, timeout=60, retries=3))
        except Exception as e:
            print(f"[flux] EIA {series_id} échec: {e}", file=sys.stderr)
            break
        data = d.get("response", {}).get("data", [])
        if not data:
            break
        for row in data:
            v = row.get("value")
            if v in (None, ""):
                continue
            try:
                rows[row["period"]] = float(v)
            except (ValueError, TypeError):
                pass
        if len(data) < 5000:
            break
        offset += 5000
    return rows


def build_us_crude_exports():
    print("[flux] fetch EIA — exports brut US ...", file=sys.stderr)
    wk = _eia_series("petroleum/move/wkly", "WCREXUS2", "weekly")
    mo = _eia_series("petroleum/move/exp", "MCREXUS2", "monthly")
    if not wk:
        print("[flux] EIA: série hebdo vide", file=sys.stderr)
        return None

    wdates = sorted(wk)
    weekly = [(d, round(wk[d])) for d in wdates]
    # Moyenne mobile 4 semaines (l'hebdo EIA est très volatil : ±1 500 kb/j
    # d'une semaine à l'autre selon les fenêtres de chargement des tankers).
    ma4 = []
    for i, (d, v) in enumerate(weekly):
        w = [x[1] for x in weekly[max(0, i - 3):i + 1]]
        ma4.append(round(sum(w) / len(w)))

    monthly = [(_first_of_month(p), round(v)) for p, v in sorted(mo.items())] if mo else []

    cur_d, cur_v = weekly[-1]
    cur_ma4 = ma4[-1]
    # Record : sur la moyenne 4 semaines (un pic hebdo isolé n'est pas un régime)
    rec_i = max(range(len(ma4)), key=lambda i: ma4[i])
    rec_v, rec_d = ma4[rec_i], weekly[rec_i][0]
    pct_rec = round(cur_ma4 / rec_v * 100, 0) if rec_v else None
    # Record mensuel (c'est le chiffre que titre la presse : « record en avril »)
    mrec_d, mrec_v = (None, None)
    if monthly:
        mrec_d, mrec_v = max(monthly, key=lambda x: x[1])
    prev4 = ma4[-5] if len(ma4) >= 5 else None
    chg_txt = f"{cur_ma4 - prev4:+.0f} kb/j 1m" if prev4 else ""

    # Régime jugé sur le PERCENTILE 5 ANS de la moyenne 4 semaines, pas sur le
    # % du record absolu : les exports US sont passés de ~0 (interdiction levée
    # fin 2015) à 4-5 Mb/j, donc « 64% du record » ne veut pas dire « faible ».
    win5 = ma4[-260:]
    pct_5y = round(sum(1 for v in win5 if v <= cur_ma4) / len(win5) * 100) if len(win5) >= 52 else None
    if pct_5y is None:
        tone, label = "eq", "Historique insuffisant"
    elif pct_5y >= 95:
        tone, label = "warn", "Exportations au record"
    elif pct_5y >= 70:
        tone, label = "pos", "Exportations élevées"
    elif pct_5y >= 30:
        tone, label = "eq", "Exportations dans la moyenne"
    elif pct_5y >= 10:
        tone, label = "warn", "Exportations en repli"
    else:
        tone, label = "neg", "Exportations au plus bas"

    return {
        "current": {
            "date": cur_d, "kbd": cur_v, "ma4": cur_ma4,
            "record_ma4": rec_v, "record_date": rec_d, "pct_record": pct_rec,
            "pct_5y": pct_5y,
            "month_record": mrec_v, "month_record_date": mrec_d,
            "last_month": (monthly[-1] if monthly else None),
            "tone": tone, "label": label, "chg_txt": chg_txt,
        },
        "weekly": [{"d": d, "v": v, "m": ma4[i]} for i, (d, v) in enumerate(weekly)],
        "monthly": [{"d": d, "v": v} for d, v in monthly],
        "n_obs": len(weekly),
        "first_date": weekly[0][0],
        "source_url": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=pet&s=wcrexus2&f=w",
        "series_id": "WCREXUS2 (hebdo) · MCREXUS2 (mensuel)",
    }


def main():
    print("[flux] start", file=sys.stderr)
    gold = build_pboc_gold()
    cn = build_cn_crude()
    us = build_us_crude_exports()
    if not any([gold, cn, us]):
        print("[flux] toutes les séries ont échoué, abandon", file=sys.stderr)
        sys.exit(1)

    # Garde anti-régression : on n'écrase JAMAIS un cache complet par un cache
    # partiel (une source en panne ne doit pas vider un panneau qui marchait).
    # On relit le cache précédent et on réinjecte les blocs manquants.
    prev = {}
    if os.path.exists(OUT_JS):
        try:
            with open(OUT_JS, encoding="utf-8") as f:
                txt = f.read()
            for name in ("__PBOC_GOLD__", "__CN_CRUDE__", "__US_CRUDE_EXP__"):
                m = re.search(r"window\.%s = (\{.*?\});\n" % name, txt, re.S)
                if m:
                    prev[name] = json.loads(m.group(1))
        except Exception as e:
            print(f"[flux] relecture cache précédent impossible: {e}", file=sys.stderr)

    blocks = {"__PBOC_GOLD__": gold, "__CN_CRUDE__": cn, "__US_CRUDE_EXP__": us}
    kept = []
    for name, val in blocks.items():
        if val is None and prev.get(name):
            blocks[name] = prev[name]
            kept.append(name)
    if kept:
        print(f"[flux] fallback cache précédent pour: {', '.join(kept)}", file=sys.stderr)

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    js = ""
    for name, val in blocks.items():
        js += f"window.{name} = {json.dumps(val, ensure_ascii=False, separators=(',', ':'))};\n"
    js += f"window.__FLUX_PHYSIQUES_UPDATED__ = {json.dumps(updated)};\n"

    wrote = []
    for outp in OUT_PATHS:
        try:
            os.makedirs(os.path.dirname(outp), exist_ok=True)
            tmp = outp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(js)
            os.replace(tmp, outp)  # écriture atomique
            wrote.append(outp)
        except Exception as e:
            print(f"[flux] écriture échouée {outp}: {e}", file=sys.stderr)

    print(
        "[flux] OK · "
        f"or={gold['current']['net_t'] if gold else 'NA'} t ({gold['current']['date'] if gold else '—'}) · "
        f"CN imports={cn['current']['mt'] if cn else 'NA'} Mt ({cn['current']['date'] if cn else '—'}) · "
        f"US exports={us['current']['ma4'] if us else 'NA'} kb/j ({us['current']['date'] if us else '—'}) · "
        f"wrote {len(wrote)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
