#!/usr/bin/env python3
"""Cache antifragile pour le chapitre Thèse · Le monde post-dollar.

Sources (aucune clé nouvelle ; FRED passe par _fred_helpers comme ailleurs) :
  1. FRED · DTWEXBGS (indice large du dollar, quotidien) + RTWEXBGS (réel,
     mensuel) + DEXCHUS + CPIAUCSL (pouvoir d'achat depuis 1971)
     + A091RC1Q027SBEA / FGRECPT (part des recettes fédérales mangée par les
     intérêts)
  2. FMI · COFER via l'API SDMX (api.imf.org) — composition des réserves de
     change mondiales, TRIMESTRIEL, toutes devises (live)
  3. FMI · International Liquidity (IL) via la même API — stock d'or officiel
     déclaré par chaque pays, en onces, mensuel (live) → tableau des acheteurs
  4. Trésor US · TIC « Major Foreign Holders » (slt_table5.txt + historique
     mfhhis01.txt) — détentions chinoises de Treasuries, MENSUEL (live)
  5. Trésor US · Fiscal Data « debt to the penny » (dette fédérale du jour)
  6. Banque mondiale · PIB mondial en dollars courants (1971 → dernière année
     pleine) + population (poids des BRICS)
  7. FMI · DataMapper PPPSH (part du PIB mondial en PPA, WEO)
  8. DefiLlama · encours USDT / USDC
  9. STATIQUES SOURCÉS (pas d'API) : achats nets d'or des banques centrales
     (World Gold Council, Gold Demand Trends) et relevés SWIFT du yuan
     (Global Currency Tracker : PDF mensuels, accès robots refusé — HTTP 403).
     Chaque valeur porte son URL ; le millésime est affiché sur le graphe.

RÈGLE DE REPLI : une source qui tombe garde la DERNIÈRE valeur réellement
collectée (relue dans le cache précédent), jamais une constante périmée ;
meta.last_success[source] date le dernier succès de chaque source.

Sortie : these_dollar_cache.json + .js (window.__THESE_DOLLAR__).
"""
import csv
import io
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "these_dollar_cache.json"
OUT_JS   = CACHE_DIR / "these_dollar_cache.js"

UA = "Mozilla/5.0 SiteCryptoFinance-TheseDollar/1.0"
# L'Akamai du FMI (DataMapper) refuse certains agents : on reprend celui que le
# chapitre Dette utilise depuis des mois sans incident.
UA_IMF_DM = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-These/1.0"

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat(timespec="seconds")
OZ_PER_TONNE = 32150.7466

# ════════════════════════════════════════════════════════════════
# DONNÉES STATIQUES SOURCÉES (aucune API publique)
# ════════════════════════════════════════════════════════════════

# World Gold Council — achats nets d'or des banques centrales (tonnes/an).
# Vérifié le 04/10/2026 :
#   2025 = 863,3 t et 2024 = 1 092,4 t (révisé) : GDT Full Year 2025
#   2023 = 1 050,8 t (révisé) : GDT Full Year 2024
#   2022 = 1 081,9 t : GDT Full Year 2023
#   2010-2021 : série longue WGC. Contrôle : leur moyenne vaut 473 t/an,
#   exactement la « moyenne 2010-2021 » que le WGC publie dans GDT FY 2024 et
#   FY 2025 (les anciennes valeurs du site donnaient 441 t : millésime périmé).
#   2026 : S1 = 345 t (T1 révisé 57 t + T2 289 t) : GDT T2 2026 (30/07/2026).
WGC_CB_ANNUAL = [
    (2010, 79), (2011, 481), (2012, 569), (2013, 629), (2014, 601),
    (2015, 580), (2016, 395), (2017, 379), (2018, 656), (2019, 605),
    (2020, 255), (2021, 450), (2022, 1082), (2023, 1051), (2024, 1092),
    (2025, 863),
]
WGC_CB_PARTIAL = {"label": "2026 (S1)", "period": "2026-H1", "tonnes": 345,
                  "detail": "T1 2026 révisé à 57 t, T2 2026 à 289 t"}
