#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_fonda_indices.py — garde des fondamentaux par part d'indice
(comparateur_fonda_indices.json) contre ce qui a déjà cassé :

  · une UNITÉ d'actions fausse (états d'Alibaba en ADR × cours de Hong Kong : P/B du
    Hang Seng lu à 0,7 au lieu de 1,2, 01/10/2026) → le P/B, le P/E et le rendement
    du dernier exercice restent dans un rapport plausible avec la fiche du jour
    (même indice, poids exacts, douze mois glissants) ;
  · une clôture Yahoo fausse (3i, P/E du FTSE 100 lu à 3,3) → P/E hors de [2, 200] ;
  · le contrôle S&P qui dérive (BPA avant préférentielles : +12 % en 2009) →
    écart moyen ≤ 4 % ;
  · un point publié sous le seuil de couverture → couverture ≥ 90 %.

Usage : python3 test_fonda_indices.py <comparateur_fonda_indices.json> <indices_fiches.json>
"""
import json
import sys

ko = []


def ok(c, m):
    print(("  ok   " if c else "  ÉCHEC ") + m)
    if not c:
        ko.append(m)


def dernier(g, k):
    x = g.get(k)
    if not x:
        return None, None
    for i in range(len(x["v"]) - 1, -1, -1):
        if x["v"][i] is not None:
            return x["debut"] + i, x["v"][i]
    return None, None


def main():
    F = json.load(open(sys.argv[1]))["groupes"]["indices"]
    S = {s["code"]: s for s in json.load(open(sys.argv[2]))["indices"]}
    for code, g in sorted(F.items()):
        v = (S.get(code) or {}).get("valorisation") or {}
        for k, borne, lib in (("pb", (0.65, 1.55), "P/B"), ("rdt_div", (0.55, 1.8), "rendement")):
            an, x = dernier(g, k)
            if x is None or not v.get(k):
                continue
            r = x / v[k]
            ok(borne[0] <= r <= borne[1], "%-9s %s %d = %.2f / fiche du jour %.2f (rapport %.2f)" % (code, lib, an, x, v[k], r))
        for i, p in enumerate((g.get("pe") or {}).get("v") or []):
            if p is not None:
                ok(2 <= p <= 200, "%-9s P/E %d = %.1f dans [2, 200]" % (code, g["pe"]["debut"] + i, p)) if not (2 <= p <= 200) else None
        for k in ("bn", "ca", "divid", "anp"):
            x = g.get(k)
            if not x:
                continue
            bas = [(x["debut"] + i, c) for i, (v_, c) in enumerate(zip(x["v"], x["cov"]))
                   if v_ is not None and c is not None and c < 90]
            ok(not bas, "%-9s %s : aucun point publié sous 90 %% du poids %s" % (code, k, bas)) if bas else None
    c = (F.get("sp500") or {}).get("controle") or {}
    ok(c.get("ecart_moyen_abs") is not None and c["ecart_moyen_abs"] <= 4.0,
       "S&P 500 : écart moyen au BPA publié %s %% ≤ 4 %%" % c.get("ecart_moyen_abs"))
    print("\n%s" % ("TOUT EST VERT" if not ko else "%d CONTRÔLE(S) EN ÉCHEC" % len(ko)))
    return 1 if ko else 0


if __name__ == "__main__":
    sys.exit(main())
