#!/usr/bin/env python3
"""
Build cache JS pour l'onglet INDICATEUR (Radar v3).

Lit :
  - radar_v3_history.json (scores agrégés)
  - radar_signals_history.json (valeurs brutes des 12 signaux)

Produit :
  - ~/Library/Caches/site_crypto_finance/radar_v3_cache.js (symlink Desktop/radar_v3_cache.js)

Schéma de sortie (window.__RADAR_V3__) :
  - scores: [{d, s, p, n, r7, r30, r90}]
  - signals_today: {signal_id: {value, zscore, contribution, sense, label, sign}}
  - performance: {bucket: {count, ret7_med, ret30_med, ret90_med, win7, win30, win90}}
  - formula: {method, signals[], signs[], weights[], rolling_window, ...}
  - validation: {ic_train, ic_valid, ratio, sign_stable}
  - signals_meta: liste des 12 signaux avec sens + description courte/longue
"""
import json
import math
from pathlib import Path
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parent
HIST_V3 = ROOT / "radar_v3_history.json"
SIGNALS_HIST = ROOT / "radar_signals_history.json"
FORMULA_MD = ROOT / "radar_v3_formula.md"
OUT_JS = Path.home() / "Library" / "Caches" / "site_crypto_finance" / "radar_v3_cache.js"

# Les 12 signaux retenus (ordre canonique de radar_v3_formula.md)
V3_SIGNALS = [
    "funding_btc_avg",
    "funding_btc_daily",
    "funding_eth_avg",
    "stables_growth_30d",
    "stables_growth_7d",
    "oi_btc_usd_close",
    "oi_btc_usd_avg",
    "ls_count_btc",
    "ls_top_count_btc",
    "ls_top_position_btc",
    "btc_eth_ratio",
    "btc_vol",
]

# Sens de chaque signal (depuis radar_v3_formula.md "signs")
V3_SIGNS = [-1, -1, 1, 1, 1, -1, -1, -1, -1, -1, 1, 1]

V3_WEIGHT = 1.0 / 12  # equal-weighted
ROLLING_WINDOW = 365
Z_CAP = 3.0

# Métadonnées d'affichage par signal
SIGNALS_META = {
    "funding_btc_avg": {
        "label": "Funding BTC (moyenne)",
        "short": "Funding rate BTC moyen",
        "long": "Funding > 0 = longs payent shorts → euphorie longs → bearish prochaines semaines (mean-reversion)",
        "icon": "fa-rotate",
        "category": "Funding",
    },
    "funding_btc_daily": {
        "label": "Funding BTC (daily)",
        "short": "Funding rate BTC du jour",
        "long": "Confirmation court-terme du signal funding moyenne",
        "icon": "fa-bolt",
        "category": "Funding",
    },
    "funding_eth_avg": {
        "label": "Funding ETH (moyenne)",
        "short": "Funding rate ETH moyen",
        "long": "ETH funding positif et stable = appétit risque crypto-natif → bullish (momentum)",
        "icon": "fa-rotate",
        "category": "Funding",
    },
    "stables_growth_30d": {
        "label": "Stables 30j",
        "short": "Croissance market cap stablecoins (30j)",
        "long": "Hausse stables = liquidité entrante en crypto → bullish",
        "icon": "fa-coins",
        "category": "Liquidité",
    },
    "stables_growth_7d": {
        "label": "Stables 7j",
        "short": "Croissance stables (7j)",
        "long": "Confirmation court-terme du signal stables 30j",
        "icon": "fa-coins",
        "category": "Liquidité",
    },
    "oi_btc_usd_close": {
        "label": "OI BTC (close)",
        "short": "Open Interest BTC en USD à la clôture",
        "long": "OI haut = surchauffe levier → correction probable (mean-reversion)",
        "icon": "fa-chart-area",
        "category": "Position",
    },
    "oi_btc_usd_avg": {
        "label": "OI BTC (moyenne)",
        "short": "Open Interest BTC moyen sur fenêtre",
        "long": "Confirmation OI close",
        "icon": "fa-chart-area",
        "category": "Position",
    },
    "ls_count_btc": {
        "label": "L/S Count BTC",
        "short": "Ratio nombre comptes long/short BTC",
        "long": "Trop de comptes long = euphorie retail → bearish (contrepied)",
        "icon": "fa-people-arrows",
        "category": "Sentiment",
    },
    "ls_top_count_btc": {
        "label": "L/S Top Count BTC",
        "short": "Ratio comptes long/short top traders BTC",
        "long": "Top traders trop long → bearish (ils prennent souvent le contrepied)",
        "icon": "fa-crown",
        "category": "Sentiment",
    },
    "ls_top_position_btc": {
        "label": "L/S Top Position BTC",
        "short": "Ratio long/short en taille (top traders)",
        "long": "Idem mais en taille de position effective, pas en nombre de comptes",
        "icon": "fa-crown",
        "category": "Sentiment",
    },
    "btc_eth_ratio": {
        "label": "Ratio BTC/ETH",
        "short": "Ratio des prix BTC/ETH",
        "long": "Ratio BTC/ETH ÉLEVÉ = BTC surperforme ETH (régime BTC-dominant) → historiquement bullish pour BTC à 30j (momentum, IC +0.24). Ratio bas (ETH surperforme) = bearish BTC.",
        "icon": "fa-scale-balanced",
        "category": "Marché",
    },
    "btc_vol": {
        "label": "Volume BTC",
        "short": "Volume d'échange BTC quotidien (klines spot, en BTC)",
        "long": "Volume élevé = participation/activité du marché en hausse → momentum haussier (IC +0.11). NB : c'est le VOLUME échangé, pas la volatilité.",
        "icon": "fa-chart-column",
        "category": "Marché",
    },
}