WGC_META = {
    "edition": "Gold Demand Trends T2 2026",
    "published": "2026-07-30",
    "verified_at": "2026-10-04",
    "avg_2010_2021": 473,
    "total_demand_2025": 5002,
    "source_url": "https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q2-2026/central-banks",
    "source_url_annual": "https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2025/central-banks",
    "note": "Estimations WGC, achats non déclarés compris. Pas d'API publique : "
            "valeurs reprises à la main de chaque édition.",
}

# SWIFT — part du yuan dans les paiements internationaux (en valeur, zone euro
# comprise). Relevés MENSUELS ponctuels, chacun vérifié le 04/10/2026 sur la
# source citée. Le site SWIFT renvoie HTTP 403 aux robots : pas de collecte
# automatique possible, et les mois manquants ne sont PAS interpolés.
YUAN_SWIFT_OBS = [
    ("2012-08", 0.84, 12, "https://swift.com/news-events/press-releases/chinese-yuan-demonstrates-strong-momentum-reach-4-international-payments-currency"),
    ("2015-08", 2.79, 4,  "https://swift.com/news-events/press-releases/chinese-yuan-demonstrates-strong-momentum-reach-4-international-payments-currency"),
    ("2017-12", 1.61, 5,  "https://www.fx-markets.com/node/3418956"),
    ("2021-12", 2.70, 4,  "https://eng.yidaiyilu.gov.cn/qwyw/rdxw/216486.htm"),
    ("2022-01", 3.20, 4,  "https://chinadailyhk.com/article/260179"),
    ("2023-11", 4.61, 4,  "https://eng.yidaiyilu.gov.cn/p/0PBCJ6A7.html"),
    ("2024-03", 4.69, 4,  "https://www.bloomberg.com/news/articles/2024-04-18/yuan-usage-extends-global-climb-as-euro-share-slips-swift-says"),
    ("2024-07", 4.74, 4,  "https://1prime.ru/20240822/yuan-851054439.html"),
    ("2025-01", 3.79, 4,  "https://www.swift.com/sites/default/files/files/rmb-tracker_february-2025.pdf"),
    ("2026-01", 3.13, 5,  "https://www.swift.com/products/global-currency-tracker/document-centre"),
    ("2026-06", 3.10, 5,  "https://tradetreasurypayments.com/articles/usd-leads-global-payments-swifts-global-currency-tracker-july-2026"),
]
# Classement complet du dernier relevé (même source que le dernier point).
SWIFT_LATEST_RANKING = {
    "month": "2026-06",
    "shares": [("USD", 50.10), ("EUR", 21.88), ("GBP", 6.71), ("JPY", 3.66), ("CNY", 3.10)],
    "source_url": "https://tradetreasurypayments.com/articles/usd-leads-global-payments-swifts-global-currency-tracker-july-2026",
}
SWIFT_META = {
    "edition": "Global Currency Tracker (ex-RMB Tracker), données de juin 2026",
    "verified_at": "2026-10-04",
    "source_url": "https://www.swift.com/products/global-currency-tracker/document-centre",
    "note": "Paiements par message SWIFT seulement : les règlements passés par "
            "CIPS ou en direct entre banques chinoises et russes n'y figurent pas.",
}

# BRICS — chronologie vérifiée le 04/10/2026 (Business Today 10/09/2026,
# CGTN 10/09/2026, Tribune India). L'Arabie saoudite, invitée en 2023, figure
# sur les listes officielles mais n'a jamais confirmé publiquement : on compte
# les membres CONFIRMÉS et on le dit.
BRICS_EXPANSION = [
    (2001, "Jim O'Neill (Goldman Sachs) forge l'acronyme BRIC", 4),
    (2009, "1er sommet des chefs d'État à Iekaterinbourg", 4),
    (2010, "L'Afrique du Sud est invitée : le BRIC devient BRICS", 5),
    (2023, "Sommet de Johannesburg : six pays invités (l'Argentine refusera)", 5),
    (2024, "Entrée de l'Égypte, de l'Éthiopie, de l'Iran et des Émirats", 9),
    (2025, "Entrée de l'Indonésie ; statut de « pays partenaire » pour dix pays", 10),
]
BRICS_MEMBERS = ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE", "IDN"]
BRICS_PARTNERS_FR = ["Biélorussie", "Bolivie", "Cuba", "Kazakhstan", "Malaisie",
                     "Nigeria", "Thaïlande", "Ouganda", "Ouzbékistan", "Vietnam"]

