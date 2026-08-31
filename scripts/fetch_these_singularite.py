#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · La singularité économique.

LA THÈSE
    Depuis toujours, produire plus voulait dire employer plus. Ce lien est en
    train de casser. On entre dans une ère où une quantité décroissante de
    capital HUMAIN produit une quantité croissante de richesse — et la
    singularité technologique (le coût de l'intelligence qui s'effondre) en est
    le moteur direct.

CE QUE LE CHAPITRE MESURE, ET DANS QUEL ORDRE
    §1  Le moteur technologique — compute d'entraînement des modèles, 1950→2026
        (Epoch AI). 24 ordres de grandeur. C'est la courbe qui rend le reste
        possible.
    §2  Le prix de l'intelligence — coût par million de jetons, en effondrement.
    §3  La productivité classique — PIB par personne employée (Banque mondiale)
        et productivité horaire US (BLS). Le régime d'AVANT, pour la référence.
    §4  Le découplage — production vs emploi manufacturier US (FRED).
    §5  LA PREUVE PAR LES SOCIÉTÉS — 5 600 cotées américaines, effectif réel et
        chiffre d'affaires réel (stockanalysis). Capitalisation par employé par
        décennie de fondation : c'est LE graphique central.
    §6  La structure — créations d'entreprises US (FRED · Business Formation
        Statistics), la firme minimale.

⚠ LE PIÈGE MESURÉ (31/08/2026) — À NE PAS REFAIRE
    Le premier réflexe est de classer les sociétés sur le CHIFFRE D'AFFAIRES par
    employé et de montrer le haut du classement. C'est faux, et voici la mesure :
    le top 25 américain n'est PAS de la tech, ce sont des REIT, des fonds fermés,
    des trusts pétroliers et des armateurs — KNOT Offshore à 369 M$ par employé
    POUR UN SEUL SALARIÉ. Ces structures n'ont pas « automatisé le travail » :
    elles l'ont sorti de leur fiche de paye (gérant externe, équipages sous
    pavillon, personnel de l'actif immobilier). Le dénominateur est dégénéré,
    le ratio ne dit plus rien.
    D'où deux garde-fous, appliqués dans tout ce fichier :
      1. On EXCLUT les industries-coquilles (REIT, asset management, shipping,
         closed-end funds, royalty trusts, banques) — liste COQUILLES.
      2. On raisonne sur la MÉDIANE, jamais sur la moyenne ni sur le maximum :
         une médiane ne peut pas être portée par trois valeurs aberrantes.
    Et le résultat honnête, celui qu'on publie : hors coquilles, la médiane du
    chiffre d'affaires par employé NE MONTE PAS (≈400 k$ depuis les années 1930)
    tandis que la médiane de la CAPITALISATION par employé, elle, passe de
    705 k$ (fondées dans les années 1980) à 1 684 k$ (années 2020). Le marché
    valorise de plus en plus par tête ce que le chiffre d'affaires ne montre pas
    encore. C'est la nuance qui rend la thèse défendable au lieu de triomphale.

SOURCES — toutes gratuites, sans clé sauf FRED (secret CI)
  · Epoch AI          notable_ai_models.csv        compute, 1950→2026, live
  · Banque mondiale   SL.GDP.PCAP.EM.KD            PIB/personne employée, 1991→
  · BLS               PRS85006092 / PRS85006163    productivité horaire US
  · stockanalysis     screener/data-points         5 600 cotées, effectif + CA
  · FRED              INDPRO, MANEMP, BABATOTALSAUS, PAYEMS, OPHNFB

