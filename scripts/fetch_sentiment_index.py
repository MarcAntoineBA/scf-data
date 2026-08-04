#!/usr/bin/env python3
"""Sentiment Index (0-100) — refonte du Crisis Index, 2026-05-20.

METHODOLOGIE
============
Indice de sentiment marche style Fear & Greed (0 = peur extreme, 100 = euphorie),
construit a partir de 19 termes Google Trends (3 buckets) + VIX (CBOE).

Buckets :
  BULLISH (9 termes)      : recherche d'opportunites, FOMO
    Bull run, buy stocks, bull market, how to invest, invest now,
    become millionaire, financial freedom, Bitcoin, Ethereum
  BEARISH (7 termes)      : panique macro
    recession, stock market crash, market crash, financial crisis,
    bear market, debt crisis, market bubble
  CAPITULATION (3 termes) : signal LEADING de bottom (pic = signal d'achat)
    bitcoin is dead, ethereum is dead, stock market crashed

Formule :
  z_term      = (valeur_mois - mean_2004+) / std_2004+        # standardisation
  Bullish_z   = moyenne(9 z-scores bullish)
  Bearish_z   = moyenne(7 z-scores bearish)
  Capitul_z   = moyenne(3 z-scores capitulation)

  GTrends_raw   = Bullish_z - Bearish_z + 0.5 * Capitul_z
  GTrends_Score = percentile(GTrends_raw, fenetre 2004+) * 100    # [0, 100]

  VIX_Score = (1 - percentile(VIX_mensuel, fenetre 2004+)) * 100  # [0, 100]
                                                                  # VIX bas = greed = haut

  Sentiment_Index = 0.70 * GTrends_Score + 0.30 * VIX_Score        # [0, 100]

Sources :
  - gtrends_cache.json : SerpAPI (52 termes, dont les 19 utilises ici)
  - VIX : FRED API serie VIXCLS (daily) -> moyenne mensuelle

Sortie :
  data/sentiment_index.json     (toutes valeurs intermediaires + serie historique)
  data/sentiment_index_meta.json
"""
import argparse
import fcntl
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError, URLError

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
# Detection environnement : Desktop (dev = source of truth pour index.Rmd)
# ou Library/Application Support (prod TCC-safe pour launchd).
# Desktop EN PREMIER : c'est ce que index.Rmd lit. Le fallback Library/
# n'est utilise que si Desktop est demonte (rare).
# Bug 2026-05-23 : SCRIPT_DIR.parent.parent = ~/Library/ quand le script
# tourne depuis Library/Application Support/SiteCryptoFinance/ -> ecrivait
# dans ~/Library/data/ sentiment_index.json fantome jamais lu par index.Rmd.
CANDIDATE_SITE_DIRS = [
    Path.home() / "Desktop" / "Site_Crypto_Finance",              # PRIORITAIRE : source of truth
    SCRIPT_DIR.parent.parent,                                     # legacy : Desktop si script y est, Library sinon
    Path.home() / "Library" / "Application Support" / "SiteCryptoFinance",
]
SITE_DIR = next((p for p in CANDIDATE_SITE_DIRS if (p / "data").exists()), CANDIDATE_SITE_DIRS[0])
DATA_DIR = SITE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Cache TCC-safe (idem update_gtrends.py)
CACHE_DIR_TCC = Path.home() / "Library" / "Caches" / "site_crypto_finance"
GTRENDS_CACHE = CACHE_DIR_TCC / "gtrends_cache.json"

# Lock pour eviter doubles executions
LOCK_FILE = CACHE_DIR_TCC / ".fetch_sentiment_index.lock"

# Sorties
OUT_CACHE = DATA_DIR / "sentiment_index.json"
OUT_META = DATA_DIR / "sentiment_index_meta.json"

# FRED API
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
VIX_CACHE = CACHE_DIR_TCC / "vix_monthly.json"

