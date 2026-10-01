#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
construire_fondamentaux.py — les fondamentaux PAR PART D'INDICE, année par année,
sur la composition DE CHAQUE DATE (compo_mensuelle/<code>.json).

Usage : python3 construire_fondamentaux.py [--source tv|etats] <code> [<code>…]
Sortie : fondamentaux_indices.json (fusionné, un bloc par indice).

Sources des exercices :
  · hors S&P 500 : TradingView (20 exercices, une seule source pour tous les pays) ;
    l'actif net seul vient des états détaillés (cinq ans hors États-Unis) ;
  · S&P 500 : les états SEC de l'univers (cotées aujourd'hui), et pour les membres
    disparus, la SEC par leur CIK avec le cours de l'avoir du fonds IVV.
Poids : ceux de la date quand la source les donne ; sinon capitalisation × flottant
d'aujourd'hui (indices au flottant), ou cours × facteur (Nikkei 225, pondéré par
les cours).
"""
import json
import os
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import agreger_indice as AG
import cours_divisions as CD
import fonda_annuel as FA
import rattacher as RT
import tv_hist as TV

ICI = os.path.dirname(os.path.abspath(__file__))
RECH = os.path.join(ICI, "..", "recherche")
COMPO = os.environ.get("COMPO_DIR") or os.path.join(RECH, "compo_mensuelle")
AUJ = os.path.join(ICI, "..", "idx", "out")
TICKERS = {"sp500": "^GSPC", "csi300": "000300.SS", "nifty50": "^NSEI", "nikkei225": "^N225", "dax40": "^GDAXI",
           "cac40": "^FCHI", "ftse100": "^FTSE", "kospi": "^KS11", "ibov": "^BVSP", "tsx": "^GSPTSE",
           "asx200": "^AXJO", "taiex": "^TWII", "hsi": "^HSI", "ftsemib": "FTSEMIB.MI", "ibex35": "^IBEX",
           "smi": "^SSMI", "ipc": "^MXX", "mscichina": "MCHI"}
PLACE = {"sp500": "us", "csi300": None, "nifty50": "nse", "nikkei225": "tyo", "dax40": "etr", "cac40": "epa",
         "ftse100": "lon", "kospi": "krx", "ibov": "bvmf", "tsx": "tsx", "asx200": "asx", "taiex": "tpe",
         "hsi": "hkg", "ftsemib": "bit", "ibex35": "bme", "smi": "swx", "ipc": "bmv",
         "mscichina": None}   # plusieurs places : chaque membre porte la sienne
# Indices de RENDEMENT (dividendes réinvestis) : le niveau publié n'est pas un prix,
# le bénéfice « par part » s'y lit quand même (niveau × rendement), mais il grossit
# des dividendes réinvestis — dit dans la note de la fiche.
RENDEMENT = {"dax40", "ibov"}
# Sociétés RENOMMÉES encore cotées, que ni l'ISIN (absent des vieilles listes) ni le
# nom d'époque ne retrouvent. Les disparues (EDF, PSA, Lafarge, Technip…) n'ont
# plus de source libre : elles restent « non chiffrées » et la couverture le dit.
# ⚠ HOMONYMES : l'ancienne Linde AG (radiée en 2019) n'est pas Linde plc, dont la
# ligne de Francfort porte un historique sans rapport avant 2018 et dont les
# comptes antérieurs sont ceux de Praxair — P/E du DAX lu à 5,8 en 2008.
PAS_DE_SUCCESSEUR = {"LINDE AG", "Linde AG", "Linde"}
# Lignes Yahoo sans cours (ROG.SW) ou renommées (Tata Motors → TMPV après la
# scission de 2025, même société cotée).
YAHOO_RENOMME = {"ROG.SW": "RO.SW", "TATAMOTORS.NS": "TMPV.NS"}
BOURSE_TV = {"sp500": "NYSE", "cac40": "EURONEXT", "dax40": "XETR", "ftse100": "LSE", "ftsemib": "MIL",
             "ibex35": "BME", "smi": "SIX", "nikkei225": "TSE", "hsi": "HKEX", "csi300": "SSE", "nifty50": "NSE",
             "asx200": "ASX", "tsx": "TSX", "ipc": "BMV", "ibov": "BMFBOVESPA", "kospi": "KRX", "taiex": "TWSE",
             "mscichina": "HKEX"}
ALIAS = {"Nokia": "NOKIA.HE", "LafargeHolcim": "HOLN.SW", "Thomson": "VANTI.PA", "Mittal Steel": "MT.AS",
         "EUROAPI": "EAPI.PA", "Infosys Technologies Ltd.": "INFY.NS", "WOODSIDE PETROLEUM LTD": "WDS.AX",
         "FIAT SPA": "STLAM.MI", "FIAT CHRYSLER AUTOMOBILES NV": "STLAM.MI", "OHL": "OHLA.MC"}
# Même société cotée sous un autre nom (préfixe du nom d'époque → cotation actuelle).
# Fiat SpA → FCA (2014) → Stellantis (2021) : même personne morale absorbante.
ALIAS_PREFIXE = [("Red Eléctrica", "RED.MC"), ("Gas Natural", "NTGY.MC"), ("Sacyr", "SCYR.MC"),
                 ("FIAT CHRYSLER", "STLAM.MI"), ("INCITEC PIVOT", "DNL.AX"),
                 # Londres : Shell (lignes A et B jusqu'en 2022), RBS → NatWest, BHP (société
                 # jumelle, mêmes droits économiques que BHP Ltd à Sydney)
                 ("ROYAL DUTCH SHELL", "SHEL.L"), ("ROYAL BANK OF SCOTLAND", "NWG.L"), ("BHP BILLITON PLC", "BHP.AX"),
                 ("BHP GROUP PLC", "BHP.AX"),
                 # Toronto
                 ("ENCANA", "OVV.TO"), ("RESEARCH IN MOTION", "BB.TO"), ("BROOKFIELD ASSET MANAGEMENT INC", "BN.TO"),
                 ("BROOKFIELD ASSET MANAG", "BN.TO"), ("ALIMENTATION COUCHE", "ATD.TO"), ("CGI GROUP", "GIB-A.TO"),
                 ("BARRICK GOLD", "ABX.TO"),
                 # São Paulo (Vale PNA convertie en ON en 2017 ; CCR devenue Motiva)
                 ("VALE PREF", "VALE3.SA"), ("VALE SA PREF", "VALE3.SA"), ("KROTON", "COGN3.SA"),
                 ("TELEFONICA BRASIL PREF", "VIVT3.SA"), ("COMPANHIA CONCESSOES R", "MOTV3.SA"), ("LOJAS AMERICANAS", "AMER3.SA"),
                 # Mexico (séries unifiées : América Movil L → B en 2023)
                 ("AMERICA MOVIL", "AMXB.MX"), ("WALMART DE MEXICO", "WALMEX.MX"), ("GRUPO TELEVISA", "TLEVISACPO.MX"),
                 # Bombay (Tata Motors → TMPV après la scission de 2025)
                 ("Tata Motors", "TMPV.NS"), ("Mahindra & Mahindra", "M&M.NS"), ("Bajaj Auto", "BAJAJ-AUTO.NS"),
                 ("CALTEX AUSTRALIA", "ALD.AX"), ("MEDIASET", "MFEB.MI"),
                 # Zurich (bon de jouissance de Roche : pas de série Yahoo ; même bénéfice par titre)
                 ("ROCHE", "RO.SW"), ("SWISS REINSURANCE", "SREN.SW")]


def alias(nom):
    if not nom:
        return None
    if nom in ALIAS:
        return ALIAS[nom]
    for p, s in ALIAS_PREFIXE:
        if nom.startswith(p):
            return s
    return None


def symbole_par_code(m, place):
    """Code de place + suffixe Yahoo, quand l'univers ne connaît pas la ligne mais
    que Yahoo a des cours (membres sortis de l'univers, codes d'époque)."""
    code = str(m.get("code_place") or m.get("ticker") or "").strip().rstrip("*")
    pl = (m.get("place") or place or "").lower()
    if not code:
        return None
    if pl in ("", "none") and code.isdigit() and len(code) == 6:
        suf = ".SS" if code.startswith("6") else ".SZ"
    else:
        suf = RT.SUFFIXE.get(pl)
    if suf is None:
        return None
    for v in dict.fromkeys([code, code.replace(".", "-"), code.replace(" ", "-"), code.replace("/", "-")]):
        c = CD.cours(v + suf)
        if c and len(c.get("mois") or {}) >= 12:
            return v + suf
    return None


def niveaux_mensuels(code):
    """Clôture de fin de mois de l'indice. ⚠ Yahoo ne rend qu'UN point pour le
    CSI 300 : ses clôtures officielles viennent de China Securities Index (la source
    de la fiche, contrôlée contre EastMoney et Sina : écart maximal 0,01 point)."""
    if code == "csi300":
        import requests
        u = ("https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=000300&startDate=20020101&endDate=%s"
             % time.strftime("%Y%m%d"))
        try:
            d = requests.get(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                                         "Referer": "https://www.csindex.com.cn/"}, timeout=60).json()
            out = {}
            for x in sorted(d.get("data") or [], key=lambda x: str(x.get("tradeDate"))):
                t, c = str(x.get("tradeDate") or ""), x.get("close")
                if len(t) == 8 and isinstance(c, (int, float)) and c > 0:
                    out["%s-%s" % (t[:4], t[4:6])] = float(c)   # la dernière séance du mois l'emporte
            return out
        except Exception:
            return {}
    return (CD.cours(TICKERS[code]) or {}).get("mois") or {}


def mois_decembre(M, y):
    """La composition de décembre Y, sinon le mois le plus proche avant (≤ 3 mois)."""
    for m in ("12", "11", "10", "09"):
        if "%d-%s" % (y, m) in M:
            return "%d-%s" % (y, m)
    return None


def compo(code):
    if code != "sp500":
        return json.load(open(os.path.join(COMPO, code + ".json")))
    # S&P 500 : les avoirs de l'IVV, avec le cours (valeur / titres) pour la route SEC
    ivv = json.load(open(os.path.join(RECH, "ivv_trimestres.json")))
    M = {}
    for d, v in ivv["dates"].items():
        M[d[:7]] = {"date": d, "membres": [
            {"isin": l.get("isin"), "ticker": None, "place": "us", "nom": l["nom"], "poids": l["poids"],
             "_prix": (l["valeur_usd"] / l["titres"]) if l.get("titres") and l.get("valeur_usd") else None,
             "_titres": l.get("titres")} for l in v["lignes"]]}
    return {"mois": M, "source": ivv.get("fonds") + " — avoirs trimestriels déposés à la SEC", "exacts": True}


def facteurs_aujourdhui(code, sym_de):
    """ff : poids exact d'aujourd'hui / part de capitalisation ; paf (Nikkei) :
    poids / cours. Lus dans la fiche du jour (idx/out/indice_<code>.json)."""
    f = os.path.join(AUJ, "indice_%s.json" % code)
    if not os.path.exists(f):
        return None, None
    d = json.load(open(f))
    ch = d["champs"]
    i_s, i_w, i_c = ch.index("sym"), ch.index("poids"), ch.index("capi_usd")
    L = [(l[i_s], l[i_w], l[i_c]) for l in d["lignes"] if l[i_w] and l[i_c]]
    if code == "nikkei225":
        paf = {}
        for s, w, _ in L:
            c = CD.cours(s) or {}
            p = (c.get("mois") or {}).get(max(c.get("mois") or {"": 0}))
            if p:
                paf[s] = w / p
        if paf:
            paf["_med"] = statistics.median(v for k, v in paf.items() if k != "_med")
        return None, paf
    sw, sc = sum(w for _, w, _ in L), sum(c for _, _, c in L)
    ff = {s: (w / sw) / (c / sc) for s, w, c in L}
    if ff:
        ff["_med"] = statistics.median(ff.values())
    return ff, None


def paf_archives(M, sym_de, paf):
    """Nikkei : facteur des membres SORTIS, tiré des mois archivés où le fichier
    officiel donne les poids — mis à l'échelle d'aujourd'hui par les membres communs."""
    for k in sorted(M, reverse=True):
        mem = [m for m in M[k]["membres"] if m.get("poids")]
        if not mem:
            continue
        r = {}
        for m in mem:
            s = sym_de(m)
            p = ((CD.cours(s) or {}).get("mois") or {}).get(k) if s else None
            if p:
                r[s] = m["poids"] / p
        comm = [paf[s] / r[s] for s in r if s in paf and s != "_med"]
        if len(comm) < 50:
            continue
        lam = statistics.median(comm)
        for s, v in r.items():
            paf.setdefault(s, lam * v)
    return paf


