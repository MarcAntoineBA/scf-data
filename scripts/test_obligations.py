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
  [8] les notations ont toutes une source et une date de vérification ;
  [9] la dette « au dernier chiffre publié » : plausible (5 à 300 % du PIB),
      datée, pas plus vieille que 300 jours (un trimestre publié avec ~4 mois
      de retard, plus une publication sautée), à moins de 25 points du FMI
      (au-delà : une erreur d'unité ou de définition) ;
  [10] la dette totale (BRI) : l'empilement retombe sur le total à 0,2 pt
      près, l'État est à moins de 15 points de la dette fraîche, la
      productivité marginale reste un nombre fini et raisonnable.
  [11] la tech dans la bande « entreprises » (SEC, oblig_tech.py) : au moins 25
      sociétés au dernier trimestre, le cloud et l'IA ⊂ la tech ⊂ les
      entreprises américaines de la BRI (entre 1 et 30 % d'entre elles), dernier
      trimestre de moins de 230 jours, aucune marche d'un trimestre à l'autre
      (+45 % / −25 % : une société entrée ou sortie d'un coup), audit SEC
      tenu (≤ 15 écarts au total publié par les sociétés).
  [13] les financières américaines (Z.1, oblig_detail_us) : les sous-secteurs ne
      dépassent pas le total, et le reste « autres » ne dépasse pas 10 % ;
  [14] les entreprises américaines par secteur (SEC) : la dette ventilée (secteurs
      et tech) ne dépasse JAMAIS la dette totale des entreprises américaines (BRI) ;
      aucun secteur ne saute de plus de 40 % d'un trimestre à l'autre (une
      couture de prédécesseur ratée) ;
  [12] le marché contre les agences (oblig_notes.py) : au moins 10 pays cotés
      du jour dont la France, l'Allemagne à 0 pb (elle est la référence), des
      écarts entre −100 et +800 pb, une cotation de moins de 4 jours ; au moins
      15 pays dans la vue mensuelle, mois de moins de 120 jours ; chaque pays
      montré a une note Moody's prise dans l'échelle ; aucune série figée.

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
        if f.get("genre") in ("credit", "emetteur"):
            continue                # les entreprises ont leur garde : test_oblig_emetteurs.py
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
    # [9] dette fraîche
    for c, s in codes_s.items():
        if c == "ez":
            continue
        f = s.get("dette_fraiche")
        if not f:
            E.append("[9] %s : pas de dette au dernier chiffre publié" % c)
            continue
        v, per = f.get("valeur"), str(f.get("periode") or "")
        if not isinstance(v, (int, float)) or not (5 < v < 300):
            E.append("[9] %s : dette fraîche invraisemblable %r" % (c, v))
        fin = None
        try:
            if len(per) == 7 and per[4] == "-" and per[5] in "TQ":
                a, t = int(per[:4]), int(per[6])
                fin = date(a + (t == 4), (3 * t) % 12 + 1, 1)
            elif len(per) == 7:
                a, m = int(per[:4]), int(per[5:7])
                fin = date(a + (m == 12), m % 12 + 1, 1)
            else:
                fin = date.fromisoformat(per[:10])
        except ValueError:
            pass
        if not fin:
            E.append("[9] %s : période illisible %r" % (c, per))
        elif (aujourd_hui - fin).days > 300:
            E.append("[9] %s : dette fraîche de %s, %d jours" % (c, per, (aujourd_hui - fin).days))
        fmi = s.get("dette_pib")
        if isinstance(v, (int, float)) and isinstance(fmi, (int, float)) and abs(v - fmi) > 25:
            E.append("[9] %s : dette fraîche %.1f %% contre %.1f %% au FMI" % (c, v, fmi))
    # [10] dette totale
    for c, d0 in det.items():
        T = (d0.get("pays") or {}).get("dette_totale")
        if not T:
            continue
        n = len(T.get("t") or [])
        for k in ("pib4", "dette_c", "dette_g", "pct_g", "pct_h", "pct_n", "pct_c"):
            if len(T.get(k) or []) != n:
                E.append("[10] %s : %s n'a pas %d points" % (c, k, n))
        for i in range(n):
            g, h, nf, tot = (T.get(k, [None] * n)[i] for k in ("pct_g", "pct_h", "pct_n", "pct_c"))
            if None not in (g, h, nf, tot) and abs(g + h + nf - tot) > 0.2:
                E.append("[10] %s %s : %s + %s + %s ≠ %s" % (c, T["t"][i], g, h, nf, tot))
                break
        f = (codes_s.get(c) or {}).get("dette_fraiche") or {}
        g = (T.get("pct_g") or [None])[-1]
        if g is not None and isinstance(f.get("valeur"), (int, float)) and abs(g - f["valeur"]) > 15:
            E.append("[10] %s : État %.1f %% (BRI) contre %.1f %% (dette fraîche)" % (c, g, f["valeur"]))
        for k, v in T.items():
            if k.startswith("pm_") and any(x is not None and not (-20 < x < 20) for x in v):
                E.append("[10] %s : %s hors bornes" % (c, k))
    # [11] la tech
    M = S.get("marche") or {}
    TK, EU = M.get("tech"), M.get("etats_unis")
    if not TK or not (TK.get("serie") or {}).get("t"):
        E.append("[11] pas de série tech")
    else:
        sr = TK["serie"]
        n = len(sr["t"])
        if any(len(sr.get(k) or []) != n for k in ("tech", "ia", "n")):
            E.append("[11] séries tech de longueurs différentes")
        elif sr["n"][-1] < 25:
            E.append("[11] tech : %d sociétés seulement au dernier trimestre" % sr["n"][-1])
        else:
            for i in range(n):
                if not (0 <= sr["ia"][i] <= sr["tech"][i]):
                    E.append("[11] %s : cloud et IA %s hors de la tech %s" % (sr["t"][i], sr["ia"][i], sr["tech"][i]))
                    break
            for i in range(1, n):
                a, b = sr["tech"][i - 1], sr["tech"][i]
                if a > 100 and not (0.75 * a <= b <= 1.45 * a):
                    E.append("[11] %s : marche %s → %s Md$" % (sr["t"][i], a, b))
                    break
            y, q = sr["t"][-1].split("-Q")
            fin = date(int(y), 3 * int(q), [31, 30, 30, 31][int(q) - 1])
            if (aujourd_hui - fin).days > 230:
                E.append("[11] tech : dernier trimestre %s, %d jours" % (sr["t"][-1], (aujourd_hui - fin).days))
            if EU and EU.get("t"):
                for t, v in zip(sr["t"], sr["tech"]):
                    if t in EU["t"]:
                        nfc = EU["entreprises"][EU["t"].index(t)]
                        if not nfc or not (0.01 * nfc <= v <= 0.30 * nfc):
                            E.append("[11] %s : tech %s Md$ pour %s Md$ d'entreprises américaines" % (t, v, nfc))
                            break
            else:
                E.append("[11] pas de série États-Unis (BRI)")
        if ((TK.get("audit") or {}).get("ecarts") or 0) > 15:
            E.append("[11] audit SEC : %s écarts" % TK["audit"]["ecarts"])
    # [12] le marché contre les agences
    NE = S.get("notes_ecarts")
    ECH_M = ["Aaa", "Aa1", "Aa2", "Aa3", "A1", "A2", "A3", "Baa1", "Baa2", "Baa3", "Ba1", "Ba2", "Ba3", "B1", "B2", "B3", "Caa1", "Caa2", "Caa3", "Ca", "C"]
    if not NE:
        E.append("[12] pas de bloc notes_ecarts")
    else:
        Q, Mo = NE.get("quotidien") or {}, NE.get("mensuel") or {}
        qp, mp = Q.get("pays") or {}, Mo.get("pays") or {}
        if len(qp) < 10 or "fr" not in qp:
            E.append("[12] cotations du jour : %d pays" % len(qp))
        if (qp.get("de") or {}).get("ecart_pb") not in (0, 0.0):
            E.append("[12] l'Allemagne n'est pas à 0 pb")
        for c, x in list(qp.items()) + list(mp.items()):
            if not (-100 <= (x.get("ecart_pb") if isinstance(x.get("ecart_pb"), (int, float)) else 9999) <= 800):
                E.append("[12] %s : écart %r hors bornes" % (c, x.get("ecart_pb")))
                break
        try:
            if (aujourd_hui - date.fromisoformat(Q.get("date", "2000-01-01"))).days > 4:
                E.append("[12] cotations du %s" % Q.get("date"))
        except ValueError:
            E.append("[12] date de cotation illisible")
        if len(mp) < 15:
            E.append("[12] vue mensuelle : %d pays" % len(mp))
        try:
            y, m = (Mo.get("mois") or "2000-01").split("-")
            if (aujourd_hui - date(int(y), int(m), 15)).days > 120:
                E.append("[12] vue mensuelle de %s" % Mo.get("mois"))
        except ValueError:
            E.append("[12] mois illisible")
        for c in set(qp) | set(mp):
            n = ((NE.get("notes") or {}).get(c) or {}).get("moodys") or {}
            if n.get("note") not in ECH_M:
                E.append("[12] %s : note Moody's %r" % (c, n.get("note")))
                break
    if EU and EU.get("t"):
        for i, t in enumerate(EU["t"]):
            if EU["etats"][i] + EU["financieres"][i] + EU["entreprises"][i] > 1.01 * EU["total"][i]:
                E.append("[11] États-Unis %s : secteurs > total" % t)
                break
    # [13] les financières américaines
    FU = (M.get("fin_us") or {}).get("serie") or {}
    if not FU.get("t"):
        E.append("[13] pas de bloc fin_us")
    else:
        for i, t in enumerate(FU["t"]):
            parts = FU["agences"][i] + FU["titrisation"][i] + FU["banques"][i] + FU["credit"][i]
            if parts > 1.02 * FU["total"][i]:
                E.append("[13] %s : sous-secteurs %.0f > total %.0f" % (t, parts, FU["total"][i]))
                break
            if FU["autres"][i] > 0.10 * FU["total"][i]:
                E.append("[13] %s : « autres » = %.0f %% des financières" % (t, 100 * FU["autres"][i] / FU["total"][i]))
                break
    # [14] les entreprises américaines par secteur
    SU = (M.get("secteurs_us") or {}).get("serie") or {}
    TKS = ((M.get("tech") or {}).get("serie")) or {}
    if not SU.get("t"):
        E.append("[14] pas de bloc secteurs_us")
    elif EU and EU.get("t"):
        cles = [x["code"] for x in (M.get("secteurs_us") or {}).get("secteurs") or []]
        for i, t in enumerate(SU["t"]):
            if t not in EU["t"]:
                continue
            ent = EU["entreprises"][EU["t"].index(t)]
            tech = TKS["tech"][TKS["t"].index(t)] if TKS.get("t") and t in TKS["t"] else 0
            ventile = sum(SU[k][i] for k in cles) + tech
            if ventile > ent:
                E.append("[14] %s : dette ventilée %.0f > dette des entreprises américaines %.0f" % (t, ventile, ent))
                break
        for k in cles:
            v = SU.get(k) or []
            if any(v[i - 1] > 50 and abs(v[i] / v[i - 1] - 1) > 0.40 for i in range(1, len(v))):
                E.append("[14] secteur %s : saut de plus de 40 %% d'un trimestre à l'autre" % k)
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
    m9 = copy.deepcopy(S)
    for s in m9["souverains"]:
        if s["code"] == "fr":
            s["dette_fraiche"]["periode"] = "2024-T4"
    cas.append(("[9] dette fraîche périmée", m9, det))
    d10 = copy.deepcopy(det); d10["fr"]["pays"]["dette_totale"]["pct_g"][-1] += 40; cas.append(("[10] empilement faux", S, d10))
    m11 = copy.deepcopy(S); m11["marche"]["tech"]["serie"]["tech"][-1] *= 1.8; cas.append(("[11] marche tech", m11, det))
    m11b = copy.deepcopy(S); m11b["marche"]["tech"]["serie"]["t"][-1] = "2025-Q1"; cas.append(("[11] tech périmée", m11b, det))
    m11c = copy.deepcopy(S); m11c["marche"]["etats_unis"]["entreprises"] = [x * 0.02 for x in m11c["marche"]["etats_unis"]["entreprises"]]
    cas.append(("[11] tech > entreprises", m11c, det))
    m12 = copy.deepcopy(S); m12["notes_ecarts"]["quotidien"]["pays"]["it"]["ecart_pb"] = 2500; cas.append(("[12] écart invraisemblable", m12, det))
    m12b = copy.deepcopy(S); m12b["notes_ecarts"]["notes"]["fr"]["moodys"]["note"] = "AA-"; cas.append(("[12] note hors échelle", m12b, det))
    m12c = copy.deepcopy(S); m12c["notes_ecarts"]["quotidien"]["date"] = "2026-01-02"; cas.append(("[12] cotation périmée", m12c, det))
    m13 = copy.deepcopy(S)
    if (m13["marche"].get("fin_us") or {}).get("serie"):
        m13["marche"]["fin_us"]["serie"]["agences"][-1] *= 1.6
        cas.append(("[13] sous-secteur gonflé", m13, det))
    m14 = copy.deepcopy(S)
    if (m14["marche"].get("secteurs_us") or {}).get("serie"):
        m14["marche"]["secteurs_us"]["serie"]["utilities"][-1] += 9000
        cas.append(("[14] dette ventilée > total", m14, det))
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