# Classement FMI des économies avancées (WEO) — sert à étiqueter le tableau
# des acheteurs d'or. La Pologne et la Hongrie sont des économies ÉMERGENTES
# pour le FMI, même si elles sont membres de l'OCDE.
IMF_ADVANCED = set("""AND AUS AUT BEL CAN CHE CYP CZE DEU DNK ESP EST FIN FRA GBR GRC
HKG HRV IRL ISL ISR ITA JPN KOR LTU LUX LVA MAC MLT NLD NOR NZL PRI PRT SGP SMR SVK
SVN SWE TWN USA""".split())
NON_COUNTRY = {"BCE", "BEA", "BIS", "EZB", "IMF", "ANT", "GX010"}
NOM_FR = {
    "RUS": "Russie", "CHN": "Chine", "TUR": "Turquie", "IND": "Inde", "POL": "Pologne",
    "KAZ": "Kazakhstan", "SAU": "Arabie saoudite", "IRQ": "Irak", "THA": "Thaïlande",
    "BRA": "Brésil", "MEX": "Mexique", "HUN": "Hongrie", "QAT": "Qatar", "KOR": "Corée du Sud",
    "JPN": "Japon", "ARE": "Émirats arabes unis", "SGP": "Singapour", "JOR": "Jordanie",
    "CZE": "Tchéquie", "EGY": "Égypte", "BLR": "Biélorussie", "KGZ": "Kirghizistan",
    "KHM": "Cambodge", "UZB": "Ouzbékistan", "PHL": "Philippines", "SRB": "Serbie",
    "IRN": "Iran", "GHA": "Ghana", "AZE": "Azerbaïdjan", "OMN": "Oman", "LBY": "Libye",
    "DZA": "Algérie", "MNG": "Mongolie", "COL": "Colombie", "ECU": "Équateur",
    "TJK": "Tadjikistan", "VEN": "Venezuela", "ARG": "Argentine", "PER": "Pérou",
    "MYS": "Malaisie", "IDN": "Indonésie", "PAK": "Pakistan", "BGD": "Bangladesh",
    "USA": "États-Unis", "DEU": "Allemagne", "ITA": "Italie", "FRA": "France",
    "NLD": "Pays-Bas", "CHE": "Suisse", "GBR": "Royaume-Uni", "ESP": "Espagne",
    "PRT": "Portugal", "AUT": "Autriche", "BEL": "Belgique", "SWE": "Suède",
    "ZAF": "Afrique du Sud", "KWT": "Koweït", "LBN": "Liban", "ROU": "Roumanie",
    "GRC": "Grèce", "AUS": "Australie", "CAN": "Canada", "NOR": "Norvège",
    "FIN": "Finlande", "DNK": "Danemark", "IRL": "Irlande", "UKR": "Ukraine",
    "BHR": "Bahreïn", "MAR": "Maroc", "TUN": "Tunisie", "ETH": "Éthiopie",
}

COFER_LABELS = {"USD": "Dollar US", "EUR": "Euro", "JPY": "Yen", "GBP": "Livre sterling",
                "CNY": "Yuan", "CAD": "Dollar canadien", "AUD": "Dollar australien",
                "CHF": "Franc suisse", "OTHC": "Autres devises"}

# ════════════════════════════════════════════════════════════════
# OUTILS
# ════════════════════════════════════════════════════════════════

def http_get(url, timeout=30, max_retries=4, accept="*/*", ua=UA):
    req = Request(url, headers={"User-Agent": ua, "Accept": accept})
    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="ignore")
        except HTTPError as e:
            if 500 <= e.code < 600 and attempt < max_retries - 1:
                time.sleep(4 * (2 ** attempt)); continue
            raise
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(4 * (2 ** attempt))
    raise last_err if last_err else RuntimeError("retries exhausted")


