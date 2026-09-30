#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_indices_fiches.py — garde de fetch_indices_fiches.py (fiches des indices).

Sans réseau : chaque contrôle fabrique ses données. Chacun a sa CONTRE-ÉPREUVE
(`--mutants`) : on casse volontairement la règle qu'il protège et on vérifie
qu'il échoue. Un test qui passe aussi sur le code cassé ne protège rien.

  [1] Fenêtres : référence = dernière clôture AU PLUS TARD la date cible (règle
      du Comparateur), et refus d'une référence plus de dix jours avant elle.
  [2] Contributions au poids de DÉPART : un titre qui a doublé ne compte pas
      deux fois ; la somme des contributions = la variation du portefeuille.
  [3] Performance : la tête est l'indice PUBLIÉ, l'écart est publié sous son
      nom, et aucune reconstitution n'est publiée au-delà de deux ans.
  [4] Pas de « poids égaux » reconstitué : seul l'équipondéré officiel passe.
  [5] Cours quotidiens absents : repli sur le screener, dit dans `source`.
  [6] La note est celle des secteurs (même fonction, mêmes seuils).
  [7] Dates de séance : une barre de Sydney horodatée la veille en UTC est
      ramenée au bon jour.
  [8] Un cours isolé cinq fois trop haut est retiré ; un vrai changement de
      niveau reste.
