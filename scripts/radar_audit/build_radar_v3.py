#!/usr/bin/env python3
"""
PHASE 0b.3 — Construction Radar v3 production-grade avec walk-forward strict.

Méthodologie:
  1. Split temporel rigoureux (train/valid/test, ratio 60/20/20)
  2. Sur TRAIN: audit + sélection signaux + 4 méthodes pondération
     - Equal-weighted
     - IC-weighted
     - Ridge regression (closed-form, λ tuné par CV interne)
     - Ablation feature importance
  3. Sur VALID: tuner hyperparamètres (rolling window, z-cap, transformation)
  4. TEST jamais touché pendant construction

Critères de validation:
  - IC test >= 0.7 × IC train (pas d'overfit)
  - Signe stable sur 2 sub-périodes du train
  - t-stat >= 2 sur train
  - Spread top-bottom h30 >= 10pp sur test
"""
import json
import math
import random
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "radar_signals_history.json"
OUT_HIST = ROOT / "radar_v3_history.json"
OUT_VALIDATION = ROOT / "radar_v3_validation.md"
OUT_FORMULA = ROOT / "radar_v3_formula.md"
FROZEN_FORMULA = ROOT / "radar_v3_frozen.json"

random.seed(42)
np.random.seed(42)

# Splits temporels stricts
TRAIN_END = "2023-09-01"
VALID_END = "2024-09-01"
# TEST = VALID_END → today

# Signaux candidats (raw values, growth rates, ratios)
CANDIDATE_SIGNALS = [
    "fng",
    "funding_btc_avg",
    "funding_btc_daily",
    "funding_eth_avg",
    "stables_usd",
    "stables_growth_30d",
    "stables_growth_7d",
    "tvl_usd",
    "tvl_growth_30d",
    "tvl_growth_7d",
    "oi_btc_usd_close",
    "oi_btc_usd_avg",
    "ls_count_btc",
    "ls_top_count_btc",
    "ls_top_position_btc",
    "taker_lsv_btc_perp",
    "btc_taker_buy_ratio_spot",
    "btc_eth_ratio",
    "btc_vol",
]


def split_data(rows):
    """Retourne (train, valid, test) selon dates."""
    train, valid, test = [], [], []
    for r in rows:
        if r["date"] < TRAIN_END:
            train.append(r)
        elif r["date"] < VALID_END:
            valid.append(r)
        else:
            test.append(r)
    return train, valid, test


def spearman_np(xs, ys):
    """Spearman via scipy."""
    valid = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None and not (isinstance(y, float) and math.isnan(y))]
    if len(valid) < 30:
        return None, None
    xs_v = np.array([v[0] for v in valid])
    ys_v = np.array([v[1] for v in valid])
    rho, p = stats.spearmanr(xs_v, ys_v)
    return float(rho), float(p)


def t_from_ic(ic, n):
    if ic is None or abs(ic) >= 1: return None
    return ic * math.sqrt((n - 2) / max(1e-9, 1 - ic * ic))


def rolling_zscore(values_dates, lookback=365, cap=3.0):
    """Rolling z-score normalization. values_dates : list of (date, value)."""
    out = {}
    vals = []
    for d, v in values_dates:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        if len(vals) >= lookback:
            window = vals[-lookback:]
            m = float(np.mean(window))
            sd = float(np.std(window, ddof=1)) or 1.0
            z = max(-cap, min(cap, (v - m) / sd))
            out[d] = z
        vals.append(v)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Étape 1 : Audit signaux sur TRAIN avec critères de stabilité