# Mapping des termes — REFONTE TRADFI-FOCUS 2026-05-21.
# Apres backtest IC, les termes crypto-natifs (Bitcoin/Ethereum/bitcoin is dead/ethereum is dead)
# se sont reveles PRO-CYCLIQUES sur BTC/ETH mais bruyaient le signal CONTRARIAN sur S&P 500.
# L'index est maintenant focalise sur la TradFi pure (S&P 500-centric) avec un usage
# contrarian valide empiriquement (IC walk-forward = -0.025, strategie LONG<25 = +65.6%).
# Crypto-natifs retires : Bitcoin, Ethereum (bullish), bitcoin is dead, ethereum is dead (capitulation).
# Capitulation TradFi renforce avec panic selling / black monday / market bottom (deja trackes).
# Versionne dans le code = audit via git blame.
TERM_MAPPING = {
    "bullish": [
        "Bull run", "buy stocks", "bull market", "how to invest", "invest now",
        "become millionaire", "financial freedom",
    ],
    "bearish": [
        "recession", "stock market crash", "market crash", "financial crisis",
        "bear market", "debt crisis",
        # "market bubble" retire 2026-05-21 : terme ambigu (pic au TOP avant crash, pas
        # pendant la peur). Reste tracke dans gtrends_cache mais hors composite.
    ],
    "capitulation": [
        "stock market crashed", "panic selling", "black monday", "market bottom",
    ],
}

# Coefficients de la formule (audit-friendly : explicites, pas de constantes magiques)
W_BULLISH = 1.0       # poids bucket bullish dans GTrends_raw
W_BEARISH = -1.0      # signe negatif = peur tire vers le bas
W_CAPITUL = 0.5       # signal d'appoint (contrarian), pas equipotent
W_GTRENDS = 0.70      # poids final GTrends dans Sentiment Index
W_VIX = 0.30          # poids final VIX dans Sentiment Index

# Fenetre de reference pour z-score et percentile = fixe depuis le debut GTrends
# (rejouer le calcul demain donne le meme resultat sur 2008 / 2020 / 2022)
HIST_START = "2004-01-01"


# ──────────────────────────────────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def acquire_lock():
    """Singleton lock — sort 0 si autre instance tourne deja."""
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        log("Une autre instance tourne deja. Exit 0.")
        sys.exit(0)


def find_emergence_date(series_by_date, all_dates, min_hits=3, lookahead_months=36, min_active=3):
    """Trouve la premiere date d'emergence significative d'un terme.
    Critere : hits >= min_hits ce mois-la ET au moins min_active mois actifs (hits>0)
    dans les lookahead_months mois suivants. Adapte aux termes episodiques (capitulation,
    crashes) qui pic rarement mais qui sont signifiants quand ils pic.
    Retourne None si jamais eligible.
    """
    n = len(all_dates)
    for i, d in enumerate(all_dates):
        v = series_by_date.get(d)
        if v is None or v < min_hits:
            continue
        # Verification de persistence dans la fenetre suivante (~3 ans)
        future = all_dates[i:min(i + lookahead_months, n)]
        active = sum(1 for fd in future if (series_by_date.get(fd) or 0) > 0)
        if active >= min_active:
            return d
    return None


def mean(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    if not xs:
        return None
    return sum(xs) / len(xs)


def stdev(xs, mu=None):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    if len(xs) < 2:
        return None
    if mu is None:
        mu = sum(xs) / len(xs)
    var = sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)