def http_json(url, **kw):
    kw.setdefault("accept", "application/json")
    return json.loads(http_get(url, **kw))


# FRED via API officielle (fredgraph.csv instable depuis ~mai 2026)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fred_helpers import fetch_fred  # noqa: E402


def load_prev():
    try:
        return json.loads(OUT_JSON.read_text())
    except (OSError, ValueError):
        return {}


def sdmx_series(d):
    """SDMX-JSON 2.1 (api.imf.org) → {clé-tuple: {période: valeur}}."""
    st = d["structure"]
    dims = st["dimensions"]["series"]
    periods = [v["id"] for v in st["dimensions"]["observation"][0]["values"]]
    out = {}
    for key, s in d["dataSets"][0]["series"].items():
        idx = [int(i) for i in key.split(":")]
        k = tuple(dims[j]["values"][idx[j]]["id"] for j in range(len(dims)))
        obs = {}
        for o, v in (s.get("observations") or {}).items():
            if v and v[0] not in (None, ""):
                try:
                    obs[periods[int(o)]] = float(v[0])
                except (TypeError, ValueError):
                    pass
        out[k] = obs
    names = {v["id"]: v["name"] for v in dims[0]["values"]}
    return out, names


# ════════════════════════════════════════════════════════════════
# COLLECTEURS
# ════════════════════════════════════════════════════════════════

def fetch_cofer():
    """FMI COFER : part de chaque devise dans les réserves ALLOUÉES, trimestriel."""
    url = ("https://api.imf.org/external/sdmx/2.1/data/COFER/"
           "G001.AFXRA..SHRO_PT.Q?startPeriod=1999")
    d = http_json(url, timeout=90)
    series, _ = sdmx_series(d)
    shares = {}
    for (cty, ind, cur, tr, fq), obs in series.items():
        if cur.startswith("CI_") and cur != "CI_T":
            shares[cur[3:]] = obs
    if "USD" not in shares or len(shares["USD"]) < 40:
        raise RuntimeError("COFER : série USD absente ou trop courte")
    quarters = sorted(shares["USD"])
    out = {"dates": quarters, "shares": {}, "labels": COFER_LABELS}
    for cur, obs in shares.items():
        # null avant que le FMI isole la devise (yuan : fin 2016 ; AUD/CAD : fin 2012)
        out["shares"][cur] = [round(obs[q], 3) if q in obs else None for q in quarters]
    out.update({
        "latest_quarter": quarters[-1],
        "source": "FMI · COFER (Currency Composition of Official Foreign Exchange Reserves)",
        "source_url": "https://data.imf.org/en/datasets/IMF.STA:COFER",
        "api_url": url,
        "note": "Parts dans les réserves de change ALLOUÉES (dont la devise est connue). "
                "L'or n'est pas une devise : il n'entre pas dans COFER.",
    })
    return out


