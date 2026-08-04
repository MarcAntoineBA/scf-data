#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Bitcoin et actifs rares.

Sources auditables :
  1. CoinGecko · /coins/bitcoin/market_chart — BTC depuis 2013 (live)
  2. FRED · GOLDAMGBD228NLBM — Or LBMA daily depuis 1968 (live)
  3. FRED · M2SL — Masse monétaire M2 US (live)
  4. FRED · DGS10 / DFII10 — Taux nominaux + TIPS (live)
  5. World Gold Council — Réserves d'or BC (hardcoded)
  6. Bitcoin halving timeline (hardcoded — historique on-chain)
  7. ETF Bitcoin spot — AUM cumulé US + HK (hardcoded BlackRock/Fidelity/etc.)
  8. Comparaison stocks-to-flow or vs BTC (hardcoded calculs S2F)

Sortie : these_bitcoin_cache.json + .js
Lancé par scf.these_bitcoin.refresh (4×/jour).
"""
import csv
import io
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_bitcoin_cache.json"
OUT_JS   = CACHE_DIR / "these_bitcoin_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseBitcoin/1.0"

# ════════════════════════════════════════════════════════════════
# HARDCODED DATASETS
# ════════════════════════════════════════════════════════════════

# Bitcoin halving timeline — réduction par 2 de l'émission tous les ~4 ans
# Source : on-chain block height records
BTC_HALVINGS = [
    # (date, block, reward_btc, price_at_halving_usd, comment)
    ("2009-01-03",      0,  50.0,    0.00, "Genesis block · Satoshi"),
    ("2012-11-28", 210000,  25.0,   12.20, "1er halving · 25 BTC/block"),
    ("2016-07-09", 420000,  12.5,  657.00, "2e halving · BTC à 657 $"),
    ("2020-05-11", 630000,   6.25, 8800.0, "3e halving · pré-Covid"),
    ("2024-04-19", 840000,   3.125, 64000.0, "4e halving · ATH 100k+ ensuite"),
    ("2028-04-30", 1050000, 1.5625, 0.0, "5e halving projection"),
]

# Réserves d'or des banques centrales (tonnes, fin année) — top 10
# Source : https://www.gold.org/goldhub/data/monthly-central-bank-statistics
CB_GOLD_RESERVES_T = [
    # (country, tonnes_2024, share_pct_reserves)
    ("USA",          8133.5, 70.6),
    ("Allemagne",    3351.5, 71.5),
    ("Italie",       2452.0, 67.0),
    ("France",       2436.9, 67.6),
    ("Russie",       2335.9, 28.5),
    ("Chine",        2280.4, 5.3),
    ("Suisse",       1040.0, 6.8),
    ("Japon",         845.9, 4.7),
    ("Inde",          876.0, 11.5),
    ("Pays-Bas",      612.5, 60.7),
]

# Stocks-to-flow comparison or vs BTC (S2F = stock / annual production)
# Sources : World Gold Council + Glassnode
STOCKS_TO_FLOW = [
    # (asset, stock_units, annual_flow, s2f_ratio)
    ("Or",         "212 580 t",      "3 644 t/an",     58),
    ("Argent",     "1 750 000 t",    "26 000 t/an",    67),
    ("Platine",    "6 250 t",        "190 t/an",       33),
    ("Palladium",  "2 800 t",        "210 t/an",       13),
    ("Bitcoin·2020", "18,4 M BTC",   "0,656 M/an",     28),
    ("Bitcoin·2024", "19,8 M BTC",   "0,328 M/an",     60),
    ("Bitcoin·2028", "20,3 M BTC",   "0,164 M/an",    124),
    ("Bitcoin·2032", "20,6 M BTC",   "0,082 M/an",    251),
    ("USD M2",     "21 600 Md$",     "1 400 Md$/an",   15),
    ("EUR M2",     "16 200 Md€",     "900 Md€/an",     18),
]

# ETF Bitcoin spot · AUM cumulés depuis approbation SEC 11 janv 2024
# Source : SEC + Bloomberg James Seyffart
BTC_SPOT_ETF_AUM = [
    # (issuer, ticker, aum_bn_usd, jurisdiction)
    ("BlackRock",      "IBIT",   55.8, "US"),
    ("Fidelity",       "FBTC",   18.4, "US"),
    ("Grayscale",      "GBTC",   17.2, "US"),
    ("ARK 21Shares",   "ARKB",    4.6, "US"),
    ("Bitwise",        "BITB",    3.8, "US"),
    ("Invesco",        "BTCO",    1.2, "US"),
    ("VanEck",         "HODL",    0.9, "US"),
    ("Franklin",       "EZBC",    0.6, "US"),
    ("ChinaAMC HK",    "3042",    0.8, "HK"),
    ("Harvest HK",     "3439",    0.4, "HK"),
]

# Volatilité comparée annualisée (%, 2024)
# Sources : Bloomberg + Glassnode
VOLATILITY_COMPARE = [
    # (asset, vol_2024_pct)
    ("Bitcoin",       45.0),
    ("Ethereum",      62.0),
    ("Nasdaq 100",    18.5),
    ("S&P 500",       14.2),
    ("Or",             12.8),
    ("Argent",        25.0),
    ("DXY",            6.4),
    ("EUR/USD",        7.1),
    ("Pétrole WTI",   31.0),
]

# Corrélation BTC / autres actifs (rolling 90j moyenne)
# Source : Glassnode + IntoTheBlock
BTC_CORRELATIONS_2024 = [
    # (asset, corr)
    ("Nasdaq 100",   0.55),
    ("S&P 500",      0.47),
    ("Or",           0.18),
    ("DXY",         -0.32),
    ("VIX",         -0.28),
    ("US 10Y yield", 0.05),
    ("Brent",        0.21),
    ("MSCI EM",      0.41),
]

# BTC monthly price snapshot — fallback offline (close du dernier jour du mois, USD)
# Source : CoinGecko archives + Glassnode (2013-2025)
BTC_MONTHLY_FALLBACK = [
    ("2013-01-31", 20),    ("2013-06-30", 95),    ("2013-12-31", 758),
    ("2014-06-30", 642),   ("2014-12-31", 320),   ("2015-06-30", 263),
    ("2015-12-31", 430),   ("2016-06-30", 670),   ("2016-12-31", 963),
    ("2017-06-30", 2480),  ("2017-12-31", 13880), ("2018-06-30", 6385),
    ("2018-12-31", 3742),  ("2019-06-30", 10760), ("2019-12-31", 7194),
    ("2020-06-30", 9136),  ("2020-12-31", 28990), ("2021-06-30", 35040),
    ("2021-12-31", 46306), ("2022-06-30", 19785), ("2022-12-31", 16547),
    ("2023-06-30", 30440), ("2023-12-31", 42265), ("2024-04-30", 60636),
    ("2024-06-30", 62800), ("2024-12-31", 93714), ("2025-01-31", 102429),
    ("2025-06-30", 108200), ("2025-12-31", 121400), ("2026-05-15", 138500),
]

# Or LBMA monthly fallback (USD/oz)
# Source : LBMA + FRED GOLDAMGBD228NLBM (archives manuelles)
GOLD_MONTHLY_FALLBACK = [
    ("2013-01-31", 1664), ("2013-12-31", 1202), ("2014-12-31", 1199),
    ("2015-12-31", 1060), ("2016-12-31", 1151), ("2017-12-31", 1296),
    ("2018-12-31", 1281), ("2019-12-31", 1517), ("2020-12-31", 1887),
    ("2021-12-31", 1806), ("2022-12-31", 1824), ("2023-12-31", 2062),
    ("2024-06-30", 2326), ("2024-12-31", 2624), ("2025-06-30", 3050),
    ("2025-12-31", 3185), ("2026-05-15", 3360),
]

# M2 US annuel fallback (Md$, FRED M2SL fin année)
M2_US_FALLBACK = [
    ("2013-12-01", 10987), ("2014-12-01", 11669), ("2015-12-01", 12340),
    ("2016-12-01", 13247), ("2017-12-01", 13832), ("2018-12-01", 14376),
    ("2019-12-01", 15315), ("2020-12-01", 19131), ("2021-12-01", 21560),
    ("2022-12-01", 21449), ("2023-12-01", 20832), ("2024-12-01", 21570),
    ("2025-12-01", 22390), ("2026-04-01", 22850),
]

# Adoption institutionnelle BTC — événements clés
INSTITUTIONAL_MILESTONES = [
    ("2020-08-11", "MicroStrategy achète 250 M$ de BTC (Saylor)"),
    ("2020-10-08", "Square (Block) achète 50 M$ de BTC"),
    ("2021-02-08", "Tesla achète 1,5 Md$ de BTC"),
    ("2021-09-07", "Salvador adopte BTC comme monnaie légale"),
    ("2024-01-11", "SEC approuve 11 ETF BTC spot américains"),
    ("2024-12-05", "BTC franchit 100 000 $"),
    ("2025-01-20", "Trump signe décret pro-crypto · Strategic BTC Reserve étude"),
    ("2025-07-15", "BlackRock IBIT dépasse 50 Md$ AUM"),
]

# ════════════════════════════════════════════════════════════════
# §2 — MATRICE DES PROPRIÉTÉS DE LA MONNAIE SAINE
# Notation 0-3 (0 nul · 1 faible · 2 bon · 3 excellent) sur les attributs
# qui font qu'un bien devient monnaie. Grille interprétative mais défendable,
# cadre Lyn Alden « What is Money? » + Saifedean Ammous « The Bitcoin Standard ».
# Lecture : Bitcoin est le seul actif à réunir TOUTES les propriétés — sauf
# l'antériorité (16 ans face aux millénaires de l'or). C'est l'argument honnête.
# ════════════════════════════════════════════════════════════════
MONETARY_PROPERTIES_COLS = [
    "Rareté absolue", "Durabilité", "Portabilité", "Divisibilité",
    "Vérifiabilité", "Résistance à la saisie", "Sans contrepartie", "Antériorité",
]
MONETARY_PROPERTIES = [
    # (asset, [scores dans l'ordre des colonnes ci-dessus])
    ("Bitcoin",        [3, 3, 3, 3, 3, 3, 3, 1]),
    ("Or",             [2, 3, 1, 1, 2, 1, 3, 3]),
    ("Immobilier",     [2, 2, 0, 0, 2, 0, 1, 3]),
    ("Actions",        [1, 1, 2, 3, 2, 1, 0, 2]),
    ("Fiat (USD)",     [0, 2, 3, 3, 2, 0, 0, 1]),
]

# ════════════════════════════════════════════════════════════════
# §3 — FENÊTRE DE MONÉTISATION · marché adressable de la réserve de valeur
# Capitalisations en milliers de Md$ (T$). Bitcoin est calculé LIVE côté JS
# (prix Yahoo × offre en circulation), les agrégats de référence sont sourcés.
# Le message : le BTC n'a capté qu'une fraction de la réserve de valeur
# mondiale — la migration est en cours, pas terminée.
# ════════════════════════════════════════════════════════════════
BTC_CIRCULATING_SUPPLY_M = 19.9   # millions de BTC en circulation (~2026) · blockchain.com
MARKET_CAPS_TN = [
    # (label, value_tn, note, live_from_btc)
    ("Bitcoin",               None,  "prix Yahoo live × ~19,9 M BTC en circulation", True),
    ("Or (stock mondial)",    22.5,  "~216 000 t au-dessus du sol × prix spot · WGC", False),
    ("M2 — États-Unis",       22.8,  "masse monétaire large US · FRED M2SL", False),
    ("Actions — mondial",    115.0,  "capitalisation boursière mondiale · SIFMA 2024", False),
    ("Monnaie large mondiale",120.0, "M2/M3 agrégé toutes devises · BIS", False),
    ("Obligations — mondial",140.0,  "encours du marché obligataire mondial · SIFMA", False),
    ("Immobilier — mondial", 330.0,  "valeur du parc résidentiel mondial · Savills", False),
]

# ════════════════════════════════════════════════════════════════
# §5 — ANTIFRAGILITÉ : chocs traversés + budget de sécurité (hash rate)
# Chaque choc majeur a été absorbé sans interruption du réseau ; le hash rate
# (donc le coût d'une attaque) ressort plus haut après chaque crise.
# Sources : 99bitcoins obituaries, blockchain.com hash-rate, presse.
# ════════════════════════════════════════════════════════════════
NETWORK_RESILIENCE = [
    # (année, choc, état du réseau après)
    ("2014", "Faillite Mt.Gox · 850 000 BTC perdus, −58 % sur l'année",
             "Aucun bloc manqué · hash rate ×30 dans les 24 mois"),
    ("2017", "Interdiction des ICO en Chine + scission Bitcoin Cash",
             "BTC garde la chaîne dominante · hash rate à l'ATH"),
    ("2020", "Krach Covid : −50 % en 24 h (« jeudi noir »)",
             "Réseau ininterrompu · nouveaux sommets 12 mois après"),
    ("2021", "Interdiction TOTALE du minage en Chine (≈ 50 % du hash rate)",
             "Hash rate −50 % puis ATH en 6 mois · migration vers les US"),
    ("2022", "Effondrements en chaîne : Luna, Celsius, FTX",
             "Protocole intact · la contagion a frappé le CeFi, pas la chaîne"),
    ("2024", "Bitcoin déclaré « mort » pour la ~480ᵉ fois depuis 2010",
             "Disponibilité du réseau ≈ 99,98 % sur 16 ans"),
]
# Hash rate annuel approximatif (EH/s) — budget de sécurité du réseau.
# Source : blockchain.com/charts/hash-rate (valeurs de référence annuelles).
HASHRATE_ANNUAL = [
    ("2014-01-01",   0.01), ("2015-01-01", 0.30), ("2016-01-01", 1.0),
    ("2017-01-01",   2.5),  ("2018-01-01", 16.0), ("2019-01-01", 40.0),
    ("2020-01-01", 100.0),  ("2021-04-01", 180.0),
    ("2021-07-01",  85.0),  # creux post-interdiction Chine
    ("2022-01-01", 190.0),  ("2023-01-01", 270.0), ("2024-01-01", 500.0),
    ("2025-01-01", 780.0),  ("2026-01-01", 920.0),
]


def http_get_text(url, timeout=25, max_retries=4, accept="application/json,*/*"):
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
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


# FRED via API officielle (la version CSV graph est cassée depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred as fetch_fred_csv  # noqa: E402


def fetch_yahoo_gold_monthly():
    """Gold front-month futures (GC=F) mensuel — remplacement de la série FRED
    GOLDAMGBD228NLBM (London AM Fixing) qui a été discontinuée."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=max&interval=1mo"
    try:
        data = json.loads(http_get_text(url, timeout=20, accept="application/json"))
    except Exception as e:
        sys.stderr.write(f"[YF GC=F] {e}\n"); return None
    try:
        r = data["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
    except (KeyError, TypeError, IndexError):
        return None
    dates, values = [], []
    for t, v in zip(ts, close):
        if v is None: continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        dates.append(d); values.append(round(v, 2))
    if not dates: return None
    return {"dates": dates, "values": values, "fred_id": "GC=F",
            "source_url": "https://finance.yahoo.com/quote/GC=F"}


def fetch_yahoo_btc():
    """BTC-USD daily depuis 2014 via Yahoo Finance (CoinGecko free tier ne donne
    plus que 365 jours). Échantillonnage weekly pour limiter la taille."""
    # 2014-09-18 (inception YF) à 2030
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
           "?period1=1410998400&period2=1893456000&interval=1d")
    try:
        data = json.loads(http_get_text(url, timeout=20, accept="application/json"))
        r = data["chart"]["result"][0]
        ts = r["timestamp"]
        close = r["indicators"]["quote"][0]["close"]
    except Exception as e:
        sys.stderr.write(f"[YF BTC-USD] {e}\n"); return None
    dates, values = [], []
    for t, v in zip(ts, close):
        if v is None: continue
        d = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")
        dates.append(d); values.append(round(float(v), 2))
    if not dates: return None
    # Sous-échantillonnage weekly (1 sur 7 + dernier point)
    keep_d, keep_v = [], []
    for i, d in enumerate(dates):
        if i == 0 or i == len(dates) - 1 or i % 7 == 0:
            keep_d.append(d); keep_v.append(values[i])
    return {"dates": keep_d, "values": keep_v,
            "source_url": "https://finance.yahoo.com/quote/BTC-USD/",
            "unit": "USD"}


