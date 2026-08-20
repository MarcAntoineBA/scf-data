#!/usr/bin/env python3
"""fetch_btc_cycle.py — Données de l'onglet "Cycle Tracker".

Agrège en un seul cache les 6 métriques de cycle BTC + rainbow + returns +
contexte halving. Suit le pattern du site : cache dans ~/Library/Caches/
(non TCC), symlink depuis ~/Desktop/Site_Crypto_Finance/, atomic write,
merge-preserve (ne JAMAIS écraser un bon cache par du vide), sanity checks.

Sources (chaque chiffre traçable — cf payload['table'][*]['source_url']) :
  • Prix BTC full history (2010+) ......... Coinmetrics community API (PriceUSD)
  • Issuance USD full history ............. Coinmetrics community API (IssTotUSD) → Puell
  • Market cap full history ............... Coinmetrics community API (CapMrktCurUSD)
  • MVRV Z-Score / NUPL / Reserve Risk .... bitcoin-data.com (BGeometrics, gratuit) — 4 ans

Métriques DÉRIVÉES localement (formule visible = auditable) :
  • Pi Cycle Top   = 111 DMA  vs  2 × 350 DMA   (signal de sommet quand 111>2×350)
  • Mayer Multiple = Prix / 200 DMA
  • Puell Multiple = Issuance USD du jour / MA365(Issuance USD)
  • Rainbow        = régression log-puissance  log10(P) = a·ln(jours depuis genesis) + b

Usage : python3 fetch_btc_cycle.py [--force]
"""
import json
import math
import shutil
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

import numpy as np
import requests

# ─────────────────────────── Config ────────────────────────────
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "btc_cycle_cache.json"
CACHE_JS = CACHE_DIR / "btc_cycle_cache.js"
# Companion léger consommé par l'Accueil (tuile « Position de cycle ») : le cache
# complet fait ~170 Ko, inacceptable sur la home. Cf build_light().
LIGHT_JS = CACHE_DIR / "btc_cycle_light.js"
SITE_DIR = Path.home() / "Desktop" / "Site_Crypto_Finance"
CACHE_MAX_HOURS = 6

CM = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
BD = "https://bitcoin-data.com/v1"
GENESIS = date(2009, 1, 3)
GENESIS_UNIX = int(datetime(2009, 1, 3, tzinfo=timezone.utc).timestamp())

# Halving : 210 000 blocs / halving. Dernier = bloc 840 000 (2024-04-20).
BLOCKS_PER_HALVING = 210_000
LAST_HALVING_BLOCK = 840_000
LAST_HALVING_DATE = "2024-04-20"
NEXT_HALVING_BLOCK = 1_050_000

# Palette site (cohérence DA navy/cyan)
COL = {"red": "#ff5570", "orange": "#f5a623", "green": "#28e07e", "bottom": "#1ec5ff"}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) CapitalAntifragile/1.0"}


def log(m):
    sys.stderr.write(f"[cycle] {m}\n")


def wait_for_network(host="community-api.coinmetrics.io", max_wait=120):
    """Anti-course « réveil du Mac » : au sortir de veille, launchd relance le
    fetcher AVANT que le Wi-Fi/DNS soit reconnecté → NameResolutionError immédiate
    → cache figé silencieusement (incident du 2026-07-19, 43h de stale). On attend
    ici que le DNS résolve (jusqu'à max_wait s) au lieu d'échouer sur-le-champ.
    Retourne True si le réseau est prêt, False si toujours down après max_wait
    (vraie panne → on laisse build() lever et merge-preserve garde l'ancien cache)."""
    import socket
    waited, delay = 0, 3
    while True:
        try:
            socket.getaddrinfo(host, 443)
            if waited:
                log(f"réseau prêt après {waited}s d'attente (réveil/reconnexion)")
            return True
        except socket.gaierror:
            if waited >= max_wait:
                log(f"WARN réseau/DNS indisponible après {waited}s — on tente quand même")
                return False
            time.sleep(delay)
            waited += delay
            delay = min(delay * 1.5, 15)


def _session():
    s = requests.Session()
    s.headers.update(UA)
    # Retries connexion/DNS (belt-and-suspenders par-dessus wait_for_network) :
    # un hoquet réseau transitoire ne doit pas tuer le run et figer le cache.
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=4, connect=4, read=2, backoff_factor=2,
                      status_forcelist=(429, 502, 503, 504),
                      allowed_methods=frozenset(["GET"]))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass  # urllib3 absent/ancien → session simple, wait_for_network reste la garde
    return s


# ─────────────────────── Fetch helpers ─────────────────────────
def cm_metric(sess, metric, start="2010-07-18"):
    """Coinmetrics community : série quotidienne complète d'une métrique.
    Retourne (dates[str], values[float]) triées asc. Lève en cas d'échec."""
    out_d, out_v = [], []
    url = CM
    params = {
        "assets": "btc", "metrics": metric, "frequency": "1d",
        "start_time": start, "page_size": 10000,
    }
    for _ in range(60):  # pagination guard
        r = sess.get(url, params=params, timeout=40)
        r.raise_for_status()
        j = r.json()
        for row in j.get("data", []):
            v = row.get(metric)
            if v is None:
                continue
            out_d.append(row["time"][:10])
            out_v.append(float(v))
        nxt = j.get("next_page_url")
        if not nxt:
            break
        url, params = nxt, None
    if len(out_v) < 500:
        raise RuntimeError(f"Coinmetrics {metric}: trop peu de points ({len(out_v)})")
    # dédup/tri par date
    pair = sorted(dict(zip(out_d, out_v)).items())
    return [p[0] for p in pair], [p[1] for p in pair]