# Métadonnées de validation (depuis radar_v3_validation.md)
VALIDATION = {
    "ic_train": 0.4018,
    "ic_valid": 0.3526,
    "ratio_valid_train": 0.88,
    "sign_stable": True,
    "method": "equal",
    "train_period": ["2018-02-01", "2023-08-31"],
    "valid_period": ["2023-09-01", "2024-08-31"],
    "test_period": ["2024-09-01", "2026-04-25"],
    "n_signals": 12,
    "rolling_window": ROLLING_WINDOW,
    "z_cap": Z_CAP,
}

# IC train par signal (depuis radar_v3_validation.md)
SIGNAL_IC = {
    "funding_btc_avg": -0.119,
    "funding_btc_daily": -0.118,
    "funding_eth_avg": 0.062,
    "stables_growth_30d": 0.091,
    "stables_growth_7d": 0.100,
    "oi_btc_usd_close": -0.558,
    "oi_btc_usd_avg": -0.558,
    "ls_count_btc": -0.205,
    "ls_top_count_btc": -0.293,
    "ls_top_position_btc": -0.194,
    "btc_eth_ratio": 0.239,
    "btc_vol": 0.106,
}


def rolling_zscore_series(values_dates, lookback=365, cap=3.0):
    """Calcule z-score rolling pour une série temporelle (date, value).
    Retourne {date: zscore} (zscore None si pas assez de données)."""
    out = {}
    vals = []
    for d, v in values_dates:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out[d] = None
            continue
        if len(vals) >= lookback:
            window = vals[-lookback:]
            m = float(np.mean(window))
            sd = float(np.std(window, ddof=1)) or 1.0
            z = max(-cap, min(cap, (v - m) / sd))
            out[d] = z
        else:
            out[d] = None
        vals.append(v)
    return out


def percentile(arr, p):
    if not arr:
        return 0
    sorted_arr = sorted(arr)
    idx = int(p * (len(sorted_arr) - 1))
    return sorted_arr[idx]


def load_formula(path):
    """Parse le bloc JSON de radar_v3_formula.md (signaux/signes/poids/méthode réels).
    Retourne {} si illisible → fallback sur les constantes hardcodées."""
    import re
    try:
        m = re.search(r"\{.*\}", path.read_text(encoding="utf-8"), re.DOTALL)
        return json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"  [WARN] formula.md illisible ({e})")
        return {}


def meta_for(sig):
    """Métadonnées d'affichage d'un signal, avec fallback si non documenté
    (le pipeline peut re-sélectionner un signal hors des 12 canoniques)."""
    return SIGNALS_META.get(sig, {
        "label": sig, "short": sig, "long": "",
        "icon": "fa-chart-simple", "category": "Autre",
    })


