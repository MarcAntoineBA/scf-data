#!/usr/bin/env python3
"""Cache du chapitre Thèse · 08 — L'effondrement comportemental.

Version 2.0 (audit du 04/10/2026). La version précédente était entièrement codée
en dur, et une partie de ses séries n'existait dans AUCUNE source (indices Google
Trends « reconstitués », boîtes d'antidépresseurs, nombre médian d'amis, adhésions
aux clubs, QI…). Règle désormais : une série est soit collectée en direct auprès
de sa source officielle, soit recopiée d'une édition publiée avec son URL et son
millésime. Ce qui n'a pas pu être vérifié n'est plus publié (vide > chiffre
plausible).

Séries en direct (gratuites, sans clé) :
  - CDC / NCHS VSRR (data.cdc.gov xkb8-kh2a) : décès par overdose aux États-Unis,
    cumul sur 12 mois glissants, mensuel, provisoire (valeur « predicted » du CDC,
    corrigée des retards de déclaration) + opioïdes de synthèse (T40.4).
  - CDC / NCHS (data.cdc.gov 44rk-q6r2) : décès définitifs annuels 1999-2018.
  - Insee BDM (SDMX) : âge moyen des mères à l'accouchement (001686826,
    métropole depuis 1901), indicateur conjoncturel de fécondité (001686832
    France, 001686825 métropole).
  - Eurostat demo_find AGEMOTH1 : âge moyen à la naissance du premier enfant
    (France, depuis 2013 : la série antérieure relève d'une autre méthode).
  - OCDE SDMX HEALTH_PHMC@DF_PHMC_CONSUM : consommation d'antidépresseurs (N06A),
    doses quotidiennes définies pour 1 000 habitants par jour.
  - Banque mondiale SP.DYN.TFRT.IN : fécondité par pays.
  - Google Trends France : lu dans le cache SerpAPI du site
    (~/Library/Caches/site_crypto_finance/gtrends_cache_FR.json) quand il existe ;
    sinon la dernière valeur collectée est conservée.

Séries recopiées d'éditions publiées (URL et millésime dans le cache) :
  PISA (OCDE), Cevipof (baromètre de la confiance politique), abstention à la
  présidentielle (ministère de l'Intérieur) et les registres REGISTRES ci-dessous.

Repli : si une source tombe, on garde la DERNIÈRE valeur réellement collectée
(relue dans le cache précédent) avec la date de son dernier succès ; jamais une
constante réinjectée en silence.

Sortie : these_effondrement_cache.json + .js (window.__THESE_EFFONDREMENT__).
Lancé par scf.these_effondrement.refresh.
"""
import csv
import io
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_effondrement_cache.json"
OUT_JS = CACHE_DIR / "these_effondrement_cache.js"
GTRENDS_FR = CACHE_DIR / "gtrends_cache_FR.json"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseEffondrement/2.0"
DOC_VERSION = "2.0"


def http_get(url, timeout=40, max_retries=4, accept="application/json,*/*", headers=None):
    h = {"User-Agent": UA, "Accept": accept}
    h.update(headers or {})
    last = None
    for attempt in range(max_retries):
        try:
            with urlopen(Request(url, headers=h), timeout=timeout) as r:
                cs = r.headers.get_content_charset() or "utf-8"
                return r.read().decode(cs, errors="replace")
        except HTTPError as e:
            last = e
            if 500 <= e.code < 600 or e.code == 429:
                time.sleep(4 * (2 ** attempt))
                continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last = e
            time.sleep(4 * (2 ** attempt))
    raise last if last else RuntimeError("échec réseau")


# ════════════════════════════════════════════════════════════════════════════
# SOURCES EN DIRECT
# ════════════════════════════════════════════════════════════════════════════

MOIS = {"January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
        "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
        "December": 12}
CDC_VSRR = "https://data.cdc.gov/resource/xkb8-kh2a.json"
CDC_FINAL = "https://data.cdc.gov/resource/44rk-q6r2.json"


def _socrata(base, where, order=None, limit=5000):
    q = {"$where": where, "$limit": str(limit)}
    if order:
        q["$order"] = order
    return json.loads(http_get(base + "?" + urlencode(q)))