"""
import copy
import os
import sys

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import fetch_indices_fiches as F  # noqa: E402

ECHECS = []


def verifier(nom, cond, detail=""):
    print(("  ok   " if cond else "  ÉCHEC ") + nom + ("" if cond else "  — " + detail))
    if not cond:
        ECHECS.append(nom)


def serie_jours(debut, n, f):
    from datetime import date, timedelta
    d0 = date.fromisoformat(debut)
    out = []
    k = 0
    while len(out) < n:
        d = d0 + timedelta(days=k)
        k += 1
        if d.weekday() >= 5:
            continue
        out.append((d.isoformat(), f(len(out))))
    return out


def t1_fenetres():
    print("[1] fenêtres de l'indice")
    s = serie_jours("2024-01-01", 700, lambda i: 100.0 + i)
    pf = F.perfs_indice(s)
    fin = s[-1][0]
    from datetime import date, timedelta
    cible = (date.fromisoformat(fin) - timedelta(days=365)).isoformat()
    ref = max(d for d, _ in s if d <= cible)
    verifier("1 an : référence au plus tard la date cible", pf["1a"][1] == ref, "%s vs %s" % (pf["1a"][1], ref))
    v_ref = dict(s)[ref]
    verifier("1 an : valeur", abs(pf["1a"][0] - round(100 * (s[-1][1] / v_ref - 1), 2)) < 1e-9)
    verifier("10 ans : série trop courte → None", pf["10a"][0] is None)
    fin_d = date.fromisoformat(fin)
    ytd_ref = max(d for d, _ in s if d <= "%d-12-31" % (fin_d.year - 1))
    verifier("depuis janvier : dernière clôture de l'année précédente", pf["ytd"][1] == ytd_ref)


PANIER = [(0.30, 1.0), (0.20, 0.0), (0.15, -0.5), (0.15, 0.2), (0.10, 0.1), (0.10, -0.2)]


def lignes_test():
    rows = []
    for i, (w, x) in enumerate(PANIER):
        rows.append({"sym": "T%d" % i, "nom": "Titre %d" % i, "cle": "t%d" % i, "w": w,
                     "p": {"1a": 100 * x, "1m": 100 * x / 10}, "v": {"ch3y": 100 * x, "ch5y": 100 * x}})
    return rows


def t2_contributions():
    print("[2] contributions au poids de départ")
    rows = lignes_test()
    niveau = {"perf": {"1a": 42.0, "1m": 3.0}, "refs": {}}
    P = F.performance(rows, niveau, None)
    w0 = [w / (1 + x) for w, x in PANIER]
    attendu = 100 * sum(a * x for a, (_, x) in zip(w0, PANIER)) / sum(w0)
    verifier("somme des contributions = portefeuille au départ", abs(P["1a"]["reconstitue"] - round(attendu, 2)) < 0.011,
             "%s vs %.2f" % (P["1a"]["reconstitue"], attendu))
    somme = sum(r["contrib"]["1a"] for r in rows)
    verifier("contributions titre par titre : même somme", abs(somme - attendu) < 0.01, "%.3f vs %.3f" % (somme, attendu))
    return P


def t3_tete_publiee(P):
    print("[3] la tête est l'indice publié")
    verifier("officiel en tête", P["1a"]["officiel"] == 42.0)
    verifier("écart publié sous son nom", abs(P["1a"]["ecart"] - (42.0 - P["1a"]["reconstitue"])) < 0.011)
    rows = lignes_test()
    P2 = F.performance(rows, {"perf": {"3a": 10.0}, "refs": {}}, None)
    verifier("3 ans : pas de reconstitution", P2.get("3a", {}).get("reconstitue") is None and P2["3a"].get("membres_actuels") is True)
    P3 = F.performance(lignes_test(), {"perf": {"3a": 10.0}, "refs": {}}, None, rentab=True)
    verifier("3 ans, indice de rentabilité : aucune statistique de membres", "mediane" not in P3.get("3a", {}))


def t4_egal():
    print("[4] pas de poids égaux reconstitué")
    P = F.performance(lignes_test(), {"perf": {"1a": 42.0}, "refs": {}}, None)
    verifier("sans équipondéré officiel : rien", "equipondere" not in P["1a"])
    P = F.performance(lignes_test(), {"perf": {"1a": 42.0}, "refs": {}}, None, egal={"perf": {"1a": 7.0}})
    verifier("avec l'officiel : sa valeur", P["1a"].get("equipondere") == 7.0)


def t5_repli():
    print("[5] repli sur le screener")
    rows = lignes_test()
    for r in rows:
        r["p"] = {}
        r["v"]["ch1y"] = 10.0
    P = F.performance(rows, {"perf": {"1a": 9.0}, "refs": {}}, None)
    verifier("source dite", P["1a"].get("source", "").startswith("screener"), P["1a"].get("source"))
    verifier("décomposition présente", P["1a"].get("reconstitue") is not None)


def t6_note():
    print("[6] la note des secteurs")
    import fetch_secteurs_mondiaux as fsm
    rows = []
    for i in range(12):
        v = {"peRatio": 12.0 + 0.4 * i, "peForward": 14.0, "evEbitda": 10.0, "psRatio": 1.5, "roic": 16.0, "roe": 16.0,
             "operatingMargin": 16.0, "profitMargin": 11.0, "debtEbitda": 1.5, "interestCoverage": 9.0,
             "currentRatio": 1.6, "revenue3y": 11.0, "revenueGrowth": 9.0, "epsGrowth": 11.0, "fcfYield": 6.0,
             "dividendYield": 3.0, "payoutRatio": 50.0, "buybackYield": 1.5, "beta": 0.9, "pbRatio": 2.0,
             "marketCapUsd": 1e10}
        rows.append({"sym": "X%d" % i, "nom": "X%d" % i, "w": 1 / 12.0, "v": v})
    g, couv = F.note_indice("x", rows, {})
    nf = g["note_fondamentale"]
    verifier("tous les critères au seuil favorable → 20/20", nf["note"] == 20.0, str(nf["note"]))
    verifier("barème = celui des secteurs", nf["criteres_total"] == fsm.NOTE_TOTAL)
    verifier("croissance sur 3 ans lue (revenue3y)", any(c["cle"] == "croissance_ca_3a_pct_median" and c["point"] == 1.0 for c in nf["criteres"]))


def t7_dates():
    print("[7] dates de séance")
    # Sydney : ouverture 10 h locales = 23 h 00 UTC la veille (hors heure d'été).
    from datetime import datetime, timezone
    pts = []
    for j in range(1, 11):
        t = int(datetime(2026, 7, j, 0, 0, tzinfo=timezone.utc).timestamp()) - 3600   # 23 h UTC la veille
        pts.append((t, 100.0 + j))
    d = F.en_dates_locales(pts)
    verifier("barre de 23 h UTC le 30/06 → séance du 01/07", d[0][0] == "2026-07-01", d[0][0])


def t8_nettoyer():
    print("[8] cours isolé aberrant")
    s = serie_jours("2026-01-01", 40, lambda i: 100.0)
    s[20] = (s[20][0], 520.0)
    propre, n = F.nettoyer(s)
    verifier("pic isolé retiré", n == 1 and all(c == 100.0 for _, c in propre))
    s2 = serie_jours("2026-01-01", 40, lambda i: 100.0 if i < 20 else 600.0)
    propre2, n2 = F.nettoyer(s2)
    verifier("vrai changement de niveau gardé", n2 == 0)


def tout():
    del ECHECS[:]
    t1_fenetres()
    P = t2_contributions()
    t3_tete_publiee(P)
    t4_egal()
    t5_repli()
    t6_note()
    t7_dates()
    t8_nettoyer()
    return list(ECHECS)


def mutants():
    """Chaque mutation casse une règle ; le test doit échouer."""
    orig = {k: getattr(F, k) for k in ("dernier_au_plus_tard", "performance", "SCREENER_FEN", "PIC_FACTEUR", "FEN_MEMBRES")}
    cas = []

    def m1():   # référence APRÈS la date cible
        def mauvais(dates, c):
            k = orig["dernier_au_plus_tard"](dates, c)
            return None if k is None else min(k + 1, len(dates) - 1)
        F.dernier_au_plus_tard = mauvais
    cas.append(("référence après la cible", m1))

    def m2():   # contributions au poids ACTUEL
        src = orig["performance"]

        def perf(rows, niveau, series, rentab=False, egal=None):
            for r in rows:
                r["w_bak"] = r["w"]
            out = src(rows, niveau, series, rentab, egal)
            for h, d in out.items():
                if d.get("reconstitue") is not None:
                    d["reconstitue"] = round(sum(r["w"] * (r.get("p") or {}).get(h, 0) for r in rows), 2)
            return out
        F.performance = perf
    cas.append(("contributions au poids actuel", m2))

    def m3():   # plus de repli screener
        F.SCREENER_FEN = {}
    cas.append(("repli screener retiré", m3))

    def m4():   # aucun cours aberrant retiré
        F.PIC_FACTEUR = 1e9
    cas.append(("nettoyage désactivé", m4))

    def m5():   # reconstitution publiée à 3 ans
        F.FEN_MEMBRES = F.FEN_MEMBRES + ("3a",)
    cas.append(("reconstitution à 3 ans", m5))

    rates = 0
    for nom, casser in cas:
        casser()
        e = tout()
        for k, v in orig.items():
            setattr(F, k, v)
        attrape = bool(e)
        print("MUTANT %-32s %s" % (nom, "attrapé" if attrape else "NON ATTRAPÉ"))
        rates += 0 if attrape else 1
    return rates


if __name__ == "__main__":
    e = tout()
    print("\n%d échec(s)" % len(e))
    code = 1 if e else 0
    if "--mutants" in sys.argv:
        r = mutants()
        print("%d mutant(s) non attrapé(s)" % r)
        code = code or (1 if r else 0)
    sys.exit(code)
