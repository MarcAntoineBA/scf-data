#!/usr/bin/env python3
"""fetch_credit_prive.py — Données de l'onglet « Crédit Privé ».

Deux couches :
  1. LIVE — séries FRED (api.stlouisfed.org) : délinquances par type de crédit,
     charge-offs, spreads de crédit (HY/BB/BBB/CCC OAS), durcissement bancaire
     (SLOOS), conditions financières (NFCI), prêts des banques aux institutions
     financières non bancaires (canal de contagion), encours de crédit conso,
     ratio de service de la dette des ménages.
  2. STATIQUE SOURCÉE — chiffres du marché du crédit privé institutionnel qui
     n'existent PAS en série temporelle publique (taille de marché, dry powder,
     taux de défaut KBRA/Lincoln, part PIK, non-accruals, décotes BDC), la
     chronologie des effondrements, et le tableau NY Fed des transitions vers
     défaut par type d'emprunt. Chaque donnée porte sa source + URL cliquable.

Sortie : credit_prive_cache.js (window.__CREDIT_PRIVE__ = {...}) + .json
Convention identique aux autres fetchers du site (economie_physique, etc.).
"""
import os
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _fred_helpers import fetch_fred

HERE = Path(__file__).resolve().parent
# Cibles d'écriture : le cache canonique lu par snapshot_site.sh (~/Library/Caches/
# site_crypto_finance) + le dépôt local (pour servir :8000 immédiatement). Sous
# launchd l'écriture Desktop est bloquée par TCC → best-effort (try/except).
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
REPO_DIR = Path(os.path.expanduser("~/Desktop/Site_Crypto_Finance"))
_OUT_DIRS = []
for _d in (CACHE_DIR, REPO_DIR, HERE):
    if _d not in _OUT_DIRS:
        _OUT_DIRS.append(_d)

# ─────────────────────────────────────────────────────────────────────────────
# 1. SÉRIES FRED — (clé, id FRED, libellé FR, start, units)
#    units : 'pct' = %, 'bn' = milliards $, 'idx' = indice/ratio
# ─────────────────────────────────────────────────────────────────────────────
FRED_SERIES = [
    # — Spreads de crédit (quotidien) : le thermomètre du risque —
    ("hy_oas",       "BAMLH0A0HYM2", "Spread High Yield US (OAS)",          "2004-01-01", "pct"),
    ("bb_oas",       "BAMLH0A1HYBB", "Spread BB (OAS)",                     "2004-01-01", "pct"),
    ("bbb_oas",      "BAMLC0A4CBBB", "Spread BBB (OAS)",                    "2004-01-01", "pct"),
    ("ccc_oas",      "BAMLH0A3HYC",  "Spread CCC & moins (OAS)",            "2004-01-01", "pct"),
    # — Délinquances bancaires par type (trimestriel) : « tracker des défauts » —
    ("deliq_consumer", "DRCLACBS",   "Délinquance crédit conso (banques)",  "2000-01-01", "pct"),
    ("deliq_cc",       "DRCCLACBS",  "Délinquance cartes de crédit",        "2000-01-01", "pct"),
    ("deliq_mortgage", "DRSFRMACBS", "Délinquance crédit immobilier",       "2000-01-01", "pct"),
    ("deliq_cre",      "DRCRELEXFACBS","Délinquance immobilier commercial", "2000-01-01", "pct"),
    ("deliq_business", "DRBLACBS",   "Délinquance prêts aux entreprises",   "2000-01-01", "pct"),
    ("chargeoff_cc",   "CORCCACBS",  "Taux de perte cartes de crédit",      "2000-01-01", "pct"),
    # — Ménages : capacité de remboursement —
    ("dsr_household",  "TDSP",       "Service de la dette / revenu (ménages)","2000-01-01","pct"),
    ("consumer_credit","TOTALSL",    "Encours total crédit conso",          "2000-01-01", "bn"),
    ("revolving",      "REVOLSL",    "Crédit renouvelable (cartes)",        "2000-01-01", "bn"),
    ("student_loans",  "SLOAS",      "Encours total prêts étudiants",       "2006-01-01", "bn"),
    # — Système : durcissement, conditions, contagion —
    ("sloos_ci",       "DRTSCILM",   "Banques durcissant le crédit aux entreprises", "2000-01-01","pct"),
    ("nfci",           "NFCI",       "Conditions financières (Chicago Fed)","2000-01-01", "idx"),
    ("nbfi_loans",     "LNFACBM027SBOG","Prêts des banques aux institutions financières non bancaires","2015-01-01","bn"),
]


def build_fred():
    out, ok, failed = {}, [], []
    for key, fid, label, start, units in FRED_SERIES:
        r = fetch_fred(fid, start=start)
        if r and r.get("dates"):
            out[key] = {
                "dates": r["dates"], "values": r["values"],
                "label": label, "fred_id": fid, "units": units,
                "source_url": r["source_url"],
                "last": r["values"][-1], "last_date": r["dates"][-1],
            }
            ok.append(f"FRED:{fid}")
            sys.stderr.write(f"[credit_prive] OK {fid:16s} {label[:40]:40s} {r['dates'][-1]} = {r['values'][-1]}\n")
        else:
            failed.append(f"FRED:{fid}")
            sys.stderr.write(f"[credit_prive] FAIL {fid}\n")
        time.sleep(0.25)
    return out, ok, failed


# ─────────────────────────────────────────────────────────────────────────────
# 2. STATIQUE SOURCÉE — voir bloc STATIC ci-dessous.
#    (rempli depuis le dossier de recherche vérifié — chaque entrée a sa source)
# ─────────────────────────────────────────────────────────────────────────────
from credit_prive_static import STATIC  # noqa: E402


def main():
    fred, ok, failed = build_fred()
    now = datetime.now(timezone.utc)
    payload = {
        "meta": {
            "updated_at": now.isoformat(),
            "updated_at_unix": int(now.timestamp()),
            "sources_ok": ok,
            "sources_failed": failed,
            "doc_version": "1.0",
        },
        "fred": fred,
    }
    payload.update(STATIC)

    blob_json = json.dumps(payload, ensure_ascii=False)
    js = ("/* credit_prive_cache.js — généré " + now.isoformat() + " */\n"
          "window.__CREDIT_PRIVE__ = " + blob_json + ";\n")
    written = []
    for d in _OUT_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "credit_prive_cache.js").write_text(js, encoding="utf-8")
            (d / "credit_prive_cache.json").write_text(blob_json, encoding="utf-8")
            written.append(str(d))
        except (OSError, PermissionError) as e:
            sys.stderr.write(f"[credit_prive] skip {d}: {e}\n")
    sys.stderr.write(f"[credit_prive] écrit credit_prive_cache.js/.json dans {len(written)} cible(s) "
                     f"({len(ok)} séries OK, {len(failed)} échecs)\n")


if __name__ == "__main__":
    main()