def fetch_overdoses_us():
    """Décès par overdose (toutes drogues) et opioïdes de synthèse, cumul 12 mois."""
    out = {}
    for cle, indic in (("total", "Number of Drug Overdose Deaths"),
                       ("opioides_synthese", "Synthetic opioids, excl. methadone (T40.4)")):
        rows = _socrata(CDC_VSRR, f"state='US' AND indicator='{indic}'")
        pts = []
        for r in rows:
            v = r.get("predicted_value") or r.get("data_value")
            if not v or r.get("month") not in MOIS:
                continue
            pts.append((f"{int(r['year']):04d}-{MOIS[r['month']]:02d}", round(float(v))))
        pts.sort()
        if len(pts) < 24:
            raise RuntimeError(f"VSRR {indic}: {len(pts)} points")
        out[cle] = {"dates": [p[0] for p in pts], "valeurs": [p[1] for p in pts]}
    final = _socrata(CDC_FINAL, "state='United States' AND sex='Both Sexes' AND "
                                "age='All Ages' AND race='All Races-All Origins'", order="year")
    ann = sorted((int(r["year"]), int(float(r["deaths"]))) for r in final if r.get("deaths"))
    if len(ann) < 10:
        raise RuntimeError("CDC final annuel incomplet")
    out["annuel_definitif"] = {"annees": [a for a, _ in ann], "valeurs": [v for _, v in ann]}
    out["definition"] = ("Décès par overdose (toutes drogues) survenus aux États-Unis sur les "
                         "12 mois qui se terminent au mois indiqué. Provisoire : estimation du "
                         "CDC corrigée des retards de déclaration (« predicted value »). "
                         "Avant 2015 : chiffres définitifs annuels.")
    out["source_url"] = "https://www.cdc.gov/nchs/nvss/vsrr/drug-overdose-data.htm"
    out["api"] = CDC_VSRR
    out["dernier_mois"] = out["total"]["dates"][-1]
    return out


INSEE_SDMX = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/"


def _insee(idbank):
    x = http_get(INSEE_SDMX + idbank, accept="application/xml,*/*")
    obs = re.findall(r'TIME_PERIOD="(\d{4})" OBS_VALUE="([-\d.]+)"', x)
    titre = re.search(r'TITLE_FR="([^"]+)"', x)
    pts = sorted((int(a), float(v)) for a, v in obs)
    if len(pts) < 10:
        raise RuntimeError(f"Insee {idbank}: {len(pts)} points")
    return {"annees": [a for a, _ in pts], "valeurs": [v for _, v in pts],
            "titre": titre.group(1) if titre else idbank, "idbank": idbank,
            "source_url": f"https://www.insee.fr/fr/statistiques/serie/{idbank}"}


def fetch_natalite_fr():
    age = _insee("001686826")
    icf_metro = _insee("001686825")
    icf_fr = _insee("001686832")
    # l'Insee publie l'ICF en enfants pour 100 femmes : on le ramène par femme
    for s in (icf_metro, icf_fr):
        s["valeurs"] = [round(v / 100, 2) for v in s["valeurs"]]
    # Eurostat : âge au premier enfant (France). Avant 2013 = autre méthode,
    # non raccordable : on ne garde que 2013+.
    eu = json.loads(http_get("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/"
                             "data/demo_find?format=JSON&lang=FR&geo=FR&indic_de=AGEMOTH1"))
    idx = eu["dimension"]["time"]["category"]["index"]
    inv = {v: k for k, v in idx.items()}
    pe = sorted((int(inv[int(k)]), v) for k, v in eu["value"].items() if int(inv[int(k)]) >= 2013)
    return {
        "age_maternite": age,
        "icf_metropole": icf_metro,
        "icf_france": icf_fr,
        "age_premier_enfant": {"annees": [a for a, _ in pe], "valeurs": [v for _, v in pe],
                               "source_url": "https://ec.europa.eu/eurostat/databrowser/view/demo_find/default/table",
                               "note": "Eurostat, âge révolu ; série raccordable depuis 2013"},
    }


OCDE_PHMC = ("https://sdmx.oecd.org/public/rest/data/OECD.ELS.HD,HEALTH_PHMC@DF_PHMC_CONSUM,1.1/"
             "FRA+DEU+ITA+ESP.PH_CON.DDD_10P3HB._Z.N06A?startPeriod=2000")


