#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Crédit d'entreprise — cinq segments (module de fetch_obligations.py).

CE QUE L'ON MESURE, par segment :
  - l'ÉCART DE CRÉDIT (OAS, en pb) : ce que l'entreprise paie en plus de l'État
    à échéance égale, options retirées. C'est le prix du risque de défaut.
  - le RENDEMENT effectif : ce que rapporte l'obligation achetée aujourd'hui.
  - l'échelle des notations (AAA → CCC), la duration, la performance totale
    d'un ETF qui réplique le segment, les émissions, les défauts.

⚠ ICE BofA sur FRED : 3 ANS d'historique seulement depuis avril 2026 (fenêtre
  glissante). L'historique long vient d'ailleurs : Moody's Baa − Trésor
  (quotidien depuis 1986), GZ spread de la Fed (mensuel depuis 1973),
  Bundesbank (entreprises allemandes − État fédéral, depuis 1979).
⚠ LICENCES : ICE (« internal use only »), Moody's et iShares restreignent la
  republication ; BCE, Bundesbank, Fed et BIS sont libres en citant la source.
  `SOURCES_SOUS_LICENCE` coupe les premières d'un coup.
⚠ Pas d'« euro IG » libre : ni FRED ni la BCE. Le rendement euro IG est
  CALCULÉ ligne à ligne sur les avoirs publiés d'un ETF (SPDR SYBC), l'écart
  face à la courbe AAA de la BCE à la même duration — un G-spread, pas un OAS,
  et c'est dit.