def build_payload():
    ok, failed = [], []

    btc_px = fetch_yahoo_btc()
    if btc_px: ok.append("YF:BTC-USD")
    else:
        failed.append("YF:BTC-USD")
        btc_px = {"dates":   [d for d, v in BTC_MONTHLY_FALLBACK],
                   "values": [v for d, v in BTC_MONTHLY_FALLBACK],
                   "source_url": "fallback hardcoded · CoinGecko archives",
                   "unit": "USD", "stale": True}

    # Or : la série FRED GOLDAMGBD228NLBM a été discontinuée, remplacée par
    # Yahoo Finance GC=F (gold futures front-month, équivalent spot).
    gold_px = fetch_yahoo_gold_monthly()
    if gold_px: ok.append("YF:GC=F")
    else:
        failed.append("YF:GC=F")
        gold_px = {"dates":   [d for d, v in GOLD_MONTHLY_FALLBACK],
                    "values": [v for d, v in GOLD_MONTHLY_FALLBACK],
                    "source_url": "fallback hardcoded · LBMA archives",
                    "unit": "USD/oz", "stale": True}

    m2 = fetch_fred_csv("M2SL", start="1980-01-01")
    if m2: ok.append("FRED:M2SL")
    else:
        failed.append("FRED:M2SL")
        m2 = {"dates":   [d for d, v in M2_US_FALLBACK],
              "values": [v for d, v in M2_US_FALLBACK],
              "source_url": "fallback hardcoded · FRED M2SL archives",
              "unit": "Md$", "stale": True}

    real_rate = fetch_fred_csv("DFII10", start="2003-01-01")
    if real_rate: ok.append("FRED:DFII10")
    else: failed.append("FRED:DFII10")

    meta = {
        "updated_at":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "doc_version":     "1.0",
    }
    payload = {
        "meta": meta,
        "btc_price":   btc_px,
        "gold_price":  gold_px,
        "m2_us":       m2,
        "real_rate_10y": real_rate,
        "btc_halvings": [{"date": d, "block": b, "reward": r, "price_usd": p, "comment": c}
                          for d, b, r, p, c in BTC_HALVINGS],
        "cb_gold_reserves": [{"country": c, "tonnes": t, "share_pct": s}
                              for c, t, s in CB_GOLD_RESERVES_T],
        "stocks_to_flow":  [{"asset": a, "stock": s, "flow": f, "s2f": r}
                            for a, s, f, r in STOCKS_TO_FLOW],
        "btc_etf_aum":     [{"issuer": i, "ticker": t, "aum_bn": a, "jurisdiction": j}
                            for i, t, a, j in BTC_SPOT_ETF_AUM],
        "volatility_compare": [{"asset": a, "vol_pct": v} for a, v in VOLATILITY_COMPARE],
        "btc_correlations":  [{"asset": a, "corr": c} for a, c in BTC_CORRELATIONS_2024],
        "institutional_milestones": [{"date": d, "event": e}
                                      for d, e in INSTITUTIONAL_MILESTONES],
        "monetary_properties": {
            "properties": MONETARY_PROPERTIES_COLS,
            "assets": [{"asset": a, "scores": s} for a, s in MONETARY_PROPERTIES],
            "source_url": "https://www.lynalden.com/what-is-money/",
        },
        "btc_supply_m": BTC_CIRCULATING_SUPPLY_M,
        "market_caps": [{"label": l, "value_tn": v, "note": n, "live_from_btc": b}
                         for l, v, n, b in MARKET_CAPS_TN],
        "network_resilience": [{"year": y, "shock": s, "outcome": o}
                                for y, s, o in NETWORK_RESILIENCE],
        "hashrate_annual": [{"date": d, "ehs": h} for d, h in HASHRATE_ANNUAL],
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
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_bitcoin] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