def fetch_antidepresseurs():
    txt = http_get(OCDE_PHMC, accept="application/vnd.sdmx.data+csv; charset=utf-8")
    series = {}
    for r in csv.DictReader(io.StringIO(txt)):
        if r.get("PHARMACEUTICAL") != "N06A" or not r.get("OBS_VALUE"):
            continue
        series.setdefault(r["REF_AREA"], []).append(
            (int(r["TIME_PERIOD"]), float(r["OBS_VALUE"]), r.get("OBS_STATUS") or ""))
    if "FRA" not in series or len(series["FRA"]) < 10:
        raise RuntimeError("OCDE N06A France absente")
    out = {}
    for pays, pts in series.items():
        pts.sort()
        out[pays] = {"annees": [a for a, _, _ in pts], "valeurs": [v for _, v, _ in pts],
                     "provisoire": [s == "P" for _, _, s in pts]}
    return {"pays": out,
            "unite": "doses quotidiennes définies (DDJ) pour 1 000 habitants par jour",
            "lecture": "66 DDJ pour 1 000 habitants ≈ 6,6 % de la population traitée chaque jour",
            "source_url": "https://data-explorer.oecd.org/vis?df[ds]=dsDisseminateFinalDMZ&df[id]=HEALTH_PHMC%40DF_PHMC_CONSUM&df[ag]=OECD.ELS.HD",
            "api": OCDE_PHMC}


WB_PAYS = {"KOR": "Corée du Sud", "HKG": "Hong Kong", "SGP": "Singapour", "CHN": "Chine",
           "ESP": "Espagne", "JPN": "Japon", "ITA": "Italie", "DEU": "Allemagne",
           "GBR": "Royaume-Uni", "FRA": "France", "USA": "États-Unis"}


def fetch_fecondite_pays():
    url = ("https://api.worldbank.org/v2/country/" + ";".join(WB_PAYS) +
           "/indicator/SP.DYN.TFRT.IN?format=json&per_page=500&date=2015:2030")
    d = json.loads(http_get(url))
    der = {}
    for x in d[1] or []:
        if x.get("value") is None:
            continue
        iso = x["countryiso3code"]
        a = int(x["date"])
        if iso not in der or a > der[iso][0]:
            der[iso] = (a, round(float(x["value"]), 2))
    if len(der) < 8:
        raise RuntimeError("Banque mondiale : trop peu de pays")
    rows = [{"iso": k, "pays": WB_PAYS[k], "annee": a, "isf": v} for k, (a, v) in der.items()]
    rows.sort(key=lambda r: r["isf"])
    return {"pays": rows, "seuil_renouvellement": 2.1,
            "source_url": "https://data.worldbank.org/indicator/SP.DYN.TFRT.IN",
            "maj_source": d[0].get("lastupdated")}


def fetch_suicide_jeunes_fr():
    """Eurostat hlth_cd_acdr2 (données CépiDc-Inserm) : taux brut de suicide des
    15-24 ans pour 100 000, France, depuis 2011. Rupture de série en 2018
    (nouveau certificat de décès, intégration de l'institut médico-légal de Paris)."""
    d = json.loads(http_get("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
                            "hlth_cd_acdr2?geo=FR&icd10=X60-X84_Y870&age=Y15-24&sex=T&format=JSON&lang=FR"))
    idx = d["dimension"]["time"]["category"]["index"]
    inv = {v: k for k, v in idx.items()}
    pts = sorted((int(inv[int(k)]), round(float(v), 2)) for k, v in d["value"].items())
    if len(pts) < 5:
        raise RuntimeError("Eurostat suicide 15-24 : trop peu de points")
    return {"annees": [a for a, _ in pts], "valeurs": [v for _, v in pts],
            "rupture": 2018, "source_url": "https://ec.europa.eu/eurostat/databrowser/view/hlth_cd_acdr2/default/table",
            "note": "Taux brut pour 100 000 jeunes de 15 à 24 ans (Eurostat, d'après le CépiDc-Inserm). "
                    "Rupture de série en 2018 : une partie du rebond est un effet de méthode."}