"""
import csv
import io
import json
import math
import os
import re
import html as H
from datetime import date, datetime

from oblig_net import (get, get_txt, get_json, fred, yahoo_jours, log, estnb, compacter, en_colonnes, hebdo, mensuel,
                       variations, centile, extremes, perf, perf_ytd, repli_max, valeur_au, il_y_a)

SOURCES_SOUS_LICENCE = os.environ.get("SCF_OBLIG_LICENCES", "1") != "0"

SEGMENTS = [
    {"code": "us-ig", "nom": "Entreprises américaines, qualité investissement", "court": "US qualité investissement",
     "anglais": "US Investment grade", "zone": "États-Unis", "pays": "États-Unis", "devise": "USD", "famille": "ig",
     "oas": "BAMLC0A0CM", "rdt": "BAMLC0A0CMEY", "tr": "BAMLCC0A0CMTRIV",
     "echelle": [("AAA", "BAMLC0A1CAAA"), ("AA", "BAMLC0A2CAA"), ("A", "BAMLC0A3CA"), ("BBB", "BAMLC0A4CBBB")],
     "etf": "LQD", "etf_nom": "iShares iBoxx $ Investment Grade Corporate Bond", "etf_car": ["LQD", "USIG", "IGLB"],
     "etat": "us", "notation": "A−", "notation_note": "notation moyenne des lignes de LQD (AAA 1 %, AA 12 %, A 46 %, BBB 40 %)"},
    {"code": "us-hy", "nom": "Entreprises américaines, haut rendement", "court": "US haut rendement",
     "anglais": "US High Yield", "zone": "États-Unis", "pays": "États-Unis", "devise": "USD", "famille": "hy",
     "oas": "BAMLH0A0HYM2", "rdt": "BAMLH0A0HYM2EY", "tr": "BAMLHYH0A0HYM2TRIV",
     "echelle": [("BB", "BAMLH0A1HYBB"), ("B", "BAMLH0A2HYB"), ("CCC et moins", "BAMLH0A3HYC")],
     "etf": "HYG", "etf_nom": "iShares iBoxx $ High Yield Corporate Bond", "etf_car": ["HYG", "USHY", "FALN"],
     "etat": "us", "notation": "B+", "notation_note": "segment spéculatif : BB, B et CCC"},
    {"code": "eu-ig", "nom": "Entreprises de la zone euro, qualité investissement", "court": "Euro qualité investissement",
     "anglais": "Euro Investment grade", "zone": "Zone euro", "pays": "Zone euro", "devise": "EUR", "famille": "ig",
     "etf": "D5BG.DE", "etf_nom": "Xtrackers EUR Corporate Bond (part capitalisante)", "etat": "de",
     "avoirs": "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-sybc-gy.xlsx",
     "avoirs_nom": "SPDR Bloomberg Euro Corporate Bond (SYBC)", "notation": "A−", "notation_note": "segment qualité investissement : AAA à BBB−"},
    {"code": "eu-hy", "nom": "Entreprises de la zone euro, haut rendement", "court": "Euro haut rendement",
     "anglais": "Euro High Yield", "zone": "Zone euro", "pays": "Zone euro", "devise": "EUR", "famille": "hy",
     "oas": "BAMLHE00EHYIOAS", "rdt": "BAMLHE00EHYIEY", "tr": "BAMLHE00EHYITRIV",
     "etf": "XHYA.DE", "etf_nom": "Xtrackers EUR High Yield Corporate Bond (part capitalisante)", "etat": "de",
     "avoirs": "https://www.ssga.com/library-content/products/fund-data/etfs/emea/holdings-daily-emea-en-sybj-gy.xlsx",
     "avoirs_nom": "SPDR Bloomberg Euro High Yield Bond (SYBJ)", "notation": "BB−", "notation_note": "segment spéculatif : BB à CCC"},
    {"code": "em", "nom": "Entreprises des pays émergents (en dollars)", "court": "Émergents, entreprises",
     "anglais": "EM corporates", "zone": "Émergents", "pays": "Monde", "devise": "USD", "famille": "em",
     "oas": "BAMLEMCBPIOAS", "rdt": "BAMLEMCBPIEY", "tr": "BAMLEMCBPITRIV",
     "echelle": [("Qualité investissement", "BAMLEMIBHGCRPIOAS"), ("Haut rendement", "BAMLEMHBHYCRPIOAS")],
     "etf": "CEMB", "etf_nom": "iShares J.P. Morgan EM Corporate Bond", "etf_car": ["CEMB", "EMHY"],
     "etat": "us", "notation": "BBB−", "notation_note": "indice mêlant qualité investissement et haut rendement"},
]

# ── Défauts : aucun fichier libre et structuré n'existe. Relevé DATÉ des
#    chiffres publiés (Moody's repris par First Trust, S&P repris par l'AFME).
#    À tenir à jour à la main ; la fiche affiche la date du relevé. ─────────
DEFAUTS = {
    "us": {"taux": 5.3, "au": "2026-08", "quoi": "taux de défaut spéculatif sur 12 mois, États-Unis (Moody's, provisoire)",
           "url": "https://www.ftportfolios.com/Blogs/InvestBlog/2026/9/17/factoid---thursday,-september-17,-2026",
           "precedent": {"taux": 5.0, "au": "2026-07"}, "moyenne_hist": None,
           "prevision": {"taux": 4.3, "au": "2026-12", "par": "Moody's, scénario central"}},
    "monde": {"taux": 4.5, "au": "2026-08", "quoi": "taux de défaut spéculatif mondial sur 12 mois (Moody's, provisoire)",
              "url": "https://www.ftportfolios.com/Blogs/InvestBlog/2026/9/17/factoid---thursday,-september-17,-2026",
              "precedent": {"taux": 4.4, "au": "2026-07"}, "moyenne_hist": 4.2,
              "prevision": {"taux": 3.6, "au": "2026-12", "par": "Moody's, scénario central"}},
    "europe": {"taux": 2.6, "au": "2026-06", "quoi": "taux de défaut spéculatif européen sur 12 mois (S&P, repris par l'AFME)",
               "url": "https://www.afme.eu/publications/data-research/european-high-yield-leveraged-loan-and-private-credit-report-q2-2026/",
               "precedent": {"taux": 3.3, "au": "2026-03"}, "moyenne_hist": None, "prevision": None},
}
DEFAUT_DE = {"us-ig": "us", "us-hy": "us", "eu-ig": "europe", "eu-hy": "europe", "em": "monde"}


# ── Sources complémentaires ─────────────────────────────────────────────────
def gz_spread():
    """Fed : GZ spread (écart moyen des obligations d'entreprises US) — mensuel, 1973."""
    t = get_txt("https://www.federalreserve.gov/econres/notes/feds-notes/ebp_csv.csv")
    if not t:
        return []
    out = []
    for r in csv.DictReader(io.StringIO(t)):
        try:
            dd = r["date"].strip()
            if "/" in dd:
                m, j, a = dd.split("/")[:3]
                dd = "%04d-%02d-%02d" % (int(a[:4]), int(m), int(j))
            out.append((dd[:10], float(r["gz_spread"])))
        except (ValueError, KeyError):
            pass
    return out


def bundesbank(cle):
    t = get_txt("https://api.statistiken.bundesbank.de/rest/data/BBSIS/" + cle, entetes={"Accept": "text/csv"})
    if not t:
        return []
    t = t.lstrip("﻿")
    out = []
    for r in csv.DictReader(io.StringIO(t), delimiter=";"):
        v = (r.get("OBS_VALUE") or "").replace(",", ".")
        d = r.get("TIME_PERIOD") or ""
        if v in ("", ".") or not d[:1].isdigit():
            continue
        try:
            out.append((d if len(d) == 10 else d + "-28", float(v)))
        except ValueError:
            pass
    return out


def ecb(flux, cle, debut=None):
    u = "https://data-api.ecb.europa.eu/service/data/%s/%s?format=csvdata" % (flux, cle) + ("&startPeriod=" + debut if debut else "")
    t = get_txt(u, entetes={"Accept": "text/csv"})
    if not t:
        return []
    out = []
    for r in csv.DictReader(io.StringIO(t)):
        try:
            out.append((r["TIME_PERIOD"], float(r["OBS_VALUE"])))
        except (ValueError, KeyError):
            pass
    return out


def ishares_ecran():
    """Écran iShares US : rendement à maturité, duration effective (modelOad), OAS."""
    if not SOURCES_SOUS_LICENCE:
        return {}
    j = get_json("https://www.ishares.com/us/product-screener/product-screener-v3.1.jsn?dcrPath=/templatedata/"
                 "config/product-screener-v3/data/en/us-ishares/ishares-product-screener-backend-config"
                 "&siteEntryPassthrough=true", accept="application/json")
    if not isinstance(j, dict):
        return {}

    def g(d, k):
        x = d.get(k)
        return x.get("r") if isinstance(x, dict) else x

    def f(x):
        try:
            return round(float(x), 2)
        except (TypeError, ValueError):
            return None

    out = {}
    for pid, d in j.items():
        t = d.get("localExchangeTicker")
        if not t:
            continue
        out[t] = {"ytm": f(g(d, "fxHedgedYield")), "duration": f(g(d, "modelOad")), "oas_pb": f(g(d, "optionAdjustedSpread")),
                  "au": g(d, "optionAdjustedSpreadAsOf"), "sec_30j": f(g(d, "thirtyDaySecYield")), "id": pid,
                  "nom": d.get("fundName")}
    return out


# ── Euro : rendement et duration recalculés ligne à ligne ───────────────────
def _rdt_ligne(prix, coupon, ech, auj):
    """Rendement actuariel et duration modifiée d'une obligation à coupon ANNUEL
    (convention des obligations en euro), à partir du prix PIED DE COUPON :
    le coupon couru est ajouté, les dates de coupon sont les anniversaires de
    l'échéance. Rend (rendement %, duration modifiée) ou None."""
    if not (prix and prix > 1 and ech > auj):
        return None
    ans = (ech - auj).days / 365.25
    if ans < 0.1 or ans > 60:
        return None
    n = int(math.floor(ans))
    frac = ans - n                         # temps jusqu'au prochain coupon
    couru = coupon * (1 - frac) if frac > 0 else 0.0
    sale = prix + couru
    temps = [frac + k for k in range(n + 1)] if frac > 1e-6 else [float(k) for k in range(1, n + 1)]
    flux = [coupon] * len(temps)
    flux[-1] += 100.0

    def pv(y):
        return sum(c / (1 + y) ** t for c, t in zip(flux, temps))

    lo, hi = -0.05, 0.60
    if (pv(lo) - sale) * (pv(hi) - sale) > 0:
        return None
    for _ in range(80):
        mi = (lo + hi) / 2
        if pv(mi) > sale:
            lo = mi
        else:
            hi = mi
    y = (lo + hi) / 2
    p = pv(y)
    mac = sum(t * c / (1 + y) ** t for c, t in zip(flux, temps)) / p
    return 100 * y, mac / (1 + y)


def avoirs_ssga(url):
    """Rendement et duration moyens pondérés d'un ETF SPDR, depuis ses avoirs."""
    if not SOURCES_SOUS_LICENCE:
        return None
    b = get(url, timeout=120)
    if not b:
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(b), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
    except Exception as e:  # noqa: BLE001
        log("[warn] avoirs SSGA : " + str(e)[:100])
        return None
    au = None
    for r in rows[:6]:
        for c in r or ():
            m = re.search(r"(\d{2})-([A-Za-z]{3})-(\d{4})", str(c or ""))
            if m:
                try:
                    au = datetime.strptime(m.group(0), "%d-%b-%Y").date()
                except ValueError:
                    pass
    tete = None
    for i, r in enumerate(rows):
        cells = [str(c or "").strip().lower() for c in r or ()]
        if any(c == "isin" for c in cells) and any(("weight" in c) or ("percent" in c) for c in cells):
            tete = i
            break
    if tete is None:
        return None
    noms = [str(c or "").strip().lower() for c in rows[tete]]

    def col(*mots):
        for i, n in enumerate(noms):
            if all(m in n for m in mots):
                return i
        return None

    iw = col("percent") if col("percent") is not None else col("weight")
    ip = col("price")
    ic = col("interest rate") if col("interest rate") is not None else col("coupon")
    im = col("maturity")
    if None in (iw, ip, ic, im):
        return None
    auj = au or date.today()
    s_w = s_y = s_d = 0.0
    n = n_ok = 0
    for r in rows[tete + 1:]:
        if not r or r[iw] in (None, ""):
            continue
        try:
            w = float(r[iw])
            prix = float(r[ip])
            cp = float(r[ic] or 0)
            ech = r[im]
            if isinstance(ech, datetime):
                ech = ech.date()
            elif isinstance(ech, str):
                ech = ech.strip()
                for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
                    try:
                        ech = datetime.strptime(ech[:11], fmt).date()
                        break
                    except ValueError:
                        continue
                if isinstance(ech, str):
                    continue
        except (ValueError, TypeError):
            continue
        n += 1
        res = _rdt_ligne(prix, cp, ech, auj)
        if not res or not (-2 < res[0] < 40):
            continue
        n_ok += 1
        s_w += w
        s_y += w * res[0]
        s_d += w * res[1]
    if n_ok < 50 or s_w <= 0:
        return None
    return {"ytm": round(s_y / s_w, 2), "duration": round(s_d / s_w, 2), "lignes": n, "lignes_calculees": n_ok,
            "au": auj.isoformat(), "poids_couvert": round(s_w, 1)}


def taux_zero_aaa(duration_ans):
    """Courbe AAA de la BCE (taux au comptant) à la maturité la plus proche de la duration."""
    m = max(1, min(30, int(round(duration_ans))))
    pts = ecb("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_%dY" % m, debut=il_y_a(date.today().isoformat(), 400))
    return (m, pts[-1][1], pts[-1][0]) if pts else None


# ── Construction ────────────────────────────────────────────────────────────
def _mens_perf(pts):
    """Série mensuelle (fin de mois) d'un prix ajusté, base 100 au début."""
    m = mensuel(pts)
    if not m:
        return None
    b = m[0][1]
    return {"d": [x[0][:7] for x in m], "v": [round(100 * x[1] / b, 2) for x in m]}


def construire(journal, series_etat=None):
    """Rend (synthèse par segment, détail {code: {...}}, longues séries de la vue)."""
    ecran = {}
    try:
        ecran = ishares_ecran()
    except Exception as e:  # noqa: BLE001
        journal.append("iShares : " + str(e)[:120])
    synth, detail = [], {}
    for sg in SEGMENTS:
        s = {k: sg[k] for k in ("code", "nom", "court", "anglais", "zone", "pays", "devise", "famille", "notation", "notation_note") if k in sg}
        s["genre"] = "credit"
        d = {"code": sg["code"], "series": {}}
        # 1. Écart et rendement (ICE, 3 ans)
        if SOURCES_SOUS_LICENCE and sg.get("oas"):
            oas = fred(sg["oas"])
            rdt = fred(sg["rdt"]) if sg.get("rdt") else []
            if oas:
                s["oas_pb"] = round(oas[-1][1] * 100, 0)
                s["date"] = oas[-1][0]
                s["oas_var"] = variations(oas, 100)
                s["oas_centile_3a"] = centile(oas)
                s["oas_ext_3a"] = {k: (round(v * 100) if isinstance(v, float) else v) for k, v in (extremes(oas) or {}).items()}
                d["series"]["oas"] = en_colonnes(oas, 3)
            else:
                journal.append("FRED %s vide" % sg["oas"])
            if rdt:
                s["rendement"] = round(rdt[-1][1], 2)
                s["rdt_var"] = variations(rdt, 100)
                s["rdt_ext_3a"] = extremes(rdt)
                d["series"]["rdt"] = en_colonnes(rdt, 3)
            ech = []
            for lib, sid in sg.get("echelle", []):
                p = fred(sid)
                if p:
                    ech.append({"lib": lib, "oas_pb": round(p[-1][1] * 100), "var_1a_pb": variations(p, 100).get("1a"),
                                "centile_3a": centile(p)})
                    d["series"]["ech_" + lib] = en_colonnes(p, 3)
            if ech:
                s["echelle"] = ech
            if sg.get("tr"):
                tr = fred(sg["tr"])
                if tr:
                    s["tr_perf"] = {"1a": perf(tr, 365), "ytd": perf_ytd(tr), "3a": perf(tr, 1090)}
                    d["series"]["tr"] = en_colonnes(tr, 2)
            s["source_oas"] = "ICE BofA, via FRED (3 ans glissants)"
        # 2. Euro IG : recalcul sur les avoirs de l'ETF, face à la courbe AAA BCE
        if sg.get("avoirs"):
            a = None
            try:
                a = avoirs_ssga(sg["avoirs"])
            except Exception as e:  # noqa: BLE001
                journal.append("avoirs %s : %s" % (sg["code"], str(e)[:100]))
            if a:
                s["avoirs"] = dict(a, fonds=sg["avoirs_nom"], url=sg["avoirs"])
                z = taux_zero_aaa(a["duration"])
                if z:
                    s["avoirs"]["ecart_aaa_pb"] = round((a["ytm"] - z[1]) * 100)
                    s["avoirs"]["aaa"] = {"maturite": z[0], "taux": round(z[1], 3), "au": z[2]}
                if "rendement" not in s:
                    s["rendement"] = a["ytm"]
                    s["rendement_source"] = "calculé sur les %d lignes de %s" % (a["lignes_calculees"], sg["avoirs_nom"])
                    s["date"] = a["au"]
                if "oas_pb" not in s and "ecart_aaa_pb" in s["avoirs"]:
                    s["ecart_pb"] = s["avoirs"]["ecart_aaa_pb"]
                    s["ecart_nature"] = "G-spread face à la courbe AAA de la BCE à %d ans" % z[0]
                if "duration" not in s:
                    s["duration"] = a["duration"]
        # 3. ETF : caractéristiques (iShares US) et performance totale (Yahoo, ajustée)
        car = []
        for t in sg.get("etf_car", []):
            if t in ecran:
                car.append(dict(ecran[t], ticker=t))
        if car:
            s["etf_car"] = car
            if car[0].get("duration") and "duration" not in s:
                s["duration"] = car[0]["duration"]
        et = yahoo_jours(sg["etf"], ajuste=True)
        if et:
            s["etf"] = {"ticker": sg["etf"], "nom": sg["etf_nom"], "depuis": et[0][0], "au": et[-1][0],
                        "perf": {"1m": perf(et, 30), "ytd": perf_ytd(et), "1a": perf(et, 365), "3a": perf(et, 1095),
                                 "5a": perf(et, 1826), "10a": perf(et, 3652)},
                        "repli_max": repli_max(et), "repli_5a": repli_max(et, 1826)}
            ans = (date.fromisoformat(et[-1][0]) - date.fromisoformat(et[0][0])).days / 365.25
            if ans > 3:
                s["etf"]["annualise_depuis_creation"] = round(100 * ((et[-1][1] / et[0][1]) ** (1 / ans) - 1), 2)
            d["etf_mensuel"] = _mens_perf(et)
            d["etf_quotidien"] = en_colonnes([p for p in et if p[0] >= il_y_a(et[-1][0], 800)], 3)
        else:
            journal.append("Yahoo %s vide" % sg["etf"])
        # 4. Défauts (relevé daté)
        dk = DEFAUT_DE.get(sg["code"])
        if dk and sg["famille"] in ("hy", "em"):
            s["defauts"] = dict(DEFAUTS[dk], perimetre=dk)
        synth.append(s)
        detail[sg["code"]] = d

    # ── Les longues séries, pour la vue et les fiches ──
    longues = {}
    try:
        if SOURCES_SOUS_LICENCE:
            baa = fred("BAA10Y")
            if baa:
                longues["baa10y"] = {"nom": "Baa − Trésor 10 ans (Moody's)", "unite": "pt", "source": "Moody's via FRED",
                                     **en_colonnes(compacter(baa, 800), 3), "dernier": baa[-1][1], "au": baa[-1][0],
                                     "centile": centile(baa), "moyenne": round(sum(v for _, v in baa) / len(baa), 3)}
            baam = fred("BAA")
            aaam = fred("AAA")
            if baam and aaam:
                a = dict(aaam)
                ec = [(d0, v - a[d0]) for d0, v in baam if d0 in a]
                longues["baa_aaa"] = {"nom": "Baa − Aaa (Moody's, mensuel)", "unite": "pt", "source": "Moody's via FRED",
                                      **en_colonnes(ec, 3), "dernier": ec[-1][1], "au": ec[-1][0], "centile": centile(ec)}
        gz = gz_spread()
        if gz:
            longues["gz"] = {"nom": "GZ spread (Réserve fédérale)", "unite": "pt", "source": "Réserve fédérale, Gilchrist-Zakrajšek",
                             **en_colonnes(gz, 3), "dernier": gz[-1][1], "au": gz[-1][0], "centile": centile(gz),
                             "moyenne": round(sum(v for _, v in gz) / len(gz), 3)}
        ent = bundesbank("D.I.UMR.RD.EUR.X2000.B.A.A.R.A.A._Z._Z.A")
        fed = bundesbank("D.I.UMR.RD.EUR.S1311.B.A604.A.R.A.A._Z._Z.A")
        if ent and fed:
            f = dict(fed)
            ec = [(d0, v - f[d0]) for d0, v in ent if d0 in f]
            longues["bbk"] = {"nom": "Entreprises allemandes − État fédéral", "unite": "pt", "source": "Bundesbank",
                              **en_colonnes(compacter(ec, 800), 3), "dernier": round(ec[-1][1], 3), "au": ec[-1][0],
                              "centile": centile(ec), "rdt_entreprises": ent[-1][1], "rdt_etat": fed[-1][1]}
            longues["bbk_rdt"] = en_colonnes(compacter(ent, 800), 3)
    except Exception as e:  # noqa: BLE001
        journal.append("séries longues crédit : " + str(e)[:120])

    # Émissions : zone euro (BCE, entreprises non financières, long terme) et États-Unis (Z.1)
    emis = {}
    try:
        br = ecb("CSEC", "M.N.U2.W0.S11.S1.N.LI.F.F3.L._Z.EUR._T.F.V.N._T", debut="2000-01")
        ne = ecb("CSEC", "M.N.U2.W0.S11.S1.N.L.F.F3.L._Z.EUR._T.F.V.N._T", debut="2000-01")
        if br:
            nd = dict(ne)
            emis["zone_euro"] = {"m": [x[0] for x in br], "brutes": [round(x[1] / 1000, 2) for x in br],
                                 "nettes": [round(nd.get(x[0], 0) / 1000, 2) if x[0] in nd else None for x in br],
                                 "unite": "Md€", "source": "BCE, CSEC (entreprises non financières, long terme)"}
        enc = fred("CBLBSNNCB")
        fl = fred("BOGZ1FU103163003Q")
        if enc:
            emis["us"] = {"t": [x[0] for x in enc if x[0] >= "1990"], "encours": [round(x[1] / 1e6, 3) for x in enc if x[0] >= "1990"],
                          "unite": "T$", "source": "Réserve fédérale, comptes financiers Z.1 (obligations des entreprises non financières)"}
            if fl:
                fd = dict(fl)
                emis["us"]["flux_nets"] = [round(fd[x] / 1000, 1) if x in fd else None for x in emis["us"]["t"]]
    except Exception as e:  # noqa: BLE001
        journal.append("émissions crédit : " + str(e)[:120])
    return synth, detail, longues, emis


if __name__ == "__main__":
    j = []
    s, d, l, e = construire(j)
    for x in s:
        print(x["code"], x.get("rendement"), x.get("oas_pb"), x.get("ecart_pb"), x.get("duration"), (x.get("etf") or {}).get("perf"), x.get("avoirs"))
    print({k: (v.get("dernier"), v.get("au"), len(v.get("d", []))) for k, v in l.items() if isinstance(v, dict) and "d" in v})
    print({k: (v.get("m", v.get("t"))[-1]) for k, v in e.items()})
    print("journal", j)