def fetch_tic():
    """Trésor US · TIC : détentions de Treasuries par pays, mensuel, 2000 → dernier mois."""
    base = "https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/"
    MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
    want = {"china": re.compile(r'^"?China, Mainland'), "japan": re.compile(r"^Japan"),
            "total": re.compile(r"^Grand Total")}
    data = {k: {} for k in want}

    # 1) historique (blocs annuels ; en cas de rupture de série, le mois figure
    #    deux fois : la PREMIÈRE colonne est la série nouvelle, on la garde)
    hist = http_get(base + "mfhhis01.txt", timeout=60)
    lines = hist.splitlines()
    cols = None
    for i, ln in enumerate(lines):
        cells = ln.split("\t")
        if cells and cells[0].strip() == "Country":
            years = [c.strip() for c in cells[1:]]
            months = [c.strip() for c in lines[i - 1].split("\t")[1:]] if i else []
            cols = []
            for m, y in zip(months, years):
                if m in MONTHS and y.isdigit():
                    cols.append(f"{int(y):04d}-{MONTHS[m]:02d}")
                else:
                    cols.append(None)
            continue
        if cols is None or not cells:
            continue
        name = cells[0].strip()
        for key, rx in want.items():
            if rx.match(name):
                for c, v in zip(cols, cells[1:]):
                    if not c or c in data[key]:
                        continue
                    try:
                        data[key][c] = float(v.strip().replace(",", ""))
                    except ValueError:
                        pass
    # 2) 13 derniers mois (fait foi sur l'historique : révisions récentes)
    cur = http_get(base + "slt_table5.txt", timeout=60)
    header = None
    for ln in cur.splitlines():
        cells = ln.split("\t")
        if cells and cells[0].strip() == "Country":
            header = [c.strip() for c in cells[1:]]
            continue
        if not header:
            continue
        name = cells[0].strip()
        for key, rx in want.items():
            if rx.match(name):
                for c, v in zip(header, cells[1:]):
                    if re.match(r"^\d{4}-\d{2}$", c or ""):
                        try:
                            data[key][c] = float(v.strip().replace(",", ""))
                        except ValueError:
                            pass
    if len(data["china"]) < 120:
        raise RuntimeError(f"TIC : série chinoise trop courte ({len(data['china'])})")
    out = {}
    for key, obs in data.items():
        ms = sorted(obs)
        out[key] = {"dates": ms, "values": [obs[m] for m in ms]}
    ch = out["china"]
    i_pk = max(range(len(ch["values"])), key=lambda i: ch["values"][i])
    out["china_peak"] = {"date": ch["dates"][i_pk], "value": ch["values"][i_pk]}
    out["latest_month"] = ch["dates"][-1]
    out["source"] = "Trésor US · TIC, Major Foreign Holders of Treasury Securities"
    out["source_url"] = base + "slt_table5.txt"
    out["history_url"] = base + "mfhhis01.txt"
    out["note"] = ("Données collectées auprès des dépositaires américains : des titres "
                   "chinois gardés en Belgique ou au Luxembourg sont attribués à ces pays. "
                   "Ruptures de série lors des enquêtes annuelles (ex. juin 2010).")
    return out


def fetch_official_gold():
    """FMI · International Liquidity : or officiel déclaré, en tonnes, par pays."""
    ua = "Mozilla/5.0 SiteCryptoFinance-TheseDollar/1.0"
    da = http_json("https://api.imf.org/external/sdmx/2.1/data/IL/.RGV_REVS.FTO.A?startPeriod=2007",
                   timeout=120, ua=ua)
    sa, names = sdmx_series(da)
    yr = str(NOW.year - 1)
    dm = http_json("https://api.imf.org/external/sdmx/2.1/data/IL/.RGV_REVS.FTO.M?startPeriod="
                   + yr + "-12", timeout=120, ua=ua)
    sm, _ = sdmx_series(dm)
    annual = {k[0]: v for k, v in sa.items()}
    monthly = {k[0]: v for k, v in sm.items()}
    rows = []
    for iso, obs in annual.items():
        if iso in NON_COUNTRY or re.match(r"^G\d", iso) or len(iso) != 3:
            continue
        base07 = obs.get("2007")
        base21 = obs.get("2021")
        # point le plus récent : mensuel s'il existe, sinon annuel
        mo = monthly.get(iso) or {}
        if mo:
            last_p = max(mo); last_v = mo[last_p]
            last_lbl = last_p.replace("-M", "-")
        else:
            ys = sorted(obs)
            if not ys:
                continue
            last_p = ys[-1]; last_v = obs[last_p]; last_lbl = last_p + "-12"
        if base07 is None:
            continue
        rows.append({
            "iso": iso,
            "name": NOM_FR.get(iso, names.get(iso, iso)),
            "group": "avancée" if iso in IMF_ADVANCED else "émergente",
            "tonnes_now": round(last_v / OZ_PER_TONNE, 1),
            "asof": last_lbl,
            "delta_since_2007": round((last_v - base07) / OZ_PER_TONNE, 1),
            "delta_since_2021": round((last_v - base21) / OZ_PER_TONNE, 1) if base21 is not None else None,
        })
    rows.sort(key=lambda r: -r["delta_since_2007"])
    if len(rows) < 50:
        raise RuntimeError("IL : trop peu de pays")
    w_a = annual.get("G001", {})
    w_m = monthly.get("G001", {})
    world = None
    if w_a.get("2007") and w_a.get("2021"):
        lp = max(w_m) if w_m else max(w_a)
        lv = w_m[lp] if w_m else w_a[lp]
        world = {"asof": lp.replace("-M", "-"),
                 "tonnes_now": round(lv / OZ_PER_TONNE),
                 "delta_since_2007": round((lv - w_a["2007"]) / OZ_PER_TONNE),
                 "delta_since_2021": round((lv - w_a["2021"]) / OZ_PER_TONNE)}
    return {
        "top": rows[:10],
        "n_countries": len(rows),
        "world": world,
        "base_years": {"long": "fin 2007", "short": "fin 2021"},
        "source": "FMI · International Liquidity (réserves d'or déclarées, en onces converties en tonnes)",
        "source_url": "https://data.imf.org/en/datasets/IMF.STA:IL",
        "note": "Réserves DÉCLARÉES au FMI. Une partie des achats n'est pas déclarée : "
                "le World Gold Council l'estime par enquête, d'où des totaux plus élevés.",
    }


