#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
exporter_fonda.py — fondamentaux_indices.json → comparateur_fonda_indices.{json,js}
(window.__CMP_FONDA_INDICES__), lu par la fiche indice (onglet Valorisation) et par
le Comparateur.

Schéma : groupes.indices.<code>.<clé> = {debut, pas: "A", v: [...], cov: [...], n: [...]}
  clés : bn (bénéfice par part), ca (chiffre d'affaires par part), divid (dividende
  par part), anp (actif net par part) — en POINTS D'INDICE ; pe, pb (multiples),
  mn (marge nette %), rdt_div (%), roe (% = bn / anp).
  v[i] = valeur de l'année debut + i, null quand la couverture est sous le seuil.
  cov[i] = part du poids de l'indice chiffrée (%), n[i] = membres chiffrés.
Chaque point est l'exercice de l'année civile (clos de juin Y à mai Y+1), rapporté
au niveau de l'indice et à la composition de FIN DÉCEMBRE Y.
"""
import json
import os
import sys
import time

ICI = os.path.dirname(os.path.abspath(__file__))
NOMS = {"sp500": "S&P 500", "csi300": "CSI 300", "nifty50": "Nifty 50", "nikkei225": "Nikkei 225",
        "dax40": "DAX 40", "cac40": "CAC 40", "ftse100": "FTSE 100", "kospi": "KOSPI", "ibov": "Bovespa",
        "tsx": "TSX Composite", "asx200": "ASX 200", "taiex": "TAIEX", "hsi": "Hang Seng", "ftsemib": "FTSE MIB",
        "ibex35": "IBEX 35", "smi": "SMI", "ipc": "IPC Mexico"}
DEVISE = {"sp500": "USD", "csi300": "CNY", "nifty50": "INR", "nikkei225": "JPY", "dax40": "EUR", "cac40": "EUR",
          "ftse100": "GBP", "kospi": "KRW", "ibov": "BRL", "tsx": "CAD", "asx200": "AUD", "taiex": "TWD",
          "hsi": "HKD", "ftsemib": "EUR", "ibex35": "EUR", "smi": "CHF", "ipc": "MXN"}
# Contrôle S&P 500 : bénéfice par action « publié » (GAAP) et dividende, sur douze
# mois à fin décembre — série S&P Dow Jones Indices reprise par R. Shiller (Yale,
# ie_data.xls, colonnes E et D) ; 2023 : S&P DJI.
def controle_sp500():
    out = {}
    try:
        import xlrd
        sh = xlrd.open_workbook(os.path.join(ICI, "ie_data.xls")).sheet_by_name("Data")
        for r in range(8, sh.nrows):
            d = sh.cell_value(r, 0)
            if isinstance(d, float) and abs(d * 100 - round(d * 100)) < 1e-6 and round(d * 100) % 100 == 12:
                e, dv = sh.cell_value(r, 3), sh.cell_value(r, 2)
                if isinstance(e, float) and e:
                    out[int(d)] = {"bn": round(e, 2), "divid": round(dv, 2) if isinstance(dv, float) else None}
    except Exception as x:
        print("contrôle S&P illisible :", x, file=sys.stderr)
    out.setdefault(2023, {"bn": 192.43, "divid": None})
    return out


def serie(o, cle, cov_cle, calc=None):
    A = o["annees"]
    v = [calc(i) if calc else o.get(cle, [None] * len(A))[i] for i in range(len(A))]
    cov = [o.get(cov_cle, [None] * len(A))[i] for i in range(len(A))]
    ok = [i for i, x in enumerate(v) if x is not None]
    if not ok:
        return None
    i0, i1 = ok[0], ok[-1]
    arr = lambda xs, d: [round(x, d) if isinstance(x, float) else x for x in xs[i0:i1 + 1]]
    return {"debut": A[i0], "pas": "A", "v": arr(v, 4), "cov": arr(cov, 1), "n": o["n_chiffres"][i0:i1 + 1],
            "n_total": o["n"][i0:i1 + 1]}


def poids_lib(code, o):
    est = o.get("poids_estimes") or []
    if not any(est):
        return "exacts à chaque date (avoirs du fonds qui réplique l'indice)"
    if code == "nikkei225":
        return "reconstitués : cours × facteur de l'indice (le Nikkei 225 est pondéré par les cours)"
    if all(est):
        return "estimés : capitalisation × flottant d'aujourd'hui (poids de la date non publiés)"
    a = [y for y, e in zip(o["annees"], est) if e]
    return "exacts depuis %d ; avant, estimés (capitalisation × flottant d'aujourd'hui)" % (max(a) + 1)


def main():
    src = json.load(open(os.path.join(ICI, "fondamentaux_indices.json")))
    ctl = controle_sp500()
    G = {}
    for code, o in src.items():
        if not o.get("annees"):
            continue
        n = len(o["annees"])
        g = {"nom": NOMS.get(code, code), "devise": DEVISE.get(code), "annees_calculees": [o["annees"][0], o["annees"][-1]],
             "mois_compo": o["mois_compo"], "niveau": {"debut": o["annees"][0], "pas": "A", "v": o.get("niveau")},
             "poids": poids_lib(code, o), "source_compo": o.get("source_compo"),
             "source_exercices": ("états SEC (sociétés cotées aujourd'hui) + SEC par CIK pour les disparues"
                                  if o.get("source_exercices") == "etats" else
                                  "TradingView (20 exercices ajustés des divisions) ; actif net : états détaillés"),
             "indice_rendement": bool(o.get("indice_rendement"))}
        for cle, cov in (("bn", "cov_ni"), ("ca", "cov_rev"), ("divid", "cov_div"), ("anp", "cov_eq"),
                         ("pe", "cov_ni"), ("pb", "cov_eq"), ("mn", "cov_rev"), ("rdt_div", "cov_div")):
            s = serie(o, cle, cov)
            if s:
                g[cle] = s
        roe = lambda i: (100 * o["bn"][i] / o["anp"][i]) if o["bn"][i] is not None and o["anp"][i] else None
        s = serie(o, "roe", "cov_eq", roe)
        if s:
            g["roe"] = s
        if code == "sp500":
            c = []
            for i, y in enumerate(o["annees"]):
                if y in ctl and o["bn"][i]:
                    c.append({"annee": y, "calcule": round(o["bn"][i], 2), "publie": ctl[y]["bn"],
                              "ecart": round(100 * (o["bn"][i] / ctl[y]["bn"] - 1), 1)})
            g["controle"] = {"grandeur": "bn", "source": "S&P Dow Jones Indices (bénéfice par action publié, GAAP, douze mois à fin décembre), série reprise par R. Shiller (Yale) ; 2023 : S&P DJI", "points": c}
        G[code] = g
    doc = {"genere_le": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "version": 1,
           "methode": ("Par part d'indice : niveau de l'indice fin décembre × Σ poids × (grandeur / capitalisation) "
                       "des membres chiffrés ÷ Σ poids chiffrés — la définition du bénéfice par action d'un indice "
                       "(celle que S&P publie pour le S&P 500). Composition et poids de fin décembre de chaque année ; "
                       "exercice de l'année civile (clos de juin Y à mai Y+1) ; point non publié sous 90 % du poids "
                       "chiffré (et 90 % des membres quand les poids sont estimés)."),
           "groupes": {"indices": G}}
    for ext in ("json", "js"):
        f = os.path.join(ICI, "comparateur_fonda_indices." + ext)
        txt = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        open(f, "w").write(txt if ext == "json" else "window.__CMP_FONDA_INDICES__=" + txt + ";\n")
    print("écrit :", {k: [x for x in ("bn", "ca", "divid", "anp") if x in v] + [v["bn"]["debut"] if "bn" in v else None] for k, v in G.items()})


if __name__ == "__main__":
    main()