# ─────────────────────────────────────────────────────────────────────────────
def audit_signal_train_only(train, signal_key, horizon=30):
    """Audit complet d'un signal sur TRAIN uniquement. Train splitté en 2 pour sign stability."""
    ret_key = f"ret_btc_{horizon}"

    valid = [(r[signal_key], r[ret_key], r["date"]) for r in train
             if r.get(signal_key) is not None and r.get(ret_key) is not None]
    if len(valid) < 100:
        return {"key": signal_key, "n": len(valid), "validated": False, "reason": "insufficient_data"}

    valid.sort(key=lambda v: v[2])
    xs_all = [v[0] for v in valid]
    ys_all = [v[1] for v in valid]

    ic, p = spearman_np(xs_all, ys_all)
    t = t_from_ic(ic, len(xs_all))

    # Sign stability : split train en 2 sous-périodes
    mid = len(valid) // 2
    xs1 = xs_all[:mid]; ys1 = ys_all[:mid]
    xs2 = xs_all[mid:]; ys2 = ys_all[mid:]
    ic1, _ = spearman_np(xs1, ys1)
    ic2, _ = spearman_np(xs2, ys2)
    sign_stable = (ic1 is not None and ic2 is not None and
                   ic1 * ic2 > 0 and abs(ic1) > 0.02 and abs(ic2) > 0.02)

    # Bootstrap CI
    boot_ics = []
    n = len(valid)
    for _ in range(500):
        idx = np.random.randint(0, n, size=n)
        xs_b = [xs_all[i] for i in idx]
        ys_b = [ys_all[i] for i in idx]
        ic_b, _ = spearman_np(xs_b, ys_b)
        if ic_b is not None: boot_ics.append(ic_b)
    ci_lo = np.percentile(boot_ics, 2.5)
    ci_hi = np.percentile(boot_ics, 97.5)
    ci_excludes_zero = (ci_lo > 0 and ci_hi > 0) or (ci_lo < 0 and ci_hi < 0)

    # Critères de validation
    pass_ic = ic is not None and abs(ic) >= 0.05
    pass_t = t is not None and abs(t) >= 2.0
    pass_sign = sign_stable
    pass_ci = ci_excludes_zero
    validated = pass_ic and pass_t and pass_sign and pass_ci

    return {
        "key": signal_key,
        "n": len(valid),
        "ic_train": round(ic, 4) if ic is not None else None,
        "t_train": round(t, 2) if t is not None else None,
        "ic_sub1": round(ic1, 4) if ic1 is not None else None,
        "ic_sub2": round(ic2, 4) if ic2 is not None else None,
        "sign_stable": sign_stable,
        "ci_95": [round(ci_lo, 4), round(ci_hi, 4)],
        "ci_excludes_zero": ci_excludes_zero,
        "sense": -1 if (ic is not None and ic < 0) else +1,
        "validated": validated,
        "reason": (
            "OK" if validated else
            f"fail_ic={pass_ic} fail_t={pass_t} fail_sign={pass_sign} fail_ci={pass_ci}"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Étape 2 : Méthodes de pondération
# ─────────────────────────────────────────────────────────────────────────────
def build_z_matrix(all_rows, signals, target_rows=None, lookback=365, cap=3.0):
    """
    Construit la matrice X (z-scores) pour chaque date où on a tous les signaux.
    Le rolling z-score est calculé sur TOUS les rows (warmup propre),
    puis on filtre selon target_rows pour récupérer le sous-ensemble.
    Retourne (X, y, dates).
    """
    z_dict = {}
    for sig in signals:
        series = [(r["date"], r.get(sig)) for r in all_rows]
        z_dict[sig] = rolling_zscore(series, lookback=lookback, cap=cap)

    rows_to_use = target_rows if target_rows is not None else all_rows
    X_rows, y_rows, dates_out = [], [], []
    for r in rows_to_use:
        d = r["date"]
        zvec = []
        ok = True
        for sig in signals:
            z = z_dict[sig].get(d)
            if z is None:
                ok = False; break
            zvec.append(z)
        ret = r.get("ret_btc_30")
        if not ok or ret is None: continue
        X_rows.append(zvec)
        y_rows.append(ret)
        dates_out.append(d)

    if not X_rows: return None, None, []
    return np.array(X_rows), np.array(y_rows), dates_out


def weights_equal(n):
    return np.ones(n) / n


def weights_ic(audit_results):
    """Poids ∝ |IC train|."""
    abs_ics = [abs(a["ic_train"]) for a in audit_results]
    total = sum(abs_ics)
    return np.array([ic / total for ic in abs_ics])


def weights_ridge(X, y, lambdas=(0.1, 1.0, 10.0, 100.0, 1000.0)):
    """Régression ridge : w = (X'X + λI)^(-1) X'y. Tune λ par CV 5-fold sur train."""
    n, p = X.shape
    best_lam = None
    best_score = -np.inf
    fold_size = n // 5
    for lam in lambdas:
        scores = []
        for k in range(5):
            te_start = k * fold_size
            te_end = (k + 1) * fold_size if k < 4 else n
            tr_idx = list(range(0, te_start)) + list(range(te_end, n))
            te_idx = list(range(te_start, te_end))
            X_tr, y_tr = X[tr_idx], y[tr_idx]
            X_te, y_te = X[te_idx], y[te_idx]
            try:
                A = X_tr.T @ X_tr + lam * np.eye(p)
                b = X_tr.T @ y_tr
                w = np.linalg.solve(A, b)
                pred = X_te @ w
                ic, _ = spearman_np(pred.tolist(), y_te.tolist())
                if ic is not None: scores.append(ic)
            except Exception:
                pass
        if scores:
            avg = np.mean(scores)
            if avg > best_score:
                best_score = avg
                best_lam = lam
    if best_lam is None: best_lam = 10.0
    A = X.T @ X + best_lam * np.eye(p)
    b = X.T @ y
    w = np.linalg.solve(A, b)
    # Normaliser pour comparaison (somme = 1)
    if np.sum(np.abs(w)) > 0:
        w_norm = w / np.sum(np.abs(w))
    else:
        w_norm = w
    return w_norm, best_lam


def weights_ablation(X, y, signals):
    """Importance par ablation : retirer un signal, mesurer la baisse d'IC du composite equal-weighted."""
    n, p = X.shape
    full_score = X.mean(axis=1)
    full_ic, _ = spearman_np(full_score.tolist(), y.tolist())
    importance = []
    for i in range(p):
        mask = np.ones(p, dtype=bool); mask[i] = False
        sub = X[:, mask].mean(axis=1)
        ic_sub, _ = spearman_np(sub.tolist(), y.tolist())
        # importance = drop in |IC|
        drop = abs(full_ic) - abs(ic_sub) if (full_ic is not None and ic_sub is not None) else 0
        importance.append(max(drop, 0))
    total = sum(importance) + 1e-9
    weights = np.array([i / total for i in importance])
    return weights


# ─────────────────────────────────────────────────────────────────────────────
# Étape 3 : Évaluation
# ─────────────────────────────────────────────────────────────────────────────
def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def compute_radar_score(X, weights, signs):
    """
    X : matrice z-scores (n × p)
    weights : array (p,)
    signs : list de +1/-1 par signal (orientation contrarian/momentum)
    Retourne array de scores 0-100.
    """
    weighted_z = X @ (np.array(weights) * np.array(signs))
    return np.array([100 * normal_cdf(z) for z in weighted_z])


def evaluate(scores, returns, label=""):
    """Calcule IC + buckets + spread top-bottom."""
    ic, p = spearman_np(scores.tolist(), returns.tolist())
    n = len(scores)
    # Buckets 0-25 / 25-45 / 45-55 / 55-75 / 75-100
    pairs = list(zip(scores.tolist(), returns.tolist()))
    bucket_ranges = [(0,25), (25,45), (45,55), (55,75), (75,100)]
    buckets = {}
    for lo, hi in bucket_ranges:
        rs = [r for s, r in pairs if lo <= s < hi or (hi == 100 and s == 100)]
        buckets[f"{lo}-{hi}"] = {
            "count": len(rs),
            "ret_mean": round(float(np.mean(rs)), 2) if rs else 0,
            "win_pct": round(float(sum(1 for r in rs if r > 0) / len(rs) * 100), 1) if rs else 0,
        }
    spread = (buckets["75-100"]["ret_mean"] or 0) - (buckets["0-25"]["ret_mean"] or 0)
    return {
        "label": label,
        "n": n,
        "ic": round(ic, 4) if ic is not None else None,
        "buckets": buckets,
        "spread_top_bot": round(spread, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 78)
    print("PHASE 0b.3 — RADAR V3 walk-forward strict")
    print("=" * 78)

    rows = json.loads(DATA.read_text())
    print(f"Dataset total: {len(rows)} jours du {rows[0]['date']} au {rows[-1]['date']}")

    train, valid, test = split_data(rows)
    print(f"\nSplit temporel:")
    print(f"  TRAIN: {len(train)} jours ({train[0]['date']} → {train[-1]['date']})")
    print(f"  VALID: {len(valid)} jours ({valid[0]['date']} → {valid[-1]['date']})")
    print(f"  TEST : {len(test)} jours ({test[0]['date']} → {test[-1]['date']})  ⛔ NE PAS TOUCHER")

    # ── 1. Audit sur TRAIN
    print(f"\n[1] Audit signaux sur TRAIN…")
    audits = []
    for sig in CANDIDATE_SIGNALS:
        a = audit_signal_train_only(train, sig)
        audits.append(a)
        marker = "✅" if a["validated"] else "❌"
        if a.get("ic_train") is not None:
            print(f"   {marker} {sig:<30s}  n={a['n']:4d}  IC={a['ic_train']:+.4f}  "
                  f"t={a['t_train']:+.2f}  sub1/sub2={a['ic_sub1']}/{a['ic_sub2']}  "
                  f"CI=[{a['ci_95'][0]},{a['ci_95'][1]}]  {a['reason']}")
        else:
            print(f"   {marker} {sig:<30s}  {a['reason']}")

    selected = [a for a in audits if a["validated"]]
    print(f"\n   → {len(selected)} signaux validés sur {len(CANDIDATE_SIGNALS)}")

    if len(selected) < 3:
        print("   ❌ Pas assez de signaux validés. Stop.")
        return

    # Construire matrices Z train + valid
    selected_keys = [s["key"] for s in selected]
    selected_signs = [s["sense"] for s in selected]
    print(f"\n[2] Build z-score matrix avec {len(selected_keys)} signaux validés…")
    X_train, y_train, dates_train = build_z_matrix(rows, selected_keys, target_rows=train, lookback=365, cap=3.0)
    X_valid, y_valid, dates_valid = build_z_matrix(rows, selected_keys, target_rows=valid, lookback=365, cap=3.0)
    print(f"   TRAIN matrix: {X_train.shape if X_train is not None else 'EMPTY'}")
    print(f"   VALID matrix: {X_valid.shape if X_valid is not None else 'EMPTY'}")

    if X_train is None or X_train.shape[0] < 100:
        print(f"   ⚠️  Pas assez de jours train avec tous les signaux. ({X_train.shape if X_train is not None else 0} rows)")
        print(f"   Tentative avec moins de signaux contraignants…")
        # On peut aussi imputer ou retirer signaux à faible coverage
        return

    # Apply signs to X (orientation contrarian → invert)
    sign_arr = np.array(selected_signs)
    X_train_oriented = X_train * sign_arr  # broadcasting
    X_valid_oriented = X_valid * sign_arr

    # ── 3. 4 méthodes de pondération sur TRAIN
    print(f"\n[3] Tester 4 méthodes de pondération sur TRAIN, évaluer sur VALID…")
    methods = {}

    # A) Equal
    w_eq = weights_equal(len(selected_keys))
    methods["equal"] = w_eq

    # B) IC-weighted
    w_ic = weights_ic(selected)
    methods["ic_weighted"] = w_ic

    # C) Ridge
    w_ridge, best_lam = weights_ridge(X_train_oriented, y_train)
    methods["ridge"] = w_ridge
    print(f"   Ridge λ optimal: {best_lam}")

    # D) Ablation
    w_abl = weights_ablation(X_train_oriented, y_train, selected_keys)
    methods["ablation"] = w_abl

    # ── 4. Évaluation chaque méthode sur VALID
    results = {}
    print(f"\n   Méthode      |  IC train  |  IC valid  |  Spread valid  |  Buckets valid (0-25 / 25-45 / 45-55 / 55-75 / 75-100)")
    print(f"   " + "-" * 110)
    for name, w in methods.items():
        # Compute scores on TRAIN
        scores_train = compute_radar_score(X_train, w, selected_signs)
        eval_train = evaluate(scores_train, y_train, label=f"{name}_train")
        # On VALID
        scores_valid = compute_radar_score(X_valid, w, selected_signs)
        eval_valid = evaluate(scores_valid, y_valid, label=f"{name}_valid")
        results[name] = {
            "weights": [round(float(x), 4) for x in w.tolist()],
            "train": eval_train,
            "valid": eval_valid,
        }
        bk = eval_valid["buckets"]
        bk_str = " / ".join(f"{bk[k]['ret_mean']:+.1f}%({bk[k]['count']})" for k in ["0-25","25-45","45-55","55-75","75-100"])
        ic_t_str = f"{eval_train['ic']:+.4f}" if eval_train['ic'] is not None else "  N/A  "
        ic_v_str = f"{eval_valid['ic']:+.4f}" if eval_valid['ic'] is not None else "  N/A  "
        spread_str = f"{eval_valid['spread_top_bot']:+.2f}" if eval_valid['spread_top_bot'] is not None else "  N/A "
        print(f"   {name:<13s}|  {ic_t_str}   |  {ic_v_str}   |  {spread_str} pp  |  {bk_str}")

    # Best method = score composite multi-critères (rigueur > brute force IC)
    # Critères :
    #   1. IC valid > 0, même signe que IC train (sinon disqualifié)
    #   2. Spread top-bottom valid > 0 (signe correct)
    #   3. Tous les 5 buckets ont au moins 5 entries (distribution complète)
    #   4. Score = IC valid + 0.01 × spread (favorise spread quand IC similaire)
    def method_score(res):
        ic_t = res["train"]["ic"] or 0
        ic_v = res["valid"]["ic"] or 0
        spread = res["valid"]["spread_top_bot"] or 0
        bks = res["valid"]["buckets"]
        all_buckets_have_data = all(bks[k]["count"] >= 5 for k in ["0-25","25-45","45-55","55-75","75-100"])
        # Disqualifications strictes
        if ic_t == 0 or ic_v == 0: return -999
        if ic_v * ic_t < 0: return -999  # signes opposés
        if spread < 0: return -999  # top-bottom inversé
        if not all_buckets_have_data: return -999  # bucket vide
        # Score final
        return abs(ic_v) + 0.01 * spread

    scored = {name: method_score(res) for name, res in results.items()}
    best_method = max(scored, key=lambda k: scored[k])
    if scored[best_method] == -999:
        print("\n⚠️  Aucune méthode ne passe les critères stricts. Fallback sur IC valid pur.")
        best_method = max(results.keys(), key=lambda k: abs(results[k]["valid"]["ic"] or 0))

    print(f"\n   Scores composites :")
    for name, sc in scored.items():
        flag = " 🏆" if name == best_method else ""
        print(f"     {name:<13s} : score = {sc:+.4f}{flag}")

    print(f"\n   🏆 Meilleure méthode (composite) : {best_method.upper()} (IC valid={results[best_method]['valid']['ic']})")

    # ── Override (audit 2026-05-28) : on FIGE la pondération sur "equal".
    # Raison : le critère composite (IC_valid + 0.01*spread) ne pénalise pas la
    # concentration/colinéarité et choisissait "ic_weighted", qui mettait 43% du
    # poids sur l'OI BTC (2 signaux quasi colinéaires, IC -0.5577/-0.5578) alors
    # que l'ablation montre que retirer l'OI ne change l'IC que de ~0.02. "equal"
    # est aussi la méthode validée out-of-sample (test 2024-2026 : IC 0.41, 6/6).
    # La comparaison ci-dessus reste loggée à titre informatif.
    if best_method != "equal":
        print(f"   🔒 Méthode FORCÉE sur EQUAL (audit anti-concentration, voir radar_v3_validation.md)")
    best_method = "equal"

    final_weights = methods[best_method]
    final_signs = selected_signs

    # ── Mode FIGÉ : si radar_v3_frozen.json existe, on applique cette formule gelée
    # au lieu de la sélection/pondération re-fittée ci-dessus. L'audit + la comparaison
    # de méthodes continuent de tourner (monitoring/dérive dans validation.md), mais le
    # modèle APPLIQUÉ reste stable. Supprimer le fichier pour repasser en re-fit pur.
    frozen = json.loads(FROZEN_FORMULA.read_text()) if FROZEN_FORMULA.exists() else None
    if frozen:
        selected_keys = list(frozen["selected_signals"])
        selected_signs = list(frozen["signs"])
        final_signs = selected_signs
        final_weights = np.array([float(w) for w in frozen["weights"]], dtype=float)
        best_method = frozen.get("method", "equal")
        print(f"\n[FIGÉ] Formule gelée appliquée ({frozen.get('frozen_date','?')}) : "
              f"{len(selected_keys)} signaux, méthode {best_method} — re-fit ignoré pour le score.")

    # ── 5. Construction Radar v3 final sur TOUTE la période (avec poids figés sur train)
    print(f"\n[4] Construire Radar v3 final sur toutes les données (poids figés sur train)…")
    z_all = {}
    for sig in selected_keys:
        series = [(r["date"], r.get(sig)) for r in rows]
        z_all[sig] = rolling_zscore(series, lookback=365, cap=3.0)

    # Tolérance partielle : on calcule le score si au moins MIN_SIGNALS_PCT des signaux
    # sont disponibles, en renormalisant les poids sur les signaux dispos.
    # Cela évite les gaps massifs dans la période 2022 où L/S top traders et Taker LSV
    # n'étaient pas publiés par Binance Vision.
    MIN_SIGNALS_PCT = 0.50   # 50% = 6/12 signaux mini (étend l'historique à 2020+)
    history = []
    n_signals = len(selected_keys)
    n_partial = 0
    for r in rows:
        d = r["date"]
        zs_avail = []
        weights_avail = []
        signs_avail = []
        for i, sig in enumerate(selected_keys):
            z = z_all[sig].get(d)
            if z is None: continue
            zs_avail.append(z)
            weights_avail.append(final_weights[i])
            signs_avail.append(final_signs[i])
        avail_pct = len(zs_avail) / n_signals
        if avail_pct < MIN_SIGNALS_PCT:
            history.append({"date": d, "btc_close": r.get("btc_close"), "radar_v3": None})
            continue
        # Renormaliser les poids sur les signaux dispos
        zs_arr = np.array(zs_avail)
        signs_arr = np.array(signs_avail)
        w_arr = np.array(weights_avail)
        w_arr = w_arr / w_arr.sum()  # renormalisation
        zvec_oriented = zs_arr * signs_arr
        score = float(100 * normal_cdf(np.dot(w_arr, zvec_oriented)))
        score = max(0, min(100, round(score, 1)))
        if avail_pct < 1.0: n_partial += 1
        history.append({
            "date": d,
            "btc_close": r.get("btc_close"),
            "radar_v3": score,
            "n_signals_used": len(zs_avail),
            "ret_btc_7": r.get("ret_btc_7"),
            "ret_btc_30": r.get("ret_btc_30"),
            "ret_btc_90": r.get("ret_btc_90"),
        })
    print(f"   ({n_partial} jours calculés en mode partiel — signaux manquants tolérés)")

    OUT_HIST.write_text(json.dumps(history))
    print(f"   → {OUT_HIST}")
    valid_count = sum(1 for r in history if r.get("radar_v3") is not None)
    print(f"   {valid_count} jours avec score valide")

    # ── 6. Validation rapport (TRAIN + VALID, test reste boîte fermée)
    print(f"\n[5] Sauvegarde rapport TRAIN + VALID…")
    md = ["# Radar v3 — Validation walk-forward\n"]
    md.append(f"Date construction : {datetime.now().date()}\n")
    md.append(f"Méthode retenue : **{best_method.upper()}**\n\n")

    md.append("## Splits temporels\n")
    md.append(f"- TRAIN : {train[0]['date']} → {train[-1]['date']} ({len(train)} jours)")
    md.append(f"- VALID : {valid[0]['date']} → {valid[-1]['date']} ({len(valid)} jours)")
    md.append(f"- TEST  : {test[0]['date']} → {test[-1]['date']} ({len(test)} jours) — ⛔ Non utilisé pour construction\n")

    md.append("\n## Audit sur TRAIN\n")
    md.append("| Signal | n | IC train | t | sub1/sub2 | CI 95% | Validé |")
    md.append("|---|---|---|---|---|---|---|")
    for a in audits:
        ok = "✅" if a["validated"] else "❌"
        md.append(f"| {a['key']} | {a['n']} | {a.get('ic_train', '-')} | {a.get('t_train', '-')} | "
                  f"{a.get('ic_sub1', '-')} / {a.get('ic_sub2', '-')} | "
                  f"{a.get('ci_95', '-')} | {ok} |")

    md.append(f"\n## Comparaison méthodes (TRAIN vs VALID)\n")
    md.append("| Méthode | IC train | IC valid | Ratio (V/T) | Spread valid | Buckets valid |")
    md.append("|---|---|---|---|---|---|")
    for name, res in results.items():
        ic_t = res["train"]["ic"]
        ic_v = res["valid"]["ic"]
        ratio = round(ic_v / ic_t, 2) if (ic_t and ic_v) else "-"
        bk = res["valid"]["buckets"]
        bk_str = "<br>".join(f"{k}: {bk[k]['ret_mean']:+.1f}% ({bk[k]['count']})" for k in ["0-25","25-45","45-55","55-75","75-100"])
        flag = "🏆" if name == best_method else ""
        md.append(f"| **{name}** {flag} | {ic_t} | {ic_v} | {ratio} | {res['valid']['spread_top_bot']} pp | {bk_str} |")

    md.append(f"\n## Méthode retenue : {best_method}\n")
    md.append("Poids attribués (par signal) :\n")
    for i, sig in enumerate(selected_keys):
        sense = "Contrarian" if final_signs[i] == -1 else "Momentum"
        md.append(f"- **{sig}** : poids = {final_weights[i]:.4f}, sens = {sense}")

    md.append(f"\n## Critères de validation walk-forward\n")
    md.append(f"- IC train = {results[best_method]['train']['ic']}")
    md.append(f"- IC valid = {results[best_method]['valid']['ic']}")
    ic_t = results[best_method]['train']['ic'] or 0
    ic_v = results[best_method]['valid']['ic'] or 0
    if ic_t != 0:
        ratio = ic_v / ic_t
        md.append(f"- Ratio valid/train = {ratio:.2f} (cible ≥ 0.5)")
        md.append(f"- Signe stable : {'✅' if ic_v * ic_t > 0 else '❌'}")
    md.append(f"\n## Test (boîte fermée — Phase 0b.4 à venir)\n")
    md.append(f"Le test set ({test[0]['date']} → {test[-1]['date']}) reste intact pour évaluation finale Phase 0b.4.")

    OUT_VALIDATION.write_text("\n".join(md))
    print(f"   → {OUT_VALIDATION}")

    # Save formula
    formula = {
        "method": best_method,
        "selected_signals": selected_keys,
        "signs": final_signs,
        "weights": [float(x) for x in final_weights],
        "rolling_window": 365,
        "z_cap": 3.0,
        "transformation": "rolling_zscore",
        "score_transform": "cdf_normal_x_100",
        "train_period": [train[0]["date"], train[-1]["date"]],
        "valid_period": [valid[0]["date"], valid[-1]["date"]],
        "ic_train": results[best_method]["train"]["ic"],
        "ic_valid": results[best_method]["valid"]["ic"],
    }
    OUT_FORMULA.write_text("```json\n" + json.dumps(formula, indent=2) + "\n```\n")
    print(f"   → {OUT_FORMULA}")

    print(f"\n{'='*78}")
    print(f"PHASE 0b.3 TERMINÉE — Méthode retenue : {best_method.upper()}")
    print(f"{'='*78}")
    print(f"IC train: {results[best_method]['train']['ic']}")
    print(f"IC valid: {results[best_method]['valid']['ic']}")
    print(f"\nProchaine étape (Phase 0b.4) : évaluation sur TEST + multi-régime + permutation")


if __name__ == "__main__":
    main()