def fetch_us_fiscal():
    out = {}
    d = http_json("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/"
                  "accounting/od/debt_to_penny?fields=record_date,tot_pub_debt_out_amt"
                  "&sort=-record_date&page[size]=1", timeout=40)
    r = d["data"][0]
    out["debt_usd"] = float(r["tot_pub_debt_out_amt"])
    out["debt_date"] = r["record_date"]
    ip = fetch_fred("A091RC1Q027SBEA", start="2015-01-01")
    rc = fetch_fred("FGRECPT", start="2015-01-01")
    if ip and rc:
        m = dict(zip(rc["dates"], rc["values"]))
        pairs = [(dt, v) for dt, v in zip(ip["dates"], ip["values"]) if m.get(dt)]
        if pairs:
            dt, v = pairs[-1]
            out["interest_pct_receipts"] = round(100 * v / m[dt], 1)
            out["interest_date"] = dt
    out["source_url"] = "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny"
    return out


def fetch_purchasing_power():
    s = fetch_fred("CPIAUCSL", start="1971-01-01")
    if not s:
        return None
    m = dict(zip(s["dates"], s["values"]))
    base_d = "1971-08-01"            # 15 août 1971 : fin de la convertibilité or
    if base_d not in m:
        return None
    return {"from": base_d, "cpi_from": m[base_d], "to": s["dates"][-1],
            "cpi_to": s["values"][-1],
            "loss_pct": round(100 * (1 - m[base_d] / s["values"][-1]), 1),
            "source_url": "https://fred.stlouisfed.org/series/CPIAUCSL"}


def fetch_world_gdp():
    d = http_json("https://api.worldbank.org/v2/country/WLD/indicator/NY.GDP.MKTP.CD"
                  "?format=json&per_page=100&date=1970:2030", timeout=40)
    obs = {int(r["date"]): r["value"] for r in d[1] if r.get("value")}
    if 1971 not in obs:
        return None
    last = max(obs)
    return {"y0": 1971, "v0": obs[1971], "y1": last, "v1": obs[last],
            "ratio": round(obs[last] / obs[1971], 1),
            "source_url": "https://data.worldbank.org/indicator/NY.GDP.MKTP.CD?locations=1W"}


