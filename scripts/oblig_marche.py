#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marché obligataire mondial — taille, émetteurs, pays (module de fetch_obligations.py).

SOURCE : BIS, Debt securities statistics (`WS_NA_SEC_DSS`) — encours de TOUS les
titres de dette émis par les résidents d'un pays, marchés domestique et
international réunis, par secteur émetteur (S13 administrations publiques,
S12 sociétés financières, S11 sociétés non financières).

⚠ L'API n'a PAS d'agrégat « monde » : on additionne les pays, en excluant la
  zone euro (U2), qui ferait double compte. C'est la règle de SIFMA (Fact Book
  2026, « Source: BIS ») : la somme reproduit sa table à ±0,3 % sur 2017-2025.
⚠ Des pays ENTRENT dans l'échantillon (15 en 1990, 29 en 2000, 50 depuis 2020 ;
  saut de 2018 : Brésil, Corée, Mexique…). La part « public contre privé » se
  lit donc sur un PANEL CONSTANT (les pays présents dès 2000-T4), publié à côté.
⚠ La valorisation varie selon le pays (N nominale > M marché > F faciale) et N
  ne commence qu'en 2020 pour le Royaume-Uni et 14 pays de l'UE : la série N
  est prolongée vers le passé par la croissance de M (raccord par ratio).
⚠ Encours en USD au change de FIN de trimestre : une partie des variations est
  un effet dollar (zone euro 2025 : +17,9 % en USD, +4,3 % en EUR).
