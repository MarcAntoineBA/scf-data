#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agreger_indice.py — les fondamentaux PAR PART D'INDICE, année civile par année.

Pour l'année Y, à la fin de décembre (composition et poids de cette date) :
    rendement_x(i) = grandeur_x(i) / capitalisation(i, décembre Y)
        x = bénéfice net, chiffre d'affaires, dividendes versés, fonds propres,
        grandeur de l'exercice clos au plus tard le 31/12/Y (et depuis moins de
        quinze mois), convertie dans la devise de cotation au taux de décembre ;
    R_x = Σ poids(i) × rendement_x(i) / Σ poids des membres chiffrés
    par part : X(Y) = niveau de l'indice(décembre Y) × R_x
C'est la définition du BPA d'un indice (celui que S&P publie pour le S&P 500) :
Σ actions de l'indice × BPA / diviseur = niveau × Σ poids × bénéfice / capitalisation.
Un membre sans données est compté au rendement moyen des autres (imputation
standard) ; la COUVERTURE (part du poids chiffrée) est publiée avec chaque point,
et un point sous COUV_MIN n'est pas publié.
"""
import math
from datetime import date

import cours_divisions as CD
import tv_hist as TV

COUV_MIN = 0.90
DEVISE_CENTIEMES = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}


class Changes:
    """Taux mensuels (clôture de fin de mois) via Yahoo : 1 unité de A en B."""
    def __init__(self):
        self.memo = {}

    def taux(self, a, b, mois):
        if a == b or not a or not b:
            return 1.0
        # tout passe par le dollar
        return self._usd(a, mois) / self._usd(b, mois) if self._usd(a, mois) and self._usd(b, mois) else None

    def _usd(self, dev, mois):
        if dev == "USD":
            return 1.0
        if dev not in self.memo:
            c = CD.cours("%sUSD=X" % dev) or {}
            self.memo[dev] = c.get("mois") or {}
        s = self.memo[dev]
        if mois in s:
            return s[mois]
        # mois voisin (au plus 2 mois avant) : un taux ne manque presque jamais
        y, m = int(mois[:4]), int(mois[5:7])
        for k in range(1, 3):
            mm = m - k
            yy = y + (mm - 1) // 12
            mm = (mm - 1) % 12 + 1
            v = s.get("%04d-%02d" % (yy, mm))
            if v:
                return v
        return None


def rendements(u, sym, mois, chg, memo):
    """{ni, rev, div, eq} rapportés à la capitalisation du mois, ou None."""
    if sym not in memo:
        c = CD.cours(sym)
        se = u.resoudre(sym)
        ex, dev = u.exercices(se) if se else ([], None)
        if c and ex:
            u.normaliser(ex, c["divisions"])
        memo[sym] = (c, ex, dev)
    c, ex, dev = memo[sym]
    if not c or not ex:
        return None
    cap = u.mcap(ex, c["mois"], mois)
    if not cap or cap <= 0:
        return None
    dev_cot = c.get("devise")
    if dev_cot in DEVISE_CENTIEMES:
        dev_cot, div100 = DEVISE_CENTIEMES[dev_cot]
        cap /= div100
    # ⚠ L'EXERCICE DE L'ANNÉE Y (règle du Comparateur) : clos de juin Y à mai Y+1.
    # « Le dernier clos avant décembre » comptait NVIDIA (clôture fin janvier) sur son
    # exercice précédent — onze mois de retard sur +65 % de bénéfice.
    y = int(mois[:4])
    cands = [e for e in ex if "%d-06-01" % y <= e["fin"] <= "%d-05-31" % (y + 1)]
    x = cands[-1] if cands else None
    if not x:
        return None
    t = chg.taux(x.get("devise") or dev or dev_cot, dev_cot, mois)
    if not t:
        return None
    out = {}
    for k in ("ni", "rev", "div", "eq"):
        v = x.get(k)
        if k == "div" and v is None and x.get("dps") and x.get("sh"):
            v = x["dps"] * x["sh"]
        out[k] = (v * t / cap) if v is not None else None
    # Une société SANS dividende n'a pas de ligne « dividendes versés » : elle compte
    # pour zéro (Amazon, Alphabet), pas comme une donnée manquante — sinon aucun
    # rendement du dividende du S&P 500 n'atteignait la couverture.
    if out.get("div") is None and out.get("ni") is not None and not x.get("dps"):
        out["div"] = 0.0
    out["fin"] = x["fin"]
    return out


def _prix_majeur(c, mois):
    """(cours ajusté du mois dans l'unité PRINCIPALE, devise) — pence → livres."""
    p = (c or {}).get("mois", {}).get(mois)
    dev = (c or {}).get("devise")
    if not p:
        return None, dev
    if dev in DEVISE_CENTIEMES:
        dev, d = DEVISE_CENTIEMES[dev]
        p /= d
    return p, dev


def rendements_tv(u, sym, mois, chg, memo, tvd):
    """Rendements par la série TradingView (20 exercices, BPA et dividende ajustés
    des divisions, montants dans la devise de cotation) :
        bénéfice / capitalisation = BPA / cours ajusté (même base d'actions)
        CA / capitalisation       = CA / actions implicites (bénéfice / BPA) / cours
        dividende / capitalisation = dividende par action / cours
    L'actif net vient des états détaillés (moteur) quand ils couvrent l'année."""
    if not tvd or not tvd.get("_tv"):
        return None
    k = ("tv", sym)
    if k not in memo:
        ex = TV.exercices(tvd)
        memo[k] = (ex, TV.actions_implicites(ex), {x["an"]: x for x in ex})
    ex, sh, par_an = memo[k]
    c = memo[sym][0] if sym in memo else CD.cours(sym)
    P, dev = _prix_majeur(c, mois)
    if not P or P <= 0:
        return None
    y = int(mois[:4])
    x = par_an.get(y)
    if not x or x.get("eps") is None:
        return None
    dev_tv = tvd.get("currency")
    dev_tv = DEVISE_CENTIEMES[dev_tv][0] if dev_tv in DEVISE_CENTIEMES else dev_tv
    t = chg.taux(dev_tv, dev, mois) if dev_tv and dev and dev_tv != dev else 1.0
    if not t:
        return None
    out = {"ni": x["eps"] * t / P, "fin": str(y), "route": "tv"}
    out["rev"] = x["rev"] * t / sh[y] / P if x.get("rev") is not None and sh.get(y) else None
    if x.get("dps") is not None:
        out["div"] = x["dps"] * t / P
    else:
        # jamais de dividende dans l'historique : zéro ; sinon inconnu
        out["div"] = 0.0 if not any((e.get("dps") or 0) > 0 for e in ex) else None
    out["eq"] = None
    out["_sh"], out["_P"], out["_dev"] = sh.get(y), P, dev
    return out


def annee(u, membres, niveau, mois, chg, memo, sym_de, repli=None, tv=None, ff=None, paf=None):
    """membres : [{"poids": %|None, ...}] ; sym_de(m) → symbole Yahoo ou None.
    tv : {symbole: données TradingView} — route PRINCIPALE quand elle est donnée
         (une seule source pour tous les exercices : pas de rupture de série au
         moment où les états détaillés commencent) ; les états détaillés ne
         servent alors qu'à l'actif net.
    repli(m, mois) → mêmes rendements par une autre source (sociétés disparues,
    lues à la SEC par leur CIK) quand la cotation actuelle ne donne rien.
    ff : {symbole: facteur de flottant d'aujourd'hui} — poids estimés = capitalisation
         × flottant (indices pondérés par le flottant, sans poids à la date).
    paf : {symbole: facteur de prix} — indice pondéré par les COURS (Nikkei 225) :
         poids estimés = cours ajusté × facteur.
    Rend {bn, ca, divid, anp, pe, mn, rdt_div, pb, cov, n, n_chiffres}."""
    lignes = []
    for m in membres:
        s = sym_de(m)
        r = None
        if s and tv is not None:
            r = rendements_tv(u, s, mois, chg, memo, tv.get(s))
            re = rendements(u, s, mois, chg, memo)
            if r and re and re.get("eq") is not None:
                r["eq"] = re["eq"]
            if not r:
                r = re
        elif s:
            r = rendements(u, s, mois, chg, memo)
        if repli and not (r and r.get("ni") is not None):
            r2 = repli(m, mois)
            if r2:
                r, m["_route"] = r2, "repli"
        # ⚠ Un bénéfice SUPÉRIEUR à la capitalisation est une erreur de données
        # (unités, classe d'actions), pas un placement miracle : écarté et compté.
        # Les PERTES extrêmes restent (AIG 2008 : −24 fois sa capitalisation, et le
        # BPA publié du S&P 500 cette année-là en porte la trace).
        if r and r.get("ni") is not None and r["ni"] > 1:
            m["_ecarte"] = r["ni"]
            r = None
        lignes.append((m, s, r))
    estimes = any(m.get("poids") is None for m, _, _ in lignes)
    if estimes:
        vals = {}
        for m, s, r in lignes:
            v = None
            if s and s in memo and memo[s][0]:
                c = memo[s][0]
                P, dev = _prix_majeur(c, mois)
                if paf is not None:
                    v = P * paf.get(s, paf.get("_med")) if P else None
                else:
                    cap = None
                    if r and r.get("_sh") and r.get("_P"):
                        cap = r["_sh"] * r["_P"]
                    elif memo[s][1]:
                        cap = u.mcap(memo[s][1], c["mois"], mois)
                        if cap and c.get("devise") in DEVISE_CENTIEMES:
                            cap /= DEVISE_CENTIEMES[c["devise"]][1]
                    t = chg.taux(dev, "USD", mois) if cap else None
                    v = cap * t if cap and t else None
                    if v and ff is not None:
                        v *= ff.get(s, ff.get("_med", 1.0))
            vals[id(m)] = v
        connus = [(m, vals.get(id(m))) for m, _, _ in lignes if m.get("poids") is not None and vals.get(id(m))]
        if connus:
            # Poids PARTIELS (CSI 300 2016-2021 : membres suspendus absents du fonds) :
            # les poids connus restent, les inconnus sont estimés à la même échelle.
            a = sum(m["poids"] for m, _ in connus) / sum(v for _, v in connus)
            for m, _, _ in lignes:
                if m.get("poids") is None:
                    m["_poids_est"] = a * (vals.get(id(m)) or 0)
                    m["_poids_connu"] = bool(vals.get(id(m)))
        else:
            tot = sum(v for v in vals.values() if v) or 1.0
            for m, _, _ in lignes:
                m["_poids_est"] = 100 * (vals.get(id(m)) or 0) / tot
                m["_poids_connu"] = bool(vals.get(id(m)))
    tot_w = sum((m["poids"] if m.get("poids") is not None else m.get("_poids_est", 0)) for m, _, _ in lignes) or 1.0
    res = {"n": len(lignes)}
    for k in ("ni", "rev", "div", "eq"):
        num = w_ok = 0.0
        for m, s, r in lignes:
            w = m["poids"] if m.get("poids") is not None else m.get("_poids_est", 0)
            if r and r.get(k) is not None and math.isfinite(r[k]):
                num += w * r[k]
                w_ok += w
        res["cov_" + k] = round(100 * w_ok / tot_w, 1)
        res["r_" + k] = num / w_ok if w_ok else None
    res["n_chiffres"] = sum(1 for _, _, r in lignes if r and r.get("ni") is not None)
    # ⚠ Poids ESTIMÉS : un membre sans cours n'a pas de capitalisation, donc un poids
    # inconnu (et non nul) — la couverture en poids se lisait « 100 % » avec 22
    # membres chiffrés sur 40. On exige alors aussi la couverture en NOMBRE.
    res["cov_n"] = round(100 * res["n_chiffres"] / (res["n"] or 1), 1)
    ok = lambda k: (res["r_" + k] is not None and res["cov_" + k] >= 100 * COUV_MIN
                    and (not estimes or res["cov_n"] >= 100 * COUV_MIN))
    res["bn"] = niveau * res["r_ni"] if ok("ni") else None
    res["ca"] = niveau * res["r_rev"] if ok("rev") else None
    res["divid"] = niveau * res["r_div"] if ok("div") else None
    res["anp"] = niveau * res["r_eq"] if ok("eq") else None
    res["pe"] = 1 / res["r_ni"] if ok("ni") and res["r_ni"] > 0 else None
    res["mn"] = 100 * res["r_ni"] / res["r_rev"] if ok("ni") and ok("rev") and res["r_rev"] else None
    res["rdt_div"] = 100 * res["r_div"] if ok("div") else None
    res["pb"] = 1 / res["r_eq"] if ok("eq") and res["r_eq"] > 0 else None
    res["poids_estimes"] = estimes
    res["routes"] = {}
    for m, s, r in lignes:
        if r and r.get("ni") is not None:
            k = m.get("_route") or r.get("route") or "etats"
            res["routes"][k] = res["routes"].get(k, 0) + 1
    res["ecartes"] = [(m.get("nom"), round(m["_ecarte"], 2)) for m, _, _ in lignes if m.get("_ecarte")]
    res["_detail"] = [(m.get("nom"), s, (m.get("_route") or (r or {}).get("route") or "etats") if r else None,
                       m["poids"] if m.get("poids") is not None else m.get("_poids_est", 0),
                       {k: (r or {}).get(k) for k in ("ni", "rev", "div", "eq")}) for m, s, r in lignes]
    res["manquants"] = sorted(((m.get("poids") if m.get("poids") is not None else m.get("_poids_est", 0), m.get("nom"))
                               for m, s, r in lignes if not (r and r.get("ni") is not None)), reverse=True)[:8]
    return res