def percentile_rank(value, sorted_values):
    """Rank percentile (0-1) de `value` dans `sorted_values`. Methode : moyenne
    des positions inferieures et superieures (handle ties)."""
    if not sorted_values or value is None or math.isnan(value):
        return None
    n = len(sorted_values)
    # binary search lower bound
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_values[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    cnt_below = lo
    # count equal
    hi2 = lo
    while hi2 < n and sorted_values[hi2] == value:
        hi2 += 1
    cnt_equal = hi2 - lo
    rank = (cnt_below + 0.5 * cnt_equal) / n
    return rank


# ──────────────────────────────────────────────────────────────────────────
# Fetch FRED VIXCLS (mensuel)
# ──────────────────────────────────────────────────────────────────────────
def align_sp500(dates, sp_dict):
    """Aligne le dict S&P {YYYY-MM-01: prix} sur la liste de dates sentiment.
    Forward-fill : si une date sentiment n'a pas de S&P, prend la derniere
    valeur disponible jusqu'a 2 mois avant. Sinon retourne None.
    """
    out = []
    last_val = None
    for d in dates:
        # d est au format "YYYY-MM-DD"
        if d in sp_dict:
            last_val = sp_dict[d]
            out.append(last_val)
        else:
            # essai mois precedent
            try:
                dt = datetime.strptime(d, "%Y-%m-%d")
                # essaie les 2 mois precedents
                for back in range(1, 3):
                    yr, mo = dt.year, dt.month - back
                    while mo < 1:
                        mo += 12; yr -= 1
                    key = f"{yr:04d}-{mo:02d}-01"
                    if key in sp_dict:
                        last_val = sp_dict[key]
                        break
            except Exception:
                pass
            out.append(last_val)
    return out


SP500_CACHE = CACHE_DIR_TCC / "sp500_monthly.json"
SP500_CACHE_TTL_HOURS = 24

def fetch_sp500_monthly(force=False):
    """Retourne dict {YYYY-MM-01: prix close mensuel} pour S&P 500 depuis 2004.

    Source PRIMAIRE : Yahoo Finance v8 chart API ^GSPC interval=1mo (cache 24h).
    Fallback 1 : cache local stale (refresh echec mais cache existant).
    Fallback 2 : yahoo_raw_v2.json (deprecated, derniere chance).

    Cache propre dans ~/Library/Caches/site_crypto_finance/sp500_monthly.json,
    independant de l'ancienne stack google-trends-crisis.
    """
    # 1) Cache frais (< TTL) ?
    if not force and SP500_CACHE.exists():
        try:
            age_h = (datetime.now().timestamp() - SP500_CACHE.stat().st_mtime) / 3600
            if age_h < SP500_CACHE_TTL_HOURS:
                cached = json.loads(SP500_CACHE.read_text())
                log(f"  S&P 500 cache frais ({age_h:.1f}h) : {len(cached)} mois, reutilisation.")
                return cached
        except Exception:
            pass

    # 2) Fetch Yahoo Finance v8 (source primaire)
    log("  Fetch Yahoo Finance v8 ^GSPC (mensuel, depuis 2004)...")
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
           "?period1=1072915200&period2=" + str(int(datetime.now().timestamp())) +
           "&interval=1mo")
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        res = data.get("chart", {}).get("result", [None])[0]
        if not res:
            raise RuntimeError("Yahoo: no result in response")
        ts_list = res.get("timestamp", [])
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        out = {}
        for ts, px in zip(ts_list, closes):
            if ts and px:
                d = datetime.fromtimestamp(ts, tz=timezone.utc)
                key = d.strftime("%Y-%m-01")
                out[key] = round(float(px), 2)
        if len(out) < 200:
            raise RuntimeError(f"Yahoo response trop court ({len(out)} mois)")
        # Ecriture cache propre
        SP500_CACHE.write_text(json.dumps(out, ensure_ascii=False))
        log(f"  S&P 500 OK (Yahoo v8) : {len(out)} mois, cache ecrit -> {SP500_CACHE.name}")
        return out
    except Exception as e:
        log(f"  WARN Yahoo v8 echec : {e}")

    # 3) Fallback 1 : cache stale (meme expire)
    if SP500_CACHE.exists():
        try:
            cached = json.loads(SP500_CACHE.read_text())
            log(f"  S&P 500 cache STALE (fallback) : {len(cached)} mois")
            return cached
        except Exception:
            pass

    # 4) Fallback 2 : ancien cache google-trends-crisis (derniere chance)
    legacy_path = SITE_DIR / "google-trends-crisis" / "data" / "yahoo_raw_v2.json"
    if legacy_path.exists():
        try:
            raw = json.loads(legacy_path.read_text())
            if "^GSPC" in raw:
                gspc = raw["^GSPC"]
                out = {}
                for ts, px in zip(gspc.get("timestamps", []), gspc.get("close", [])):
                    if ts and px:
                        d = datetime.fromtimestamp(ts, tz=timezone.utc)
                        key = d.strftime("%Y-%m-01")
                        out[key] = round(float(px), 2)
                if out:
                    log(f"  S&P 500 OK (legacy cache google-trends-crisis) : {len(out)} mois")
                    return out
        except Exception as e:
            log(f"  S&P legacy cache error : {e}")

    log("  ERREUR : toutes les sources S&P 500 ont echoue")
    return {}