GTRENDS_TERMES = ["pourquoi vivre", "sens de la vie"]


def fetch_gtrends_fr():
    """Moyennes annuelles des requêtes du cache Google Trends (SerpAPI) du site.
    Les indices y sont normalisés PAR LOT de 5 requêtes : seule l'évolution d'une
    même requête dans le temps a un sens (on la ramène à base 100 = 2010)."""
    if not GTRENDS_FR.exists():
        return None
    rows = json.loads(GTRENDS_FR.read_text())
    meta_p = CACHE_DIR / "gtrends_cache_FR_meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    out = {}
    for t in GTRENDS_TERMES:
        par_an = {}
        for r in rows:
            if r.get("keyword") == t and r.get("hits") is not None:
                par_an.setdefault(int(r["date"][:4]), []).append(float(r["hits"]))
        if not par_an:
            continue
        # année complète seulement (12 mois)
        ann = sorted((a, sum(v) / len(v)) for a, v in par_an.items() if len(v) == 12)
        base = dict(ann).get(2010)
        if not base:
            continue
        out[t] = {"annees": [a for a, _ in ann],
                  "base100": [round(v / base * 100) for _, v in ann],
                  "brut": [round(v, 2) for _, v in ann]}
    if not out:
        return None
    return {"termes": out, "pays": "France",
            "dernier_mois": meta.get("date_max"), "collecte": meta.get("updated_at"),
            "note": ("Indices Google Trends normalisés par lot de requêtes : on compare chaque "
                     "requête à elle-même (base 100 = moyenne 2010). Années complètes seulement."),
            "source_url": "https://trends.google.com/trends/explore?geo=FR"}


# ════════════════════════════════════════════════════════════════════════════
# SÉRIES RECOPIÉES D'ÉDITIONS PUBLIÉES (vérifiées le 04/10/2026)
# ════════════════════════════════════════════════════════════════════════════

# PISA — score moyen en compréhension de l'écrit (lecture), élèves de 15 ans.
# Dernière édition publiée : PISA 2025 (résultats du 8 septembre 2026, OCDE,
# « PISA 2025 Results (Volume I) », tableau I.1). 2000-2022 : éditions précédentes.
# Moyenne OCDE : les 23 pays présents à toutes les vagues depuis 2000
# (tableau I.B1.2a.37 de PISA 2025), seule moyenne comparable dans le temps.
PISA = {
    "edition": "PISA 2025",
    "publiee_le": "2026-09-08",
    "source_url": "https://www.oecd.org/en/publications/pisa-2025-results-volume-i_7e2f1a4c-en.html",
    "source_page": "https://www.oecd.org/en/about/programmes/pisa.html",
    "annees": [2000, 2009, 2018, 2022, 2025],
    "pays": [
        {"pays": "France", "scores": [505, 496, 493, 474, 456]},
        {"pays": "Allemagne", "scores": [484, 497, 498, 480, 465]},
        {"pays": "Italie", "scores": [487, 486, 476, 482, 474]},
        {"pays": "États-Unis", "scores": [504, 500, 505, 504, 490],
         "note": "2025 : normes d'échantillonnage PISA non toutes respectées"},
        {"pays": "Royaume-Uni", "scores": [523, 494, 504, 494, 494]},
        {"pays": "Corée du Sud", "scores": [525, 539, 514, 515, 501]},
        {"pays": "Japon", "scores": [522, 520, 504, 516, 503]},
        {"pays": "Finlande", "scores": [546, 536, 520, 490, 474]},
    ],
    "moyenne_ocde23": {"libelle": "Moyenne OCDE (23 pays présents depuis 2000)",
                       "scores": [500, 499, 493, 482, 466]},
    "moyenne_ocde_2025": 461,
    "maths_ocde": {"2018": 489, "2022": 472, "2025": 463},
}