def construire(code, u, rt, chg, source):
    d = compo(code)
    M = d["mois"]
    niv = niveaux_mensuels(code)
    annees = sorted({int(k[:4]) for k in M if int(k[:4]) < int(time.strftime("%Y"))})
    memo_sym = {}
    cik_tic = {}
    an = None
    if code == "sp500":
        import sec_natif as SN
        an = SN.Annuaire()
        # la cotation AMÉRICAINE d'un CIK (GE avait « GCP.F », une ligne de Francfort)
        us = lambda t: "." not in t or bool(re.match(r"^[A-Z]+\.[A-Z]$", t))
        for t, v in sorted(u.sec.items(), key=lambda kv: (not us(kv[0]), len(kv[0]))):
            if v.get("cik") and "-" not in t and us(t):
                cik_tic.setdefault(int(v["cik"]), t)

    def sym_de(m):
        cle = (m.get("isin"), m.get("ticker") or m.get("code_place"), m.get("nom"))
        if code == "sp500" and "berkshire" in (m.get("nom") or "").lower():
            return None   # route SEC dédiée (BPA par action A, cours de la B)
        if cle not in memo_sym:
            s = alias(m.get("nom"))
            if m.get("nom") in PAS_DE_SUCCESSEUR and not (m.get("isin") or "").startswith("IE"):
                memo_sym[cle] = None
                return None
            if not s:
                # La cotation de la PLACE DE L'INDICE d'abord : le nom ou l'ISIN
                # trouvaient parfois une autre ligne (Reckitt → gré à gré américain,
                # Anglo American → Zurich), sans série TradingView ou dans une autre
                # devise.
                s0, meth = rt.symbole(m, PLACE[code])
                suf = RT.SUFFIXE.get((m.get("place") or PLACE[code] or "").lower())
                maison = bool(s0) and suf is not None and (s0.endswith(suf) if suf else "." not in s0)
                # une AUTRE place n'est admise que trouvée par l'ISIN : le nom seul
                # envoyait des sociétés chinoises radiées vers un gré à gré américain
                s = s0 if maison or code == "sp500" else (symbole_par_code(m, PLACE[code]) or (s0 if meth == "isin" else None))
            s = YAHOO_RENOMME.get(s, s)
            # cotation sans cours ou introuvable : l'ISIN chez TradingView
            if code != "sp500" and m.get("isin") and (not s or not CD.cours(s)):
                tvs = TV.recherche_isin(m["isin"], BOURSE_TV.get(code))
                y = TV.yahoo_de_tv(tvs, m["isin"]) if tvs else None
                if y and CD.cours(y):
                    s = y
                    TV.FORCE[y] = tvs
            if not s and an is not None:
                # même CIK qu'une société cotée aujourd'hui (Wal-Mart Stores → WMT)
                s = next((cik_tic[c] for c in an.ciks(m["nom"]) if c in cik_tic), None)
            memo_sym[cle] = s
        return memo_sym[cle]
    tous = {}
    for k in M:
        for m in M[k]["membres"]:
            s = sym_de(m)
            if s:
                tous[s] = 1
    with ThreadPoolExecutor(6) as ex:
        list(ex.map(CD.cours, list(tous)))
    tv = TV.charger(list(tous)) if source == "tv" else None
    if tv is not None:
        # cotations sans série TradingView par leur code : l'ISIN, quand la composition le donne
        isin_de = {}
        for k in M:
            for m in M[k]["membres"]:
                if m.get("isin"):
                    s = sym_de(m)
                    if s:
                        isin_de.setdefault(s, m["isin"])
        rate = [s for s in tous if not tv.get(s, {}).get("_tv") and s in isin_de]
        for s in rate:
            tvs = TV.recherche_isin(isin_de[s], BOURSE_TV.get(code))
            if tvs:
                TV.FORCE[s] = tvs
        if rate:
            tv.update(TV.charger(rate))
    ff, paf = facteurs_aujourdhui(code, sym_de)
    if paf is not None:
        paf = paf_archives(M, sym_de, paf)
    repli = None
    if code == "sp500":
        import repli_sec
        dates = sorted(M)
        suivant = {k: {m["nom"]: m.get("_titres") for m in M[dates[i + 1]]["membres"]} for i, k in enumerate(dates[:-1])}
        repli = repli_sec.ReplisSEC(an, suivant)
    memo = {}
    out = {"annees": [], "mois_compo": []}
    for y in annees:
        k = mois_decembre(M, y)
        if not k or k not in niv:
            continue
        r = AG.annee(u, [dict(m) for m in M[k]["membres"]], niv[k], k, chg, memo, sym_de,
                     repli=repli, tv=tv, ff=ff, paf=paf)
        out["annees"].append(y)
        out["mois_compo"].append(k)
        out.setdefault("niveau", []).append(round(niv[k], 2))
        for c in ("bn", "ca", "divid", "anp", "pe", "mn", "rdt_div", "pb", "cov_ni", "cov_rev", "cov_div", "cov_eq",
                  "cov_n", "n", "n_chiffres", "poids_estimes", "routes"):
            v = r.get(c)
            out.setdefault(c, []).append(round(v, 4) if isinstance(v, float) else v)
        out.setdefault("manquants", []).append(r["manquants"][:5])
        out.setdefault("ecartes", []).append(r.get("ecartes") or [])
        # détail membre par membre (audit) : DETAIL=2012,2020
        if str(y) in (os.environ.get("DETAIL") or "").split(","):
            out.setdefault("_detail", {})[str(y)] = r["_detail"]
    out["source_compo"] = d.get("source")
    out["exacts"] = d.get("exacts")
    out["source_exercices"] = source
    out["indice_rendement"] = code in RENDEMENT
    out["rattaches"] = "%d membres distincts, %d rattachés" % (len(memo_sym), sum(1 for v in memo_sym.values() if v))
    out["tv_trouves"] = sum(1 for s in tous if tv and tv.get(s, {}).get("_tv")) if tv else None
    if repli:
        out["replis_sec"] = repli.stats
    return out