"""
import csv
import io
import json
import time
import urllib.request
from collections import defaultdict

BIS = "https://stats.bis.org/api/v1/data"
URL_DSS = (BIS + "/WS_NA_SEC_DSS/Q.N..XW.S1+S11+S12+S13.S1.N.L.LE.F3.T._Z.USD._T.N+M+F.V.N._T?format=csv")
URL_DEVISES = (BIS + "/WS_NA_SEC_DSS/Q.N..XW.S1.S1.N.L.LE.F3.T._Z.USD.XDC+X1+_T.N.V.N._T?format=csv&startPeriod={debut}")
URL_IDS_SECT = BIS + "/WS_DEBT_SEC2_PUB/Q.3P.3P.1+2+B+J+S.1.C.A.A.TO1.A.A.A.A.A.I+C+G?format=csv&startPeriod=1995-Q1"
URL_IDS_CUR = BIS + "/WS_DEBT_SEC2_PUB/Q.3P.3P.1.1.C.A.A.USD+EUR+GBP+JPY+CHF+CNY+AUD+CAD+TO1.A.A.A.A.A.I?format=csv&startPeriod={debut}"
# ⚠ imf.org renvoie 403 à un User-Agent personnalisé, 200 à celui d'urllib : on n'en pose pas.
URL_IMF_GDP = "https://www.imf.org/external/datamapper/api/v1/NGDPD"
URL_WB_GDP = "https://api.worldbank.org/v2/country/all/indicator/NY.GDP.MKTP.CD?format=json&per_page=20000&date={a}:{a}"
URL_ECB = ("https://data-api.ecb.europa.eu/service/data/CSEC/"
           "M.N.U2.W0.S1+S11+S12+S13.S1.N.L+LI+LD.LE+F.F3.T._Z.EUR._T.N+F.V.N._T?format=csvdata&startPeriod=2020-12")

AGREGATS = {"U2", "I8", "I9", "I10", "B6", "3P", "5R", "4T", "4U", "4W", "4Y", "3C", "1C", "5J"}
ISO3 = {"AR": "ARG", "AT": "AUT", "AU": "AUS", "BE": "BEL", "BG": "BGR", "BR": "BRA", "CA": "CAN", "CH": "CHE",
        "CL": "CHL", "CN": "CHN", "CO": "COL", "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DK": "DNK", "EE": "EST",
        "ES": "ESP", "FI": "FIN", "FR": "FRA", "GB": "GBR", "GR": "GRC", "HK": "HKG", "HR": "HRV", "HU": "HUN",
        "ID": "IDN", "IE": "IRL", "IL": "ISR", "IN": "IND", "IS": "ISL", "IT": "ITA", "JP": "JPN", "KR": "KOR",
        "LT": "LTU", "LU": "LUX", "LV": "LVA", "MT": "MLT", "MX": "MEX", "MY": "MYS", "NL": "NLD", "NO": "NOR",
        "NZ": "NZL", "PE": "PER", "PH": "PHL", "PK": "PAK", "PL": "POL", "PT": "PRT", "RO": "ROU", "RU": "RUS",
        "SA": "SAU", "SE": "SWE", "SG": "SGP", "SI": "SVN", "SK": "SVK", "TH": "THA", "TR": "TUR", "TW": "TWN",
        "US": "USA", "ZA": "ZAF"}
NOMS = {"AR": "Argentine", "AT": "Autriche", "AU": "Australie", "BE": "Belgique", "BG": "Bulgarie", "BR": "Brésil",
        "CA": "Canada", "CH": "Suisse", "CL": "Chili", "CN": "Chine", "CO": "Colombie", "CY": "Chypre",
        "CZ": "Tchéquie", "DE": "Allemagne", "DK": "Danemark", "EE": "Estonie", "ES": "Espagne", "FI": "Finlande",
        "FR": "France", "GB": "Royaume-Uni", "GR": "Grèce", "HK": "Hong Kong", "HR": "Croatie", "HU": "Hongrie",
        "ID": "Indonésie", "IE": "Irlande", "IL": "Israël", "IN": "Inde", "IS": "Islande", "IT": "Italie",
        "JP": "Japon", "KR": "Corée du Sud", "LT": "Lituanie", "LU": "Luxembourg", "LV": "Lettonie", "MT": "Malte",
        "MX": "Mexique", "MY": "Malaisie", "NL": "Pays-Bas", "NO": "Norvège", "NZ": "Nouvelle-Zélande",
        "PE": "Pérou", "PH": "Philippines", "PK": "Pakistan", "PL": "Pologne", "PT": "Portugal", "RO": "Roumanie",
        "RU": "Russie", "SA": "Arabie saoudite", "SE": "Suède", "SG": "Singapour", "SI": "Slovénie",
        "SK": "Slovaquie", "TH": "Thaïlande", "TR": "Turquie", "TW": "Taïwan", "US": "États-Unis",
        "ZA": "Afrique du Sud"}


def _get(url, tries=3, timeout=180):
    der = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
                return r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            der = e
            time.sleep(4 * (i + 1))
    raise der


def _lignes(txt):
    return [r for r in csv.DictReader(io.StringIO(txt)) if r.get("OBS_VALUE") not in (None, "", "NaN")]


def _qk(t):
    y, q = t.split("-Q")
    return int(y), int(q)


def _raccord(par_val, ordre=("N", "M", "F")):
    """Une série par pays et secteur : la valorisation préférée, prolongée vers le
    passé par la croissance de la suivante (ratio au premier trimestre commun)."""
    dispo = [v for v in ordre if par_val.get(v)]
    if not dispo:
        return {}, None
    s = dict(par_val[dispo[0]])
    for autre in dispo[1:]:
        a = par_val[autre]
        premier = min(s, key=_qk)
        if premier not in a or not a[premier]:
            continue
        r = s[premier] / a[premier]
        for t, x in a.items():
            if _qk(t) < _qk(premier):
                s[t] = x * r
    return s, dispo[0]


def _pib_md_usd(annee, journal):
    """PIB en Md USD courants par code ISO2 : FMI (WEO), repli Banque mondiale."""
    try:
        d = json.loads(_get(URL_IMF_GDP))["values"]["NGDPD"]
        out = {i2: d.get(i3, {}).get(str(annee)) for i2, i3 in ISO3.items()}
        out["_monde"] = d.get("WEOWORLD", {}).get(str(annee))
        out["_source"] = "FMI, Perspectives de l'économie mondiale (NGDPD)"
        return out
    except Exception as e:  # noqa: BLE001
        journal.append("PIB FMI : " + str(e)[:120])
    try:
        d = json.loads(_get(URL_WB_GDP.format(a=annee)))[1]
        i3_i2 = {v: k for k, v in ISO3.items()}
        out = {}
        for r in d:
            c = r.get("countryiso3code")
            if r.get("value") is None:
                continue
            if c in i3_i2:
                out[i3_i2[c]] = r["value"] / 1e9
            if c == "WLD":
                out["_monde"] = r["value"] / 1e9
        out["_source"] = "Banque mondiale (NY.GDP.MKTP.CD)"
        return out
    except Exception as e:  # noqa: BLE001
        journal.append("PIB Banque mondiale : " + str(e)[:120])
    return {}


def construire(journal):
    brut = defaultdict(lambda: defaultdict(dict))  # (pays, secteur) → valorisation → {trimestre: Md$}
    for r in _lignes(_get(URL_DSS)):
        if r.get("UNIT_MULT") != "9":
            raise ValueError("BIS DSS : unité inattendue " + str(r.get("UNIT_MULT")))
        brut[(r["REF_AREA"], r["REF_SECTOR"])][r["VALUATION"]][r["TIME_PERIOD"]] = float(r["OBS_VALUE"])
    serie, valo = {}, {}
    for (a, s), pv in brut.items():
        if a in AGREGATS:
            continue
        serie[(a, s)], valo[(a, s)] = _raccord(pv)
    pays = sorted({a for a, _ in serie})
    avec_total = [a for a in pays if serie.get((a, "S1"))]
    # Hong Kong ne publie que ses administrations : compté comme SIFMA le compte.
    if serie.get(("HK", "S13")) and "HK" not in avec_total:
        serie[("HK", "S1")] = serie[("HK", "S13")]
        avec_total.append("HK")
    trims = sorted({t for a in avec_total for t in serie[(a, "S1")]}, key=_qk)
    trims = [t for t in trims if _qk(t) >= (1989, 4)]

    monde = {"t": [], "n": [], "total": [], "etats": [], "financieres": [], "entreprises": []}
    for t in trims:
        pres = [a for a in avec_total if t in serie[(a, "S1")]]
        monde["t"].append(t)
        monde["n"].append(len(pres))
        monde["total"].append(round(sum(serie[(a, "S1")][t] for a in pres), 1))
        for s, cle in (("S13", "etats"), ("S12", "financieres"), ("S11", "entreprises")):
            monde[cle].append(round(sum(serie.get((a, s), {}).get(t, 0) for a in pres), 1))
    # Le dernier trimestre ne compte que s'il est COMPLET (même nombre de pays
    # que le précédent) : un trimestre à moitié publié ferait une fausse chute.
    while len(monde["n"]) > 2 and monde["n"][-1] < monde["n"][-2] - 2:
        for k in monde:
            monde[k].pop()

    # Panel constant : les pays présents dès 2000-T4 — la seule façon honnête de
    # dire qui, des États ou du privé, a grossi le plus vite.
    panel = sorted(a for a in avec_total if "2000-Q4" in serie[(a, "S1")])
    pn = {"pays": panel, "t": [], "total": [], "etats": [], "financieres": [], "entreprises": []}
    for t in monde["t"]:
        if _qk(t) < (2000, 4):
            continue
        if not all(t in serie[(a, "S1")] for a in panel):
            continue
        pn["t"].append(t)
        pn["total"].append(round(sum(serie[(a, "S1")][t] for a in panel), 1))
        for s, cle in (("S13", "etats"), ("S12", "financieres"), ("S11", "entreprises")):
            pn[cle].append(round(sum(serie.get((a, s), {}).get(t, 0) for a in panel), 1))

    t_der = monde["t"][-1]
    t_q4 = t_der if t_der.endswith("Q4") else f"{int(t_der[:4]) - 1}-Q4"
    pib = _pib_md_usd(int(t_q4[:4]), journal)

    # Devises : part des titres en devises étrangères, par pays (quand publiée).
    etr = {}
    try:
        dv = defaultdict(dict)
        for r in _lignes(_get(URL_DEVISES.format(debut=f"{int(t_der[:4]) - 1}-Q1"))):
            dv[r["REF_AREA"]][(r["CURRENCY_DENOM"], r["TIME_PERIOD"])] = float(r["OBS_VALUE"])
        for a, d in dv.items():
            ts = sorted({t for (_, t) in d}, key=_qk)
            for t in reversed(ts):
                if ("X1", t) in d and ("_T", t) in d and d[("_T", t)]:
                    etr[a] = round(100 * d[("X1", t)] / d[("_T", t)], 1)
                    break
    except Exception as e:  # noqa: BLE001
        journal.append("BIS devises : " + str(e)[:120])

    lignes = []
    for a in avec_total:
        # dernier trimestre publié PAR PAYS (au plus tard t_der)
        ts = [t for t in serie[(a, "S1")] if _qk(t) <= _qk(t_der)]
        if not ts:
            continue
        t = max(ts, key=_qk)
        if _qk(t) < _qk(t_der) and (_qk(t_der)[0] - _qk(t)[0]) * 4 + _qk(t_der)[1] - _qk(t)[1] > 4:
            continue  # plus d'un an de retard : hors classement
        v = serie[(a, "S1")][t]
        v4 = serie[(a, "S1")].get(t_q4)
        g = pib.get(a)
        # croissance sur 5 ans (même pays, même valorisation raccordée)
        t5 = f"{_qk(t)[0] - 5}-Q{_qk(t)[1]}"
        v5 = serie[(a, "S1")].get(t5)
        lignes.append({
            "iso": a, "nom": NOMS.get(a, a), "t": t,
            "total": round(v, 1),
            "etats": round(serie.get((a, "S13"), {}).get(t, 0), 1) if a != "HK" else round(v, 1),
            "financieres": round(serie.get((a, "S12"), {}).get(t, 0), 1) if a != "HK" else None,
            "entreprises": round(serie.get((a, "S11"), {}).get(t, 0), 1) if a != "HK" else None,
            "pct_pib": round(100 * v4 / g, 1) if (v4 and g) else None,
            "crois_5a": round(100 * ((v / v5) ** (1 / 5) - 1), 1) if v5 else None,
            "devises_etr_pct": etr.get(a),
            "valorisation": {"N": "nominale", "M": "de marché", "F": "faciale"}.get(valo.get((a, "S1")), None),
            "depuis": min(serie[(a, "S1")], key=_qk),
        })
    lignes.sort(key=lambda r: -r["total"])
    tot = monde["total"][-1]
    for r in lignes:
        r["part"] = round(100 * r["total"] / tot, 2)

    # Les États-Unis seuls, aux mêmes trimestres que le monde : c'est le pays où se
    # loge la dette des groupes tech (oblig_tech.py), qui se lit donc aussi face à
    # l'encours américain — 38 % du marché mondial à lui seul.
    eu = {"t": [], "total": [], "etats": [], "financieres": [], "entreprises": []}
    if serie.get(("US", "S1")):
        for t in monde["t"]:
            if t not in serie[("US", "S1")]:
                continue
            eu["t"].append(t)
            eu["total"].append(round(serie[("US", "S1")][t], 1))
            for s, cle in (("S13", "etats"), ("S12", "financieres"), ("S11", "entreprises")):
                eu[cle].append(round(serie.get(("US", s), {}).get(t, 0), 1))

    out = {
        "source": "BIS — Debt securities statistics (WS_NA_SEC_DSS)",
        "source_url": "https://data.bis.org/topics/DSS",
        "periode": t_der,
        "monde": monde,
        "panel": pn,
        "etats_unis": eu if eu["t"] else None,
        "pays": lignes,
        "pib": {"annee": t_q4[:4], "source": pib.get("_source"),
                "monde_md_usd": round(pib["_monde"], 0) if pib.get("_monde") else None,
                "encours_q4": monde["total"][monde["t"].index(t_q4)] if t_q4 in monde["t"] else None},
        "controle_sifma": {"annee": 2025, "sifma_md_usd": 160704,
                           "note": "SIFMA Capital Markets Fact Book 2026, table 1 (source BIS)"},
    }
    if out["pib"]["monde_md_usd"] and out["pib"]["encours_q4"]:
        out["pib"]["encours_pct_pib_monde"] = round(100 * out["pib"]["encours_q4"] / out["pib"]["monde_md_usd"], 1)

    # Titres INTERNATIONAUX (≈ 22 % du total) : la seule ventilation mondiale par devise.
    try:
        ids = defaultdict(dict)
        for r in _lignes(_get(URL_IDS_SECT)):
            ids[(r["ISSUER_BUS_IMM"], r["MEASURE"])][r["TIME_PERIOD"]] = float(r["OBS_VALUE"]) / 1000
        cur = defaultdict(dict)
        t_ids = max(ids[("1", "I")], key=_qk)
        for r in _lignes(_get(URL_IDS_CUR.format(debut=f"{int(t_ids[:4]) - 1}-Q1"))):
            cur[r["ISSUE_CUR"]][r["TIME_PERIOD"]] = float(r["OBS_VALUE"]) / 1000
        tot_i = cur.get("TO1", {}).get(t_ids)
        ts_i = sorted(ids[("1", "C")], key=_qk)
        out["international"] = {
            "periode": t_ids,
            "encours": round(ids[("1", "I")][t_ids], 0),
            "par_secteur": {k: round(ids[(c, "I")].get(t_ids, 0), 0) for c, k in
                            (("2", "etats"), ("B", "financieres"), ("J", "entreprises"), ("S", "organisations"))},
            "emissions_brutes_4t": round(sum(ids[("1", "C")].get(t, 0) for t in ts_i[-4:]), 0),
            "emissions_nettes_4t": round(sum(ids[("1", "G")].get(t, 0) for t in ts_i[-4:]), 0),
            "devises": {k: round(100 * v[t_ids] / tot_i, 1) for k, v in cur.items() if k != "TO1" and t_ids in v and tot_i},
            "emissions": {"t": ts_i[-40:], "brutes": [round(ids[("1", "C")].get(t, 0), 0) for t in ts_i[-40:]],
                          "nettes": [round(ids[("1", "G")].get(t, 0), 0) for t in ts_i[-40:]]},
        }
    except Exception as e:  # noqa: BLE001
        journal.append("BIS international : " + str(e)[:120])

    # Zone euro, mensuel (BCE CSEC) : émissions brutes, remboursements, nettes.
    try:
        ecb = defaultdict(dict)
        for r in _lignes(_get(URL_ECB)):
            ecb[f"{r['REF_SECTOR']}_{r['ACCOUNTING_ENTRY']}_{r['STO']}_{r['VALUATION']}"][r["TIME_PERIOD"]] = float(r["OBS_VALUE"]) / 1000
        mois = sorted(ecb["S1_L_LE_N"])
        m = mois[-1]
        mm = sorted(ecb.get("S1_LI_F_F", {}))[-24:]
        out["zone_euro"] = {
            "mois": m, "source": "BCE — Securities issues statistics (CSEC)",
            "encours_md_eur": {k: round(ecb[f"{s}_L_LE_N"][m], 0) for s, k in
                               (("S1", "total"), ("S13", "etats"), ("S12", "financieres"), ("S11", "entreprises"))},
            "emissions": {"m": mm,
                          "brutes": [round(ecb["S1_LI_F_F"].get(x, 0), 1) for x in mm],
                          "remb": [round(ecb.get("S1_LD_F_F", {}).get(x, 0), 1) for x in mm],
                          "nettes": [round(ecb.get("S1_L_F_F", {}).get(x, 0), 1) for x in mm],
                          "nettes_etats": [round(ecb.get("S13_L_F_F", {}).get(x, 0), 1) for x in mm],
                          "nettes_entreprises": [round(ecb.get("S11_L_F_F", {}).get(x, 0), 1) for x in mm]},
        }
    except Exception as e:  # noqa: BLE001
        journal.append("BCE CSEC : " + str(e)[:120])
    return out


if __name__ == "__main__":
    j = []
    o = construire(j)
    print(json.dumps({k: (v if k not in ("monde", "panel") else {kk: vv[-3:] for kk, vv in v.items()})
                      for k, v in o.items() if k != "pays"}, ensure_ascii=False, indent=1)[:4000])
    print([(p["iso"], p["total"], p["part"], p["pct_pib"], p["crois_5a"]) for p in o["pays"][:12]])
    print("journal", j)
