#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
construire_histo_indices.py — la concentration EXACTE des indices, mois par mois.

À partir des avoirs mensuels d'un fonds qui RÉPLIQUE physiquement l'indice
(iShares : le fichier d'avoirs se demande à une date passée), on lit à chaque
fin de mois le poids réel de chaque membre, donc — sans aucune reconstitution —
le poids du premier, des cinq et des dix premiers, le nombre effectif de titres
et l'indice de Herfindahl. C'est la composition DE CHAQUE DATE : les titres
sortis depuis y sont, ceux entrés depuis n'y sont pas encore.

Sortie : indices_histo.json  {code: {source, mois[], n[], top1[], top5[], top10[],
neff[], hhi[], premier[]}} — lue par fetch_indices_fiches.py, qui la prolonge
chaque mois avec ses propres poids exacts.

Écart d'un fonds à son indice : le fonds tient quelques liquidités et, rarement,
un titre en transition. Les poids sont ramenés à 100 % sur les seules actions.
Contrôle au 29/09/2026 (agent de recherche) : FTSE MIB au centième près de la
fiche officielle, FTSE 100 à 0,07 point.
"""
import csv
import glob
import io
import json
import os
import re
import sys

SOURCES = {
    # code : (dossier des fichiers mensuels, nom du fonds, date de début fiable)
    "dax40": ("DAX_EXS1", "iShares Core DAX UCITS ETF (EXS1)", "2006-01"),
    "ftse100": ("FTSE100_ISF", "iShares Core FTSE 100 UCITS ETF (ISF)", "2006-11"),
    "ftsemib": ("FTSEMIB_IMIB", "iShares FTSE MIB UCITS ETF (IMIB)", "2007-07"),
    # Avant 11/2014, le seul fonds lisible répliquait le SMI puis le SLI : on
    # ne prend que CSSMI, qui réplique le SMI.
    "smi": ("SMI_CSSMI", "iShares SMI ETF (CH) (CSSMI)", "2014-11"),
}
EN_TETES = (("Ticker", "Name", "Asset Class", "Weight (%)", "Equity"),
            ("Emittententicker", "Name", "Anlageklasse", "Gewichtung (%)", "Aktien"))


def nombre(s):
    s = (s or "").replace("’", "").replace("'", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def lire(f):
    """[(poids, nom)] des ACTIONS d'un fichier d'avoirs iShares (anglais ou allemand)."""
    t = open(f, encoding="utf-8-sig", errors="replace").read()
    lignes = t.splitlines()
    for tick, nom, classe, poids, action in EN_TETES:
        i = next((k for k, l in enumerate(lignes) if l.replace('"', "").startswith(tick + ",")), None)
        if i is None:
            continue
        out = []
        for r in csv.DictReader(io.StringIO("\n".join(lignes[i:]))):
            if (r.get(classe) or "").strip() != action:
                continue
            w = nombre(r.get(poids))
            if w and w > 0:
                out.append((w, (r.get(nom) or "").strip()))
        return out
    return []


def serie(dossier):
    out = {"mois": [], "n": [], "top1": [], "top5": [], "top10": [], "neff": [], "hhi": [], "premier": []}
    for f in sorted(glob.glob(os.path.join(dossier, "*.csv"))):
        b = os.path.basename(f)[:8]
        if not re.match(r"^\d{8}$", b):
            continue
        ws = lire(f)
        if len(ws) < 15:
            continue
        tot = sum(w for w, _ in ws)
        ws = sorted(((w / tot, n) for w, n in ws), reverse=True)
        hhi = sum(w * w for w, _ in ws)
        mois = b[:4] + "-" + b[4:6]
        if out["mois"] and out["mois"][-1] == mois:
            for k in out:
                out[k].pop()
        out["mois"].append(mois)
        out["n"].append(len(ws))
        out["top1"].append(round(100 * ws[0][0], 2))
        out["top5"].append(round(100 * sum(w for w, _ in ws[:5]), 2))
        out["top10"].append(round(100 * sum(w for w, _ in ws[:10]), 2))
        out["neff"].append(round(1 / hhi, 1))
        out["hhi"].append(round(10000 * hhi))
        out["premier"].append(ws[0][1].title())
    return out


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    sortie = sys.argv[2] if len(sys.argv) > 2 else "indices_histo.json"
    res = {}
    for code, (dossier, fonds, debut) in SOURCES.items():
        s = serie(os.path.join(base, dossier))
        garde = [i for i, m in enumerate(s["mois"]) if m >= debut]
        s = {k: [v[i] for i in garde] for k, v in s.items()}
        if len(s["mois"]) < 24:
            print("[warn] %s : %d mois — écarté" % (code, len(s["mois"])), file=sys.stderr)
            continue
        res[code] = dict(s, source="Avoirs mensuels du fonds %s, qui réplique physiquement l'indice ; "
                                   "poids ramenés à 100 %% sur les actions." % fonds, pas="mensuel")
        print("[ok] %-8s %3d mois %s → %s ; 10 premiers %s → %s %%"
              % (code, len(s["mois"]), s["mois"][0], s["mois"][-1], s["top10"][0], s["top10"][-1]), file=sys.stderr)
    with open(sortie, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "indices": res}, fh, ensure_ascii=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