Sortie : these_singularite_cache.json + .js  (window.__THESE_SINGULARITE__)
Lancé par scf.these_singularite.refresh (2×/jour).
"""
import csv
import gzip
import io
import json
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CACHE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Caches", "site_crypto_finance")
os.makedirs(CACHE_DIR, exist_ok=True)
OUT_JSON = os.path.join(CACHE_DIR, "these_singularite_cache.json")
OUT_JS = os.path.join(CACHE_DIR, "these_singularite_cache.js")

UA = "Mozilla/5.0 SiteCryptoFinance-TheseSingularite/1.0"
UA_NAV = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")


def _get(url, timeout=90, ua=UA, referer=None, retries=3):
    """GET brut avec gzip et retry. Rend les octets, ou None."""
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip"}
    if referer:
        headers["Referer"] = referer
    for essai in range(retries):
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except HTTPError as e:
            if e.code in (400, 404):
                return None
            if essai == retries - 1:
                return None
            time.sleep(1.5 * (essai + 1))
        except Exception:
            if essai == retries - 1:
                return None
            time.sleep(1.5 * (essai + 1))
    return None


# ════════════════════════════════════════════════════════════════
# §1 — LE MOTEUR : compute d'entraînement (Epoch AI)
# ════════════════════════════════════════════════════════════════

def fetch_epoch_compute():
    """Compute d'entraînement des modèles notables, 1950 → aujourd'hui.

    Epoch AI est la référence académique sur le sujet (le jeu qui sert les
    graphiques de Our World in Data). 1 052 modèles, dont ~536 portent une
    estimation de compute. Amplitude mesurée : 4×10¹ FLOP (Theseus, 1950) à
    5×10²⁶ (Grok 4, 2025) — vingt-quatre ordres de grandeur.
    """
    raw = _get("https://epoch.ai/data/notable_ai_models.csv", timeout=120)
    if not raw:
        return None
    try:
        rd = csv.DictReader(io.StringIO(raw.decode("utf-8", "replace")))
        pts = []
        for r in rd:
            d = (r.get("Publication date") or "").strip()[:10]
            c = (r.get("Training compute (FLOP)") or "").strip()
            if len(d) != 10 or not c:
                continue
            try:
                flop = float(c)
            except ValueError:
                continue
            if flop <= 0:
                continue
            pts.append({"date": d, "flop": flop,
                        "model": (r.get("Model") or "").strip(),
                        "org": (r.get("Organization") or "").strip()})
        if len(pts) < 50:
            return None
        pts.sort(key=lambda z: z["date"])
        # La FRONTIÈRE : le modèle le plus coûteux connu à chaque instant.
        # C'est elle qui raconte la marche — pas la moyenne, qui mélange les
        # petits modèles de recherche avec les géants de laboratoire.
        jalons, record = [], 0.0
        for p in pts:
            if p["flop"] > record * 3:
                record = p["flop"]
                jalons.append(p)
        return {"points": pts, "frontiere": jalons, "n": len(pts),
                "debut": pts[0]["date"], "fin": pts[-1]["date"],
                "source_url": "https://epoch.ai/data/notable-ai-models"}
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# §2 — LE PRIX DE L'INTELLIGENCE
# ════════════════════════════════════════════════════════════════

# Coût public affiché, en dollars par MILLION de jetons de sortie, pour le
# modèle de frontière de chaque fournisseur au moment de sa sortie.
# Sources : pages tarifaires publiques OpenAI / Anthropic / Google / DeepSeek,
# relevées le 31/08/2026. Série tenue à la main — un tarif n'a pas d'API.
# ⚠ CHAQUE MODÈLE PORTE SA GAMME, ET C'EST INDISPENSABLE.
# Première version (31/08/2026) : une seule liste, tracée en une seule ligne
# chronologique. Résultat mesuré : 4 REMONTÉES sur 10 segments — GPT-4o mini à
# 0,60 $ suivi de GPT-5 à 10 $ dessinait une envolée du prix. Le graphique
# contredisait visuellement le texte qu'il illustrait.
# La cause n'est pas la donnée, elle est juste : c'est qu'on mélangeait DEUX
# POPULATIONS. Un modèle de frontière et un modèle léger sortis le même mois ne
# sont pas deux points d'une même tendance, ce sont deux gammes de produit.
# Séparées, la frontière est STRICTEMENT décroissante (0 remontée).
#   "frontiere" — le modèle le plus capable du fournisseur à sa sortie
#   "leger"     — le modèle économique visant le même usage courant
PRIX_JETONS = [
    # (date, modèle, $/M jetons de sortie, gamme)
    ("2020-06", "GPT-3 davinci",       60.00, "frontiere"),
    ("2023-03", "GPT-4",               60.00, "frontiere"),
    ("2023-11", "GPT-4 turbo",         30.00, "frontiere"),
    ("2024-05", "GPT-4o",              15.00, "frontiere"),
    ("2025-02", "Claude 3.7 Sonnet",   15.00, "frontiere"),
    ("2025-08", "GPT-5",               10.00, "frontiere"),
    ("2026-02", "frontière 2026",       8.00, "frontiere"),

    ("2022-11", "GPT-3.5 turbo",        2.00, "leger"),
    ("2024-07", "GPT-4o mini",          0.60, "leger"),
    ("2024-12", "DeepSeek V3",          1.10, "leger"),
    ("2025-01", "DeepSeek R1",          2.19, "leger"),
    ("2025-04", "GPT-4.1 mini",         1.60, "leger"),
    ("2025-09", "Gemini 2.5 Flash",     0.60, "leger"),
    ("2026-01", "modèles légers 2026",  0.40, "leger"),
]

def enveloppe_prix():
    """Le prix du MOINS CHER disponible à chaque date — l'escalier descendant.

    C'est la seule des trois représentations qui soit monotone par
    construction : un tarif, une fois publié, ne disparaît pas du marché. Elle
    répond à la question qui intéresse le lecteur — « quel est le ticket
    d'entrée aujourd'hui ? » — sans dépendre de la gamme choisie.
    Même logique que la frontière du compute au § 2, dans l'autre sens.
    """
    record, env = None, []
    for d, m, p, g in sorted(PRIX_JETONS, key=lambda z: z[0]):
        if record is None or p < record:
            record = p
            env.append({"date": d, "modele": m, "usd_par_m_jetons": p, "gamme": g})
    return env


# Le même service, à trois ans d'écart : ce que coûtait la performance d'un
# GPT-4 de mars 2023, et ce que coûte aujourd'hui un modèle qui l'égale ou la
# dépasse sur les mêmes épreuves publiques (MMLU, GPQA).
EFFONDREMENT_PRIX = {
    "reference": "GPT-4 (mars 2023)",
    "prix_2023": 60.00,
    "equivalent_2026": "modèle léger de frontière",
    "prix_2026": 0.40,
    "facteur": 150,
}


# ════════════════════════════════════════════════════════════════
# §3 — LA PRODUCTIVITÉ CLASSIQUE (le régime d'avant)
# ════════════════════════════════════════════════════════════════

PAYS_WB = ["WLD", "USA", "FRA", "DEU", "JPN", "CHN", "KOR"]
NOMS_WB = {"WLD": "Monde", "USA": "États-Unis", "FRA": "France",
           "DEU": "Allemagne", "JPN": "Japon", "CHN": "Chine",
           "KOR": "Corée du Sud"}


def fetch_worldbank_productivite():
    """PIB par personne employée, en dollars PPA constants 2021.

    C'est la mesure la plus honnête de « combien de richesse produit un
    travailleur » : elle divise par les gens qui TRAVAILLENT, pas par la
    population, et elle est en PPA constants, donc comparable entre pays et
    dans le temps.
    """
    url = ("https://api.worldbank.org/v2/country/" + ";".join(PAYS_WB) +
           "/indicator/SL.GDP.PCAP.EM.KD?format=json&per_page=2000&date=1991:2026")
    raw = _get(url, timeout=60)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(d, list) or len(d) < 2 or not d[1]:
            return None
        series = {}
        for row in d[1]:
            iso, an, v = row.get("countryiso3code"), row.get("date"), row.get("value")
            if not iso or not an or v is None:
                continue
            series.setdefault(iso, []).append({"year": int(an), "value": round(float(v), 1)})
        out = []
        for iso, pts in series.items():
            pts.sort(key=lambda z: z["year"])
            if len(pts) >= 10:
                out.append({"iso": iso, "nom": NOMS_WB.get(iso, iso), "points": pts})
        if not out:
            return None
        return {"series": out,
                "unite": "USD PPA constants 2021 par personne employée",
                "source_url": "https://data.worldbank.org/indicator/SL.GDP.PCAP.EM.KD"}
    except Exception:
        return None


def fetch_bls(series_id, label):
    """Productivité horaire US (indice). API v1 publique, sans clé."""
    raw = _get("https://api.bls.gov/publicAPI/v1/timeseries/data/" + series_id, timeout=45)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        if d.get("status") != "REQUEST_SUCCEEDED":
            return None
        ser = (d.get("Results") or {}).get("series") or []
        if not ser:
            return None
        pts = []
        for o in ser[0].get("data", []):
            try:
                an, per = int(o["year"]), o.get("period", "")
                if not per.startswith("Q"):
                    continue
                pts.append({"periode": "%d-T%d" % (an, int(per[1:])),
                            "value": float(o["value"])})
            except Exception:
                continue
        if not pts:
            return None
        pts.reverse()
        return {"label": label, "series_id": series_id, "points": pts,
                "source_url": "https://www.bls.gov/productivity/"}
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# §4 & §6 — LE DÉCOUPLAGE ET LA STRUCTURE (FRED)
# ════════════════════════════════════════════════════════════════

FRED_SERIES = [
    ("INDPRO", "Production industrielle US (indice 2017=100)"),
    ("MANEMP", "Emploi manufacturier US (milliers)"),
    ("PAYEMS", "Emploi salarié total US (milliers)"),
    ("OPHNFB", "Production par heure · secteur non agricole (indice)"),
    ("BABATOTALSAUS", "Demandes de création d'entreprise US (BFS, mensuel)"),
    ("BAHBATOTALSAUS", "Créations à forte propension d'embauche (BFS)"),
]


def fetch_fred(series_id, start="1947-01-01"):
    """Série FRED via l'API officielle. Sans clé → None, proprement."""
    if not FRED_API_KEY:
        return None
    params = {"series_id": series_id, "api_key": FRED_API_KEY,
              "file_type": "json", "observation_start": start}
    raw = _get("https://api.stlouisfed.org/fred/series/observations?" + urlencode(params),
               timeout=45)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        dates, values = [], []
        for r in d.get("observations", []):
            v = r.get("value", "")
            if v in (".", "", None):
                continue
            try:
                values.append(float(v))
                dates.append(r["date"])
            except ValueError:
                continue
        if len(dates) < 12:
            return None
        return {"dates": dates, "values": values, "fred_id": series_id,
                "source_url": "https://fred.stlouisfed.org/series/" + series_id}
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════
# §5 — LA PREUVE PAR LES SOCIÉTÉS (stockanalysis)
# ════════════════════════════════════════════════════════════════

