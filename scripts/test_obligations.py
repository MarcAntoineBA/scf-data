#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Garde du collecteur obligataire (fetch_obligations.py).

Relit les fichiers écrits et vérifie ce qu'un visiteur verrait de faux :
  [1] chaque fichier existe, se relit, pèse moins d'1 Mo (au-delà, le dépôt
      de collecte le publie en pièce jointe, moins bien servie) ;
  [2] le marché mondial : un total plausible (> 100 000 Md$), des secteurs qui
      ne dépassent pas le total, un dernier trimestre complet (pas de chute
      de plus de 15 % d'un trimestre à l'autre : un pays manquant) ;
  [3] chaque taux d'État est un nombre plausible (−2 % à 30 %), daté, et sa
      date n'est pas plus vieille que sa fréquence ne le permet (quotidien :
      15 jours ; mensuel : 100 jours, l'OCDE publie avec deux mois de retard) ;
  [4] chaque écart à la référence se recalcule : taux du pays − taux de la
      référence à la même date, à 1 pb près (pour les pays quotidiens) ;
  [5] les fiches annoncées existent : chaque code « pays-échéance » a sa
      maturité dans le détail du pays ;
  [6] le crédit : rendement entre 1 et 25 %, écart entre 10 et 3 000 pb,
      rendement > écart (le taux d'État implicite est positif) ;
  [7] aucune variation « sur 1 mois » ne repose sur un point trop ancien
      (contrôlé en relisant la série : le point de référence existe à ±7 j) ;
  [8] les notations ont toutes une source et une date de vérification.

Usage :  test_obligations.py [dossier]          (défaut : $SCF_OBLIG_OUT ou le cache)
         test_obligations.py --mutants [dossier] (chaque garde doit rougir sur
                                                  une copie volontairement abîmée)
"""
import copy
import json
import os
import sys
from datetime import date

DOSSIER = next((a for a in sys.argv[1:] if not a.startswith("--")), None) or os.environ.get("SCF_OBLIG_OUT") \
    or os.path.expanduser("~/Library/Caches/site_crypto_finance")
PAYS = ["us", "de", "fr", "it", "es", "gb", "jp", "ca", "au", "ch", "nl", "be", "at", "ie", "pt", "gr", "cn", "in", "ez"]


def lire(nom):
    with open(os.path.join(DOSSIER, nom), encoding="utf-8") as fh:
        return json.load(fh)


def serie(s):
    """{d0, dj, v} ou {d, v} → [(date ISO, v)]"""
    if not s:
        return []
    if "d0" in s:
        o = date.fromisoformat(s["d0"]).toordinal()
        out = []
        for dj, v in zip(s["dj"], s["v"]):
            o += dj
            out.append((date.fromordinal(o).isoformat(), v))
        return out
    return list(zip(s.get("d", []), s.get("v", [])))


def controles(S, det, credit_det, aujourd_hui):
    E = []
    M = S.get("marche") or {}
    mo = M.get("monde") or {}
    if not mo.get("total"):
        E.append("[2] marché mondial absent")
    else:
        t = mo["total"][-1]
        if t < 100000:
            E.append("[2] encours mondial invraisemblable : %s Md$" % t)
        for k in ("etats", "financieres", "entreprises"):
            if mo[k][-1] > t * 1.001:
                E.append("[2] secteur %s > total" % k)
        if len(mo["total"]) > 1 and mo["total"][-1] < 0.85 * mo["total"][-2]:
            E.append("[2] dernier trimestre incomplet (chute de %.0f %%)" % (100 * (1 - mo["total"][-1] / mo["total"][-2])))
    codes_s = {s["code"]: s for s in S.get("souverains", [])}
    for c, s in codes_s.items():
        for m, x in (s.get("maturites") or {}).items():
            v, d = x.get("taux"), x.get("date")
            if not isinstance(v, (int, float)) or not (-2 < v < 30):
                E.append("[3] %s-%s : taux invraisemblable %r" % (c, m, v))
            if not d:
                E.append("[3] %s-%s : sans date" % (c, m))
                continue
            age = (aujourd_hui - date.fromisoformat(d[:10])).days
            lim = 100 if s.get("mensuel") else 15     # l'OCDE publie avec deux mois de retard
            if age > lim and not s.get("reprise_du"):
                E.append("[3] %s-%s : donnée de %d jours (limite %d)" % (c, m, age, lim))
    # [4] écarts recalculés (pays quotidiens)
    for c, s in codes_s.items():
        if s.get("mensuel") or not s.get("reference") or c not in det:
            continue
        ref = det.get(s["reference"])
        if not ref:
            continue
        for m, x in (s.get("maturites") or {}).items():
            if x.get("ecart_ref_pb") is None:
                continue
            a = serie(((det[c].get("maturites") or {}).get(m) or {}).get("serie"))
            b = dict(serie(((ref.get("maturites") or {}).get(m) or {}).get("serie")))
            d = ((det[c].get("maturites") or {}).get(m) or {}).get("ecart_ref_date")
            if not a or not b or not d or d not in b:
                continue
            va = dict(a).get(d)
            if va is None:
                continue
            attendu = round((va - b[d]) * 100, 1)
            if abs(attendu - x["ecart_ref_pb"]) > 1.0:
                E.append("[4] %s-%s : écart %s pb, recalculé %s pb" % (c, m, x["ecart_ref_pb"], attendu))
    # [5] fiches
    for f in S.get("fiches", []):
        if f.get("genre") == "credit":
            continue
        p, m = f["pays"], str(f["maturite"])
        if p not in det or m not in (det[p].get("maturites") or {}):
            E.append("[5] fiche %s sans maturité dans le détail" % f["code"])
    # [6] crédit
    for x in S.get("credit", []):
        r, e = x.get("rendement"), x.get("oas_pb") if x.get("oas_pb") is not None else x.get("ecart_pb")
        if r is None or not (1 < r < 25):
            E.append("[6] %s : rendement %r" % (x["code"], r))
        if e is None or not (10 < e < 3000):
            E.append("[6] %s : écart %r" % (x["code"], e))
        if r is not None and e is not None and r * 100 <= e:
            E.append("[6] %s : écart (%s pb) ≥ rendement (%s %%)" % (x["code"], e, r))
    # [7] variations sur 1 mois appuyées sur un point réel
    for c, d0 in det.items():
        for m, x in (d0.get("maturites") or {}).items():
            v1m = (x.get("var") or {}).get("1m")
            pts = serie(x.get("serie"))
            if v1m is None or len(pts) < 3:
                continue
            fin = date.fromisoformat(pts[-1][0])
            cible = date.fromordinal(fin.toordinal() - 30)
            proches = [p for p in pts if abs((date.fromisoformat(p[0]) - cible).days) <= (40 if d0.get("mensuel") else 7)]
            if not proches:
                E.append("[7] %s-%s : variation 1 mois sans point de référence proche" % (c, m))
    # [8] notations
    for c, d0 in det.items():
        N = (d0.get("pays") or {}).get("notation")
        if not N:
            continue
        if not N.get("verifie_le"):
            E.append("[8] %s : notation sans date de vérification" % c)
        for a, x in (N.get("agences") or {}).items():
            if not x.get("url"):
                E.append("[8] %s %s : notation sans source" % (c, a))
    return E


def charger():
    E = []
    noms = ["obligations.json", "obligations.js", "oblig_credit.json", "oblig_vue.json"] + ["oblig_%s.json" % p for p in PAYS]
    for n in noms:
        p = os.path.join(DOSSIER, n)
        if not os.path.isfile(p):
            E.append("[1] %s absent" % n)
            continue
        if os.path.getsize(p) >= 1_000_000:
            E.append("[1] %s pèse %d o (≥ 1 Mo)" % (n, os.path.getsize(p)))
        if n.endswith(".json"):
            try:
                lire(n)
            except Exception as e:  # noqa: BLE001
                E.append("[1] %s ne se relit pas : %s" % (n, e))
    S = lire("obligations.json")
    det = {p: lire("oblig_%s.json" % p) for p in PAYS if os.path.isfile(os.path.join(DOSSIER, "oblig_%s.json" % p))}
    cr = lire("oblig_credit.json") if os.path.isfile(os.path.join(DOSSIER, "oblig_credit.json")) else {}
    return E, S, det, cr


def mutants(S, det, cr, auj):
    """Chaque mutant abîme UNE chose ; chaque garde doit le voir."""
    ok = True
    cas = []
    m1 = copy.deepcopy(S); m1["marche"]["monde"]["total"][-1] *= 0.6; cas.append(("[2] trimestre incomplet", m1, det))
    m2 = copy.deepcopy(S); m2["souverains"][0]["maturites"]["10"]["taux"] = 52.0; cas.append(("[3] taux invraisemblable", m2, det))
    m3 = copy.deepcopy(S); m3["souverains"][0]["maturites"]["10"]["date"] = "2024-01-02"; cas.append(("[3] donnée périmée", m3, det))
    m4 = copy.deepcopy(S)
    for s in m4["souverains"]:
        if s["code"] == "fr":
            s["maturites"]["10"]["ecart_ref_pb"] += 40
    cas.append(("[4] écart faux", m4, det))
    m5 = copy.deepcopy(S); m5["fiches"].append({"code": "fr-7", "pays": "fr", "maturite": 77}); cas.append(("[5] fiche fantôme", m5, det))
    m6 = copy.deepcopy(S); m6["credit"][0]["oas_pb"] = 900; m6["credit"][0]["rendement"] = 6.0; cas.append(("[6] écart ≥ rendement", m6, det))
    d7 = copy.deepcopy(det)
    s7 = d7["us"]["maturites"]["10"]["serie"]
    # on retire 60 jours de points autour de « il y a un mois »
    pts = serie(s7)
    fin = date.fromisoformat(pts[-1][0]).toordinal()
    garde = [p for p in pts if not (fin - 60 <= date.fromisoformat(p[0]).toordinal() <= fin - 5)]
    o = [date.fromisoformat(p[0]).toordinal() for p in garde]
    d7["us"]["maturites"]["10"]["serie"] = {"d0": garde[0][0], "dj": [0] + [o[i] - o[i - 1] for i in range(1, len(o))], "v": [p[1] for p in garde]}
    d7["us"]["maturites"]["10"]["var"]["1m"] = 12.0
    cas.append(("[7] variation sans point", S, d7))
    d8 = copy.deepcopy(det); d8["fr"]["pays"]["notation"]["agences"]["sp"]["url"] = ""; cas.append(("[8] note sans source", S, d8))
    for nom, s, d in cas:
        e = controles(s, d, cr, auj)
        vu = any(x.startswith(nom[:3]) for x in e)
        print(("  ok   " if vu else "  RATÉ ") + nom + ("" if vu else " — la garde n'a rien vu"))
        ok = ok and vu
    return ok


def main():
    E, S, det, cr = charger()
    auj = date.today()
    E += controles(S, det, cr, auj)
    print("Garde obligations (%s) : %d fiche(s), %d pays, %d segment(s)" % (DOSSIER, len(S.get("fiches", [])), len(S.get("souverains", [])), len(S.get("credit", []))))
    for e in E:
        print("  ✗ " + e)
    if "--mutants" in sys.argv:
        print("Mutants :")
        if not mutants(S, det, cr, auj):
            return 2
    print("OK" if not E else "%d anomalie(s)" % len(E))
    return 1 if E else 0


if __name__ == "__main__":
    sys.exit(main())