def main():
    args = sys.argv[1:]
    source = "tv"
    if args and args[0] == "--source":
        source, args = args[1], args[2:]
    u = FA.Univers(os.path.join(ICI, "cache"))
    rt = RT.Rattacheur()
    chg = AG.Changes()
    f = os.path.join(ICI, os.environ.get("SORTIE", "fondamentaux_indices.json"))
    tout = json.load(open(f)) if os.path.exists(f) else {}
    for code in args:
        t0 = time.time()
        src = "etats" if code == "sp500" and source == "tv" and os.environ.get("SP_TV") != "1" else source
        tout[code] = construire(code, u, rt, chg, src)
        o = tout[code]
        print("[ok] %s en %.0f s — %s — TV %s — %s" % (code, time.time() - t0, o["rattaches"], o["tv_trouves"], o.get("replis_sec")), flush=True)
        for i, y in enumerate(o["annees"]):
            print("   %d  BPA %9s  CA %9s  P/E %6s  marge %5s  div %5s  P/B %5s  couv %5s/%5s n %s/%s %s manque %s"
                  % (y, o["bn"][i] and round(o["bn"][i], 1), o["ca"][i] and round(o["ca"][i], 0), o["pe"][i] and round(o["pe"][i], 1),
                     o["mn"][i] and round(o["mn"][i], 1), o["rdt_div"][i] and round(o["rdt_div"][i], 2),
                     o["pb"][i] and round(o["pb"][i], 2), o["cov_ni"][i], o["cov_eq"][i], o["n_chiffres"][i], o["n"][i],
                     o["routes"][i], [n[:18] for _, n in o["manquants"][i][:3]]), flush=True)
        json.dump(tout, open(f, "w"), ensure_ascii=False)


if __name__ == "__main__":
    main()
