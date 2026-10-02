#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Le DÉTAIL des emprunteurs américains (module de fetch_obligations.py, 02/10/2026).

Demande de MA : dans « Qui emprunte, depuis 1989 », voir la composition des
financières et celle des entreprises, et l'évolution par secteur.

1. LES FINANCIÈRES — Réserve fédérale, comptes financiers Z.1 (via FRED), titres de
   dette au passif de chaque sous-secteur, trimestriel. La somme des sous-secteurs
   retombe sur le total à 0,5-3,5 % près depuis 1990 ; le reste est publié « autres ».
   ⚠ Les deux tiers de la dette des « financières » américaines sont du CRÉDIT
     IMMOBILIER titrisé par les agences publiques (Fannie Mae, Freddie Mac, Ginnie
     Mae…), pas de la dette de banques.
   ⚠ La ventilation « banques » de la BRI ne sert pas ici : aux États-Unis, les
     banques empruntent par leurs HOLDINGS (JPMorgan Chase & Co…), que la BRI range
     avec les autres financières (les banques n'y font que 3 % des financières).

2. LES ENTREPRISES PAR SECTEUR — aucune statistique officielle ne ventile les
   obligations d'entreprise par secteur d'activité. On reconstitue la dette des plus
   gros émetteurs non financiers américains de chaque secteur, société par société,
   depuis leurs comptes déposés à la SEC — même méthode, même fonction
   (`oblig_tech.dette_societe`) que la tech, qui reste comptée par oblig_tech.
   Le reste de la dette des entreprises (BRI) est publié « non ventilé ».
   ⚠ Sont écartés : les groupes dont la dette est celle d'une filiale FINANCIÈRE
     (GE avant 2016 portait GE Capital ; Ford, GM, Caterpillar, Deere : leurs filiales
     de crédit sont des financières dans les comptes nationaux) ; les groupes non
     américains (Linde) ; les symboles réattribués (PARA désigne désormais une autre
     société). Les successeurs sont recousus (Exxon, Disney, Cigna, Dow).
   ⚠ Chevron et T-Mobile ne publient leur dette à long terme qu'une fois par an dans
     leurs comptes électroniques : leur dernier bilan est reporté (au plus trois
     trimestres, et c'est écrit).
   ⚠ Quand la reconstitution s'écarte de plus de 3 % du total publié par la société
     elle-même (Valero : la dette de sa coentreprise), le total publié fait foi.
"""
import datetime as dt
import os
import time

import oblig_tech as OT
from oblig_net import fred

# ── 1. Les financières (Z.1) ────────────────────────────────────────────────
Z1 = {"total": "FBDSILQ027S",            # secteurs financiers domestiques, titres de dette
      "agences": "GSEMPUQ027S",          # agences (GSE) et pools de prêts immobiliers qu'elles garantissent
      "titrisation": "IABSDSL",          # émetteurs de titres adossés à des actifs (titrisation privée)
      "banques": "USCDIDSL",             # établissements de dépôt
      "holdings": "HCDSL",               # holdings (bancaires pour l'essentiel)
      "credit": "FINCDSL"}               # sociétés de crédit (Ford Credit, GM Financial…)
Z1_URL = "https://www.federalreserve.gov/releases/z1/"


def _q(iso):
    """'2026-04-01' (début de trimestre, convention FRED) → '2026-Q2'."""
    return "%s-Q%d" % (iso[:4], (int(iso[5:7]) - 1) // 3 + 1)


def financieres(journal, debut="1990-01-01"):
    S = {}
    for k, sid in Z1.items():
        p = fred(sid, debut=debut)
        if not p:
            journal.append("Z.1 %s (%s) vide" % (k, sid))
            return None
        S[k] = {_q(d): v / 1000.0 for d, v in p}          # millions → milliards de dollars
    ts = sorted(set.intersection(*[set(v) for v in S.values()]), key=OT._qk)
    out = {"t": [], "total": [], "agences": [], "titrisation": [], "banques": [], "credit": [], "autres": []}
    for t in ts:
        tot = S["total"][t]
        b = S["banques"][t] + S["holdings"][t]
        parts = [S["agences"][t], S["titrisation"][t], b, S["credit"][t]]
        reste = tot - sum(parts)
        if reste < -0.02 * tot:
            journal.append("Z.1 %s : sous-secteurs > total (%.0f > %.0f)" % (t, sum(parts), tot))
            continue
        out["t"].append(t)
        out["total"].append(round(tot, 1))
        out["agences"].append(round(S["agences"][t], 1))
        out["titrisation"].append(round(S["titrisation"][t], 1))
        out["banques"].append(round(b, 1))
        out["credit"].append(round(S["credit"][t], 1))
        out["autres"].append(round(max(0.0, reste), 1))
    if not out["t"]:
        return None
    return {"source": "Réserve fédérale, comptes financiers des États-Unis (Z.1), titres de dette au passif de chaque sous-secteur, via FRED",
            "source_url": Z1_URL, "periode": out["t"][-1], "serie": out, "unite": "Md$"}


# ── 2. Les entreprises par secteur (SEC) ───────────────────────────────────
SECTEURS = [("telecoms", "Télécoms et médias"), ("sante", "Santé"), ("energie", "Pétrole et gaz"),
            ("utilities", "Électricité et gaz (réseaux)"), ("conso", "Consommation"), ("industrie", "Industrie et transport")]
# (code, nom, CIK, secteur, prédécesseurs [CIK], compté à partir de)
UNIVERS = [
    ("T", "AT&T", 732717, "telecoms", [], None), ("VZ", "Verizon", 732712, "telecoms", [], None),
    ("CMCSA", "Comcast", 1166691, "telecoms", [], None), ("TMUS", "T-Mobile US", 1283699, "telecoms", [], None),
    ("CHTR", "Charter", 1091667, "telecoms", [], None), ("DIS", "Disney", 1744489, "telecoms", [1001039], None),
    ("WBD", "Warner Bros. Discovery", 1437107, "telecoms", [], None), ("FOXA", "Fox", 1754301, "telecoms", [], None),
    ("NFLX", "Netflix", 1065280, "telecoms", [], None),
    ("ABBV", "AbbVie", 1551152, "sante", [], None), ("UNH", "UnitedHealth", 731766, "sante", [], None),
    ("CVS", "CVS Health", 64803, "sante", [], None), ("PFE", "Pfizer", 78003, "sante", [], None),
    ("AMGN", "Amgen", 318154, "sante", [], None), ("MRK", "Merck & Co", 310158, "sante", [], None),
    ("JNJ", "Johnson & Johnson", 200406, "sante", [], None), ("LLY", "Eli Lilly", 59478, "sante", [], None),
    ("BMY", "Bristol Myers Squibb", 14272, "sante", [], None), ("HCA", "HCA Healthcare", 860730, "sante", [], None),
    ("CI", "Cigna", 1739940, "sante", [701221], None), ("ELV", "Elevance Health", 1156039, "sante", [], None),
    ("GILD", "Gilead", 882095, "sante", [], None), ("TMO", "Thermo Fisher", 97745, "sante", [], None),
    ("ABT", "Abbott", 1800, "sante", [], None), ("DHR", "Danaher", 313616, "sante", [], None),
    ("BDX", "Becton Dickinson", 10795, "sante", [], None), ("ZTS", "Zoetis", 1555280, "sante", [], None),
    ("HUM", "Humana", 49071, "sante", [], None), ("BSX", "Boston Scientific", 885725, "sante", [], None),
    ("XOM", "ExxonMobil", 2115436, "energie", [34088], None), ("CVX", "Chevron", 93410, "energie", [], None),
    ("COP", "ConocoPhillips", 1163165, "energie", [], None), ("ET", "Energy Transfer", 1276187, "energie", [], None),
    ("KMI", "Kinder Morgan", 1506307, "energie", [], None), ("WMB", "Williams", 107263, "energie", [], None),
    ("EPD", "Enterprise Products", 1061219, "energie", [], None), ("OXY", "Occidental", 797468, "energie", [], None),
    ("MPLX", "MPLX", 1552000, "energie", [], None), ("OKE", "ONEOK", 1039684, "energie", [], None),
    ("TRGP", "Targa Resources", 1389170, "energie", [], None), ("EOG", "EOG Resources", 821189, "energie", [], None),
    ("PSX", "Phillips 66", 1534701, "energie", [], None), ("MPC", "Marathon Petroleum", 1510295, "energie", [], None),
    ("VLO", "Valero", 1035002, "energie", [], None),
    ("NEE", "NextEra Energy", 753308, "utilities", [], None), ("DUK", "Duke Energy", 1326160, "utilities", [], None),
    ("SO", "Southern Company", 92122, "utilities", [], None), ("D", "Dominion Energy", 715957, "utilities", [], None),
    ("AEP", "American Electric Power", 4904, "utilities", [], None), ("EXC", "Exelon", 1109357, "utilities", [], None),
    ("XEL", "Xcel Energy", 72903, "utilities", [], None), ("PCG", "PG&E", 1004980, "utilities", [], None),
    ("SRE", "Sempra", 1032208, "utilities", [], None), ("EIX", "Edison International", 827052, "utilities", [], None),
    ("ED", "Consolidated Edison", 1047862, "utilities", [], None), ("DTE", "DTE Energy", 936340, "utilities", [], None),
    ("ETR", "Entergy", 65984, "utilities", [], None), ("PEG", "PSEG", 788784, "utilities", [], None),
    ("WEC", "WEC Energy", 783325, "utilities", [], None), ("AEE", "Ameren", 1002910, "utilities", [], None),
    ("CNP", "CenterPoint Energy", 1130310, "utilities", [], None), ("FE", "FirstEnergy", 1031296, "utilities", [], None),
    ("PPL", "PPL", 922224, "utilities", [], None), ("ES", "Eversource", 72741, "utilities", [], None),
    ("HD", "Home Depot", 354950, "conso", [], None), ("WMT", "Walmart", 104169, "conso", [], None),
    ("LOW", "Lowe's", 60667, "conso", [], None), ("PEP", "PepsiCo", 77476, "conso", [], None),
    ("KO", "Coca-Cola", 21344, "conso", [], None), ("PM", "Philip Morris", 1413329, "conso", [], None),
    ("MO", "Altria", 764180, "conso", [], None), ("MCD", "McDonald's", 63908, "conso", [], None),
    ("SBUX", "Starbucks", 829224, "conso", [], None), ("KHC", "Kraft Heinz", 1637459, "conso", [], None),
    ("MDLZ", "Mondelez", 1103982, "conso", [], None), ("TGT", "Target", 27419, "conso", [], None),
    ("COST", "Costco", 909832, "conso", [], None), ("PG", "Procter & Gamble", 80424, "conso", [], None),
    ("NKE", "Nike", 320187, "conso", [], None), ("GIS", "General Mills", 40704, "conso", [], None),
    ("KDP", "Keurig Dr Pepper", 1418135, "conso", [], "2018-07-09"), ("STZ", "Constellation Brands", 16918, "conso", [], None),
    ("BKNG", "Booking", 1075531, "conso", [], None), ("MAR", "Marriott", 1048286, "conso", [], None),
    ("BA", "Boeing", 12927, "industrie", [], None), ("RTX", "RTX", 101829, "industrie", [], None),
    ("LMT", "Lockheed Martin", 936468, "industrie", [], None), ("GD", "General Dynamics", 40533, "industrie", [], None),
    ("NOC", "Northrop Grumman", 1133421, "industrie", [], None), ("HON", "Honeywell", 773840, "industrie", [], None),
    ("MMM", "3M", 66740, "industrie", [], None), ("UNP", "Union Pacific", 100885, "industrie", [], None),
    ("CSX", "CSX", 277948, "industrie", [], None), ("NSC", "Norfolk Southern", 702165, "industrie", [], None),
    ("UPS", "UPS", 1090727, "industrie", [], None), ("FDX", "FedEx", 1048911, "industrie", [], None),
    ("WM", "Waste Management", 823768, "industrie", [], None), ("RSG", "Republic Services", 1060391, "industrie", [], None),
    ("CARR", "Carrier", 1783180, "industrie", [], None), ("OTIS", "Otis", 1781335, "industrie", [], None),
    ("DOW", "Dow", 1751788, "industrie", [29915], None),
]
REPORT_MAX = 3          # trimestres de report d'un dernier bilan, au plus


def _serie_societe(cik, preds, journal, code):
    """{date de bilan: dette (USD)} recousue sur les prédécesseurs ; le total publié par
    la société fait foi aux dates où la reconstitution s'en écarte de plus de 3 %."""
    serie, n_ctl, n_bad = {}, 0, 0
    for c in [cik] + preds:
        d = OT._get(OT.URL_CF.format(c))
        time.sleep(0.15)
        gaap = (d.get("facts") or {}).get("us-gaap") or {}
        s, au = OT.dette_societe(gaap)
        n_ctl += au["n"]
        if au["ecarts"]:
            ref = OT._instants(gaap, OT.T_REF)
            for e, r in ref.items():
                if e in s and r > 1e9 and abs(s[e] - r) > 0.03 * r:
                    s[e] = r
                    n_bad += 1
        debut = min(serie) if serie else "9999"
        for e, v in s.items():
            if e < debut:                 # le successeur fait foi sur ses propres dates
                serie[e] = v
    if n_bad:
        journal.append("secteurs %s : total publié retenu à %d date(s)" % (code, n_bad))
    return serie, n_ctl, n_bad


def secteurs(journal, precedent=None):
    """Séries trimestrielles (Md$) de la dette des plus gros émetteurs de chaque secteur.
    Réutilise le passage précédent s'il a moins de 20 h (comme la tech)."""
    if precedent and precedent.get("genere_le"):
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(precedent["genere_le"])).total_seconds() / 3600
            if age < 20 and os.environ.get("SCF_OBLIG_TECH_FORCE") != "1":
                return precedent
        except ValueError:
            pass
    par_soc, fiches, n_ctl, n_bad = {}, [], 0, 0
    for code, nom, cik, sect, preds, depuis in UNIVERS:
        try:
            serie, a, b = _serie_societe(cik, preds, journal, code)
        except Exception as e:  # noqa: BLE001
            journal.append("secteurs %s : %s" % (code, str(e)[:100]))
            continue
        n_ctl += a
        n_bad += b
        if depuis:
            serie = {e: v for e, v in serie.items() if e >= depuis}
        if not serie:
            continue
        q = {}
        for e in sorted(serie):
            q[OT._trim(e)] = (e, serie[e])
        par_soc[code] = (sect, q)
        der = max(q, key=OT._qk)
        fiches.append({"code": code, "nom": nom, "secteur": sect, "md": round(q[der][1] / 1e9, 1), "au": q[der][0]})
    if len(par_soc) < 0.8 * len(UNIVERS):
        raise ValueError("SEC : %d sociétés sur %d seulement" % (len(par_soc), len(UNIVERS)))
    tous = sorted({t for _, q in par_soc.values() for t in q}, key=OT._qk)
    # Début : le premier trimestre où ≥ 95 % de la dette de fin 2010 est présente
    # (avant, la somme monterait par ENTRÉES dans le XBRL, pas par emprunts).
    ref_t = "2010-Q4"
    ref = {c: q[ref_t][1] for c, (_, q) in par_soc.items() if ref_t in q}
    tot_ref = sum(ref.values())
    debut = ref_t
    for t in tous:
        if OT._qk(t) > OT._qk(ref_t):
            break
        if tot_ref and sum(v for c, v in ref.items() if t in par_soc[c][1]) >= 0.95 * tot_ref:
            debut = t
            break
    aujourd = dt.date.today().isoformat()
    out = {"t": [], "n": []}
    for s, _ in SECTEURS:
        out[s] = []
    reportes = {}
    t = debut
    while OT._qk(t) <= OT._qk(tous[-1]):
        tot = {s: 0.0 for s, _ in SECTEURS}
        n = attente = 0
        rep_t = {}
        for c, (sect, q) in par_soc.items():
            avant = [x for x in q if OT._qk(x) < OT._qk(t)]
            apres = [x for x in q if OT._qk(x) > OT._qk(t)]
            v = None
            if t in q:
                v = q[t][1]
            elif avant and apres:
                v = q[max(avant, key=OT._qk)][1]            # trou : dernier bilan reporté
            elif avant:
                der = max(avant, key=OT._qk)
                ecart = (OT._qk(t)[0] - OT._qk(der)[0]) * 4 + OT._qk(t)[1] - OT._qk(der)[1]
                if ecart <= REPORT_MAX:
                    v = q[der][1]                          # ne publie plus ce poste chaque trimestre
                    rep_t[c] = der
                attente += 1 if OT._jours(OT._fin_trim(t), aujourd) < 140 else 0
            if v is not None:
                tot[sect] += v
                n += 1
        # trimestre trop récent où il manque des dépôts : on s'arrête
        if attente > 0.15 * len(par_soc):
            break
        reportes.update(rep_t)                 # seulement pour les trimestres publiés
        out["t"].append(t)
        for s, _ in SECTEURS:
            out[s].append(round(tot[s] / 1e9, 1))
        out["n"].append(n)
        t = OT._suivant(t)
    if reportes:
        journal.append("secteurs : dernier bilan reporté pour %s" % ", ".join("%s (%s)" % (c, d) for c, d in sorted(reportes.items())))
    fiches.sort(key=lambda f: -f["md"])
    return {"genere_le": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source": "SEC EDGAR, comptes déposés (10-K, 10-Q), données XBRL",
            "source_url": OT.SOURCE_URL, "periode": out["t"][-1] if out["t"] else None,
            "secteurs": [{"code": s, "nom": n} for s, n in SECTEURS], "serie": out, "societes": fiches,
            "n_univers": len(UNIVERS), "audit": {"controles": n_ctl, "totaux_publies_retenus": n_bad},
            "reportes": sorted(reportes)}


if __name__ == "__main__":
    import json
    j = []
    F = financieres(j)
    print("financières :", F["periode"], {k: v[-1] for k, v in F["serie"].items() if k != "t"})
    S = secteurs(j)
    s = S["serie"]
    for i in (0, len(s["t"]) // 2, len(s["t"]) - 1):
        print(s["t"][i], {k: s[k][i] for k, _ in SECTEURS}, s["n"][i])
    print("journal", j)
