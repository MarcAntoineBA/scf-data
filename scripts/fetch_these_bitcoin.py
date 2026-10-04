#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Bitcoin et actifs rares.

Sources (toutes gratuites, sans clé sauf FRED déjà en place) :
  1. Yahoo Finance · cours quotidiens BTC-USD, ^NDX, ^GSPC, GC=F, DX-Y.NYB, ^TNX
     → cours du Bitcoin, volatilité sur un an glissant, corrélations 90 j / 1 an
       (calculées ICI, date de calcul publiée — plus de chiffres « 2024 » figés)
  2. FRED · M2SL (masse monétaire US), WALCL (bilan de la Fed), DFII10
  3. mempool.space · hauteur de bloc (→ offre émise exacte, prochain halving)
                    + hash rate quotidien depuis 2009 (→ moyenne 7 j, hebdo)
  4. World Gold Council · stock d'or au-dessus du sol (page « how much gold »,
     mise à jour trimestrielle) → capitalisation de l'or au cours du jour
  5. Banque mondiale · capitalisation boursière mondiale (CM.MKT.LCAP.CD, annuel)
  6. bitbo.io/treasuries · BTC détenus par les ETF et par les États (quotidien)
  7. Millésimes affichés (pas d'API) : obligations mondiales (SIFMA, éd. 2026),
     immobilier mondial (Savills, données fin 2024), production minière d'or
     2025 (WGC), réserves d'or des banques centrales fin 2024 (WGC).

Règles : repli = DERNIÈRE valeur réellement collectée (relue dans le cache
précédent), jamais une constante périmée réinjectée en silence ; horodatage de
dernier succès par source dans meta.sources_last_ok.

Sortie : these_bitcoin_cache.json + .js
Lancé par scf.these_bitcoin.refresh (4×/jour).
"""
import json
import math
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:  # Yahoo répond 403 sans usurpation TLS sur certains réseaux (cloud)
    from curl_cffi import requests as _cffi
except Exception:  # pragma: no cover — absent sur le Mac : urllib suffit
    _cffi = None

_OUT_OVERRIDE = os.environ.get("THESE_OUT_DIR")  # tests : écrire ailleurs
CACHE_DIR = Path(_OUT_OVERRIDE) if _OUT_OVERRIDE else (
    Path.home() / "Library" / "Caches" / "site_crypto_finance")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_bitcoin_cache.json"
OUT_JS   = CACHE_DIR / "these_bitcoin_cache.js"
PREV_JSON = Path(os.environ.get("THESE_PREV_JSON", "")) if os.environ.get("THESE_PREV_JSON") else (
    Path.home() / "Library" / "Caches" / "site_crypto_finance" / "these_bitcoin_cache.json")

UA = "Mozilla/5.0 SiteCryptoFinance-TheseBitcoin/1.1"
NOW = datetime.now(timezone.utc)

# ════════════════════════════════════════════════════════════════
# DONNÉES DE RÉFÉRENCE (historiques ou millésimées, sources notées)
# ════════════════════════════════════════════════════════════════

# Halvings : hauteur de bloc et date UTC du bloc (historique on-chain).
# Le 5e est ESTIMÉ à chaque passage depuis la hauteur courante (10 min/bloc).
BTC_HALVINGS = [
    # (date, block, reward_btc, price_usd_close, comment)
    ("2009-01-03",       0, 50.0,       0.0, "Bloc genèse · Satoshi Nakamoto"),
    ("2012-11-28",  210000, 25.0,      12.4, "1er halving · 25 BTC par bloc"),
    ("2016-07-09",  420000, 12.5,     650.6, "2e halving · 12,5 BTC par bloc"),
    ("2020-05-11",  630000, 6.25,    8601.8, "3e halving · 6,25 BTC par bloc"),
    ("2024-04-20",  840000, 3.125,  64994.4, "4e halving · 3,125 BTC par bloc"),
    ("2028-04-01", 1050000, 1.5625,     0.0, "5e halving · date estimée"),
]

# Réserves d'or des banques centrales, tonnes, FIN 2024 (WGC, statistiques
# mensuelles des banques centrales). Millésime affiché sur la page.
CB_GOLD_RESERVES_T = [
    ("États-Unis",   8133.5), ("Allemagne",    3351.5), ("Italie",  2451.8),
    ("France",       2437.0), ("Russie",       2335.9), ("Chine",   2279.6),
    ("Suisse",       1040.0), ("Inde",          876.2), ("Japon",    846.0),
    ("Pays-Bas",      612.5),
]
CB_GOLD_ASOF = "fin 2024"
CB_GOLD_URL = "https://www.gold.org/goldhub/data/gold-reserves-by-country"

# Or — stock au-dessus du sol : repli si la page WGC change de forme.
GOLD_STOCK_FALLBACK = {"tonnes": 222600, "periode": "fin T2 2026", "cb_tonnes": 39000,
                       "source_url": "https://www.gold.org/goldhub/data/how-much-gold",
                       "verifie_le": "2026-10-04"}
# Production minière d'or (WGC, Gold Demand Trends année 2025, publié janv. 2026)
GOLD_MINE_PROD = {"tonnes": 3672, "annee": 2025,
                  "source_url": "https://www.gold.org/download/file/20432/GDT-Full-Year-2025-Exec-Summary.pdf"}
# Achats nets des banques centrales (WGC, Gold Demand Trends), tonnes
CB_NET_PURCHASES = [(2022, 1082), (2023, 1037), (2024, 1045), (2025, 863)]

TROY_OZ_PER_TONNE = 32150.7466

# Agrégats sans API : millésime le plus récent, vérifié le 2026-10-04.
BONDS_GLOBAL = {"value_tn": 160.7, "annee": 2025, "edition": "SIFMA Capital Markets Fact Book 2026",
                "source_url": "https://www.sifma.org/news/blog/2026-capital-markets-fact-book-key-findings"}
REAL_ESTATE_GLOBAL = {"value_tn": 393.3, "annee": 2024, "edition": "Savills World Research 2025",
                      "source_url": "https://www.savills.com/impacts/market-trends/total-value-of-global-real-estate.html"}
EQUITY_FALLBACK = {"value_tn": 141.3, "annee": 2025}   # Banque mondiale, si l'API tombe

# Jalons de l'adoption institutionnelle (dates vérifiées le 2026-10-04)
INSTITUTIONAL_MILESTONES = [
    ("2020-08-11", "MicroStrategy (Strategy) achète pour 250 M$ de BTC : première trésorerie d'entreprise cotée en bitcoin"),
    ("2020-10-08", "Square (Block) place 50 M$ de sa trésorerie en BTC"),
    ("2021-02-08", "Tesla annonce 1,5 Md$ de BTC à son bilan"),
    ("2021-09-07", "Le Salvador fait du BTC une monnaie légale (acceptation rendue facultative en janvier 2025, accord avec le FMI)"),
    ("2024-01-10", "La SEC autorise 11 ETF Bitcoin au comptant (cotation dès le 11 janvier)"),
    ("2024-12-05", "Le BTC franchit 100 000 $"),
    ("2025-01-23", "Décret présidentiel américain sur les actifs numériques : étude d'un stock national"),
    ("2025-03-06", "Décret créant la Strategic Bitcoin Reserve américaine, alimentée par les bitcoins saisis"),
    ("2025-07-07", "L'ETF IBIT de BlackRock dépasse 700 000 BTC détenus, 18 mois après son lancement"),
    ("2025-11-13", "La Banque nationale tchèque achète du BTC dans un portefeuille-test d'1 M$, hors réserves officielles"),
]

# §2 — Matrice des propriétés de la monnaie saine (grille interprétative,
# cadre Lyn Alden « What is Money? » + Saifedean Ammous « The Bitcoin Standard »)
MONETARY_PROPERTIES_COLS = [
    "Rareté absolue", "Durabilité", "Portabilité", "Divisibilité",
    "Vérifiabilité", "Résistance à la saisie", "Sans contrepartie", "Antériorité",
]
MONETARY_PROPERTIES = [
    ("Bitcoin",        [3, 3, 3, 3, 3, 3, 3, 1]),
    ("Or",             [2, 3, 1, 1, 2, 1, 3, 3]),
    ("Immobilier",     [2, 2, 0, 0, 2, 0, 1, 3]),
    ("Actions",        [1, 1, 2, 3, 2, 1, 0, 2]),
    ("Fiat (USD)",     [0, 2, 3, 3, 2, 0, 0, 1]),
]

# §5 — Chocs traversés (historique)
NETWORK_RESILIENCE = [
    ("2014", "Faillite Mt.Gox · 850 000 BTC perdus, −58 % sur l'année",
             "Aucun bloc manqué · hash rate ×30 dans les 24 mois"),
    ("2017", "Interdiction des ICO en Chine + scission Bitcoin Cash",
             "BTC garde la chaîne dominante · hash rate au plus haut"),
    ("2020", "Krach Covid : jusqu'à −48 % en moins de deux jours (« jeudi noir », 12-13 mars)",
             "Réseau ininterrompu · nouveaux sommets 12 mois après"),
    ("2021", "Interdiction TOTALE du minage en Chine (≈ 50 % du hash rate)",
             "Hash rate divisé par deux puis record retrouvé en quelques mois · migration vers les États-Unis"),
    ("2022", "Effondrements en chaîne : Luna, Celsius, FTX",
             "Protocole intact · la contagion a frappé les intermédiaires, pas la chaîne"),
    ("2024", "Bitcoin déclaré « mort » pour la ~480ᵉ fois depuis 2010",
             "Disponibilité du réseau ≈ 99,98 % depuis 2009"),
]

# ════════════════════════════════════════════════════════════════
# OUTILS HTTP
# ════════════════════════════════════════════════════════════════

def http_get_text(url, timeout=25, max_retries=4, accept="application/json,*/*", ua=UA):
    req = Request(url, headers={"User-Agent": ua, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                time.sleep(20 * (2 ** attempt)); continue
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(5 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(5 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


def yahoo_get_json(url, timeout=25):
    """Yahoo : urllib d'abord (marche depuis le Mac), curl_cffi chrome en repli."""
    try:
        return json.loads(http_get_text(url, timeout=timeout, max_retries=2))
    except Exception as e:
        if _cffi is None:
            raise
        sys.stderr.write(f"[YF urllib] {e} → curl_cffi\n")
        r = _cffi.get(url, impersonate="chrome", timeout=timeout)
        r.raise_for_status()
        return r.json()


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


# ════════════════════════════════════════════════════════════════
# COLLECTE
# ════════════════════════════════════════════════════════════════

def fetch_yahoo_daily(ticker, period1=1410998400):
    """Clôtures quotidiennes [(date ISO locale de la place, close)], None si échec."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}"
           f"?period1={period1}&period2={int(time.time()) + 86400}&interval=1d")
    try:
        data = yahoo_get_json(url)
        r = data["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
        off = int(r.get("meta", {}).get("gmtoffset") or 0)
    except Exception as e:
        sys.stderr.write(f"[YF {ticker}] {e}\n"); return None
    out = {}
    for t, v in zip(ts, close):
        if v is None or not math.isfinite(v) or v <= 0:
            continue
        d = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y-%m-%d")
        out[d] = float(v)
    if len(out) < 100:
        return None
    return sorted(out.items())


def fetch_yahoo_gold_monthly():
    """Or (GC=F, front-month, équivalent spot) mensuel — la série FRED LBMA est
    discontinuée."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=max&interval=1mo"
    try:
        data = yahoo_get_json(url)
        r = data["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
    except Exception as e:
        sys.stderr.write(f"[YF GC=F] {e}\n"); return None
    dates, values = [], []
    for t, v in zip(ts, close):
        if v is None: continue
        dates.append(datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"))
        values.append(round(v, 2))
    if not dates: return None
    return {"dates": dates, "values": values, "fred_id": "GC=F",
            "source_url": "https://finance.yahoo.com/quote/GC=F"}


def weekly_sample(series):
    """[(d, v)] → un point sur 7 en partant du DERNIER (le dernier point est gardé)."""
    n = len(series)
    keep = [series[i] for i in range(n - 1, -1, -7)][::-1]
    if keep and keep[0] != series[0]:
        keep = [series[0]] + keep
    return keep


def _d(s):
    return date.fromisoformat(s)


def rolling_vol(series, days, ann):
    """Volatilité annualisée (%) sur les `days` jours calendaires précédant chaque
    point d'échantillonnage hebdomadaire. series = [(d, close)] trié."""
    rets = []  # (date, log-return)
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        rets.append((_d(d1), math.log(v1 / v0)))
    if len(rets) < 30:
        return []
    out = []
    dates = [r[0] for r in rets]
    vals = [r[1] for r in rets]
    # sommes cumulées pour une fenêtre glissante O(n)
    c1, c2 = [0.0], [0.0]
    for x in vals:
        c1.append(c1[-1] + x); c2.append(c2[-1] + x * x)
    first = dates[0] + timedelta(days=days)
    j = 0
    sample_idx = list(range(len(dates) - 1, -1, -7))[::-1]
    for i in sample_idx:
        if dates[i] < first:
            continue
        lo = dates[i] - timedelta(days=days)
        while dates[j] <= lo:
            j += 1
        n = i - j + 1
        if n < 20:
            continue
        s1 = c1[i + 1] - c1[j]; s2 = c2[i + 1] - c2[j]
        var = (s2 - s1 * s1 / n) / (n - 1)
        out.append((dates[i].isoformat(), round(math.sqrt(max(var, 0)) * math.sqrt(ann) * 100, 1)))
    return out


def window_vol(series, start, end, ann):
    """Volatilité annualisée (%) des rendements dont la date ∈ ]start, end]."""
    xs = []
    for (d0, v0), (d1, v1) in zip(series, series[1:]):
        if start < d1 <= end:
            xs.append(math.log(v1 / v0))
    if len(xs) < 30:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return round(math.sqrt(var) * math.sqrt(ann) * 100, 1)


def pearson(a, b):
    n = len(a)
    if n < 20: return None
    ma = sum(a) / n; mb = sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a)); sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0: return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def paired_returns(btc, other, diff=False):
    """Rendements alignés sur les jours de cotation communs (clôture BTC du même
    jour calendaire). diff=True : variation absolue (taux), sinon log-rendement."""
    bd = dict(btc)
    common = [(d, v) for d, v in other if d in bd]
    out = []
    for (d0, v0), (d1, v1) in zip(common, common[1:]):
        rb = math.log(bd[d1] / bd[d0])
        ro = (v1 - v0) if diff else math.log(v1 / v0)
        out.append((d1, rb, ro))
    return out


def corr_window(pairs, end, days):
    lo = (_d(end) - timedelta(days=days)).isoformat()
    sel = [(rb, ro) for d, rb, ro in pairs if lo < d <= end]
    if len(sel) < 20: return None, len(sel)
    c = pearson([s[0] for s in sel], [s[1] for s in sel])
    return (round(c, 2) if c is not None else None), len(sel)


def compute_market_stats(px):
    """px : dict ticker → [(d, close)]. Volatilités et corrélations datées."""
    btc = px.get("BTC-USD")
    if not btc:
        return None
    end = btc[-1][0]
    stats = {"as_of": end, "method": (
        "Volatilité : écart-type des log-rendements quotidiens sur 365 jours "
        "calendaires, annualisé (√365 pour le Bitcoin qui cote tous les jours, "
        "√252 pour les marchés). Corrélation : Pearson des rendements quotidiens "
        "sur les jours de cotation communs (taux 10 ans : variation du rendement).")}
    # Volatilité glissante 1 an, hebdo
    roll = {}
    for key, tk, ann in (("btc", "BTC-USD", 365), ("ndx", "^NDX", 252), ("gold", "GC=F", 252),
                         ("spx", "^GSPC", 252)):
        s = px.get(tk)
        if s:
            roll[key] = rolling_vol(s, 365, ann)
    stats["vol_roll"] = {k: {"dates": [d for d, _ in v], "values": [x for _, x in v]}
                         for k, v in roll.items() if v}
    # Volatilité des 12 derniers mois par actif
    one_y = (_d(end) - timedelta(days=365)).isoformat()
    vol_now = []
    for label, tk, ann in (("Bitcoin", "BTC-USD", 365), ("Ethereum", "ETH-USD", 365),
                           ("Nasdaq 100", "^NDX", 252), ("S&P 500", "^GSPC", 252),
                           ("Or", "GC=F", 252), ("Dollar (DXY)", "DX-Y.NYB", 252)):
        s = px.get(tk)
        if not s: continue
        v = window_vol(s, one_y, s[-1][0], ann)
        if v is not None:
            vol_now.append({"asset": label, "vol_pct": v, "last_date": s[-1][0]})
    stats["vol_now"] = vol_now
    # Volatilité du BTC par année civile pleine
    cal = []
    for y in range(int(btc[0][0][:4]) + 1, _d(end).year):
        v = window_vol(btc, f"{y - 1}-12-31", f"{y}-12-31", 365)
        if v is not None:
            cal.append({"year": y, "vol_pct": v})
    stats["vol_btc_calendar"] = cal
    # Corrélations 90 j et 1 an
    corr = []
    for label, tk, diff in (("Nasdaq 100", "^NDX", False), ("S&P 500", "^GSPC", False),
                            ("Or", "GC=F", False), ("Dollar (DXY)", "DX-Y.NYB", False),
                            ("Taux US 10 ans", "^TNX", True)):
        s = px.get(tk)
        if not s: continue
        pairs = paired_returns(btc, s, diff=diff)
        if not pairs: continue
        last = pairs[-1][0]
        c90, n90 = corr_window(pairs, last, 90)
        c365, n365 = corr_window(pairs, last, 365)
        if c90 is None: continue
        row = {"asset": label, "c90": c90, "c365": c365, "n90": n90, "last_date": last}
        if tk == "^NDX":
            # moyenne de la corrélation 90 j depuis 2016 (échantillon hebdo)
            hist = []
            dates = [p[0] for p in pairs]
            for k in range(len(dates) - 1, -1, -5):
                if dates[k] < "2016-01-01": break
                c, _ = corr_window(pairs, dates[k], 90)
                if c is not None: hist.append(c)
            if hist:
                row["c90_moy_depuis_2016"] = round(sum(hist) / len(hist), 2)
                row["c90_min_depuis_2016"] = round(min(hist), 2)
                row["c90_max_depuis_2016"] = round(max(hist), 2)
        corr.append(row)
    stats["corr"] = corr
    return stats


def supply_at_height(h):
    """Offre émise exacte (BTC) après le bloc h (formule d'émission)."""
    total, reward, start = 0.0, 50.0, 0
    while start <= h:
        end = min(h, start + 209999)
        total += (end - start + 1) * reward
        start += 210000; reward /= 2
    return total


def fetch_chain_state():
    h = None
    for url in ("https://mempool.space/api/blocks/tip/height",
                "https://blockchain.info/q/getblockcount"):
        try:
            h = int(http_get_text(url, timeout=20, max_retries=2).strip()); break
        except Exception as e:
            sys.stderr.write(f"[height {url}] {e}\n")
    if not h or h < 800000:
        return None
    supply = supply_at_height(h)
    next_h = (h // 210000 + 1) * 210000
    blocks_left = next_h - h
    est = NOW + timedelta(seconds=blocks_left * 600)
    reward = 50.0 / (2 ** (h // 210000))
    return {"height": h, "supply_btc": round(supply, 2), "date": NOW.date().isoformat(),
            "reward_now": reward, "blocks_per_year": 52560,
            "issuance_per_year": round(reward * 52560),
            "next_halving_height": next_h, "next_halving_est": est.date().isoformat(),
            "next_halving_reward": reward / 2,
            "method": "offre = somme des récompenses jusqu'à la hauteur courante ; "
                      "date du prochain halving estimée au rythme cible de 10 min par bloc",
            "source_url": "https://mempool.space/"}


def fetch_hashrate():
    try:
        d = json.loads(http_get_text("https://mempool.space/api/v1/mining/hashrate/all", timeout=40))
        rows = d["hashrates"]
    except Exception as e:
        sys.stderr.write(f"[mempool hashrate] {e}\n"); return None
    daily = []
    for r in rows:
        v = r.get("avgHashrate")
        if v is None or v <= 0: continue
        daily.append((datetime.fromtimestamp(int(r["timestamp"]), timezone.utc).date().isoformat(), v / 1e18))
    if len(daily) < 1000: return None
    # moyenne mobile 7 jours (le relevé quotidien est bruité)
    ma = []
    for i in range(6, len(daily)):
        ma.append((daily[i][0], sum(x for _, x in daily[i - 6:i + 1]) / 7))
    ma = [p for p in ma if p[0] >= "2014-01-01"]
    wk = weekly_sample(ma)
    out = {"dates": [d for d, _ in wk], "ehs": [round(v, 3 if v < 1 else 1) for _, v in wk],
           "unit": "EH/s (moyenne 7 jours)", "source_url": "https://mempool.space/graphs/mining/hashrate-difficulty"}
    # Interdiction chinoise 2021 : sommet avant, creux, date du retour au sommet
    pre = [p for p in ma if "2021-01-01" <= p[0] <= "2021-06-15"]
    post = [p for p in ma if "2021-05-15" <= p[0] <= "2021-09-30"]
    if pre and post:
        pk = max(pre, key=lambda p: p[1]); tr = min(post, key=lambda p: p[1])
        rec = next((p for p in ma if p[0] > tr[0] and p[1] >= pk[1]), None)
        out["chine_2021"] = {"sommet": round(pk[1], 1), "sommet_date": pk[0],
                             "creux": round(tr[1], 1), "creux_date": tr[0],
                             "retour_date": rec[0] if rec else None}
    return out


def fetch_gold_stock():
    url = GOLD_STOCK_FALLBACK["source_url"]
    try:
        t = http_get_text(url, timeout=30, accept="text/html,*/*",
                          ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
    except Exception as e:
        sys.stderr.write(f"[WGC stock] {e}\n"); return None
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))
    m = re.search(r"Total above-ground stock \(end-(Q\d) (\d{4})\):\s*([\d,]+)\s*tonnes", txt)
    if not m:
        m2 = re.search(r"Total above-ground stock \(end-(\d{4})\):\s*([\d,]+)\s*tonnes", txt)
        if not m2: return None
        periode, tonnes = f"fin {m2.group(1)}", int(m2.group(2).replace(",", ""))
    else:
        periode, tonnes = f"fin {m.group(1).replace('Q', 'T')} {m.group(2)}", int(m.group(3).replace(",", ""))
    if not (150000 < tonnes < 300000): return None
    cb = re.search(r"Central banks ~?([\d,]+)\s*t", txt)
    cb_t = int(cb.group(1).replace(",", "")) if cb else None
    return {"tonnes": tonnes, "periode": periode, "cb_tonnes": cb_t, "source_url": url,
            "verifie_le": NOW.date().isoformat()}


def fetch_world_equity():
    url = ("https://api.worldbank.org/v2/country/WLD/indicator/CM.MKT.LCAP.CD"
           "?format=json&per_page=10&mrv=10")
    try:
        d = json.loads(http_get_text(url, timeout=30))
        rows = [r for r in d[1] if r.get("value")]
        r = max(rows, key=lambda r: r["date"])
        return {"value_tn": round(r["value"] / 1e12, 1), "annee": int(r["date"]),
                "source_url": "https://data.worldbank.org/indicator/CM.MKT.LCAP.CD"}
    except Exception as e:
        sys.stderr.write(f"[WB equity] {e}\n"); return None


def _num(s):
    s = (s or "").strip().replace(",", "")
    try: return float(s)
    except ValueError: return None


def fetch_bitbo():
    """BTC détenus par les ETF et par les États — bitbo.io/treasuries (dépôts des émetteurs)."""
    url = "https://bitbo.io/treasuries/"
    try:
        t = http_get_text(url, timeout=40, accept="text/html,*/*")
    except Exception as e:
        sys.stderr.write(f"[bitbo] {e}\n"); return None, None
    m = re.search(r'td-last-updated"[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})', t)
    as_of = None
    if m:
        try: as_of = datetime.strptime(m.group(1), "%B %d, %Y").date().isoformat()
        except ValueError: pass

    def table(anchor):
        i = t.find(f'id="{anchor}"')
        if i < 0: return []
        j = t.find("</table>", i)
        rows = []
        for r in re.findall(r"<tr>(.*?)</tr>", t[i:j], re.S):
            name = re.search(r'td-company"[^>]*>\s*(?:<a[^>]*>)?\s*([^<]+?)\s*<', r, re.S)
            flag = re.search(r'data-tooltip="([^"]+)"', r)
            sym = re.search(r'td-symbol"[^>]*>([^<]*)<', r)
            btc = re.search(r'td-company_btc"[^>]*>([^<]*)<', r)
            if not (name and btc): continue
            v = _num(btc.group(1))
            if v is None: continue
            rows.append({"name": re.sub(r"\s+", " ", name.group(1)).strip(),
                         "country": flag.group(1) if flag else "",
                         "symbol": (sym.group(1).strip() if sym else ""), "btc": v})
        return rows

    etf = table("etfs"); gov = table("countries")
    if len(etf) < 5:
        return None, None
    us = [r for r in etf if r["country"] == "US" and "OTC" not in r["symbol"]]
    etf_out = {"as_of": as_of, "source_url": url,
               "rows": sorted(etf, key=lambda r: -r["btc"])[:12],
               "total_btc": round(sum(r["btc"] for r in etf)),
               "us_spot_btc": round(sum(r["btc"] for r in us)),
               "n_fonds": len(etf)}
    gov_out = {"as_of": as_of, "source_url": url, "rows": gov} if gov else None
    return etf_out, gov_out


def build_payload(prev):
    ok, failed = [], []
    last_ok = dict((prev.get("meta") or {}).get("sources_last_ok") or {})
    now_iso = NOW.isoformat(timespec="seconds")

    def mark(name, success):
        (ok if success else failed).append(name)
        if success: last_ok[name] = now_iso

    def keep_prev(key):
        v = prev.get(key)
        if isinstance(v, dict):
            v = dict(v); v["stale"] = True
        return v

    # ── Cours quotidiens Yahoo ─────────────────────────────────
    px = {}
    for tk in ("BTC-USD", "ETH-USD", "^NDX", "^GSPC", "GC=F", "DX-Y.NYB", "^TNX"):
        s = fetch_yahoo_daily(tk)
        mark(f"YF:{tk}", bool(s))
        if s: px[tk] = s
        time.sleep(0.6)

    if px.get("BTC-USD"):
        wk = weekly_sample(px["BTC-USD"])
        btc_px = {"dates": [d for d, _ in wk], "values": [round(v, 2) for _, v in wk],
                  "source_url": "https://finance.yahoo.com/quote/BTC-USD/", "unit": "USD"}
    else:
        btc_px = keep_prev("btc_price")

    stats = compute_market_stats(px) if px.get("BTC-USD") else None
    if not stats:
        stats = keep_prev("market_stats")

    gold_px = fetch_yahoo_gold_monthly()
    mark("YF:GC=F_mensuel", bool(gold_px))
    if not gold_px: gold_px = keep_prev("gold_price")

    m2 = fetch_fred_csv("M2SL", start="1980-01-01")
    mark("FRED:M2SL", bool(m2))
    if not m2: m2 = keep_prev("m2_us")

    walcl = fetch_fred_csv("WALCL", start="2003-01-01")
    mark("FRED:WALCL", bool(walcl))
    if walcl:
        # une valeur par mois (dernière du mois) : suffisant et léger
        bym = {}
        for d, v in zip(walcl["dates"], walcl["values"]):
            bym[d[:7]] = (d, v)
        ds = sorted(bym.values())
        fed = {"dates": [d for d, _ in ds], "values": [round(v / 1000) for _, v in ds],
               "unit": "Md$", "last_date": walcl["dates"][-1],
               "last_value": round(walcl["values"][-1] / 1000),
               "source_url": "https://fred.stlouisfed.org/series/WALCL"}
    else:
        fed = keep_prev("fed_balance")

    real_rate = fetch_fred_csv("DFII10", start="2003-01-01")
    mark("FRED:DFII10", bool(real_rate))
    if not real_rate: real_rate = keep_prev("real_rate_10y")

    chain = fetch_chain_state()
    mark("mempool:height", bool(chain))
    if not chain: chain = keep_prev("chain")

    hr = fetch_hashrate()
    mark("mempool:hashrate", bool(hr))
    if not hr: hr = keep_prev("hashrate")

    gstock = fetch_gold_stock()
    mark("WGC:stock", bool(gstock))
    if not gstock:
        gstock = prev.get("gold_stock") or dict(GOLD_STOCK_FALLBACK)

    eq = fetch_world_equity()
    mark("WorldBank:CM.MKT.LCAP.CD", bool(eq))
    if not eq:
        eq = (prev.get("world_equity") or dict(EQUITY_FALLBACK,
              source_url="https://data.worldbank.org/indicator/CM.MKT.LCAP.CD"))

    etf, gov = fetch_bitbo()
    mark("bitbo:treasuries", bool(etf))
    if not etf: etf = keep_prev("etf_holdings")
    if not gov: gov = keep_prev("gov_holdings")

    # ── Agrégats dérivés ────────────────────────────────────────
    supply_btc = (chain or {}).get("supply_btc")
    btc_last = btc_px["values"][-1] if btc_px and btc_px.get("values") else None
    gold_last = gold_px["values"][-1] if gold_px and gold_px.get("values") else None
    gold_last_date = gold_px["dates"][-1] if gold_px and gold_px.get("dates") else None
    caps = []
    if btc_last and supply_btc:
        caps.append({"key": "btc", "label": "Bitcoin", "value_tn": round(btc_last * supply_btc / 1e12, 3),
                     "as_of": btc_px["dates"][-1], "live": True,
                     "note": f"cours du jour × {supply_btc / 1e6:.2f} M BTC émis".replace(".", ",")})
    if gold_last and gstock:
        caps.append({"key": "gold", "label": "Or (stock mondial)",
                     "value_tn": round(gstock["tonnes"] * TROY_OZ_PER_TONNE * gold_last / 1e12, 2),
                     "as_of": gold_last_date, "live": True,
                     "note": f"{gstock['tonnes']:,}".replace(",", " ") + f" t ({gstock['periode']}, WGC) × cours du jour"})
    if m2 and m2.get("values"):
        caps.append({"key": "m2", "label": "M2 — États-Unis", "value_tn": round(m2["values"][-1] / 1000, 2),
                     "as_of": m2["dates"][-1], "live": True, "note": "masse monétaire large · FRED M2SL"})
    caps.append({"key": "equity", "label": "Actions — monde", "value_tn": eq["value_tn"],
                 "as_of": str(eq["annee"]), "live": True,
                 "note": f"sociétés cotées, fin {eq['annee']} · Banque mondiale"})
    caps.append({"key": "bonds", "label": "Obligations — monde", "value_tn": BONDS_GLOBAL["value_tn"],
                 "as_of": str(BONDS_GLOBAL["annee"]), "live": False,
                 "note": f"encours fin {BONDS_GLOBAL['annee']} · {BONDS_GLOBAL['edition']}"})
    caps.append({"key": "realestate", "label": "Immobilier — monde", "value_tn": REAL_ESTATE_GLOBAL["value_tn"],
                 "as_of": str(REAL_ESTATE_GLOBAL["annee"]), "live": False,
                 "note": f"résidentiel + commercial + terres, fin {REAL_ESTATE_GLOBAL['annee']} · {REAL_ESTATE_GLOBAL['edition']}"})

    # Ratio stock/flux (S2F) : or (stock WGC / production 2025) et Bitcoin
    s2f = []
    if gstock:
        s2f.append({"asset": "Or", "stock": f"{gstock['tonnes']:,} t".replace(",", " "),
                    "flow": f"{GOLD_MINE_PROD['tonnes']:,} t/an ({GOLD_MINE_PROD['annee']})".replace(",", " "),
                    "s2f": round(gstock["tonnes"] / GOLD_MINE_PROD["tonnes"])})
    if chain:
        def s2f_btc(h, label):
            sup = supply_at_height(h); rew = 50.0 / (2 ** (h // 210000))
            flow = rew * 52560
            return {"asset": label, "stock": f"{sup / 1e6:.2f} M BTC".replace(".", ","),
                    "flow": f"{flow / 1e6:.3f} M BTC/an".replace(".", ","), "s2f": round(sup / flow)}
        s2f.append(s2f_btc(chain["height"], "Bitcoin · aujourd'hui"))
        s2f.append(s2f_btc(1050000, "Bitcoin · après 2028"))
        s2f.append(s2f_btc(1260000, "Bitcoin · après 2032"))

    halvings = [{"date": d, "block": b, "reward": r, "price_usd": p, "comment": c}
                for d, b, r, p, c in BTC_HALVINGS]
    if px.get("BTC-USD"):  # clôture du jour du halving quand Yahoo la couvre
        bd = dict(px["BTC-USD"])
        for hv in halvings:
            if hv["date"] in bd: hv["price_usd"] = round(bd[hv["date"]], 1)
    if chain:
        halvings[-1]["date"] = chain["next_halving_est"]
        halvings[-1]["comment"] = "5e halving · date estimée au rythme de 10 min par bloc"

    # Champs de compatibilité (ancienne page) recalculés sur les vraies données
    vol_compat = [{"asset": r["asset"], "vol_pct": r["vol_pct"]} for r in (stats or {}).get("vol_now", [])]
    corr_compat = [{"asset": r["asset"], "corr": r["c90"]} for r in (stats or {}).get("corr", [])]
    hr_annual = []
    if hr:
        seen = set()
        for d, v in zip(hr["dates"], hr["ehs"]):
            if d[:4] not in seen:
                seen.add(d[:4]); hr_annual.append({"date": d, "ehs": v})
        hr_annual.append({"date": hr["dates"][-1], "ehs": hr["ehs"][-1]})
    etf_compat = []
    if etf and btc_last:
        for r in etf["rows"][:10]:
            etf_compat.append({"issuer": r["name"], "ticker": r["symbol"].split(":")[0],
                               "aum_bn": round(r["btc"] * btc_last / 1e9, 1), "jurisdiction": r["country"]})

    meta = {
        "updated_at":      now_iso,
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "sources_last_ok": last_ok,
        "doc_version":     "2.0",
    }
    payload = {
        "meta": meta,
        "btc_price":   btc_px,
        "gold_price":  gold_px,
        "m2_us":       m2,
        "fed_balance": fed,
        "real_rate_10y": real_rate,
        "chain": chain,
        "hashrate": hr,
        "market_stats": stats,
        "gold_stock": gstock,
        "gold_mine_prod": GOLD_MINE_PROD,
        "cb_net_purchases": [{"year": y, "tonnes": t} for y, t in CB_NET_PURCHASES],
        "world_equity": eq,
        "market_caps_v2": caps,
        "etf_holdings": etf,
        "gov_holdings": gov,
        "btc_halvings": halvings,
        "cb_gold_reserves": [{"country": c, "tonnes": t} for c, t in CB_GOLD_RESERVES_T],
        "cb_gold_asof": CB_GOLD_ASOF,
        "cb_gold_url": CB_GOLD_URL,
        "stocks_to_flow": s2f,
        "institutional_milestones": [{"date": d, "event": e} for d, e in INSTITUTIONAL_MILESTONES],
        "monetary_properties": {
            "properties": MONETARY_PROPERTIES_COLS,
            "assets": [{"asset": a, "scores": s} for a, s in MONETARY_PROPERTIES],
            "source_url": "https://www.lynalden.com/what-is-money/",
        },
        "network_resilience": [{"year": y, "shock": s, "outcome": o}
                               for y, s, o in NETWORK_RESILIENCE],
        # ── compatibilité avec la version précédente de la page ──
        "btc_supply_m": round(supply_btc / 1e6, 3) if supply_btc else prev.get("btc_supply_m"),
        "market_caps": [{"label": c["label"], "value_tn": (None if c["key"] == "btc" else c["value_tn"]),
                         "note": c["note"], "live_from_btc": c["key"] == "btc"} for c in caps],
        "volatility_compare": vol_compat or prev.get("volatility_compare"),
        "btc_correlations": corr_compat or prev.get("btc_correlations"),
        "hashrate_annual": hr_annual or prev.get("hashrate_annual"),
        "btc_etf_aum": etf_compat or prev.get("btc_etf_aum"),
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_bitcoin_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_BITCOIN__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    if _OUT_OVERRIDE:
        return
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_bitcoin_cache.json", "these_bitcoin_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        prev = json.loads(PREV_JSON.read_text()) if PREV_JSON.exists() else {}
    except Exception:
        prev = {}
    try:
        payload, n_ok, n_fail = build_payload(prev)
    except Exception as e:
        import traceback; traceback.print_exc()
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    if not payload.get("btc_price"):
        sys.stderr.write("[FATAL] aucun cours BTC (ni frais ni précédent) — cache conservé\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_bitcoin] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
