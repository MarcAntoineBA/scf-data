#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
histo_depuis_compo.py — prolonge indices_histo.json (la concentration EXACTE, date
par date) avec les compositions mensuelles pesées (recherche/compo_mensuelle/) :
S&P 500 (avoirs trimestriels IVV), TSX, IPC, Bovespa (avoirs BlackRock), Nifty 50
(fichiers officiels NSE), ASX 200 (IOZ), Hang Seng (2800/3115), CSI 300 (2846), SMI
(depuis 2006). Seuls les mois dont les poids connus font au moins 97 % de l'indice
entrent (CSI 300 2016-2021 : membres suspendus absents du fonds) — jamais
d'estimation dans cette série.

Usage : python3 histo_depuis_compo.py <compo_mensuelle/> <indices_histo.json>
"""
import json
import os
import sys

SOURCES = {
    "sp500": "Avoirs trimestriels de l'iShares Core S&P 500 ETF (IVV) déposés à la SEC",
    "tsx": "Avoirs mensuels de l'iShares Core S&P/TSX Capped Composite ETF (XIC)",
    "ipc": "Avoirs mensuels de l'iShares NAFTRAC",
    "ibov": "Avoirs mensuels de l'iShares Ibovespa (BOVA11)",
    "nifty50": "Fichiers mensuels officiels de NSE Indices",
    "asx200": "Avoirs mensuels de l'iShares Core S&P/ASX 200 ETF (IOZ)",
    "hsi": "Avoirs mensuels de l'iShares Core Hang Seng Index ETF",
    "csi300": "Avoirs mensuels de l'iShares Core CSI 300 ETF (2846)",
    "smi": "Avoirs mensuels des fonds iShares répliquant le SMI",
    "mscichina": "Avoirs mensuels de l'iShares MSCI China ETF (MCHI)",
}
SEUIL = 97.0


def serie(d):
    out = {k: [] for k in ("mois", "n", "top1", "top5", "top10", "neff", "hhi", "premier")}
    for mois in sorted(d["mois"]):
        mem = d["mois"][mois]["membres"]
        ws = [(m["poids"], m.get("nom") or "") for m in mem if isinstance(m.get("poids"), (int, float)) and m["poids"] > 0]
        conn = sum(w for w, _ in ws)
        if len(ws) < 15 or conn < SEUIL:
            continue
        ws = sorted(((w / conn, n) for w, n in ws), reverse=True)
        hhi = sum(w * w for w, _ in ws)
        out["mois"].append(mois)
        out["n"].append(len(mem))
        out["top1"].append(round(100 * ws[0][0], 2))
        out["top5"].append(round(100 * sum(w for w, _ in ws[:5]), 2))
        out["top10"].append(round(100 * sum(w for w, _ in ws[:10]), 2))
        out["neff"].append(round(1 / hhi, 1))
        out["hhi"].append(round(10000 * hhi))
        out["premier"].append(ws[0][1].title() if ws[0][1].isupper() else ws[0][1])
    return out


def main():
    dos, f = sys.argv[1], sys.argv[2]
    doc = json.load(open(f)) if os.path.exists(f) else {"version": 1, "indices": {}}
    for code, src in SOURCES.items():
        p = os.path.join(dos, code + ".json")
        if not os.path.exists(p):
            continue
        s = serie(json.load(open(p)))
        if len(s["mois"]) < 24:
            print("[écarté] %s : %d mois pesés" % (code, len(s["mois"])))
            continue
        avant = doc["indices"].get(code)
        if avant and len(avant.get("mois") or []) >= len(s["mois"]):
            print("[gardé] %s : la série en place est plus longue (%d mois)" % (code, len(avant["mois"])))
            continue
        s["source"] = src + ", qui réplique l'indice ; poids ramenés à 100 % sur les actions."
        s["pas"] = "mensuel"
        if code == "sp500":
            s["pas_base"] = "trimestriel"
        doc["indices"][code] = s
        print("[ok] %-8s %3d dates %s → %s ; 10 premiers %s → %s %%" % (code, len(s["mois"]), s["mois"][0], s["mois"][-1], s["top10"][0], s["top10"][-1]))
    json.dump(doc, open(f, "w"), ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