# Cevipof — Baromètre de la confiance politique, % « très » + « plutôt » confiance.
# Vagues annuelles seulement (les vagues intermédiaires 6bis, 11b, 13b sont des
# panels ré-interrogés, non comparables). Dates = terrain de l'enquête.
CEVIPOF = {
    "source_url": "https://www.sciencespo.fr/cevipof/fr/content/les-resultats-par-vague.html",
    "derniere_vague": "Vague 17 (terrain du 23 janvier au 3 février 2026)",
    "derniere_vague_url": ("https://www.sciencespo.fr/cevipof/sites/sciencespo.fr.cevipof/files/"
                           "Barometre_confiance_CEVIPOFVague17_fev2026_vd1.pdf"),
    "question": ("« Avez-vous très confiance, plutôt confiance, plutôt pas confiance ou pas "
                 "confiance du tout dans… » — part de « très » + « plutôt » confiance"),
    "vagues": [
        # (vague, date de terrain, gouvernement, Assemblée nationale, justice, médias, partis)
        ("V1", "2009-12", 32, 38, None, 24, 14),
        ("V4", "2012-12", 26, 28, 45, 23, 12),
        ("V5", "2013-12", 25, 36, 44, 23, 11),
        ("V6", "2014-12", 23, 39, 48, 25, 9),
        ("V7", "2015-12", 29, 41, 44, 24, 12),
        ("V8", "2016-12", 28, 42, 44, 24, 11),
        ("V9", "2017-12", 30, 29, 44, 24, 9),
        ("V10", "2018-12", 22, 23, 44, 23, 9),
        ("V11", "2020-01", 27, 31, 46, 28, 13),
        ("V12", "2021-01", 35, 38, 48, 28, 16),
        ("V13", "2022-01", 35, 38, 46, 29, 21),
        ("V14", "2023-02", 26, 28, 44, 28, 16),
        ("V15", "2024-01", 28, 29, 45, 28, 20),
        ("V16", "2025-02", 23, 24, 44, 31, 16),
        ("V17", "2026-01", 17, 20, 45, 29, 15),
    ],
}

# Abstention à l'élection présidentielle (France entière, % des inscrits).
# Source : ministère de l'Intérieur, résultats officiels.
ABSTENTION = {
    "source_url": "https://www.archives-resultats-elections.interieur.gouv.fr/",
    "lignes": [(1981, 18.9, 14.1), (1988, 18.6, 15.9), (1995, 21.6, 20.3),
               (2002, 28.4, 20.3), (2007, 16.2, 16.0), (2012, 20.5, 19.6),
               (2017, 22.2, 25.4), (2022, 26.3, 28.0)],
}