def fetch_vix_monthly(force=False):
    """Retourne dict {date_iso (YYYY-MM-01): vix_close_mensuel_moyen}.

    Cache 24h. Source = FRED VIXCLS (daily close VIX, depuis 1990).
    Agregation mensuelle = moyenne arithmetique des closes du mois.
    """
    if not force and VIX_CACHE.exists():
        try:
            age_h = (datetime.now().timestamp() - VIX_CACHE.stat().st_mtime) / 3600
            if age_h < 24:
                log(f"  VIX cache frais ({age_h:.1f}h), reutilisation.")
                return json.loads(VIX_CACHE.read_text())
        except Exception:
            pass

    log("  Fetch FRED VIXCLS (daily, depuis 1990)...")
    params = {
        "series_id": "VIXCLS",
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": "1990-01-01",
    }
    url = FRED_API_URL + "?" + urlencode(params)
    try:
        with urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read())
    except (HTTPError, URLError, TimeoutError) as e:
        log(f"  ERREUR fetch VIX : {e}")
        if VIX_CACHE.exists():
            log("  Fallback : cache VIX existant (stale)")
            return json.loads(VIX_CACHE.read_text())
        sys.exit(1)

    obs = data.get("observations", [])
    if not obs:
        log("  ERREUR : pas d'observations VIX")
        sys.exit(1)

    # Aggregation mensuelle (moyenne)
    monthly = {}  # date_first_of_month -> [closes...]
    for ob in obs:
        v = ob.get("value")
        d = ob.get("date")
        if v is None or v == "." or not d:
            continue
        try:
            fv = float(v)
        except ValueError:
            continue
        # premier jour du mois
        d_obj = datetime.strptime(d, "%Y-%m-%d").date()
        key = d_obj.strftime("%Y-%m-01")
        monthly.setdefault(key, []).append(fv)

    monthly_avg = {k: round(sum(vs) / len(vs), 3) for k, vs in monthly.items()}
    VIX_CACHE.write_text(json.dumps(monthly_avg, ensure_ascii=False))
    log(f"  VIX OK : {len(monthly_avg)} mois (de {min(monthly_avg)} a {max(monthly_avg)})")
    return monthly_avg


# ──────────────────────────────────────────────────────────────────────────
# Load gtrends cache
# ──────────────────────────────────────────────────────────────────────────
def load_gtrends():
    if not GTRENDS_CACHE.exists():
        log(f"ERREUR : gtrends_cache.json manquant a {GTRENDS_CACHE}")
        sys.exit(1)
    rows = json.loads(GTRENDS_CACHE.read_text())
    # Index par (keyword, date)
    by_kw = {}
    for r in rows:
        kw = r["keyword"]
        d = r["date"]
        hits = r["hits"]
        by_kw.setdefault(kw, {})[d] = hits
    log(f"  GTrends OK : {len(by_kw)} termes")
    return by_kw