def bd_metric(sess, path, field, retries=3):
    """bitcoin-data.com : série (≈4 ans). Tier gratuit = ~8 req/h.
    Sur 429 (quota épuisé) on ABANDONNE tout de suite — réessayer dans la même
    heure ne ferait que gaspiller le quota ; merge-preserve garde l'ancienne
    valeur et le prochain cycle launchd la rafraîchit. Retry seulement sur
    erreur réseau/5xx transitoire."""
    delay = 3.0
    for attempt in range(retries):
        try:
            r = sess.get(f"{BD}/{path}", timeout=40)
            if r.status_code == 429:
                log(f"WARN bitcoin-data {path}: 429 quota — abandon (merge-preserve + prochain cycle)")
                return None, None
            r.raise_for_status()
            j = r.json()
            d = [row["d"] for row in j if row.get(field) is not None]
            v = [float(row[field]) for row in j if row.get(field) is not None]
            if len(v) < 30:
                raise RuntimeError(f"{path}: {len(v)} pts")
            return d, v
        except Exception as e:
            if attempt < retries - 1:
                log(f"WARN bitcoin-data {path}: {e} — retry réseau {attempt+1}/{retries} dans {delay:.0f}s")
                time.sleep(delay)
                delay *= 1.8
            else:
                log(f"WARN bitcoin-data {path}: {e} — abandon (merge-preserve prendra le relais)")
    return None, None


def block_tip(sess):
    """Hauteur de bloc actuelle (pour ETA halving server-side fallback)."""
    for url, parse in (
        ("https://mempool.space/api/blocks/tip/height", lambda r: int(r.text)),
        ("https://blockchain.info/q/getblockcount", lambda r: int(r.text)),
    ):
        try:
            r = sess.get(url, timeout=15)
            r.raise_for_status()
            return parse(r)
        except Exception as e:
            log(f"WARN tip {url}: {e}")
    return None