def fetch_brics_weight():
    out = {"members": BRICS_MEMBERS, "partners_fr": BRICS_PARTNERS_FR}
    # PIB en PPA (part du monde) — WEO via DataMapper
    ind = http_json("https://www.imf.org/external/datamapper/api/v1/indicators",
                    timeout=40, ua=UA_IMF_DM)
    src = ((ind.get("indicators") or {}).get("PPPSH") or {}).get("source", "")
    d = http_json("https://www.imf.org/external/datamapper/api/v1/PPPSH/" + "/".join(BRICS_MEMBERS),
                  timeout=40, ua=UA_IMF_DM)
    vals = (d.get("values") or {}).get("PPPSH") or {}
    year = str(NOW.year - 1)        # dernière année écoulée (estimation FMI)
    tot, ok = 0.0, 0
    for iso in BRICS_MEMBERS:
        v = (vals.get(iso) or {}).get(year)
        if v is not None:
            tot += v; ok += 1
    if ok == len(BRICS_MEMBERS):
        out["ppp_share_pct"] = round(tot, 1)
        out["ppp_year"] = int(year)
        out["ppp_source"] = src or "World Economic Outlook"
    # Population — Banque mondiale, dernière année publiée
    p = http_json("https://api.worldbank.org/v2/country/" + ";".join(BRICS_MEMBERS + ["WLD"])
                  + "/indicator/SP.POP.TOTL?format=json&mrnev=1&per_page=100", timeout=40)
    pops = {r["countryiso3code"]: (r["value"], r["date"]) for r in p[1] if r.get("value")}
    if all(i in pops for i in BRICS_MEMBERS) and "WLD" in pops:
        s = sum(pops[i][0] for i in BRICS_MEMBERS)
        out["pop_bn"] = round(s / 1e9, 2)
        out["pop_share_pct"] = round(100 * s / pops["WLD"][0], 1)
        out["pop_year"] = int(pops["WLD"][1])
    out["source_url"] = "https://www.imf.org/external/datamapper/PPPSH@WEO"
    if "ppp_share_pct" not in out and "pop_bn" not in out:
        raise RuntimeError("BRICS : ni PPA ni population")
    return out


def fetch_stablecoins():
    d = http_json("https://stablecoins.llama.fi/stablecoins?includePrices=false", timeout=40)
    out = {}
    for a in d.get("peggedAssets", []):
        sym = a.get("symbol")
        if sym in ("USDT", "USDC"):
            v = (a.get("circulating") or {}).get("peggedUSD")
            if v:
                out[sym.lower() + "_usd"] = round(v)
    if "usdt_usd" not in out or "usdc_usd" not in out:
        raise RuntimeError("DefiLlama : USDT/USDC absents")
    out["date"] = NOW.date().isoformat()
    out["source_url"] = "https://defillama.com/stablecoins"
    return out