# ──────────────────────────────────────────────────────────────────────────
# Coeur du calcul
# ──────────────────────────────────────────────────────────────────────────
def compute_sentiment_index():
    log("=== Sentiment Index : computation ===")
    log("Step 1: load sources")
    gt = load_gtrends()
    vix = fetch_vix_monthly()
    sp500 = fetch_sp500_monthly()

    all_terms = TERM_MAPPING["bullish"] + TERM_MAPPING["bearish"] + TERM_MAPPING["capitulation"]
    missing = [t for t in all_terms if t not in gt]
    if missing:
        log(f"ERREUR : termes manquants dans gtrends_cache : {missing}")
        sys.exit(1)
    log(f"  {len(all_terms)} termes utilises (9 bullish, 7 bearish, 3 capitulation)")

    # Step 2: aligner sur l'ensemble des dates GTrends (filter >= HIST_START)
    all_dates = sorted({d for kw in all_terms for d in gt[kw].keys() if d >= HIST_START})
    log(f"Step 2: fenetre = {all_dates[0]} -> {all_dates[-1]} ({len(all_dates)} mois)")

    # Step 3: z-score EXPANDING par terme — NO LOOK-AHEAD BIAS.
    # Fix methodologique critique (2026-05-21) : a chaque date t, mu et sigma sont
    # recalcules sur [emerge_kw, t] uniquement (jamais de donnees futures).
    # Le chart historique reflete maintenant exactement ce qu'un observateur aurait vu
    # a chaque epoque, pas une vue retrospective biaisee depuis 2026.
    # Le z-score requiert MIN_WINDOW=12 observations pour etre stable ; avant => None.
    log("Step 3: z-score EXPANDING par terme (no look-ahead, MIN_WINDOW=12)")
    MIN_WINDOW = 12
    import bisect
    term_stats = {}     # {kw: {"emerge": str, "mean_final": float, "std_final": float}}
    term_z_series = {}  # {kw: [z(t) pour chaque date]}
    for kw in all_terms:
        emerge = find_emergence_date(gt[kw], all_dates)
        if emerge is None:
            log(f"  WARN: '{kw}' jamais eligible (hits insuffisants)")
            term_stats[kw] = {"mean": None, "std": None, "emerge": None}
            term_z_series[kw] = [None] * len(all_dates)
            continue
        try:
            emerge_idx = all_dates.index(emerge)
        except ValueError:
            term_stats[kw] = {"mean": None, "std": None, "emerge": None}
            term_z_series[kw] = [None] * len(all_dates)
            continue

        zs = []
        window = []     # accumulateur des hits depuis emerge_idx
        mu_running = None; sd_running = None
        for i, d in enumerate(all_dates):
            v = gt[kw].get(d)
            if i < emerge_idx:
                zs.append(None)
                continue
            if v is not None:
                window.append(v)
            # MIN_WINDOW observations requises pour z-score stable
            if len(window) < MIN_WINDOW or v is None:
                zs.append(None)
                continue
            # mu et sd EXPANDING sur [emerge_idx, i]
            mu_running = sum(window) / len(window)
            var = sum((x - mu_running) ** 2 for x in window) / (len(window) - 1)
            sd_running = math.sqrt(var)
            if sd_running == 0:
                zs.append(0.0)
            else:
                zs.append((v - mu_running) / sd_running)
        term_z_series[kw] = zs
        term_stats[kw] = {"mean": mu_running, "std": sd_running, "emerge": emerge}
        if emerge != all_dates[0]:
            log(f"  '{kw}': emergence {emerge} (z-score expanding, no look-ahead)")

    # Step 4: lookup z-score (deja calcule)
    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    def z(kw, d):
        idx = date_to_idx.get(d)
        if idx is None: return None
        return term_z_series[kw][idx]

    # Step 5: agregation par bucket (moyenne des z-scores)
    log("Step 4-5: agregation par bucket + GTrends_raw + percentile")
    bullish_z_series = []
    bearish_z_series = []
    capitul_z_series = []
    gtrends_raw_series = []
    for d in all_dates:
        bull_zs = [z(kw, d) for kw in TERM_MAPPING["bullish"]]
        bear_zs = [z(kw, d) for kw in TERM_MAPPING["bearish"]]
        cap_zs = [z(kw, d) for kw in TERM_MAPPING["capitulation"]]
        b_z = mean(bull_zs)
        br_z = mean(bear_zs)
        c_z = mean(cap_zs)
        bullish_z_series.append(b_z)
        bearish_z_series.append(br_z)
        capitul_z_series.append(c_z)
        if b_z is None or br_z is None or c_z is None:
            gtrends_raw_series.append(None)
        else:
            gtrends_raw_series.append(W_BULLISH * b_z + W_BEARISH * br_z + W_CAPITUL * c_z)

    # Step 6: GTrends_Score = percentile EXPANDING de gtrends_raw (no look-ahead)
    # A chaque date t, on rank raw(t) parmi raw(s) pour s in [0, t].
    log("Step 6: GTrends_Score percentile EXPANDING")
    gtrends_score_series = []
    raw_history_sorted = []  # liste triee, maj par bisect.insort
    for x in gtrends_raw_series:
        if x is None:
            gtrends_score_series.append(None)
            continue
        bisect.insort(raw_history_sorted, x)
        if len(raw_history_sorted) < MIN_WINDOW:
            gtrends_score_series.append(None)
            continue
        gtrends_score_series.append(round(percentile_rank(x, raw_history_sorted) * 100, 2))

    # Pour l'audit visuel : conversion 0-100 par percentile historique de chaque bucket z-score.
    # IMPORTANT : ces 3 series sont des PROJECTIONS visuelles pour le chart, pas des composantes
    # entrant dans le calcul final. Le calcul utilise les z-scores bruts (steps 4-5).
    #   Bullish_pct      : monte = plus de signal bullish = score plus haut (intuitif)
    #   Bearish_pct_inv  : INVERSE car bearish_z haut = peur = score doit etre BAS sur l'echelle 0-100
    #                      (pour rester coherent avec la convention F&G : 0=peur, 100=greed)
    #   Capitul_pct      : MEME logique INVERSEE — capitulation HAUTE = peur extreme = bas sur l'axe
    # Percentile EXPANDING des buckets (bullish_pct/bearish_pct/capitul_pct)
    def expanding_pct(series, invert=False):
        out = []
        hist_sorted = []
        for x in series:
            if x is None:
                out.append(None)
                continue
            bisect.insort(hist_sorted, x)
            if len(hist_sorted) < MIN_WINDOW:
                out.append(None)
                continue
            p = percentile_rank(x, hist_sorted)
            out.append(round((1 - p) * 100, 2) if invert else round(p * 100, 2))
        return out

    bullish_pct_series = expanding_pct(bullish_z_series, invert=False)
    bearish_pct_series = expanding_pct(bearish_z_series, invert=True)
    capitul_pct_series = expanding_pct(capitul_z_series, invert=True)

    # Decomposition par terme individuel : percentile 0-100 EXPANDING par terme.
    # bullish direct, bearish/capitulation INVERSES (haut z = peur = bas score 0-100).
    term_pct_series = {}
    for kw in TERM_MAPPING["bullish"]:
        term_pct_series[kw] = expanding_pct(term_z_series[kw], invert=False)
    for kw in TERM_MAPPING["bearish"]:
        term_pct_series[kw] = expanding_pct(term_z_series[kw], invert=True)
    for kw in TERM_MAPPING["capitulation"]:
        term_pct_series[kw] = expanding_pct(term_z_series[kw], invert=True)

    # Step 7: VIX_Score EXPANDING (no look-ahead). Percentile inverse (haut VIX = bas score)
    log("Step 7: VIX_Score percentile EXPANDING inverse")
    vix_series_aligned = [vix.get(d) for d in all_dates]
    vix_score_series = []
    vix_history_sorted = []
    for v in vix_series_aligned:
        if v is None:
            vix_score_series.append(None)
            continue
        bisect.insort(vix_history_sorted, v)
        if len(vix_history_sorted) < MIN_WINDOW:
            vix_score_series.append(None)
            continue
        p = percentile_rank(v, vix_history_sorted)
        vix_score_series.append(round((1 - p) * 100, 2))

    # Step 8: Sentiment Index = blend
    sentiment_series = []
    for gs, vs in zip(gtrends_score_series, vix_score_series):
        if gs is None and vs is None:
            sentiment_series.append(None)
        elif gs is None:
            sentiment_series.append(round(vs, 2))   # fallback : VIX seul
        elif vs is None:
            sentiment_series.append(round(gs, 2))   # fallback : GT seul
        else:
            sentiment_series.append(round(W_GTRENDS * gs + W_VIX * vs, 2))

    # ──────────────────────────────────────────────────────────────────
    # Decomposition actuelle (audit-ready)
    # ──────────────────────────────────────────────────────────────────
    last_idx = len(all_dates) - 1
    last_date = all_dates[last_idx]
    current_term_z = {kw: round(z(kw, last_date) or 0.0, 3) for kw in all_terms}

    current = {
        "date": last_date,
        "value": sentiment_series[last_idx],
        "prev_month": sentiment_series[last_idx - 1] if last_idx >= 1 else None,
        "prev_quarter": sentiment_series[last_idx - 3] if last_idx >= 3 else None,
        "prev_year": sentiment_series[last_idx - 12] if last_idx >= 12 else None,
        "components": {
            "gtrends_score": gtrends_score_series[last_idx],
            "vix_score": vix_score_series[last_idx],
            "bullish_z": round(bullish_z_series[last_idx] or 0.0, 3) if bullish_z_series[last_idx] is not None else None,
            "bearish_z": round(bearish_z_series[last_idx] or 0.0, 3) if bearish_z_series[last_idx] is not None else None,
            "capitulation_z": round(capitul_z_series[last_idx] or 0.0, 3) if capitul_z_series[last_idx] is not None else None,
            "gtrends_raw": round(gtrends_raw_series[last_idx] or 0.0, 3) if gtrends_raw_series[last_idx] is not None else None,
            "vix_value": vix.get(last_date),
            "vix_percentile": round((1 - vix_score_series[last_idx] / 100) * 100, 1) if vix_score_series[last_idx] is not None else None,
        },
        "term_z_scores": current_term_z,
    }

    # ──────────────────────────────────────────────────────────────────
    # Output structure
    # ──────────────────────────────────────────────────────────────────
    out = {
        "schema_version": "1.0",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "SerpAPI gtrends_cache.json + FRED VIXCLS",
        "method": {
            "buckets": TERM_MAPPING,
            "term_count": {"bullish": len(TERM_MAPPING["bullish"]),
                           "bearish": len(TERM_MAPPING["bearish"]),
                           "capitulation": len(TERM_MAPPING["capitulation"])},
            "weights": {"W_BULLISH": W_BULLISH, "W_BEARISH": W_BEARISH, "W_CAPITUL": W_CAPITUL,
                        "W_GTRENDS": W_GTRENDS, "W_VIX": W_VIX},
            "normalization": "z-score EXPANDING par terme (mu/sigma sur [emerge_terme, t], MIN_WINDOW=12), puis percentile rank EXPANDING — NO LOOK-AHEAD",
            "scale": "0 = peur extreme, 100 = euphorie (style Fear & Greed)",
            "hist_start": HIST_START,
        },
        "term_stats": {kw: {"mean": round(s["mean"], 2), "std": round(s["std"], 2),
                            "emerge": s.get("emerge")}
                       for kw, s in term_stats.items() if s["mean"] is not None and s["std"] is not None},
        "monthly": {
            "dates": all_dates,
            "sentiment_index": sentiment_series,
            "gtrends_score": gtrends_score_series,
            "vix_score": vix_score_series,
            "bullish_z": [round(x, 3) if x is not None else None for x in bullish_z_series],
            "bearish_z": [round(x, 3) if x is not None else None for x in bearish_z_series],
            "capitulation_z": [round(x, 3) if x is not None else None for x in capitul_z_series],
            "gtrends_raw": [round(x, 3) if x is not None else None for x in gtrends_raw_series],
            # Projections percentile 0-100 (visualisation audit uniquement, pas dans le calcul)
            "bullish_pct": bullish_pct_series,
            "bearish_pct": bearish_pct_series,          # inversé : haut bearish_z = bas sur axe 0-100
            "capitulation_pct": capitul_pct_series,     # inversé idem
            # S&P 500 mensuel aligne sur les dates sentiment (source Yahoo Finance ^GSPC 2004+)
            "sp500": align_sp500(all_dates, sp500),
            # Decomposition par terme individuel (19 termes, percentile 0-100, F&G compatible)
            "terms": term_pct_series,
        },
        "current": current,
    }
    return out


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────
def sanity_check(out):
    """Garde-fous : verifie que la sortie est dans les bornes attendues.
    Echec = lever une exception pour eviter d'ecrire un cache corrompu.
    Warning = log mais on continue (utile pour mouvements legitimes).
    """
    cur = out["current"]["value"]
    if cur is None or not isinstance(cur, (int, float)) or not (0 <= cur <= 100):
        raise ValueError(f"SANITY FAIL: current.value={cur} hors [0,100]")

    si = out["monthly"]["sentiment_index"]
    n_total = len(si)
    n_null = sum(1 for v in si if v is None)
    pct_null = n_null / n_total if n_total else 1
    if pct_null > 0.15:  # plus de 15% null = anomalie suspecte
        raise ValueError(f"SANITY FAIL: {n_null}/{n_total} valeurs null ({pct_null:.1%} > 15%)")

    prev = out["current"].get("prev_month")
    if prev is not None and abs(cur - prev) > 25:
        log(f"  ⚠ SANITY WARN: ecart M-1 anormal {cur:.1f} vs {prev:.1f} ({cur-prev:+.1f} pts) — verifier la qualite des donnees GTrends/VIX")

    # Verifie que les 19 termes sont presents et avec des stats
    ts = out.get("term_stats", {})
    n_terms_with_stats = len([k for k, v in ts.items() if v.get("mean") is not None])
    if n_terms_with_stats < 15:
        raise ValueError(f"SANITY FAIL: {n_terms_with_stats}/19 termes ont des stats valides")

    log(f"  Sanity OK : cur={cur:.1f}, {n_null} null ({pct_null:.1%}), {n_terms_with_stats}/19 termes valides")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="force re-fetch VIX (ignore TTL 24h)")
    args = ap.parse_args()

    fd = acquire_lock()
    try:
        if args.force and VIX_CACHE.exists():
            VIX_CACHE.unlink()

        out = compute_sentiment_index()
        sanity_check(out)

        # Ecriture atomique
        tmp = OUT_CACHE.with_suffix(OUT_CACHE.suffix + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False))
        tmp.replace(OUT_CACHE)
        log(f"=> {OUT_CACHE} ({OUT_CACHE.stat().st_size:,} bytes)")

        meta = {
            "updated_at": out["updated_at"],
            "n_months": len(out["monthly"]["dates"]),
            "date_min": out["monthly"]["dates"][0],
            "date_max": out["monthly"]["dates"][-1],
            "current_value": out["current"]["value"],
            "current_date": out["current"]["date"],
            "source": out["source"],
        }
        OUT_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        log(f"=> {OUT_META}")

        # Resume console
        c = out["current"]
        log("")
        log("=" * 65)
        log(f"Sentiment Index @ {c['date']} = {c['value']}/100")
        prev = c.get("prev_month")
        if prev is not None:
            log(f"  vs M-1 : {prev} ({c['value'] - prev:+.2f})")
        log(f"  GTrends_Score = {c['components']['gtrends_score']}")
        log(f"    bullish_z      = {c['components']['bullish_z']}")
        log(f"    bearish_z      = {c['components']['bearish_z']}")
        log(f"    capitulation_z = {c['components']['capitulation_z']}")
        log(f"    gtrends_raw    = {c['components']['gtrends_raw']}")
        log(f"  VIX_Score = {c['components']['vix_score']} (VIX={c['components']['vix_value']}, pct={c['components']['vix_percentile']}%)")
        log("=" * 65)

    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
