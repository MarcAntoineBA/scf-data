#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compo_mscichina.py — composition mensuelle EXACTE de MSCI China, d'après les avoirs
de fin de mois du fonds iShares MSCI China ETF (MCHI, réplication physique),
paramètre asOfDate de l'API produit de blackrock.com (depuis 2011).

Sortie : compo_mensuelle/mscichina.json au format commun
  {code, source, exacts, mois: {AAAA-MM: {date, membres: [{isin, ticker, place, nom, poids}]}}}
place = sha | she | hkg | us. Reprenable : chaque mois lu est gardé dans
_cache_mscichina.jsonl.
"""
import datetime as dt
import json
import os
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ICI, "..", "..", "idx"))
import poids_exacts_asie as PA  # noqa: E402

DOS = os.path.join(ICI, "..", "compo_mensuelle")
CACHE = os.path.join(DOS, "_cache_mscichina.jsonl")


def fins_de_mois(debut, fin):
    y, m = debut
    while (y, m) <= fin:
        d = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        yield "%04d-%02d" % (y, m), d
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def lire_mois(dernier):
    """Le dernier jour ouvré publié du mois (on recule jusqu'à 8 jours)."""
    d = dernier
    for _ in range(9):
        if d.weekday() < 5:
            try:
                return PA.mscichina(d.isoformat())
            except Exception:
                pass
        d -= dt.timedelta(days=1)
    return None


def main():
    fin = tuple(int(x) for x in (sys.argv[1] if len(sys.argv) > 1 else "2026-09").split("-"))
    vus = {}
    if os.path.exists(CACHE):
        for l in open(CACHE):
            x = json.loads(l)
            vus[x["mois"]] = x
    with open(CACHE, "a") as fc:
        for mois, dernier in fins_de_mois((2011, 3), fin):
            if mois in vus:
                continue
            r = lire_mois(dernier)
            if not r:
                print("[absent] %s" % mois, flush=True)
                vus[mois] = {"mois": mois, "absent": True}
                fc.write(json.dumps(vus[mois]) + "\n")
                continue
            date_, lignes, _ = r
            membres = [{"isin": l["isin"], "ticker": l["code_place"].split("/")[-1], "place": l["place"],
                        "nom": l["nom"], "poids": round(l["poids"], 6)} for l in lignes]
            vus[mois] = {"mois": mois, "date": date_, "membres": membres}
            fc.write(json.dumps(vus[mois], ensure_ascii=False) + "\n")
            print("[ok] %s %s %d lignes, 1er %s %.2f %%" % (mois, date_, len(membres), membres[0]["nom"][:24], membres[0]["poids"]), flush=True)
            time.sleep(0.4)
    M = {k: {"date": v["date"], "membres": v["membres"]} for k, v in sorted(vus.items()) if not v.get("absent")}
    abs_ = sorted(k for k, v in vus.items() if v.get("absent"))
    doc = {"code": "mscichina", "exacts": True, "mois_absents": abs_,
           "source": "Avoirs de fin de mois de l'iShares MSCI China ETF (MCHI, réplication physique), API produit de "
                     "blackrock.com (asOfDate) ; poids ramenés à 100 sur les actions cotées (Shanghai, Shenzhen, Hong Kong, "
                     "New York).",
           "mois": M}
    json.dump(doc, open(os.path.join(DOS, "mscichina.json"), "w"), ensure_ascii=False)
    print("écrit : %d mois (%s → %s), %d absents" % (len(M), min(M), max(M), len(abs_)))


if __name__ == "__main__":
    main()