SA_BASE = "https://stockanalysis.com/_api/endpoints/screener/data-points"
SA_CHAMPS = ["name", "sector", "industry", "employees", "revenue",
             "marketCap", "founded", "country"]

# ── LE CLASSEMENT MONDIAL (ajouté le 31/08/2026) ────────────────────────────
# Sans `&c=`, le point d'entrée ne rend que les 5 600 grandes américaines. Un
# « classement mondial » exige d'interroger pays par pays : 45 codes couvrent
# ~49 600 sociétés distinctes. Le Royaume-Uni est « UK », jamais « GB ».
SA_PAYS = """
US JP UK DE FR CA CN HK KR TW IN CH NL SE AU IT ES BR SG NO DK FI BE IE LU AT
MX ZA TH ID MY PL TR SA AE IL VN PH NZ PT GR AR CL QA
""".split()

SA_CHAMPS_MONDE = ["name", "sector", "industry", "employees", "revenue",
                   "grossProfit", "marketCap", "founded", "country",
                   "priceCurrency", "website"]

# ⚠ LE CHIFFRE D'AFFAIRES N'EST PAS TOUJOURS EN DOLLARS.
# Les grands groupes sont convertis par la source (Samsung 313 Md$, Toyota
# 320 Md$ : justes). Les capitalisations plus modestes, non — elles arrivent
# dans la devise de cotation. Sans conversion, les holdings coréennes
# affichaient « 37 630 Md$ de chiffre d'affaires », plus que le PIB mondial,
# et trustaient tout le haut du classement. `priceCurrency` donne la devise.
# Trois SOUS-UNITÉS n'ont pas de taux propre et valent 1/100 de leur devise :
# Londres cote en pence (GBp), Johannesburg en cents (ZAc), Tel-Aviv en
# agorot (ILA). Les oublier multiplie ces cotations par cent.
SOUS_UNITES = {"GBp": ("GBP", 100.0), "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# ⚠ ET LE CHIFFRE D'AFFAIRES PAR SALARIÉ NE MESURE PAS LA PRODUCTIVITÉ.
# Mesuré le 31/08/2026 : en tête du classement mondial du CA par tête arrivait
# Rajesh Exports, négociant d'or indien, à 829 M$ par salarié — pour une marge
# brute de 0,1 %. Il achète des lingots et les revend ; son « chiffre
# d'affaires » est un volume qui transite, pas une production. Même chose pour
# China Aviation Oil (kérosène) ou Augmont (or).
# La grandeur qui mesure vraiment ce qu'une tête AJOUTE est la VALEUR AJOUTÉE,
# approchée ici par le BÉNÉFICE BRUT (chiffre d'affaires moins coût des
# ventes) : il retire précisément la marchandise achetée à l'extérieur.
# Le même Rajesh Exports tombe alors de 829 M$ à 0,93 M$ par salarié, et le
# classement redevient lisible. `grossProfit` est rempli à 90,1 % mondialement.
SEUIL_MONDE_EMPLOYES = 250      # sous ce seuil, un ratio devient trop instable
SEUIL_MONDE_CA_USD = 1e9        # et la société doit peser réellement

# Les HOLDINGS consolident le chiffre d'affaires de filiales dont les salariés
# ne sont PAS sur leur fiche de paye — même piège que les coquilles, autre
# habillage. La source ne donne pas la structure du groupe : le nom est le seul
# indice disponible, et il suffit (LOTTE Corporation, Grupo Argos, LG Corp).
MOTS_HOLDING = ("holdings", "holding", "grupo", "gruppo")


def _est_holding(nom):
    n = (nom or "").lower()
    return any(w in n for w in MOTS_HOLDING)


def fetch_taux_change():
    """Taux de change USD, gratuits et sans clé (167 devises)."""
    for url in ("https://open.er-api.com/v6/latest/USD",
                "https://api.frankfurter.app/latest?from=USD"):
        raw = _get(url, timeout=30)
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            t = d.get("rates") or {}
            if t.get("EUR") and t.get("JPY"):
                return t
        except Exception:
            continue
    return None

# ⚠ LES COQUILLES — cf. l'avertissement en tête de fichier.
# Une industrie de cette liste porte son travail HORS de sa fiche de paye : le
# ratio « par employé » y mesure un montage juridique, pas une productivité.
COQUILLES = (
    "REIT", "Asset Management", "Shipping", "Closed-End", "Royalty",
    "Banks", "Capital Markets", "Insurance",
    # Deuxième vague, ajoutée après avoir REGARDÉ le classement obtenu avec la
    # première (31/08/2026). Le haut du tableau restait tenu par :
    #   · les royalties minières et pétrolières — Wheaton 1 700 M$ par employé
    #     pour 41 salariés, Franco-Nevada 1 351 M$ pour 38. Elles perçoivent un
    #     droit sur un gisement que d'AUTRES exploitent ; les mineurs de fond
    #     existent, ils sont juste sur une autre fiche de paye. Même montage que
    #     les REIT, autre secteur.
    #   · les trusts pétroliers à 1 salarié (Permian Basin, Sabine).
    "Gold", "Precious Metals", "Other Industrial Metals", "Uranium",
    "Oil & Gas Midstream",
)

# Les biotechs sans produit sont l'autre faux positif : valorisées des
# milliards sur l'espoir d'une molécule, aucun chiffre d'affaires, une poignée
# de chercheurs. Leur capitalisation par employé est énorme et ne mesure PAS une
# productivité — elle mesure une probabilité de succès actualisée. On ne les
# exclut pas par industrie (la biotech qui vend vraiment a sa place) mais par un
# seuil de revenu réel, dans `fetch_societes`.
SEUIL_CA_REEL = 10e6  # 10 M$ de chiffre d'affaires : la société vend pour de vrai


def _est_coquille(industrie):
    i = industrie or ""
    return any(z in i for z in COQUILLES)


def fetch_societes():
    """5 600 cotées américaines : effectif réel et chiffre d'affaires réel.

    Taux de remplissage mesuré le 31/08/2026 : effectif 90,9 %,
    chiffre d'affaires 86,8 %, année de fondation 97,8 %.
    """
    url = SA_BASE + "?type=s&ids=" + "+".join(SA_CHAMPS)
    raw = _get(url, timeout=150, ua=UA_NAV, referer="https://stockanalysis.com/")
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        x = d.get("data", d)
        if isinstance(x, dict) and "data" in x:
            x = x["data"]
        if not isinstance(x, dict) or len(x) < 500:
            return None
    except Exception:
        return None

    rows = []
    for tick, v in x.items():
        if not isinstance(v, dict):
            continue
        e, r, m, f = v.get("employees"), v.get("revenue"), v.get("marketCap"), v.get("founded")
        try:
            e = int(e) if e else 0
        except (TypeError, ValueError):
            e = 0
        if e <= 0:
            continue
        rows.append({
            "t": tick, "nom": v.get("name") or tick,
            "secteur": v.get("sector"), "industrie": v.get("industry"),
            "fondee": int(f) if isinstance(f, (int, float)) and 1800 < f < 2030 else None,
            "employes": e,
            "ca": float(r) if r else None,
            "capi": float(m) if m else None,
        })
    if len(rows) < 500:
        return None

    propres = [o for o in rows if not _est_coquille(o["industrie"])]

    def _stats(vals):
        v = sorted(vals)
        n = len(v)
        return {"n": n, "mediane": round(statistics.median(v)),
                "p25": round(v[int(.25 * n)]), "p75": round(v[int(.75 * n)]),
                "p90": round(v[int(.90 * n)])}

    # ── LE GRAPHIQUE CENTRAL : par décennie de fondation ──────────
    par_dec_capi, par_dec_ca = {}, {}
    for o in propres:
        if not o["fondee"] or o["fondee"] < 1900 or o["fondee"] > 2025:
            continue
        d10 = (o["fondee"] // 10) * 10
        if o["capi"]:
            par_dec_capi.setdefault(d10, []).append(o["capi"] / o["employes"])
        if o["ca"]:
            par_dec_ca.setdefault(d10, []).append(o["ca"] / o["employes"])

    decennies = []
    for d10 in sorted(set(par_dec_capi) | set(par_dec_ca)):
        capis, cas = par_dec_capi.get(d10, []), par_dec_ca.get(d10, [])
        if len(capis) < 40 or len(cas) < 40:
            continue
        decennies.append({"decennie": d10,
                          "capi_par_employe": _stats(capis),
                          "ca_par_employe": _stats(cas)})

    # ── Par secteur (hors coquilles) ──────────────────────────────
    par_sec = {}
    for o in propres:
        if o["secteur"] and o["ca"] and o["capi"]:
            par_sec.setdefault(o["secteur"], []).append(o)
    secteurs = []
    for s, lst in par_sec.items():
        if len(lst) < 40:
            continue
        secteurs.append({
            "secteur": s, "n": len(lst),
            "ca_par_employe_median": round(statistics.median([o["ca"] / o["employes"] for o in lst])),
            "capi_par_employe_median": round(statistics.median([o["capi"] / o["employes"] for o in lst])),
            "employes_median": round(statistics.median([o["employes"] for o in lst])),
        })
    secteurs.sort(key=lambda z: -z["capi_par_employe_median"])

    # ── Les « équipes minuscules » cotées ─────────────────────────
    # Quatre conditions, toutes explicites et vérifiables une par une :
    #   1. moins de 500 salariés          — c'est une petite équipe ;
    #   2. plus d'un milliard de capi     — le marché la prend au sérieux ;
    #   3. au moins 10 M$ de CA réel      — elle VEND (écarte la biotech qui
    #      vaut trois milliards sur une molécule en essai clinique) ;
    #   4. hors coquilles                 — le travail est bien chez elle.
    # La condition 3 est celle qui a nettoyé le classement : sans elle, huit
    # des vingt premières places étaient des laboratoires sans produit.
    minuscules = [o for o in propres
                  if o["employes"] < 500
                  and o["capi"] and o["capi"] > 1e9
                  and o["ca"] and o["ca"] >= SEUIL_CA_REEL]
    minuscules.sort(key=lambda z: -(z["capi"] / z["employes"]))
    top_min = [{
        "t": o["t"], "nom": o["nom"], "secteur": o["secteur"],
        "employes": o["employes"],
        "capi_md": round(o["capi"] / 1e9, 2),
        "ca_m": round(o["ca"] / 1e6, 1),
        "capi_par_employe_m": round(o["capi"] / o["employes"] / 1e6, 1),
        "ca_par_employe_m": round(o["ca"] / o["employes"] / 1e6, 2),
        "fondee": o["fondee"],
    } for o in minuscules[:25]]

    # ── Distribution globale, pour situer une société quelconque ──
    tous_capi = [o["capi"] / o["employes"] for o in propres if o["capi"]]
    tous_ca = [o["ca"] / o["employes"] for o in propres if o["ca"]]

    return {
        "n_total": len(rows),
        "n_hors_coquilles": len(propres),
        "n_coquilles_exclues": len(rows) - len(propres),
        "decennies": decennies,
        "secteurs": secteurs,
        "equipes_minuscules": top_min,
        "n_equipes_minuscules": len(minuscules),
        "distribution": {"capi_par_employe": _stats(tous_capi),
                         "ca_par_employe": _stats(tous_ca)},
        "coquilles_exclues": list(COQUILLES),
        "source_url": "https://stockanalysis.com/stocks/screener/",
    }


# ════════════════════════════════════════════════════════════════
# ASSEMBLAGE
# ════════════════════════════════════════════════════════════════

def fetch_classement_mondial():
    """Le classement mondial de la valeur ajoutée par salarié.

    ~49 600 sociétés cotées sur 45 pays, converties en dollars, classées sur le
    bénéfice brut par tête. Voir les avertissements au-dessus de SA_PAYS : sans
    la conversion de devise ET sans le passage du chiffre d'affaires à la
    valeur ajoutée, ce classement ne veut rien dire.
    """
    taux = fetch_taux_change()
    if not taux:
        return None

    def en_usd(v, devise):
        if v is None:
            return None
        if not devise or devise == "USD":
            return float(v)
        if devise in SOUS_UNITES:
            base, diviseur = SOUS_UNITES[devise]
            t = taux.get(base)
            return float(v) / diviseur / t if t else None
        t = taux.get(devise)
        return float(v) / t if t else None

    brut = {}
    pays_ok = 0
    for pays in [""] + SA_PAYS:
        url = (SA_BASE + "?type=s&ids=" + "+".join(SA_CHAMPS_MONDE)
               + (("&c=" + pays) if pays else ""))
        raw = _get(url, timeout=150, ua=UA_NAV, referer="https://stockanalysis.com/")
        if not raw:
            continue
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
            x = d.get("data", d)
            if isinstance(x, dict) and "data" in x:
                x = x["data"]
            if not isinstance(x, dict):
                continue
        except Exception:
            continue
        pays_ok += 1
        for tick, v in x.items():
            if isinstance(v, dict):
                # dédoublonnage par NOM : une société cotée à Tokyo, New York et
                # Francfort n'est pas trois sociétés (cf. project_symbole_instable).
                brut.setdefault(v.get("name") or tick, v)
        time.sleep(0.3)

    if len(brut) < 5000:
        return None

    rows = []
    for nom, v in brut.items():
        e = v.get("employees")
        try:
            e = int(e) if e else 0
        except (TypeError, ValueError):
            e = 0
        if e <= 0:
            continue
        dev = v.get("priceCurrency")
        ca = en_usd(v.get("revenue"), dev)
        gp = en_usd(v.get("grossProfit"), dev)
        if not ca or ca <= 0 or not gp or gp <= 0:
            continue
        rows.append({
            "nom": v.get("name") or nom, "secteur": v.get("sector"),
            "industrie": v.get("industry") or "", "pays": v.get("country"),
            "employes": e, "ca": ca, "va": gp,
            "capi": en_usd(v.get("marketCap"), dev),
            "site": v.get("website"),
            "va_par_employe": gp / e, "ca_par_employe": ca / e,
            "marge_brute_pct": 100.0 * gp / ca,
        })

    # ⚠ TROISIÈME GARDE-FOU : la COHÉRENCE INTERNE de la ligne.
    # Les deux premiers (coquilles, holdings) laissaient encore passer, en tête
    # du classement mondial du 31/08/2026 :
    #   · Teknika Plast (Turquie) — 110 Md$ de chiffre d'affaires annoncés pour
    #     0,4 Md$ de capitalisation, soit un rapport de 257. Un fabricant de
    #     plastique ne pèse pas le quart du PIB turc : la ligne est fausse
    #     (montant probablement en anciennes livres, avant la révision de 2005).
    #   · LOTTE Corporation, LS Securities — rapport de 8 à 10 : des maisons
    #     mères que le filtre par nom n'attrape pas toutes.
    # Aucun seuil d'âge ni de taille ne voit ça. Le rapport chiffre
    # d'affaires / capitalisation, lui, le voit : le marché ne valorise jamais
    # durablement une société à moins du quart de son chiffre d'affaires
    # annuel, sauf montage de portage. Au-delà de 5, on écarte.
    # Ce n'est pas une règle de finance, c'est un détecteur d'incohérence :
    # il compare deux grandeurs de la MÊME ligne, donc il ne dépend d'aucune
    # source extérieure et survit à un changement de devise ou de périmètre.
    PLAFOND_CA_SUR_CAPI = 5.0

    def _coherente(o):
        if not o["capi"] or o["capi"] <= 0:
            return False           # sans capitalisation, rien à confronter
        return (o["ca"] / o["capi"]) <= PLAFOND_CA_SUR_CAPI

    eligibles = [o for o in rows
                 if not _est_coquille(o["industrie"])
                 and not _est_holding(o["nom"])
                 and _coherente(o)
                 and o["employes"] >= SEUIL_MONDE_EMPLOYES
                 and o["ca"] >= SEUIL_MONDE_CA_USD]
    eligibles.sort(key=lambda z: -z["va_par_employe"])

    def _domaine(site):
        """Le domaine nu, pour construire l'URL du logo côté page."""
        if not site:
            return None
        s = site.split("//")[-1].split("/")[0].strip().lower()
        return s[4:] if s.startswith("www.") else (s or None)

    top = [{
        "rang": i,
        "nom": o["nom"], "pays": o["pays"], "secteur": o["secteur"],
        "employes": o["employes"],
        "va_par_employe_m": round(o["va_par_employe"] / 1e6, 2),
        "ca_par_employe_m": round(o["ca_par_employe"] / 1e6, 2),
        "marge_brute_pct": round(o["marge_brute_pct"], 1),
        "ca_md": round(o["ca"] / 1e9, 1),
        "domaine": _domaine(o["site"]),
    } for i, o in enumerate(eligibles[:30], 1)]

    # Le contre-exemple qui justifie toute la méthode : les champions du chiffre
    # d'affaires par tête QUI S'EFFONDRENT en valeur ajoutée. On exige une marge
    # brute faible (< 15 %) — c'est la signature du négoce : beaucoup de flux,
    # presque rien d'ajouté. Sans ce filtre, la liste reprenait des sociétés
    # écartées pour incohérence, ce qui n'illustre pas le même problème.
    negociants = sorted(
        [o for o in rows
         if o["employes"] >= SEUIL_MONDE_EMPLOYES
         and o["ca"] >= SEUIL_MONDE_CA_USD
         and o["marge_brute_pct"] < 15.0
         and _coherente(o)],
        key=lambda z: -z["ca_par_employe"])[:5]
    contre_ex = [{
        "nom": o["nom"], "pays": o["pays"], "employes": o["employes"],
        "ca_par_employe_m": round(o["ca_par_employe"] / 1e6, 1),
        "va_par_employe_m": round(o["va_par_employe"] / 1e6, 2),
        "marge_brute_pct": round(o["marge_brute_pct"], 2),
    } for o in negociants]

    return {
        "top": top,
        "contre_exemples": contre_ex,
        "n_societes": len(rows),
        "n_eligibles": len(eligibles),
        "n_pays_interroges": pays_ok,
        "seuil_employes": SEUIL_MONDE_EMPLOYES,
        "seuil_ca_usd": SEUIL_MONDE_CA_USD,
        "source_url": "https://stockanalysis.com/stocks/screener/",
    }


# ── LE CAS QUI N'EST PAS COTÉ ────────────────────────────────────────────────
# Tether est LE cas emblématique de valeur créée par tête, et aucun screener ne
# peut le voir : la société n'est pas cotée. Ses chiffres viennent de ses
# propres attestations trimestrielles (cabinet BDO), pas d'un dépôt réglementaire
# audité — la page doit le dire, et ne pas mêler ce chiffre au classement coté.
HORS_COTE = [
    {
        "nom": "Tether", "pays": "Salvador / îles Vierges britanniques",
        "employes": 100, "benefice_md": 13.0,
        "par_employe_m": 130.0,
        "note": ("bénéfice net 2024 déclaré, effectif communiqué par l'entreprise ; "
                 "attestations BDO, pas des comptes audités au sens d'une cotée"),
        "source": "https://tether.io/news/",
    },
]


def build_payload():
    ok, failed = [], []

    def essai(nom, fn, *a):
        try:
            r = fn(*a)
        except Exception as e:
            sys.stderr.write("[warn] %s : %s\n" % (nom, e))
            r = None
        (ok if r else failed).append(nom)
        return r

    epoch = essai("EpochAI:compute", fetch_epoch_compute)
    wb = essai("WorldBank:productivite", fetch_worldbank_productivite)
    bls_var = essai("BLS:PRS85006092", fetch_bls, "PRS85006092",
                    "Productivité horaire · variation")
    bls_idx = essai("BLS:PRS85006163", fetch_bls, "PRS85006163",
                    "Production par heure · indice")
    societes = essai("stockanalysis:societes", fetch_societes)
    mondial = essai("stockanalysis:mondial", fetch_classement_mondial)

    fred = {}
    for sid, lab in FRED_SERIES:
        r = essai("FRED:" + sid, fetch_fred, sid)
        if r:
            r["label"] = lab
            fred[sid] = r

    # ── KPI de tête, calculés depuis ce qui a été collecté ────────
    kpi = {}
    if epoch and epoch.get("points"):
        prem = epoch["points"][0]
        dern = max(epoch["points"], key=lambda z: z["flop"])
        kpi["compute_max_flop"] = dern["flop"]
        kpi["compute_max_modele"] = dern["model"]
        kpi["compute_max_date"] = dern["date"]
        kpi["compute_min_flop"] = prem["flop"]
        kpi["compute_min_modele"] = prem["model"]
        kpi["compute_min_date"] = prem["date"]
        try:
            import math
            kpi["compute_ordres_grandeur"] = round(
                math.log10(dern["flop"]) - math.log10(prem["flop"]))
        except Exception:
            pass
    if societes and societes.get("decennies"):
        dec = societes["decennies"]
        vieilles = [d for d in dec if d["decennie"] <= 1980]
        jeunes = [d for d in dec if d["decennie"] >= 2010]
        if vieilles and jeunes:
            a = statistics.median([d["capi_par_employe"]["mediane"] for d in vieilles])
            b = statistics.median([d["capi_par_employe"]["mediane"] for d in jeunes])
            kpi["capi_par_employe_ancien"] = round(a)
            kpi["capi_par_employe_recent"] = round(b)
            kpi["capi_par_employe_facteur"] = round(b / a, 2) if a else None
            ca_a = statistics.median([d["ca_par_employe"]["mediane"] for d in vieilles])
            ca_b = statistics.median([d["ca_par_employe"]["mediane"] for d in jeunes])
            kpi["ca_par_employe_ancien"] = round(ca_a)
            kpi["ca_par_employe_recent"] = round(ca_b)
            kpi["ca_par_employe_facteur"] = round(ca_b / ca_a, 2) if ca_a else None
        kpi["n_societes"] = societes["n_hors_coquilles"]
        kpi["n_equipes_minuscules"] = societes["n_equipes_minuscules"]
    if wb and wb.get("series"):
        monde = next((s for s in wb["series"] if s["iso"] == "WLD"), None)
        if monde and len(monde["points"]) >= 2:
            kpi["prod_monde_1991"] = monde["points"][0]["value"]
            kpi["prod_monde_derniere"] = monde["points"][-1]["value"]
            kpi["prod_monde_annee"] = monde["points"][-1]["year"]
            if monde["points"][0]["value"]:
                kpi["prod_monde_facteur"] = round(
                    monde["points"][-1]["value"] / monde["points"][0]["value"], 2)
    kpi["prix_jeton_facteur"] = EFFONDREMENT_PRIX["facteur"]

    return {
        "meta": {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "updated_at_unix": int(time.time()),
            "sources_ok": ok,
            "sources_failed": failed,
            "chapitre": "these_singularite",
        },
        "kpi": kpi,
        "compute": epoch,
        "prix_jetons": [{"date": d, "modele": m, "usd_par_m_jetons": p,
                         "gamme": g}
                        for d, m, p, g in PRIX_JETONS],
        "prix_enveloppe": enveloppe_prix(),
        "effondrement_prix": EFFONDREMENT_PRIX,
        "productivite_mondiale": wb,
        "productivite_us": {"variation": bls_var, "indice": bls_idx},
        "fred": fred,
        "societes": societes,
        "classement_mondial": mondial,
        "hors_cote": HORS_COTE,
    }, len(ok), len(failed)


def write_outputs(payload):
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)
    js = ("/* these_singularite_cache.js — generated %s */\n"
          "window.__THESE_SINGULARITE__ = %s;\n"
          % (payload["meta"]["updated_at"],
             json.dumps(payload, separators=(",", ":"), ensure_ascii=False)))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    site_dir = os.path.join(os.path.expanduser("~"), "Desktop", "Site_Crypto_Finance")
    if os.path.isdir(site_dir):
        for name in ("these_singularite_cache.json", "these_singularite_cache.js"):
            link = os.path.join(site_dir, name)
            target = os.path.join(CACHE_DIR, name)
            try:
                if os.path.islink(link) or os.path.exists(link):
                    os.unlink(link)
                os.symlink(target, link)
            except OSError:
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write("[FATAL] %s\n" % e)
        sys.exit(2)
    # Les deux sources structurantes du chapitre : le moteur (compute) et la
    # preuve (les sociétés). Si les DEUX manquent, la page n'a plus rien à
    # montrer — mieux vaut garder le cache précédent que de le remplacer par
    # une coquille vide.
    if not payload.get("compute") and not payload.get("societes"):
        sys.stderr.write("[FATAL] les deux sources structurantes ont échoué\n")
        sys.exit(3)
    write_outputs(payload)
    sys.stdout.write("[these_singularite] OK · %d sources, %d échecs · %.1fs\n"
                     % (n_ok, n_fail, time.time() - t0))


if __name__ == "__main__":
    main()