# ─────────────────────── Math helpers ──────────────────────────
def sma(v, n):
    """Moyenne mobile simple, alignée (NaN avant n points)."""
    a = np.asarray(v, dtype=float)
    out = np.full(a.shape, np.nan)
    if len(a) >= n:
        c = np.cumsum(np.insert(a, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def decim(dates, *series, step):
    """Décime des séries alignées (1 point / step). Retourne lignes [unixTs, *vals].

    Le DERNIER point réel est toujours inclus, même s'il ne tombe pas sur un multiple
    de `step`. Sans ça la décimation part de l'origine de l'histoire et les jours de la
    fin sont purement SUPPRIMÉS : le 2026-08-20 les graphes hebdo s'arrêtaient au 16 août
    à 62 818 $ alors que la donnée allait au 19 août à 69 268 $ (+7 % invisibles), et le
    tableau affichait Mayer 1,004 quand la courbe finissait à 0,907. Le dernier intervalle
    est simplement plus court que les autres — c'est le prix à payer pour une fin à J-1.
    """
    idx = list(range(0, len(dates), step))
    if idx and idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    rows = []
    for i in idx:
        ts = int(datetime.strptime(dates[i], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
        vals = []
        ok = True
        for s in series:
            x = s[i]
            if x is None or (isinstance(x, float) and math.isnan(x)):
                vals.append(None)
            else:
                vals.append(round(float(x), 6))
        rows.append([ts] + vals)
    return rows


def period_returns(dates, prices):
    """Retours % par semaine ISO / mois / trimestre / année (close-to-close).
    Renvoie dicts imbriqués {année: {clé: pct}} + annual {année: pct}."""
    # close de fin de chaque période
    def last_by(keyfn):
        d = {}
        for dt, p in zip(dates, prices):
            d[keyfn(dt)] = p  # dates triées asc -> dernière écrase
        return d

    dd = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates]

    def to_nested(closes_keyed, year_of, sub_of, prev_key):
        """closes_keyed: {periodkey: close}. Construit {year:{sub:pct}}."""
        keys = sorted(closes_keyed.keys())
        out = {}
        for i, k in enumerate(keys):
            if i == 0:
                continue
            prev = closes_keyed[keys[i - 1]]
            if prev and prev > 0:
                pct = (closes_keyed[k] / prev - 1.0) * 100.0
                y, sub = year_of(k), sub_of(k)
                out.setdefault(str(y), {})[str(sub)] = round(pct, 2)
        return out

    # Mensuel
    m_close = {}
    for d, p in zip(dd, prices):
        m_close[(d.year, d.month)] = p
    monthly = to_nested(m_close, lambda k: k[0], lambda k: k[1], None)

    # Trimestriel
    q_close = {}
    for d, p in zip(dd, prices):
        q_close[(d.year, (d.month - 1) // 3 + 1)] = p
    quarterly = to_nested(q_close, lambda k: k[0], lambda k: k[1], None)

    # Hebdo ISO
    w_close = {}
    for d, p in zip(dd, prices):
        iso = d.isocalendar()
        w_close[(iso[0], iso[1])] = p
    weekly = to_nested(w_close, lambda k: k[0], lambda k: k[1], None)

    # Annuel
    y_close = {}
    for d, p in zip(dd, prices):
        y_close[d.year] = p
    yk = sorted(y_close.keys())
    annual = {}
    for i, y in enumerate(yk):
        if i == 0:
            continue
        prev = y_close[yk[i - 1]]
        if prev and prev > 0:
            annual[str(y)] = round((y_close[y] / prev - 1.0) * 100.0, 2)

    return {"weekly": weekly, "monthly": monthly, "quarterly": quarterly, "annual": annual}


def _rb_log_func(x, a, b, c):
    """Fonction de fit du Rainbow Chart V2 : ln(prix) = a·ln(b + x) + c."""
    return a * np.log(b + x) + c


def rainbow_fit(dates, prices):
    """Réplique EXACTE du Bitcoin Rainbow Chart V2 (blockchaincenter / Coinglass).
    Fit : ln(prix) = a·ln(b + index) + c, index = jours depuis le 1er point (1..N).
    Bandes : largeur 0.3 en ln, la régression tombe dans la bande Accumulate
    (i=2, décalage i_decrease=1.5) → les bandes montent surtout au-dessus.
    9 bandes, couleurs & labels EXACTS (= ceux affichés par Coinglass)."""
    from scipy.optimize import curve_fit
    d0 = datetime.strptime(dates[0], "%Y-%m-%d").date()
    idx = np.array([(datetime.strptime(d, "%Y-%m-%d").date() - d0).days + 1 for d in dates], dtype=float)
    pv = np.asarray(prices, dtype=float)
    m = pv > 0
    x = idx[m]; y = np.log(pv[m])
    popt, _ = curve_fit(_rb_log_func, x, y, p0=[3.0, 1.0, -10.0], maxfev=200000)
    a, b, c = (float(p) for p in popt)
    yhat = _rb_log_func(x, a, b, c)
    ss_res = float(np.sum((y - yhat) ** 2)); ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    first_ts = int(datetime.strptime(dates[0], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    return {
        "model": "v2_ln_index",            # ln(prix)=a·ln(b+x)+c, x=(ts-first_unix)/86400+1
        "a": round(a, 8), "b": round(b, 6), "c": round(c, 8),
        "r2": round(r2, 4), "first_unix": first_ts,
        "band_width": 0.3, "i_decrease": 1.5,   # bande j ∈ [center+(j-2.5)·0.3, center+(j-1.5)·0.3] en ln
        # bas→haut (j=0 Fire sale → j=8 Maximum bubble) — couleurs Coinglass exactes
        "band_labels": ["Fire sale!", "BUY!", "Accumulate", "Still cheap", "HODL!",
                        "Is this a bubble?", "FOMO Intensifies", "Sell. Seriously, SELL!",
                        "Maximum bubble territory"],
        "band_colors": ["#4472c4", "#54989f", "#63be7b", "#b1d580", "#feeb84",
                        "#f6b45a", "#ed7d31", "#d64018", "#c00200"],
    }


def mvrv_zscore_full(mcap_dates, mcap, mvrv_dates, mvrv_ratio):
    """MVRV Z-Score historique complet, formule EXACTE de Coinglass / Awe & Wonder :

        Z = (MarketCap − RealizedCap) / σ(MarketCap)

    où σ(MarketCap) est l'écart-type CUMULATIF (depuis la genèse jusqu'à la date t).
    Le tier gratuit Coinmetrics ne donne pas RealizedCap directement, mais donne
    le ratio MVRV (CapMVRVCur = MarketCap / RealizedCap) → on dérive
    RealizedCap = MarketCap / MVRV, sur tout l'historique (2010+).

    Retourne (dates[str], z[float]). On démarre l'affichage au 2010-08-18 (comme
    Coinglass) : le 1er mois a un σ cumulatif minuscule → z aberrant (>15), on le
    coupe pour que l'échelle Y reste lisible. Le σ reste calculé depuis la genèse."""
    mv_map = dict(zip(mcap_dates, mcap))
    rt_map = dict(zip(mvrv_dates, mvrv_ratio))
    dates = sorted(set(mv_map) & set(rt_map))
    if len(dates) < 500:
        return None, None
    MV = np.array([mv_map[d] for d in dates], dtype=float)
    ratio = np.array([rt_map[d] for d in dates], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        RV = np.where(ratio > 0, MV / ratio, np.nan)
    # σ cumulatif (population) du market cap, depuis la genèse
    n = np.arange(1, len(MV) + 1)
    cs = np.cumsum(MV)
    cs2 = np.cumsum(MV * MV)
    mean = cs / n
    var = np.maximum(cs2 / n - mean * mean, 0.0)
    sd = np.sqrt(var)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = np.where(sd > 0, (MV - RV) / sd, np.nan)
    # coupe l'affichage au 2010-08-18 (cf docstring)
    out_d, out_z = [], []
    for d, val in zip(dates, z):
        if d < "2010-08-18":
            continue
        if math.isnan(val) or math.isinf(val):
            continue
        out_d.append(d)
        out_z.append(float(val))
    return out_d, out_z


def nupl_full(mvrv_dates, mvrv_ratio):
    """NUPL historique complet dérivé du ratio MVRV (Coinmetrics, gratuit, 2010+).

        NUPL = (MarketCap − RealizedCap) / MarketCap = 1 − RealizedCap/MarketCap
             = 1 − 1 / (MarketCap/RealizedCap) = 1 − 1 / ratioMVRV

    Même source que le MVRV Z-Score → cohérence garantie entre les deux graphes.
    Retourne (dates[str], nupl[float]), clippé au 2010-08-18 comme le MVRV."""
    out_d, out_v = [], []
    for d, r in sorted(zip(mvrv_dates, mvrv_ratio)):
        if d < "2010-08-18" or r is None or r <= 0:
            continue
        v = 1.0 - 1.0 / r
        if math.isnan(v) or math.isinf(v):
            continue
        out_d.append(d)
        out_v.append(float(v))
    return out_d, out_v


# ─────────────────────── Status scoring ────────────────────────
def status_pi(ratio):
    # ratio = 111DMA / (2×350DMA). →1 = sommet imminent.
    if ratio is None:
        return None
    if ratio >= 0.95:
        return "red"
    if ratio >= 0.75:
        return "orange"
    if ratio >= 0.45:
        return "green"
    return "bottom"


def status_mvrvz(v):
    if v is None:
        return None
    if v >= 7:
        return "red"
    if v >= 3:
        return "orange"
    if v >= 0:
        return "green"
    return "bottom"


def status_puell(v):
    if v is None:
        return None
    if v >= 4:
        return "red"
    if v >= 1.2:
        return "orange"
    if v >= 0.5:
        return "green"
    return "bottom"


def status_reserve(v):
    if v is None:
        return None
    if v >= 0.02:
        return "red"
    if v >= 0.008:
        return "orange"
    if v >= 0.002:
        return "green"
    return "bottom"


def status_mayer(v):
    if v is None:
        return None
    if v >= 2.4:
        return "red"
    if v >= 1.0:
        return "orange"
    if v >= 0.8:
        return "green"
    return "bottom"


def status_nupl(v):
    if v is None:
        return None
    if v >= 0.75:
        return "red"
    if v >= 0.5:
        return "orange"
    if v >= 0.25:
        return "green"
    return "bottom"


STATUS_RANK = {"bottom": 0, "green": 1, "orange": 2, "red": 3}

# Ancres de normalisation : valeur brute → position de cycle 0-100, en passant
# EXACTEMENT par les seuils du tableau (bottom/vert/orange/rouge mappés sur
# 0 / 25 / 50 / 75 / 100). Interpolation linéaire par morceaux, clampée.
COMPOSITE_ANCHORS = {
    "pi":      [0.20, 0.45, 0.75, 0.95, 1.05],
    "mvrvz":   [-1.0, 0.0, 3.0, 7.0, 10.0],
    "puell":   [0.20, 0.50, 1.20, 4.0, 6.0],
    "reserve": [0.0005, 0.002, 0.008, 0.02, 0.04],
    "mayer":   [0.50, 0.80, 1.0, 2.4, 3.0],
    "nupl":    [-0.20, 0.25, 0.50, 0.75, 0.90],
}


def _interp_score(v, anchors):
    """v → 0-100 via interpolation linéaire à travers [a0..a4] = 0/25/50/75/100."""
    ys = [0, 25, 50, 75, 100]
    if v <= anchors[0]:
        return 0.0
    if v >= anchors[-1]:
        return 100.0
    for i in range(4):
        if anchors[i] <= v <= anchors[i + 1]:
            frac = (v - anchors[i]) / (anchors[i + 1] - anchors[i])
            return ys[i] + frac * 25
    return 50.0


# Regroupement par DIMENSION pour ne pas surpondérer des indicateurs très corrélés.
# Corrélations mesurées (prix hebdo) : MVRV↔NUPL≈0.79, MVRV↔Reserve≈0.98, NUPL↔Reserve≈0.88
# (même famille « coût de base on-chain ») ; Puell↔Mayer≈0.91 (tous deux ≈ prix / moyenne
# mobile) ; Pi Cycle = le plus indépendant. On moyenne DANS chaque dimension puis on
# moyenne les dimensions → chaque signal indépendant pèse pareil, pas de double comptage.
COMPOSITE_GROUPS = [
    {"key": "valuation", "label": "Valorisation on-chain", "members": ["mvrvz", "nupl", "reserve"]},
    {"key": "trend",     "label": "Prix vs moyenne mobile", "members": ["puell", "mayer"]},
    {"key": "pitop",     "label": "Signal de sommet (Pi Cycle)", "members": ["pi"]},
]


def _comp_status(v):
    if v is None:
        return None
    return "red" if v >= 75 else "orange" if v >= 50 else "green" if v >= 25 else "bottom"


# ── Dimension TEMPORELLE : position dans le cycle de 4 ans par le seul calendrier,
# indépendante de l'amplitude (qui se compresse à chaque cycle). 0 = timing de creux,
# 100 = timing de sommet. Ancres = médianes mesurées du rythme : sommet à ~37 % du cycle
# (≈ J+530 après le halving), creux à ~62 % (≈ J+890). Phase = position dans l'intervalle
# entre deux halvings successifs.
HALVING_TS = [1230940800, 1354060800, 1467984000, 1589155200, 1713571200, 1839214154]
TEMPORAL_FTOP, TEMPORAL_FBOT = 0.368, 0.618


def _halving_phase(ts):
    """(jours depuis le dernier halving, phase 0-1 dans l'intervalle de halving)."""
    hv = HALVING_TS
    k = len(hv) - 2
    for i in range(len(hv) - 1):
        if hv[i] <= ts < hv[i + 1]:
            k = i
            break
    days = (ts - hv[k]) / 86400.0
    ph = max(0.0, min(1.0, (ts - hv[k]) / (hv[k + 1] - hv[k])))
    return days, ph


def temporal_score(ts):
    """Position de cycle 0-100 estimée par le calendrier seul (sommet→100, creux→0)."""
    if ts is None:
        return None
    _, ph = _halving_phase(ts)
    if ph <= TEMPORAL_FTOP:
        return 50.0 + ph / TEMPORAL_FTOP * 50.0
    if ph <= TEMPORAL_FBOT:
        return 100.0 - (ph - TEMPORAL_FTOP) / (TEMPORAL_FBOT - TEMPORAL_FTOP) * 100.0
    return (ph - TEMPORAL_FBOT) / (1 - TEMPORAL_FBOT) * 50.0


def compute_composite(table, ts=None):
    """Position de cycle 0-100, AUDITABLE et CONTINUE. Chaque métrique est normalisée
    0-100 par interpolation à travers ses seuils ; on moyenne DANS chaque dimension
    (groupe d'indicateurs corrélés) puis on moyenne les dimensions disponibles, pour
    éviter qu'un même signal présent en double (ex. MVRV/NUPL) compte plusieurs fois.
    Recalculé APRÈS merge-preserve."""
    breakdown = []
    submap = {}
    for r in table:
        st = r.get("status")
        v = r.get("value")
        anc = COMPOSITE_ANCHORS.get(r["key"])
        sub = None
        if v is not None and anc is not None and st is not None:
            sub = round(_interp_score(v, anc))
            submap[r["key"]] = sub
        breakdown.append({"key": r["key"], "label": r["label"], "status": st,
                          "subscore": sub, "display": r.get("display", "—")})
    # 1) score par dimension = moyenne des membres disponibles
    groups = []
    group_scores = []
    for g in COMPOSITE_GROUPS:
        subs = [submap[k] for k in g["members"] if k in submap]
        gscore = round(sum(subs) / len(subs)) if subs else None
        if gscore is not None:
            group_scores.append(gscore)
        groups.append({"key": g["key"], "label": g["label"], "members": g["members"],
                       "score": gscore, "status": _comp_status(gscore),
                       "n": len(subs), "n_total": len(g["members"])})
    # 1bis) dimension TEMPORELLE (calendrier) — indépendante de l'amplitude
    tsc = temporal_score(ts) if ts is not None else None
    if tsc is not None:
        tsub = round(tsc)
        days, ph = _halving_phase(ts)
        submap["time"] = tsub
        group_scores.append(tsub)
        groups.append({"key": "timing", "label": "Position temporelle", "members": ["time"],
                       "score": tsub, "status": _comp_status(tsub), "n": 1, "n_total": 1})
        breakdown.append({"key": "time", "label": "J+%d · %d%% du cycle" % (round(days), round(ph * 100)),
                          "status": _comp_status(tsub), "subscore": tsub,
                          "display": "J+%d · %d%% du cycle" % (round(days), round(ph * 100))})
    # 2) composite = moyenne des dimensions disponibles
    comp = round(sum(group_scores) / len(group_scores)) if group_scores else None
    cstatus = _comp_status(comp)
    label = {"red": "Sommet / distribution", "orange": "Cycle avancé",
             "green": "Début–milieu de cycle", "bottom": "Bas de cycle / accumulation"}.get(cstatus, "—")
    return {"value": comp, "status": cstatus, "label": label,
            "n_metrics": len(submap), "n_total": len(table),
            "n_groups": len(group_scores), "n_groups_total": len(COMPOSITE_GROUPS) + 1,
            "provisional": len(submap) < len(table),
            "scores": list(submap.values()),
            "group_scores": group_scores,
            "groups": groups,
            "breakdown": breakdown,
            "formula": "Les indicateurs sont regroupés par DIMENSION pour éviter de surpondérer "
                       "des signaux très corrélés (MVRV·NUPL·Reserve ≈ « coût de base on-chain » ; "
                       "Puell·Mayer ≈ prix / moyenne mobile). On ajoute une dimension TEMPORELLE — "
                       "la position dans le cycle de 4 ans par le calendrier seul (sommet ≈ J+530 "
                       "après le halving, creux ≈ J+890) — indépendante de l'amplitude, qui se "
                       "compresse à chaque cycle. Chaque métrique est normalisée 0-100, on moyenne "
                       "DANS chaque dimension, puis le composite = moyenne des dimensions disponibles."}


def composite_weekly(price_w, pi_w, mayer_w, puell_w, mz_series, nu_series, rr_series):
    """Position de cycle 0-100 reconstruite sur tout l'historique hebdo. À chaque
    date, on prend la valeur as-of (≤ date) de chaque métrique disponible, on la
    normalise avec EXACTEMENT les mêmes ancres/dimensions que compute_composite,
    puis composite = moyenne des dimensions disponibles. Garantit que la courbe
    historique coïncide avec la jauge live (même source de vérité)."""
    def lut(series, valfn):
        out = []
        for row in series or []:
            v = valfn(row)
            if v is None:
                continue
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                continue
            out.append((row[0], v))
        return out
    metrics = {
        "pi":      lut(pi_w,      lambda r: (r[1] / r[2]) if (len(r) > 2 and r[1] is not None and r[2]) else None),
        "mvrvz":   lut(mz_series, lambda r: r[1]),
        "nupl":    lut(nu_series, lambda r: r[1]),
        "reserve": lut(rr_series, lambda r: r[1]),
        "puell":   lut(puell_w,   lambda r: r[1]),
        "mayer":   lut(mayer_w,   lambda r: r[1]),
    }

    def asof(lst, t):
        lo, hi = 0, len(lst)
        while lo < hi:
            mid = (lo + hi) // 2
            if lst[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        return lst[lo - 1][1] if lo > 0 else None

    out = []
    for row in price_w or []:
        t = row[0]
        submap = {}
        for k, lst in metrics.items():
            v = asof(lst, t)
            if v is not None:
                submap[k] = round(_interp_score(v, COMPOSITE_ANCHORS[k]))
        gscores = []
        for g in COMPOSITE_GROUPS:
            subs = [submap[k] for k in g["members"] if k in submap]
            if subs:
                gscores.append(round(sum(subs) / len(subs)))
        if gscores:  # dimension temporelle ajoutée sur les points où ≥1 métrique existe
            gscores.append(round(temporal_score(t)))
            out.append([t, round(sum(gscores) / len(gscores))])
    return out


# ─────────────────────────── Build ─────────────────────────────
def build():
    sess = _session()

    # 1) Prix + issuance + mcap (full history, Coinmetrics)
    pdates, prices = cm_metric(sess, "PriceUSD")
    log(f"Coinmetrics prix : {len(prices)} pts {pdates[0]}→{pdates[-1]}")
    try:
        idates, iss = cm_metric(sess, "IssTotUSD")
    except Exception as e:
        log(f"WARN issuance: {e}")
        idates, iss = None, None
    # Market cap + ratio MVRV (full history, gratuit) → MVRV Z-Score maison
    try:
        mcap_d, mcap_v = cm_metric(sess, "CapMrktCurUSD")
        mvrvr_d, mvrvr_v = cm_metric(sess, "CapMVRVCur")
    except Exception as e:
        log(f"WARN mcap/mvrv: {e}")
        mcap_d = mcap_v = mvrvr_d = mvrvr_v = None

    # 2) Dérivés (full history)
    p = np.asarray(prices, dtype=float)
    dma111 = sma(prices, 111)
    dma350x2 = sma(prices, 350) * 2.0
    dma200 = sma(prices, 200)
    mayer = p / dma200

    # Puell = issuance / MA365(issuance), aligné sur les dates prix
    puell_series = None
    puell_now = None
    if iss is not None:
        iss_map = dict(zip(idates, iss))
        iss_aligned = [iss_map.get(d) for d in pdates]
        ia = np.asarray([x if x is not None else np.nan for x in iss_aligned], dtype=float)
        ma365 = sma(np.nan_to_num(ia, nan=0.0), 365)
        with np.errstate(divide="ignore", invalid="ignore"):
            puell_series = ia / ma365
        # dernier point valide
        for k in range(len(puell_series) - 1, -1, -1):
            if not math.isnan(puell_series[k]) and not math.isinf(puell_series[k]):
                puell_now = float(puell_series[k]); break

    # Pi cycle ratio courant
    ratio_pi = None
    for k in range(len(dma111) - 1, -1, -1):
        if not math.isnan(dma111[k]) and not math.isnan(dma350x2[k]) and dma350x2[k] > 0:
            ratio_pi = float(dma111[k] / dma350x2[k]); break
    mayer_now = float(mayer[-1]) if not math.isnan(mayer[-1]) else None
    price_now = float(prices[-1])
    asof_price = pdates[-1]

    # 3a) MVRV Z-Score + NUPL : full history maison (Coinmetrics), comme Coinglass.
    # Tous deux dérivés du même ratio MVRV → cohérence garantie entre les 2 graphes.
    mz_d = mz_v = nu_d = nu_v = None
    if mcap_d and mvrvr_d:
        mz_d, mz_v = mvrv_zscore_full(mcap_d, mcap_v, mvrvr_d, mvrvr_v)
        if mz_d:
            log(f"MVRV Z-Score maison : {len(mz_d)} pts {mz_d[0]}→{mz_d[-1]} (now={mz_v[-1]:.2f})")
    if mvrvr_d:
        nu_d, nu_v = nupl_full(mvrvr_d, mvrvr_v)
        if nu_d:
            log(f"NUPL maison : {len(nu_d)} pts {nu_d[0]}→{nu_d[-1]} (now={nu_v[-1]:.3f})")
    mvrvz_now = mz_v[-1] if mz_v else None
    nupl_now = nu_v[-1] if nu_v else None

    # 3b) Reserve Risk : bitcoin-data (~4 ans ; HODL Bank / coin-days indispo en gratuit
    # Coinmetrics, donc pas extensible). --no-onchain : on saute l'appel (merge-preserve restaure).
    if "--no-onchain" in sys.argv:
        log("--no-onchain : appel bitcoin-data reserve-risk sauté (merge-preserve restaurera)")
        rr_d = rr_v = None
    else:
        rr_d, rr_v = bd_metric(sess, "reserve-risk", "reserveRisk")
    reserve_now = rr_v[-1] if rr_v else None

    # 4) Décimation séries pour graphes (hebdo ≈ 1/7 sur full history)
    step = 7
    price_w = decim(pdates, prices, step=step)
    pi_w = decim(pdates, dma111, dma350x2, step=step)
    mayer_w = decim(pdates, mayer, step=step)
    puell_w = decim(pdates, puell_series, step=step) if puell_series is not None else []
    # MVRV Z + NUPL : full history maison → hebdo. Reserve : bitcoin-data ~4 ans, brut quotidien.
    mz_series = decim(mz_d, mz_v, step=step) if mz_d else []
    nu_series = decim(nu_d, nu_v, step=step) if nu_d else []
    rr_series = decim(rr_d, rr_v, step=2) if rr_d else []

    # 5) Returns heatmaps
    returns = period_returns(pdates, prices)

    # 6) Rainbow
    rainbow = rainbow_fit(pdates, prices)

    # 7) Halving context
    tip = block_tip(sess)
    halving = {
        "next_block": NEXT_HALVING_BLOCK,
        "last_block": LAST_HALVING_BLOCK,
        "last_date": LAST_HALVING_DATE,
        "blocks_per_halving": BLOCKS_PER_HALVING,
        "genesis_unix": GENESIS_UNIX,
        "tip_height": tip,
        "tip_fetched_unix": int(time.time()) if tip else None,
        "target_block_minutes": 10,
        # ETA serveur (fallback ; le JS recalcule en live via mempool.space)
        "eta_unix": (int(time.time()) + (NEXT_HALVING_BLOCK - tip) * 600) if tip else None,
    }

    # 8) Table rouge/orange/vert (auditable : valeur + seuils + source + formule)
    table = [
        {
            "key": "pi", "label": "Pi Cycle Top",
            "value": round(ratio_pi, 4) if ratio_pi else None,
            "display": (f"{ratio_pi*100:.0f}% du croisement" if ratio_pi else "—"),
            "status": status_pi(ratio_pi),
            "zone": {"red": "Sommet (croisement)", "orange": "Surchauffe",
                     "green": "Neutre", "bottom": "Bas de cycle"}.get(status_pi(ratio_pi)),
            "thresholds": "111 DMA / (2×350 DMA) : ≥0.95 rouge · ≥0.75 orange · ≥0.45 vert · <0.45 bas",
            "formula": "Signal de sommet quand la 111 DMA dépasse 2 × la 350 DMA",
            "source_url": "https://www.coinglass.com/pro/i/pi-cycle-top-indicator",
            "as_of": asof_price,
        },
        {
            "key": "mvrvz", "label": "MVRV Z-Score",
            "value": round(mvrvz_now, 3) if mvrvz_now is not None else None,
            "display": (f"{mvrvz_now:.2f}" if mvrvz_now is not None else "—"),
            "status": status_mvrvz(mvrvz_now),
            "zone": {"red": "Survalorisation extrême", "orange": "Élevé",
                     "green": "Neutre", "bottom": "Sous-évaluation"}.get(status_mvrvz(mvrvz_now)),
            "thresholds": "≥7 rouge · ≥3 orange · ≥0 vert · <0 bas (accumulation)",
            "formula": "(Market Cap − Realized Cap) / σ cumulatif(Market Cap) — Realized Cap = MarketCap / ratio MVRV",
            "source_url": "https://www.coinglass.com/pro/i/bitcoin-mvrv-zscore",
            "as_of": mz_d[-1] if mz_d else None,
        },
        {
            "key": "puell", "label": "Puell Multiple",
            "value": round(puell_now, 3) if puell_now is not None else None,
            "display": (f"{puell_now:.2f}" if puell_now is not None else "—"),
            "status": status_puell(puell_now),
            "zone": {"red": "Mineurs en surprofit (top)", "orange": "Élevé",
                     "green": "Neutre", "bottom": "Capitulation mineurs"}.get(status_puell(puell_now)),
            "thresholds": "≥4 rouge · ≥1.2 orange · ≥0.5 vert · <0.5 bas",
            "formula": "Émission USD du jour / MA365(émission USD)",
            "source_url": "https://www.coinglass.com/pro/i/PM",
            "as_of": asof_price,
        },
        {
            "key": "reserve", "label": "Reserve Risk",
            "value": round(reserve_now, 6) if reserve_now is not None else None,
            "display": (f"{reserve_now:.4f}" if reserve_now is not None else "—"),
            "status": status_reserve(reserve_now),
            "zone": {"red": "Cher vs conviction", "orange": "Au-dessus",
                     "green": "Attractif", "bottom": "Opportunité forte"}.get(status_reserve(reserve_now)),
            "thresholds": "≥0.02 rouge · ≥0.008 orange · ≥0.002 vert · <0.002 bas",
            "formula": "Prix / HODL Bank (confiance des détenteurs LT)",
            "source_url": "https://www.coinglass.com/pro/i/reserve-risk",
            "as_of": rr_d[-1] if rr_d else None,
        },
        {
            "key": "mayer", "label": "Mayer Multiple",
            "value": round(mayer_now, 3) if mayer_now is not None else None,
            "display": (f"{mayer_now:.2f}" if mayer_now is not None else "—"),
            "status": status_mayer(mayer_now),
            "zone": {"red": "Surchauffe (>200 DMA)", "orange": "Au-dessus tendance",
                     "green": "Neutre", "bottom": "Sous la 200 DMA"}.get(status_mayer(mayer_now)),
            "thresholds": "≥2.4 rouge · ≥1.0 orange · ≥0.8 vert · <0.8 bas",
            "formula": "Prix / 200 DMA",
            "source_url": "https://www.coinglass.com/pro/i/mayer-multiple",
            "as_of": asof_price,
        },
        {
            "key": "nupl", "label": "NUPL",
            "value": round(nupl_now, 4) if nupl_now is not None else None,
            "display": (f"{nupl_now:.2f}" if nupl_now is not None else "—"),
            "status": status_nupl(nupl_now),
            "zone": {"red": "Euphorie / Avidité", "orange": "Croyance / Déni",
                     "green": "Optimisme / Anxiété", "bottom": "Capitulation / Espoir"}.get(status_nupl(nupl_now)),
            "thresholds": "≥0.75 rouge · ≥0.5 orange · ≥0.25 vert · <0.25 bas",
            "formula": "(Market Cap − Realized Cap) / Market Cap",
            "source_url": "https://www.coinglass.com/pro/i/nupl",
            "as_of": nu_d[-1] if nu_d else None,
        },
    ]

    # 9) Composite cycle 0-100 (recalculé aussi après merge-preserve dans main)
    composite = compute_composite(table, price_w[-1][0] if price_w else None)

    payload = {
        "meta": {
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "updated_unix": int(time.time()),
            "price_now": round(price_now, 2),
            "price_asof": asof_price,
            "history_start": pdates[0],
            "n_price_points": len(prices),
            "sources": {
                "price_issuance_mcap": "Coinmetrics community API (gratuit, full history 2010+)",
                "onchain_4y": "bitcoin-data.com / BGeometrics (gratuit, ~4 ans)",
                "halving": "mempool.space / blockchain.info",
            },
            "coverage_note": "Pi Cycle · Mayer · Puell · Rainbow · MVRV-Z · NUPL = full history calculée. "
                             "Reserve Risk = 4 ans (HODL Bank / coin-days indispo en gratuit).",
            "palette": COL,
        },
        "composite": composite,
        "composite_weekly": composite_weekly(price_w, pi_w, mayer_w, puell_w, mz_series, nu_series, rr_series),
        "table": table,
        "price_weekly": price_w,
        "pi_cycle_weekly": pi_w,
        "mayer_weekly": mayer_w,
        "puell_weekly": puell_w,
        "mvrvz_series": mz_series,
        "nupl_series": nu_series,
        "reserve_series": rr_series,
        "rainbow": rainbow,
        "returns": returns,
        "halving": halving,
    }
    return payload


# ─────────────────────── Merge-preserve ────────────────────────
def merge_preserve(new):
    """Ne jamais régresser : si une série on-chain est vide mais présente dans
    l'ancien cache, on conserve l'ancienne (sanity contre flap réseau)."""
    if not CACHE_JSON.exists():
        return new
    try:
        old = json.loads(CACHE_JSON.read_text())
    except Exception:
        return new
    for key in ("mvrvz_series", "nupl_series", "reserve_series", "puell_weekly"):
        if not new.get(key) and old.get(key):
            new[key] = old[key]
            log(f"merge-preserve: {key} conservé depuis l'ancien cache")
    # table : restaure la valeur si elle est tombée à None
    if old.get("table"):
        oldmap = {r["key"]: r for r in old["table"]}
        for r in new.get("table", []):
            if r.get("value") is None and oldmap.get(r["key"], {}).get("value") is not None:
                o = oldmap[r["key"]]
                r.update({"value": o["value"], "display": o["display"],
                          "status": o["status"], "zone": o["zone"], "as_of": o["as_of"]})
                log(f"merge-preserve: table[{r['key']}] valeur conservée")
    return new


def sanity(p):
    assert p["meta"]["n_price_points"] > 1000, "trop peu de points prix"
    assert p["meta"]["price_now"] > 1000, "prix BTC absurde"
    assert len(p["price_weekly"]) > 100, "price_weekly vide"
    # La décimation ne doit JAMAIS amputer la fin de série. Le 2026-08-20, `decim` partait
    # de l'origine de l'histoire : les graphes s'arrêtaient au 16 août quand la donnée
    # allait au 19 — un décalage muet, sans erreur, que seule une comparaison à l'œil
    # avec la jauge révélait. Le dernier point hebdo DOIT porter la date du dernier prix.
    asof = p["meta"].get("price_asof")
    if asof and p["price_weekly"]:
        last_w = datetime.fromtimestamp(p["price_weekly"][-1][0], timezone.utc).strftime("%Y-%m-%d")
        assert last_w == asof, f"price_weekly s'arrête au {last_w} alors que le prix va au {asof}"
    assert p["rainbow"]["r2"] > 0.8, f"rainbow R² faible ({p['rainbow']['r2']})"
    assert any(r["value"] is not None for r in p["table"]), "table entièrement vide"
    # MVRV Z-Score full history : la série doit couvrir >10 ans et rester dans une
    # plage plausible (top historique ~12, bas ~ −1). Garde-fou anti-régression.
    mz = p.get("mvrvz_series") or []
    if mz:
        assert len(mz) > 400, f"mvrvz_series trop courte ({len(mz)} pts)"
        zs = [r[1] for r in mz if r[1] is not None]
        assert zs and max(zs) < 15 and min(zs) > -3, f"mvrvz_series hors plage (min={min(zs):.2f} max={max(zs):.2f})"
    # NUPL full history (dérivé du ratio MVRV) : >10 ans, borné [−2, 1].
    nu = p.get("nupl_series") or []
    if nu:
        assert len(nu) > 400, f"nupl_series trop courte ({len(nu)} pts)"
        ns = [r[1] for r in nu if r[1] is not None]
        assert ns and max(ns) < 1.0 and min(ns) > -2.5, f"nupl_series hors plage (min={min(ns):.2f} max={max(ns):.2f})"
    # composite_weekly : historique non régressif + son DERNIER point doit coïncider
    # avec la jauge live (même formule), sinon divergence silencieuse historique/live.
    cw = p.get("composite_weekly") or []
    cv = (p.get("composite") or {}).get("value")
    if cw:
        assert len(cw) > 400, f"composite_weekly trop courte ({len(cw)} pts)"
        vs = [r[1] for r in cw]
        assert 0 <= min(vs) and max(vs) <= 100, f"composite_weekly hors [0,100] (min={min(vs)} max={max(vs)})"
        if cv is not None:
            assert abs(cw[-1][1] - cv) <= 2, f"composite_weekly[-1]={cw[-1][1]} diverge du composite live={cv}"


def build_light(p):
    """Payload MINIMAL pour la tuile « Position de cycle » de l'Accueil.

    btc_cycle_cache.js pèse ~170 Ko (toutes les séries hebdo depuis 2010) : bien
    trop lourd pour la page d'accueil, qui n'a besoin que du composite, de ses 4
    dimensions, du détail auditable et d'une courbe d'historique dégrossie.
    Ce companion fait ~10 Ko et porte EXACTEMENT les mêmes chiffres (même source
    de vérité : on ne recalcule rien ici, on projette).
    """
    c = p.get("composite") or {}
    meta = p.get("meta") or {}
    # Historique : on garde ~1 point/mois (1 sur 4 des points hebdo) pour la
    # sparkline, + on force le DERNIER point (= la jauge live) à être présent.
    cw = p.get("composite_weekly") or []
    spark = cw[::4]
    if cw and (not spark or spark[-1] != cw[-1]):
        spark.append(cw[-1])
    return {
        "meta": {
            "updated_at": meta.get("updated_at"),
            "updated_unix": meta.get("updated_unix"),
            "price_asof": meta.get("price_asof"),
            "price_now": meta.get("price_now"),
        },
        "composite": {
            "value": c.get("value"),
            "status": c.get("status"),
            "label": c.get("label"),
            "provisional": c.get("provisional"),
            "n_metrics": c.get("n_metrics"),
            "n_total": c.get("n_total"),
            "group_scores": c.get("group_scores"),
            "groups": c.get("groups"),
            "breakdown": c.get("breakdown"),
        },
        "weekly": spark,
        "palette": meta.get("palette") or COL,
    }


def write_outputs(p):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_j = CACHE_DIR / ".btc_cycle.tmp.json"
    tmp_j.write_text(json.dumps(p, separators=(",", ":"), ensure_ascii=False))
    tmp_j.replace(CACHE_JSON)
    js = (f"/* btc_cycle_cache.js — {p['meta']['updated_at']} */\n"
          f"window.__BTC_CYCLE__={json.dumps(p, separators=(',', ':'), ensure_ascii=False)};\n")
    tmp_s = CACHE_DIR / ".btc_cycle.tmp.js"
    tmp_s.write_text(js)
    tmp_s.replace(CACHE_JS)
    # Companion léger (Accueil) — écrit dans le MÊME passage que le cache complet
    # pour qu'il ne puisse jamais diverger de la jauge du Cycle Tracker.
    light = build_light(p)
    ljs = (f"/* btc_cycle_light.js — {p['meta']['updated_at']} — "
           f"companion léger de btc_cycle_cache.js (Accueil) */\n"
           f"window.__BTC_CYCLE_LIGHT__={json.dumps(light, separators=(',', ':'), ensure_ascii=False)};\n")
    tmp_l = CACHE_DIR / ".btc_cycle_light.tmp.js"
    tmp_l.write_text(ljs)
    tmp_l.replace(LIGHT_JS)
    # symlink racine site
    if SITE_DIR.exists():
        for name in ("btc_cycle_cache.json", "btc_cycle_cache.js", "btc_cycle_light.js"):
            link = SITE_DIR / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    # --relight : régénère UNIQUEMENT le companion léger depuis le cache JSON
    # déjà sur disque (zéro appel réseau). Sert au premier amorçage et après
    # toute évolution du format de build_light().
    if "--relight" in sys.argv:
        if not CACHE_JSON.exists():
            log("FATAL --relight : btc_cycle_cache.json absent")
            sys.exit(2)
        p = json.loads(CACHE_JSON.read_text())
        write_outputs(p)
        c = p.get("composite") or {}
        log(f"relight OK · composite={c.get('value')} ({c.get('label')}) · "
            f"{LIGHT_JS.stat().st_size // 1024} Ko")
        return
    if CACHE_JSON.exists() and CACHE_JS.exists() and "--force" not in sys.argv:
        age = (time.time() - CACHE_JSON.stat().st_mtime) / 3600
        if age < CACHE_MAX_HOURS:
            log(f"Cache frais ({age:.1f}h) — skip (--force pour forcer)")
            return
    t0 = time.time()
    wait_for_network()  # anti-course réveil : attend que le DNS résolve avant de fetch
    try:
        payload = build()
    except Exception as e:
        log(f"FATAL build: {e}")
        sys.exit(2)
    payload = merge_preserve(payload)
    # Recalcul du composite APRÈS merge (sinon il sous-compte les métriques
    # restaurées depuis l'ancien cache — bug "3/6" au lieu de 4/6).
    payload["composite"] = compute_composite(
        payload["table"], payload["price_weekly"][-1][0] if payload.get("price_weekly") else None)
    # composite_weekly recalculé après merge (séries on-chain éventuellement restaurées)
    payload["composite_weekly"] = composite_weekly(
        payload.get("price_weekly"), payload.get("pi_cycle_weekly"), payload.get("mayer_weekly"),
        payload.get("puell_weekly"), payload.get("mvrvz_series"), payload.get("nupl_series"),
        payload.get("reserve_series"))
    try:
        sanity(payload)
    except AssertionError as e:
        log(f"FATAL sanity: {e} — cache NON écrit (préservation)")
        sys.exit(3)
    write_outputs(payload)
    c = payload["composite"]
    log(f"OK en {time.time()-t0:.1f}s · composite={c['value']} ({c['label']}) · "
        f"{payload['meta']['n_price_points']} pts prix · rainbow R²={payload['rainbow']['r2']}")
    for r in payload["table"]:
        log(f"   {r['label']:>16} = {r['display']:>20}  [{r['status']}]")


if __name__ == "__main__":
    main()
