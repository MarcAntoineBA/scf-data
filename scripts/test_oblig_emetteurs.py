#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Garde des fiches obligataires par entreprise (fetch_oblig_emetteurs.py).

Relit les fichiers écrits et vérifie ce qu'un visiteur verrait de faux :
  [1] chaque fichier existe, se relit, pèse moins d'1 Mo ;
  [2] la couverture : au moins 60 entreprises, chacune avec sa fiche ;
  [3] chaque obligation se RECALCULE : rendement − écart = le taux d'État de
      la fiche à la même durée, à 3 pb près (un écart calculé contre la
      mauvaise courbe, ou une durée fausse, se voit ici) ; rendement entre −1
      et 40 %, écart entre −150 et 4 000 pb ;
  [4] le chiffre de tête est le dernier point de sa série, et rendement −
      écart retombe sur l'État à la même durée (à 3 pb) ;
  [5] la fraîcheur : prix SPDR de moins de 7 jours, chiffre de tête de moins
      de 10 jours quand l'entreprise a des obligations cotées en Allemagne ;
  [6] les deux sources s'accordent : Francfort et l'indice SPDR, le même
      jour, à moins de 30 pb de rendement en médiane (au-delà : mauvaise
      place de cotation, mauvaise devise, ou prix d'un autre titre) ;
  [7] aucune variation « sur 1 mois » ne repose sur un point trop ancien ;
  [8] les notes : dans l'échelle de leur agence, une action de moins de deux
      ans, une note composite qui est bien la médiane ;
  [9] les comptes : coût de la dette entre 0 et 15 %, dette et trésorerie
      positives ;
  [10] les émissions : rendement à l'émission − écart = un taux d'État
      plausible (−1 à 12 %), date passée, montant positif ; le mur des
      échéances retombe sur l'encours à 2 % près.

Usage :  test_oblig_emetteurs.py [dossier]
         test_oblig_emetteurs.py --mutants [dossier]
"""
import copy
import json
import os
import sys
from datetime import date

DOSSIER = next((a for a in sys.argv[1:] if not a.startswith("--")), None) or os.environ.get("SCF_OBLIG_OUT") \
    or os.path.expanduser("~/Library/Caches/site_crypto_finance")
ECH = {"sp": ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"],
       "fitch": ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D"],
       "moodys": ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]}


def lire(nom):
    with open(os.path.join(DOSSIER, nom), encoding="utf-8") as fh:
        return json.load(fh)


def serie(s, k="v"):
    if not s:
        return []
    if "d0" in s:
        o = date.fromisoformat(s["d0"]).toordinal()
        out = []
        for dj, v in zip(s["dj"], s[k]):
            o += dj
            out.append((date.fromordinal(o).isoformat(), v))
        return out
    return list(zip(s.get("d", []), s.get(k, [])))


def interp(etat, m):
    P = sorted(etat)
    if not P:
        return None
    if m <= P[0][0]:
        return P[0][1]
    if m >= P[-1][0]:
        return P[-1][1]
    for i in range(1, len(P)):
        if P[i][0] >= m:
            (a, x), (b, y) = P[i - 1], P[i]
            return x + (y - x) * (m - a) / (b - a)


def med(v):
    v = sorted(v)
    return v[len(v) // 2] if v else None


def controles(S, det, auj):
    E = []
    ems = S.get("emetteurs") or []
    if len(ems) < 60:
        E.append("[2] seulement %d entreprises" % len(ems))
    au = S.get("prix_spdr_au")
    if not au or (auj - date.fromisoformat(au)).days > 7:
        E.append("[5] prix SPDR du %s (plus de 7 jours)" % au)
    for x in ems:
        c = x["code"]
        d = det.get(c)
        if d is None:
            E.append("[2] %s : fiche absente" % c)
            continue
        etat = [tuple(p) for p in (d.get("courbe") or {}).get("etat") or []]
        au_c = (d.get("courbe") or {}).get("au")
        # [3] chaque obligation se recalcule (au jour de la courbe)
        for o in d.get("obligations") or []:
            if o.get("rdt") is None:
                continue
            if not (-1 < o["rdt"] < 40) or not (-150 < o.get("ecart", 0) < 4000):
                E.append("[3] %s %s : rendement %s / écart %s invraisemblables" % (c, o["isin"], o["rdt"], o.get("ecart")))
                continue
            if o.get("dev") == x.get("dev") and o.get("date") == au_c and etat and o.get("ans") is not None:
                g = interp(etat, o["ans"])
                if g is not None and abs((o["rdt"] - o["ecart"] / 100) - g) > 0.03 + 0.006:
                    E.append("[3] %s %s : rendement − écart = %.2f %%, l'État à %.1f ans = %.2f %%" % (c, o["isin"], o["rdt"] - o["ecart"] / 100, o["ans"], g))
        # [4] le chiffre de tête
        ref = x.get("ref")
        if ref:
            r = serie((d.get("series") or {}).get("r%d" % ref))
            e = serie((d.get("series") or {}).get("e%d" % ref))
            if not r or abs(r[-1][1] - x["rdt"]) > 0.002 or r[-1][0] != x.get("date"):
                E.append("[4] %s : la tête (%s au %s) n'est pas le dernier point de la série" % (c, x.get("rdt"), x.get("date")))
            elif not e or abs(e[-1][1] - x["ecart"]) > 1:
                E.append("[4] %s : l'écart de tête (%s) n'est pas celui de la série" % (c, x.get("ecart")))
            elif x.get("date") == au_c and etat:
                g = interp(etat, ref)
                if g is not None and abs(x["rdt"] - x["ecart"] / 100 - g) > 0.035:
                    E.append("[4] %s : rendement − écart = %.2f %%, l'État à %d ans = %.2f %%" % (c, x["rdt"] - x["ecart"] / 100, ref, g))
            if x.get("date") and x.get("n_cotees", 0) > 0 and not x.get("a144") and (auj - date.fromisoformat(x["date"])).days > 10:
                E.append("[5] %s : chiffre de tête du %s" % (c, x["date"]))
            # [7] la variation sur un mois repose sur un point à ±7 jours
            v1 = (x.get("var_ecart_pb") or {}).get("1m")
            if v1 is not None and e:
                fin = date.fromisoformat(e[-1][0]).toordinal()
                proche = [p for p in e if abs(date.fromisoformat(p[0]).toordinal() - (fin - 30)) <= 7]
                if not proche:
                    E.append("[7] %s : variation sur 1 mois sans point de référence" % c)
        # [6] les deux sources
        ecs = [o["ecart_sources_pb"] for o in d.get("obligations") or [] if o.get("ecart_sources_pb") is not None]
        if len(ecs) >= 5 and abs(med(ecs)) > 30:
            E.append("[6] %s : Francfort et SPDR divergent de %s pb en médiane" % (c, med(ecs)))
        # [8] les notes
        N = d.get("notes") or {}
        A = N.get("agences") or {}
        crans = []
        for ag, v in A.items():
            if v.get("note") not in ECH.get(ag, []):
                E.append("[8] %s : note %s hors échelle (%s)" % (c, v.get("note"), ag))
                continue
            crans.append(ECH[ag].index(v["note"]))
            if not v.get("derniere_action") or (auj - date.fromisoformat(v["derniere_action"])).days > 730:
                E.append("[8] %s : note %s sans action depuis deux ans" % (c, ag))
        if crans and N.get("cran") != sorted(crans)[len(crans) // 2]:
            E.append("[8] %s : note composite %s, médiane %s" % (c, N.get("cran"), sorted(crans)[len(crans) // 2]))
        # [9] les comptes
        C = d.get("comptes")
        if C:
            for v in C.get("cout_dette") or []:
                if v is not None and not (0 < v < 15):
                    E.append("[9] %s : coût de la dette %s %%" % (c, v))
            for k in ("dette", "tresorerie"):
                if any(v is not None and v < 0 for v in C.get(k) or []):
                    E.append("[9] %s : %s négative" % (c, k))
        # [10] les émissions et le mur
        for o in d.get("emissions") or []:
            if o.get("emis") and o["emis"] > auj.isoformat():
                E.append("[10] %s %s : émise dans le futur (%s)" % (c, o["isin"], o["emis"]))
            if o.get("montant") is not None and o["montant"] <= 0:
                E.append("[10] %s %s : montant %s" % (c, o["isin"], o["montant"]))
            if o.get("rdt_emis") is not None and o.get("ecart_emis") is not None and not (-1 < o["rdt_emis"] - o["ecart_emis"] / 100 < 12):
                E.append("[10] %s %s : à l'émission, rendement − écart = %.2f %%" % (c, o["isin"], o["rdt_emis"] - o["ecart_emis"] / 100))
        M = d.get("mur") or {}
        if x.get("encours_md") and M.get("md"):
            if abs(sum(M["md"]) - x["encours_md"]) > 0.02 * x["encours_md"] + 0.1:
                E.append("[10] %s : mur des échéances %.1f ≠ encours %.1f" % (c, sum(M["md"]), x["encours_md"]))
    return E


def charger():
    E = []
    S = lire("oblig_emetteurs.json")
    noms = ["oblig_emetteurs.json", "oblig_em_registre.json"] + ["oblig_em_%s.json" % x["code"] for x in S.get("emetteurs") or []]
    det = {}
    for n in noms:
        p = os.path.join(DOSSIER, n)
        if not os.path.isfile(p):
            E.append("[1] %s absent" % n)
            continue
        if os.path.getsize(p) >= 1_000_000:
            E.append("[1] %s pèse %d o (≥ 1 Mo)" % (n, os.path.getsize(p)))
        try:
            j = lire(n)
        except Exception as e:  # noqa: BLE001
            E.append("[1] %s ne se relit pas : %s" % (n, e))
            continue
        if n.startswith("oblig_em_") and n != "oblig_em_registre.json":
            det[n[len("oblig_em_"):-5]] = j
    return E, S, det


def mutants(S, det, auj):
    ok = True
    cas = []
    c0 = next(x["code"] for x in S["emetteurs"] if x.get("ref") == 10 and len(det[x["code"]].get("obligations") or []) > 5 and det[x["code"]].get("notes"))
    m2 = copy.deepcopy(S); m2["emetteurs"] = m2["emetteurs"][:40]; cas.append(("[2] couverture effondrée", m2, det))
    d3 = copy.deepcopy(det)
    o3 = next(o for o in d3[c0]["obligations"] if o.get("rdt") is not None and o.get("date") == d3[c0]["courbe"]["au"])
    o3["ecart"] += 25
    cas.append(("[3] écart contre une mauvaise courbe", S, d3))
    m4 = copy.deepcopy(S)
    next(x for x in m4["emetteurs"] if x["code"] == c0)["rdt"] += 0.4
    cas.append(("[4] tête qui n'est pas la série", m4, det))
    m5 = copy.deepcopy(S); m5["prix_spdr_au"] = "2026-01-02"; cas.append(("[5] prix périmés", m5, det))
    d6 = copy.deepcopy(det)
    for o in d6[c0]["obligations"]:
        o["ecart_sources_pb"] = 80.0
    cas.append(("[6] sources divergentes", S, d6))
    d7 = copy.deepcopy(det)
    s7 = d7[c0]["series"]["e10"]
    pts = serie(s7)
    fin = date.fromisoformat(pts[-1][0]).toordinal()
    garde = [p for p in pts if not (fin - 60 <= date.fromisoformat(p[0]).toordinal() <= fin - 5)]
    o = [date.fromisoformat(p[0]).toordinal() for p in garde]
    d7[c0]["series"]["e10"] = {"d0": garde[0][0], "dj": [0] + [o[i] - o[i - 1] for i in range(1, len(o))], "v": [p[1] for p in garde]}
    m7 = copy.deepcopy(S)
    x7 = next(x for x in m7["emetteurs"] if x["code"] == c0)
    x7.setdefault("var_ecart_pb", {})["1m"] = 12.0
    cas.append(("[7] variation sans point", m7, d7))
    d8 = copy.deepcopy(det)
    ag = next(iter(d8[c0]["notes"]["agences"]))
    d8[c0]["notes"]["agences"][ag]["derniere_action"] = "2019-05-02"
    cas.append(("[8] note fantôme", S, d8))
    c9 = next((x["code"] for x in S["emetteurs"] if det[x["code"]].get("comptes")), None)
    if c9:
        d9 = copy.deepcopy(det); d9[c9]["comptes"]["cout_dette"][-1] = 38.0; cas.append(("[9] coût de la dette absurde", S, d9))
    c10 = next((x["code"] for x in S["emetteurs"] if det[x["code"]].get("emissions")), None)
    if c10:
        d10 = copy.deepcopy(det); d10[c10]["emissions"][0]["emis"] = "2031-01-01"; cas.append(("[10] émission dans le futur", S, d10))
    for nom, s, d in cas:
        e = controles(s, d, auj)
        vu = any(x.startswith(nom.split("]")[0] + "]") for x in e)
        print(("  ok   " if vu else "  RATÉ ") + nom + ("" if vu else " — la garde n'a rien vu"))
        ok = ok and vu
    return ok


def main():
    E, S, det = charger()
    auj = date.today()
    E += controles(S, det, auj)
    n_ob = sum(len(d.get("obligations") or []) for d in det.values())
    print("Garde émetteurs (%s) : %d entreprises, %d obligations" % (DOSSIER, len(S.get("emetteurs") or []), n_ob))
    for e in E:
        print("  ✗ " + e)
    if "--mutants" in sys.argv:
        print("Mutants :")
        if not mutants(S, det, auj):
            return 2
    print("OK" if not E else "%d anomalie(s)" % len(E))
    return 1 if E else 0


if __name__ == "__main__":
    sys.exit(main())