# Registres complémentaires, vérifiés le 04/10/2026 sur la source primaire.
# Chaque registre porte sa source et son URL ; la page affiche le millésime.
REGISTRES = {
    "antidep_boites": {
        "titre": "Boîtes d'antidépresseurs remboursées en France",
        "source": "Assurance Maladie, Medic'AM (tous régimes, pharmacie de ville)",
        "url": "https://www.assurance-maladie.ameli.fr/etudes-et-donnees/medicaments-type-prescripteur-medicam",
        "points": [{"libelle": str(a), "valeur": v} for a, v in [
            (2012, 61.0), (2014, 60.8), (2016, 62.5), (2018, 64.4), (2019, 65.6), (2020, 67.5),
            (2021, 71.4), (2022, 74.7), (2023, 78.4), (2024, 84.2), (2025, 87.5)]],
        "unite": "millions de boîtes", "note": "Classe N06A, somme des boîtes remboursées par année.",
    },
    "pensees_suicidaires_17ans": {
        "titre": "Jeunes de 17 ans ayant pensé au suicide dans l'année",
        "source": "OFDT, enquête ESCAPAD (Observatoire national du suicide, 2025)",
        "url": "https://drees.solidarites-sante.gouv.fr/sites/default/files/2025-02/Fiche%202%20-%20Pens%C3%A9es%20suicidaires%20et%20tentatives%20de%20suicide%20parmi%20les%20adolescents%20fran%C3%A7ais%20de%2017%20ans.pdf",
        "series": [
            {"nom": "Filles", "points": [[2011, 13.7], [2014, 13.3], [2017, 14.8], [2022, 24.0]]},
            {"nom": "Garçons", "points": [[2011, 7.8], [2014, 7.5], [2017, 8.2], [2022, 12.3]]},
        ],
        "unite": "% des jeunes de 17 ans", "note": "« Au cours des douze derniers mois, avez-vous pensé à vous suicider ? » (France métropolitaine).",
    },
    "suicide_jeunes_us": {
        "titre": "Taux de suicide des 15-24 ans aux États-Unis",
        "source": "CDC / NCHS (WONDER et Data Briefs 464, 541, 572)",
        "url": "https://www.cdc.gov/nchs/products/databriefs/db572.htm",
        "points": [[2000, 10.2], [2010, 10.5], [2017, 14.5], [2019, 13.9], [2020, 14.2],
                   [2021, 15.2], [2022, 13.7], [2023, 13.5], [2024, 13.2]],
        "note": "Pour 100 000 jeunes de 15 à 24 ans. 2021-2024 : taux deux sexes recalculés à partir des décès et des taux par sexe publiés.",
    },
    "depression_us": {
        "source": "CDC / NCHS, NHANES (Data Briefs 7 et 527)",
        "url": "https://www.cdc.gov/nchs/products/databriefs/db527.htm",
        "points": [["2005-2006", 5.4], ["2013-2014", 8.2], ["2017-2020", 8.3], ["2021-2023", 13.1]],
        "note": "Part des 12 ans et plus présentant une dépression (questionnaire PHQ-9, score ≥ 10). Mode de questionnaire modifié en 2021.",
    },
    "abstinence_jeunes_us": {
        "titre": "Jeunes Américains de 18 à 24 ans sans rapport sexuel dans l'année",
        "source": "General Social Survey (Ueda et al., JAMA Network Open, 2020)",
        "url": "https://jamanetwork.com/journals/jamanetworkopen/fullarticle/2767066",
        "series": [
            {"nom": "Hommes 18-24 ans", "points": [["2000-2002", 18.9], ["2016-2018", 30.9]]},
            {"nom": "Femmes 18-24 ans", "points": [["2000-2002", 15.1], ["2016-2018", 19.1]]},
        ],
        "unite": "%", "note": "Part des 18-24 ans déclarant n'avoir eu aucun rapport sexuel au cours des 12 derniers mois.",
    },
    "amitie_us": {
        "titre": "Amitié aux États-Unis",
        "points": [
            {"annee": 1990, "libelle": "1990", "aucun": 3, "dix_plus": 33},
            {"annee": 2021, "libelle": "2021", "aucun": 12, "dix_plus": 13},
            {"annee": 2025, "libelle": "2025", "aucun": 16, "dix_plus": 15},
        ],
        "source": "Survey Center on American Life (AEI)",
        "url": "https://www.americansurveycenter.org/research/the-state-of-american-friendship-change-challenges-and-loss/",
        "url_2025": "https://www.americansurveycenter.org/wp-content/uploads/2025/07/American-Social-Life-Survey-Topline-Questionnaire-2.pdf",
        "note": ("Part des adultes américains déclarant n'avoir aucun ami proche, ou dix ou plus. 1990 : sondage Gallup, "
                 "cité par l'AEI (l'article de Gallup donnait 1 % sans ami proche) ; 2021 : State of American Friendship ; "
                 "2025 : American Social Life Survey (6 061 personnes). Hommes : 3 % sans ami proche en 1990, 15 % en 2021."),
    },
    "religion_us": {
        "titre": "Pratique religieuse chaque semaine",
        "source": "Gallup (États-Unis) · IFOP (France)",
        "url": "https://news.gallup.com/poll/1690/religion.aspx",
        "series": [
            {"nom": "États-Unis : office chaque semaine ou presque", "points": [[1992, 44], [2000, 46], [2024, 31], [2025, 31]]},
            {"nom": "France : messe chaque dimanche", "points": [[1961, 35], [2012, 6], [2025, 5]]},
        ],
        "unite": "% des adultes",
        "note": "Mesures différentes selon le pays ; France : IFOP pour l'Observatoire français du catholicisme (2025).",
    },
    "cocaine_saisies_fr": {
        "titre": "Saisies de cocaïne en France",
        "source": "OFDT d'après l'OFAST ; 2025 : chiffre provisoire du ministère de l'Intérieur",
        "url": "https://www.ofdt.fr/sites/ofdt/files/2026-02/note-offre-stupefiants-2024.pdf",
        "points": [{"libelle": str(a), "valeur": v} for a, v in [
            (2010, 4.1), (2012, 5.6), (2014, 6.8), (2016, 8.5), (2017, 17.5), (2018, 16.4), (2019, 15.8),
            (2020, 13.1), (2021, 26.5), (2022, 27.7), (2023, 23.2), (2024, 53.5), (2025, 84.3)]],
        "unite": "tonnes", "note": "Toutes administrations (douane, gendarmerie, police), outre-mer compris. 2025 provisoire.",
    },
    "atteintes_elus": {
        "titre": None,
        "unite": "faits recensés par an", "unite_courte": "faits",
        "points": [
            {"annee": 2019, "nombre": 421, "serie": "ancienne"},
            {"annee": 2020, "nombre": 1276, "serie": "ancienne"},
            {"annee": 2021, "nombre": 1720, "serie": "ancienne"},
            {"annee": 2022, "nombre": 2430, "serie": "CALAÉ"},
            {"annee": 2023, "nombre": 2759, "serie": "CALAÉ"},
            {"annee": 2024, "nombre": 2501, "serie": "CALAÉ"},
            {"annee": 2025, "nombre": 2478, "serie": "CALAÉ"},
        ],
        "definition": ("Atteintes aux élus (menaces et outrages pour les deux tiers, violences, dégradations) "
                       "recensées par le ministère de l'Intérieur. 2019-2021 : plaintes et signalements (ancienne série, "
                       "qui comptait 2 265 faits en 2022) ; depuis 2022 : procédures recensées par le Centre d'analyse "
                       "et de lutte contre les atteintes aux élus (CALAÉ), tous élus confondus."),
        "millesime": "Dernière année publiée : 2025 (rapport du CALAÉ, juillet 2026)",
        "source": "Ministère de l'Intérieur, CALAÉ",
        "url": "https://www.interieur.gouv.fr/sites/minint/files/medias/documents/2026-07/CALAE_rapport_annuel_2025.pdf",
    },
    "demissions_maires": {
        "titre": "Démissions volontaires de maires",
        "source": "Cevipof, Martial Foucault (juin 2025), d'après le Répertoire national des élus",
        "url": "https://www.sciencespo.fr/cevipof/files/Note1_demissions_maires_CEVIPOF_MF_19062025.pdf",
        "points": [{"libelle": str(a), "valeur": v} for a, v in [
            (2015, 152), (2016, 243), (2017, 293), (2018, 316), (2019, 180),
            (2021, 362), (2022, 536), (2023, 613), (2024, 500)]],
        "unite": "démissions par an",
        "note": "Années complètes seulement (2020 et 2025 partielles). 2 189 démissions entre juillet 2020 et mars 2025.",
    },
    "edelman_fr": {
        "edition": "Edelman Trust Barometer 2026", "url": "https://www.edelman.com/trust/trust-barometer",
        "france": {"gouvernement": 30, "medias": 40, "ong": 48, "entreprises": 52},
        "moyenne_28_pays": {"gouvernement": 53, "medias": 54, "ong": 58, "entreprises": 64},
        "note": "La France a la confiance dans le gouvernement la plus basse des 28 pays étudiés.",
    },
    "tissu_social_us": {
        "lignes": [
            {"indicateur": "Adultes sans aucun ami proche", "avant": "3 % (1990)", "apres": "16 % (2025)",
             "source": "Gallup, AEI", "url": "https://www.americansurveycenter.org/wp-content/uploads/2025/07/American-Social-Life-Survey-Topline-Questionnaire-2.pdf"},
            {"indicateur": "Adultes avec dix amis proches ou plus", "avant": "33 % (1990)", "apres": "15 % (2025)",
             "source": "Gallup, AEI", "url": "https://www.americansurveycenter.org/research/the-state-of-american-friendship-change-challenges-and-loss/"},
            {"indicateur": "Office religieux chaque semaine ou presque", "avant": "46 % (2000)", "apres": "31 % (2025)",
             "source": "Gallup", "url": "https://news.gallup.com/poll/1690/religion.aspx"},
            {"indicateur": "Temps quotidien consacré à la vie sociale (15 ans et plus)", "avant": "47 min (2003)", "apres": "35 min (2025)",
             "source": "BLS, American Time Use Survey", "url": "https://www.bls.gov/news.release/atus.t01.htm"},
            {"indicateur": "Temps quotidien passé avec des amis", "avant": "60 min (2003)", "apres": "20 min (2020)",
             "source": "Kannan et Veazie, 2023 (ATUS)", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC9811250/"},
        ],
    },
}


# ════════════════════════════════════════════════════════════════════════════
# ASSEMBLAGE
# ════════════════════════════════════════════════════════════════════════════

LIVE = [
    ("overdoses_us", fetch_overdoses_us),
    ("natalite_fr", fetch_natalite_fr),
    ("antidepresseurs_ocde", fetch_antidepresseurs),
    ("fecondite_pays", fetch_fecondite_pays),
    ("gtrends_fr", fetch_gtrends_fr),
    ("suicide_jeunes_fr", fetch_suicide_jeunes_fr),
]


def charger_precedent():
    try:
        return json.loads(OUT_JSON.read_text())
    except Exception:
        return {}


def build_payload():
    prev = charger_precedent()
    prev_src = ((prev.get("meta") or {}).get("sources") or {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload, sources, ok, failed = {}, {}, [], []
    for cle, fn in LIVE:
        err = None
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001 — une source qui tombe ne doit pas tout casser
            val, err = None, f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[{cle}] {err}\n")
        if val:
            payload[cle] = val
            sources[cle] = {"ok": True, "dernier_succes": now}
            ok.append(cle)
        else:
            payload[cle] = prev.get(cle)  # repli : dernière valeur réellement collectée
            old = prev_src.get(cle) or {}
            sources[cle] = {"ok": False, "dernier_succes": old.get("dernier_succes"),
                            "erreur": (err or "aucune donnée")[:300]}
            failed.append(cle)

    payload["pisa_lecture"] = PISA
    # forme historique conservée pour les lecteurs existants (DESK)
    payload["pisa_reading"] = [{"country": p["pays"], "y2000": p["scores"][0],
                                "y2009": p["scores"][1], "y2018": p["scores"][2],
                                "y2022": p["scores"][3], "y2025": p["scores"][4]}
                               for p in PISA["pays"]]
    payload["cevipof_confiance"] = {
        k: v for k, v in CEVIPOF.items() if k != "vagues"}
    payload["cevipof_confiance"]["vagues"] = [
        {"vague": v, "terrain": t, "gouvernement": g, "assemblee": a, "justice": j,
         "medias": m, "partis": p} for v, t, g, a, j, m, p in CEVIPOF["vagues"]]
    payload["fr_abstention"] = [{"year": y, "t1": t1, "t2": t2}
                                for y, t1, t2 in ABSTENTION["lignes"]]
    payload["fr_abstention_source"] = ABSTENTION["source_url"]
    for k, v in REGISTRES.items():
        payload[k] = v

    payload["meta"] = {
        "updated_at": now,
        "updated_at_unix": int(time.time()),
        "sources_ok": ok,
        "sources_failed": failed,
        "sources": sources,
        "doc_version": DOC_VERSION,
    }
    return payload, len(ok), len(failed)


def write_outputs(payload):
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    tmp = OUT_JSON.with_suffix(".json.tmp")
    tmp.write_text(body)
    os.replace(tmp, OUT_JSON)
    js = (f"/* these_effondrement_cache.js — generated {payload['meta']['updated_at']} */\n"
          f"window.__THESE_EFFONDREMENT__ = {body};\n")
    tmpj = OUT_JS.with_suffix(".js.tmp")
    tmpj.write_text(js)
    os.replace(tmpj, OUT_JS)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_effondrement_cache.json", "these_effondrement_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(target)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[FATAL] {e}\n")
        sys.exit(2)
    if n_ok == 0 and OUT_JSON.exists():
        sys.stderr.write("[GARDE] aucune source en direct — cache précédent conservé tel quel\n")
        sys.exit(1)
    write_outputs(payload)
    sys.stdout.write(f"[these_effondrement] OK · {n_ok} sources en direct, {n_fail} en repli · "
                     f"{time.time() - t0:.1f}s\n")


if __name__ == "__main__":
    main()