def main():
    print("[1/5] Lecture des fichiers source...")
    hist_v3 = json.load(open(HIST_V3))
    sigs_hist = json.load(open(SIGNALS_HIST))
    print(f"  hist_v3        : {len(hist_v3)} entrées")
    print(f"  signals_hist   : {len(sigs_hist)} entrées")

    # Index par date pour merge rapide
    sigs_by_date = {row["date"]: row for row in sigs_hist}

    # ---- Modèle réel depuis formula.md (évite la dérive vs constantes hardcodées) ----
    formula = load_formula(FORMULA_MD)
    if formula.get("selected_signals"):
        v3_signals = formula["selected_signals"]
        v3_signs = formula["signs"]
        v3_weights = formula["weights"]
        print(f"  Modèle (formula.md) : {formula.get('method')} · {len(v3_signals)} signaux")
    else:
        v3_signals, v3_signs = V3_SIGNALS, V3_SIGNS
        v3_weights = [V3_WEIGHT] * len(V3_SIGNALS)
        print(f"  Modèle (fallback hardcodé) : {len(v3_signals)} signaux")
    validation = {
        "method": formula.get("method", "equal"),
        "n_signals": len(v3_signals),
        "ic_train": formula.get("ic_train", VALIDATION["ic_train"]),
        "ic_valid": formula.get("ic_valid", VALIDATION["ic_valid"]),
        "ratio_valid_train": round(formula["ic_valid"] / formula["ic_train"], 2)
            if formula.get("ic_train") else VALIDATION["ratio_valid_train"],
        "rolling_window": formula.get("rolling_window", ROLLING_WINDOW),
        "z_cap": formula.get("z_cap", Z_CAP),
        "train_period": formula.get("train_period", VALIDATION["train_period"]),
        "valid_period": formula.get("valid_period", VALIDATION["valid_period"]),
        "sign_stable": True,
    }

    # ---- Calcul des z-scores rolling pour chaque signal (sur 365j) ----
    print("[2/5] Calcul des z-scores rolling 365j pour chaque signal...")
    zscore_by_signal = {}
    for sig in v3_signals:
        vd = [(row["date"], row.get(sig)) for row in sigs_hist]
        zscore_by_signal[sig] = rolling_zscore_series(vd, lookback=ROLLING_WINDOW, cap=Z_CAP)
        valid_count = sum(1 for v in zscore_by_signal[sig].values() if v is not None)
        print(f"  {sig:30s} : {valid_count} z-scores valides")

    # ---- Cutoff : dernière date à couverture COMPLÈTE ----
    # Les métriques OI / Long-Short (data.binance.vision) sont publiées avec ~1 jour
    # de retard : la date la plus récente n'a souvent que 6/11 signaux, ce qui ampute
    # le score des facteurs à plus fort poids (chute artificielle de ~10 pts). On borne
    # le score titre ET le graphe scores[] à la dernière date dont n_signals_used ==
    # couverture max récente — sinon le graphe finit sur un faux décrochage (un point
    # partiel calculé sur 6 signaux apparaît comme une chute brutale au bord droit).
    scored_rows = [r for r in hist_v3 if r.get("radar_v3") is not None]
    if not scored_rows:
        print("  ERREUR : pas de score v3 valide trouvé")
        return
    max_cov = max(r.get("n_signals_used", 0) for r in scored_rows[-10:])
    last_score_row = next((r for r in reversed(scored_rows)
                           if r.get("n_signals_used", 0) >= max_cov), scored_rows[-1])
    today_date = last_score_row["date"]
    n_dropped = len(scored_rows) - 1 - scored_rows.index(last_score_row)

    # ---- Construction de scores[] (compact), borné au cutoff ----
    print("[3/5] Construction de scores[]...")
    scores = []
    for row in hist_v3:
        if row.get("radar_v3") is None:
            continue
        if row["date"] > today_date:
            continue  # date partielle en bout de série (lag OI/LS) → hors graphe
        scores.append({
            "d": row["date"],
            "s": round(row["radar_v3"], 1),
            "p": round(row["btc_close"], 2) if row.get("btc_close") else None,
            "n": row.get("n_signals_used", 12),
            "r7": round(row["ret_btc_7"], 2) if row.get("ret_btc_7") is not None else None,
            "r30": round(row["ret_btc_30"], 2) if row.get("ret_btc_30") is not None else None,
            "r90": round(row["ret_btc_90"], 2) if row.get("ret_btc_90") is not None else None,
        })
    print(f"  {len(scores)} scores valides (borné à {today_date})")

    # ---- Calcul performance par bucket ----
    print("[4/5] Calcul performance par bucket...")
    BUCKETS = [
        ("Tres Baissier (0-25)", lambda s: s < 25),
        ("Baissier (25-45)", lambda s: 25 <= s < 45),
        ("Neutre (45-55)", lambda s: 45 <= s < 55),
        ("Haussier (55-75)", lambda s: 55 <= s < 75),
        ("Tres Haussier (75-100)", lambda s: s >= 75),
    ]
    perf = {}
    for name, pred in BUCKETS:
        bucket = [r for r in scores if pred(r["s"])]
        r7 = [r["r7"] for r in bucket if r["r7"] is not None]
        r30 = [r["r30"] for r in bucket if r["r30"] is not None]
        r90 = [r["r90"] for r in bucket if r["r90"] is not None]
        win7 = (sum(1 for x in r7 if x > 0) / len(r7) * 100) if r7 else 0
        win30 = (sum(1 for x in r30 if x > 0) / len(r30) * 100) if r30 else 0
        win90 = (sum(1 for x in r90 if x > 0) / len(r90) * 100) if r90 else 0
        perf[name] = {
            "count": len(bucket),
            "ret7_med": round(median(r7), 2) if r7 else 0,
            "ret30_med": round(median(r30), 2) if r30 else 0,
            "ret90_med": round(median(r90), 2) if r90 else 0,
            "win7": round(win7, 1),
            "win30": round(win30, 1),
            "win90": round(win90, 1),
        }
        print(f"  {name:25s} : count={len(bucket):4d} ret7={perf[name]['ret7_med']:>+6.2f}% "
              f"ret30={perf[name]['ret30_med']:>+6.2f}% ret90={perf[name]['ret90_med']:>+6.2f}%")

    # ---- Construction de signals_today (valeurs courantes des 11 signaux) ----
    # cutoff (today_date / last_score_row / n_dropped) déjà calculé plus haut.
    print("[5/5] Construction de signals_today...")
    today_signals_raw = sigs_by_date.get(today_date, {})
    tail = f"  [{n_dropped} jour(s) partiel(s) ignoré(s)]" if n_dropped else ""
    print(f"  Date courante ({last_score_row.get('n_signals_used')} sig) : {today_date}, score v3 = {last_score_row['radar_v3']}{tail}")

    signals_today = {}
    contributions = []
    for i, sig in enumerate(v3_signals):
        raw = today_signals_raw.get(sig)
        z = zscore_by_signal[sig].get(today_date)
        sign = v3_signs[i]
        m = meta_for(sig)
        # Contribution au score : (z × sign) directement comme indicateur de force au moment T
        contribution = z * sign if z is not None else None
        sense = "Momentum" if sign == 1 else "Contrarian"
        signals_today[sig] = {
            "value": raw,
            "zscore": round(z, 3) if z is not None else None,
            "contribution": round(contribution, 3) if contribution is not None else None,
            "sense": sense,
            "sign": sign,
            "weight": round(v3_weights[i], 4),
            "label": m["label"],
            "short": m["short"],
            "long": m["long"],
            "icon": m["icon"],
            "category": m["category"],
            "ic_train": SIGNAL_IC.get(sig),
        }
        if contribution is not None:
            contributions.append((sig, contribution))
        z_str = f"{z:+.2f}" if z is not None else "n/a"
        print(f"  {sig:30s} sense={sense:11s} z={z_str} contrib={contribution if contribution is None else round(contribution, 2)}")

    # Top contributeurs (positifs et négatifs) pour la synthèse
    contributions_sorted = sorted(contributions, key=lambda x: x[1], reverse=True)
    top_bullish = [c[0] for c in contributions_sorted[:3] if c[1] > 0]
    top_bearish = [c[0] for c in contributions_sorted[-3:] if c[1] < 0][::-1]

    # ---- Métadonnées des signaux pour la doc table ----
    signals_meta_export = []
    for i, sig in enumerate(v3_signals):
        m = meta_for(sig)
        signals_meta_export.append({
            "id": sig,
            "label": m["label"],
            "category": m["category"],
            "icon": m["icon"],
            "sense": "Momentum" if v3_signs[i] == 1 else "Contrarian",
            "sign": v3_signs[i],
            "weight": round(v3_weights[i], 4),
            "ic_train": SIGNAL_IC.get(sig),
            "short": m["short"],
            "long": m["long"],
        })

    # ---- Sortie JS ----
    cache = {
        "updated": today_date,
        "signal_score": round(last_score_row["radar_v3"], 1),
        "btc_price": round(last_score_row["btc_close"], 2) if last_score_row.get("btc_close") else None,
        "scores": scores,
        "signals_today": signals_today,
        "signals_meta": signals_meta_export,
        "performance": perf,
        "validation": validation,
        "top_bullish": top_bullish,
        "top_bearish": top_bearish,
    }

    js_content = "window.__RADAR_V3__ = " + json.dumps(cache, ensure_ascii=False, separators=(",", ":")) + ";\n"
    OUT_JS.write_text(js_content, encoding="utf-8")
    size_kb = OUT_JS.stat().st_size / 1024
    print(f"\n[OK] Cache écrit dans {OUT_JS} ({size_kb:.1f} KB)")
    print(f"[OK] Score courant : {cache['signal_score']} (date: {cache['updated']})")
    print(f"[OK] Top bullish   : {top_bullish}")
    print(f"[OK] Top bearish   : {top_bearish}")


if __name__ == "__main__":
    main()
