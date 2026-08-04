"""Fetcher FRED — St Louis Fed Financial Stress (STLFSI4) + Sahm Rule realtime (SAHMREALTIME).

Deux indicateurs macro de détection de crise / récession, présentés dans
l'onglet Indicateur (catégorie Liquidité) :

  - STLFSI4 : index hebdomadaire centré sur 0 (moyenne historique). >0 = stress,
              <0 = conditions faciles. Pics historiques : LTCM, dotcom, GFC,
              COVID, hausse Fed 2022.

  - SAHMREALTIME : différence moyenne mobile 3m du taux de chômage US vs son
              min sur 12m. ≥0.5 = récession en cours (indicateur coïncident
              historiquement parfait depuis 1970).

Sortie : ~/Desktop/Site_Crypto_Finance/fred_stress_indicators_cache.js
  window.__STLFSI4__       = { current: {...}, history: [...], source_url, fred_id }
  window.__SAHMREALTIME__  = { current: {...}, history: [...], source_url, fred_id }
"""
import json
import os
import sys
from datetime import datetime, timezone

# Permet l'import du helper FRED partage
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _fred_helpers import fetch_fred

OUT_JS = os.path.expanduser("~/Desktop/Site_Crypto_Finance/fred_stress_indicators_cache.js")
# Source de vérité accessible au launchd (où vivent les autres caches) PUIS miroir Desktop.
# Sans Library/Caches, l'advisor (Library-résident) ne pouvait pas lire ce cache sous launchd.
OUT_PATHS = [
    os.path.expanduser("~/Library/Caches/site_crypto_finance/fred_stress_indicators_cache.js"),
    OUT_JS,
]


def _dir_stlfsi4(v):
    """Direction/tone selon valeur STLFSI4.

    Centred on 0 (historical average). Pics historiques :
      LTCM 1998 ~+4, dotcom 2002 ~+2, GFC 2008 ~+8, COVID 2020 ~+6.
    """
    if v is None:
        return 'eq', '—'
    if v > 2:    return 'neg',  'Stress severe'
    if v > 0.5:  return 'warn', 'Stress eleve'
    if v > -0.5: return 'eq',   'Normal'
    if v > -1.5: return 'pos',  'Conditions faciles'
    return 'pos', 'Conditions tres faciles'


def _dir_sahm(v):
    """Direction/tone selon valeur Sahm Rule.

    Seuil de declenchement : 0.5 (recession en cours coincidente).
    """
    if v is None:
        return 'eq', '—'
    if v >= 0.5: return 'neg',  'Recession en cours'
    if v >= 0.3: return 'warn', 'Pre-recession'
    if v >= 0.1: return 'eq',   'Hausse chomage'
    return 'pos', 'Marche emploi sain'


def _downsample(dates, values, max_points=600):
    """Downsample lineairement a max_points (preserve premier/dernier)."""
    n = len(dates)
    if n <= max_points:
        return dates, values
    step = n / max_points
    out_d, out_v = [], []
    for i in range(max_points):
        idx = min(n - 1, int(round(i * step)))
        out_d.append(dates[idx])
        out_v.append(values[idx])
    if out_d[-1] != dates[-1]:
        out_d[-1] = dates[-1]
        out_v[-1] = values[-1]
    return out_d, out_v


def _build_block(series_id, dir_fn, label_short):
    print(f"[fred-stress] fetch {series_id} ...", file=sys.stderr)
    raw = fetch_fred(series_id)
    if not raw or not raw.get("dates"):
        print(f"[fred-stress] {series_id} fetch failed / empty", file=sys.stderr)
        return None
    dates = raw["dates"]
    values = raw["values"]
    cur_d = dates[-1]
    cur_v = values[-1]
    tone, label = dir_fn(cur_v)
    # Variation 4 semaines / 4 mois pour stlfsi (weekly) et sahm (monthly)
    chg_txt = ''
    if len(values) >= 5:
        prev = values[-5]
        d = cur_v - prev
        if series_id == 'STLFSI4':
            chg_txt = f"{d:+.2f} 4sem"
        else:
            chg_txt = f"{d:+.2f} 4m"
    # Downsample histo
    ds_d, ds_v = _downsample(dates, values, max_points=600)
    history = [{"d": d, "u": v} for d, v in zip(ds_d, ds_v)]
    return {
        "current": {
            "date": cur_d,
            "value": round(cur_v, 3),
            "label": label,
            "tone": tone,
            "chg_txt": chg_txt,
        },
        "history": history,
        "n_obs": len(dates),
        "first_date": dates[0],
        "fred_id": series_id,
        "source_url": raw["source_url"],
    }


def main():
    print(f"[fred-stress] start", file=sys.stderr)
    stlfsi = _build_block("STLFSI4", _dir_stlfsi4, "Stress")
    sahm = _build_block("SAHMREALTIME", _dir_sahm, "Sahm")
    if not stlfsi and not sahm:
        print(f"[fred-stress] both series failed, aborting", file=sys.stderr)
        sys.exit(1)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "stlfsi4":      stlfsi,
        "sahmrealtime": sahm,
        "updated":      updated,
    }
    js = (
        f"window.__STLFSI4__ = {json.dumps(stlfsi, ensure_ascii=False, separators=(',', ':'))};\n"
        f"window.__SAHMREALTIME__ = {json.dumps(sahm, ensure_ascii=False, separators=(',', ':'))};\n"
        f"window.__FRED_STRESS_UPDATED__ = {json.dumps(updated)};\n"
    )
    wrote = []
    for outp in OUT_PATHS:
        try:
            os.makedirs(os.path.dirname(outp), exist_ok=True)
            with open(outp, "w", encoding="utf-8") as f:
                f.write(js)
            wrote.append(outp)
        except Exception as e:
            print(f"[fred-stress] écriture échouée {outp}: {e}", file=sys.stderr)
    print(f"[fred-stress] OK · STLFSI4={stlfsi['current']['value'] if stlfsi else 'NA'} · SAHM={sahm['current']['value'] if sahm else 'NA'} · wrote {wrote}", file=sys.stderr)


if __name__ == "__main__":
    main()
