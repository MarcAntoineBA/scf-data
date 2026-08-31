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
PRIX_JETONS = [
    ("2020-06", "GPT-3 davinci", 60.00),
    ("2022-11", "GPT-3.5 turbo", 2.00),
    ("2023-03", "GPT-4", 60.00),
    ("2023-11", "GPT-4 turbo", 30.00),
    ("2024-05", "GPT-4o", 15.00),
    ("2024-07", "GPT-4o mini", 0.60),
    ("2024-12", "DeepSeek V3", 1.10),
    ("2025-01", "DeepSeek R1", 2.19),
    ("2025-04", "GPT-4.1 mini", 1.60),
    ("2025-08", "GPT-5", 10.00),
    ("2026-01", "modèles légers 2026", 0.40),
]

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
        "prix_jetons": [{"date": d, "modele": m, "usd_par_m_jetons": p}
                        for d, m, p in PRIX_JETONS],
        "effondrement_prix": EFFONDREMENT_PRIX,
        "productivite_mondiale": wb,
        "productivite_us": {"variation": bls_var, "indice": bls_idx},
        "fred": fred,
        "societes": societes,
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