# Repli si FRED tombe : relu du cache précédent (jamais une constante).
def build_payload():
    prev = load_prev()
    prev_meta = prev.get("meta") or {}
    last_success = dict(prev_meta.get("last_success") or {})
    ok, failed, stale = [], [], []
    payload = {}

    def run(key, label, fn):
        try:
            v = fn()
            if v is None:
                raise RuntimeError("vide")
            payload[key] = v
            ok.append(label)
            last_success[label] = NOW_ISO
        except Exception as e:                                   # noqa: BLE001
            sys.stderr.write(f"[{label}] {e}\n")
            failed.append(label)
            if prev.get(key) is not None:
                payload[key] = prev[key]
                stale.append(key)

    def fred_block(key, sid, start):
        def f():
            s = fetch_fred(sid, start=start)
            if not s:
                return None
            return s
        run(key, f"FRED:{sid}", f)

    fred_block("usd_dxy", "DTWEXBGS", "2006-01-01")
    fred_block("cny_usd", "DEXCHUS", "2000-01-01")
    fred_block("real_dxy", "RTWEXBGS", "2006-01-01")
    run("cofer", "IMF:COFER", fetch_cofer)
    run("tic", "TREASURY:TIC", fetch_tic)
    run("cb_gold_official", "IMF:IL_GOLD", fetch_official_gold)
    run("us_fiscal", "TREASURY+FRED:fiscal", fetch_us_fiscal)
    run("usd_purchasing_power", "FRED:CPIAUCSL", fetch_purchasing_power)
    run("world_gdp", "WB:NY.GDP.MKTP.CD", fetch_world_gdp)
    run("brics_weight", "IMF+WB:BRICS", fetch_brics_weight)
    run("stablecoins", "DEFILLAMA:stablecoins", fetch_stablecoins)

    # ── Champs historiques (forme ancienne conservée pour compatibilité) ──
    cofer = payload.get("cofer")
    if cofer:
        usd = cofer["shares"]["USD"]
        # Annuel = point du T4 seulement : jamais une année partielle.
        payload["usd_reserves_share"] = [
            {"year": int(q[:4]), "pct": round(v, 1)}
            for q, v in zip(cofer["dates"], usd) if q.endswith("Q4") and v is not None]
        lq = cofer["latest_quarter"]
        payload["reserves_by_currency"] = [
            {"currency": c, "pct": round(cofer["shares"][c][-1], 1), "country": COFER_LABELS.get(c, c),
             "quarter": lq}
            for c in ("USD", "EUR", "JPY", "GBP", "CNY", "CAD", "AUD", "CHF", "OTHC")
            if c in cofer["shares"] and cofer["shares"][c][-1] is not None]
    tic = payload.get("tic")
    if tic:
        ch = tic["china"]
        dec = {d[:4]: v for d, v in zip(ch["dates"], ch["values"]) if d.endswith("-12")}
        payload["china_treasury"] = [{"year": int(y), "bn_usd": round(v)} for y, v in sorted(dec.items())
                                     if int(y) >= 2000]
    payload["cb_gold_wgc"] = {
        "annual": [{"year": y, "tonnes": t} for y, t in WGC_CB_ANNUAL],
        "partial": WGC_CB_PARTIAL,
        **WGC_META,
    }
    # ancienne clé, mêmes valeurs vérifiées (années pleines seulement)
    payload["cb_gold_net_buys"] = [{"year": y, "tonnes_net": t} for y, t in WGC_CB_ANNUAL]
    payload["yuan_swift"] = {
        "obs": [{"month": m, "pct": p, "rank": r, "source_url": u} for m, p, r, u in YUAN_SWIFT_OBS],
        "latest_ranking": {"month": SWIFT_LATEST_RANKING["month"],
                           "shares": [{"currency": c, "pct": p} for c, p in SWIFT_LATEST_RANKING["shares"]],
                           "source_url": SWIFT_LATEST_RANKING["source_url"]},
        **SWIFT_META,
    }
    # ancienne clé : relevés vérifiés, un point par mois (plus d'année inventée)
    payload["yuan_share_swift"] = [{"year": m, "pct": p} for m, p, r, u in YUAN_SWIFT_OBS]
    payload["brics_timeline"] = [{"year": y, "event": e, "members": n} for y, e, n in BRICS_EXPANSION]
    og = payload.get("cb_gold_official")
    if og:
        payload["top_cb_gold_buyers"] = [
            {"country": r["name"], "tonnes_cumulative": round(r["delta_since_2007"]),
             "region": "Économie avancée" if r["group"] == "avancée" else "Économie émergente"}
            for r in og["top"]]

    if "usd_dxy" not in payload:
        raise RuntimeError("DTWEXBGS indisponible et aucun cache précédent")

    payload["meta"] = {
        "updated_at":      NOW_ISO,
        "updated_at_unix": int(time.time()),
        "sources_ok":      ok,
        "sources_failed":  failed,
        "stale_blocks":    stale,
        "last_success":    last_success,
        "doc_version":     "2.0",
    }
    order = ["meta"] + [k for k in payload if k != "meta"]
    return {k: payload[k] for k in order}, len(ok), len(failed)


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js = (
        f"/* these_dollar_cache.js — generated {payload['meta']['updated_at']} */\n"
        f"window.__THESE_DOLLAR__ = "
        f"{json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )
    OUT_JS.write_text(js)
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("these_dollar_cache.json", "these_dollar_cache.js"):
            link = site_dir / name
            target = CACHE_DIR / name
            try:
                if link.is_symlink() or link.exists(): link.unlink()
                link.symlink_to(target)
            except OSError as e:
                sys.stderr.write(f"[SYMLINK] {e}\n")
                shutil.copy2(target, link)


def main():
    t0 = time.time()
    try:
        payload, n_ok, n_fail = build_payload()
    except Exception as e:
        sys.stderr.write(f"[FATAL] {e}\n"); sys.exit(2)
    write_outputs(payload)
    dt = time.time() - t0
    sys.stdout.write(f"[these_dollar] OK · {n_ok} sources, {n_fail} failed · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
