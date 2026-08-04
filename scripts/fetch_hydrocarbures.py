"""Fetcher Hydrocarbures — pétrole brut & gaz naturel, par pays, historique maximal.

Alimente DEUX consommateurs à partir d'UN SEUL cache (source unique = cohérence
garantie entre la carte de l'Atlas Économique et l'onglet dédié Hydrocarbures) :
  - onglet « Hydrocarbures »            → window.__HYDRO__
  - carte + dossier pays de l'Atlas     → même objet, chargé en lazy

────────────────────────────────────────────────────────────────────────────────
SOURCES (toutes publiques, toutes auditables, testées en direct le 2026-07-29)
────────────────────────────────────────────────────────────────────────────────
 1. JODI-Oil World Database ......... mensuel, 118 pays, 2002-01 → présent
    https://www.jodidata.org/_resources/files/downloads/oil-data/world_primary_csv.zip
    Déclarations OFFICIELLES des gouvernements (Joint Organisations Data
    Initiative : APEC, Eurostat, IEA, OLADE, OPEC, UNSD, GECF).
    Flux retenus : INDPROD (production), TOTIMPSB (imports), TOTEXPSB (exports),
    CLOSTLV (stocks de clôture), REFINOBS (intrants raffinerie).
    Unités : KBD = milliers de barils/jour · KBBL = milliers de barils.

 2. JODI-Gas World Database ......... mensuel, 94 pays, 2009-01 → présent
    https://www.jodidata.org/jodi-publisher/gas/24/GAS_world_NewFormat.zip
    ⚠ Ce chemin est servi par un composant Vue (`<file-list type="gas">`), il
    n'apparaît PAS dans le HTML brut de la page de téléchargement. Les anciens
    chemins `/_resources/.../gas-data/jodi_gas_csv*.zip` existent encore mais
    sont FIGÉS EN 2018 — ne jamais y revenir.
    Flux : INDPROD, IMPPIP/IMPLNG (pipeline vs GNL), EXPPIP/EXPLNG, TOTIMPSB,
    TOTEXPSB, CLOSTLV, TOTDEMC. Unité M3 = MILLIONS de m³.

 3. OWID / Energy Institute ......... annuel, ~250 entités, 1900 → 2024
    ourworldindata.org/grapher/{oil,gas}-production-by-country + dataset complet
    = la profondeur historique (le siècle), en TWh.

 4. EIA International .............. annuel, 254 entités, 1980 → 2025
    Production / consommation / imports de pétrole. C'est la SEULE source qui
    estime les pays qui ne déclarent pas (Iran, Venezuela, Russie…).
    + Stocks OCDE mensuels (42 entités) + SPR hebdo 1982+ + stockage gaz US hebdo.
    Clé gratuite obligatoire : DEMO_KEY renvoie OVER_RATE_LIMIT.

 5. Eurostat ....................... mensuel, 37 pays, 2013-01 → présent
    nrg_stk_oilm  = niveaux de stocks pétroliers, AVEC la catégorie
                    « réserves d'urgence UE » (STKCL_EUE) et « territoire
                    national » (STKCL_NAT) → les vraies réserves stratégiques UE.
    nrg_ti_oilm   = imports de pétrole PAR PAYS PARTENAIRE (174 partenaires).
    nrg_ti_gasm   = imports de gaz par partenaire.

 6. CIA World Factbook ............. millésime unique, ~230 pays, domaine public
    Réserves prouvées de brut ET de gaz (la seule couverture large récente).

 7. FRED (Fed de Saint-Louis) ...... quotidien, Brent depuis 1987, WTI depuis 1986
    DCOILBRENTEU · DCOILWTICO · DHHNGSP · DGASNYH · DDFUELNYH.
    ⚠ Yahoo est INUTILISABLE côté serveur ici : urllib se fait renvoyer 429
    systématiquement (il faudrait `curl_cffi` avec empreinte Chrome, absent de
    l'anaconda qui exécute les launchd). Ce n'est pas une perte — FRED donne des
    décennies là où Yahoo donne 2 ans. Le TICK LIVE vient du navigateur, via le
    proxy maison `/live/quotes` (CORS *), y compris pour le TTF que FRED n'a pas.

 8. GIE AGSI+ / ALSI ............... QUOTIDIEN, ~25 pays, 2011-01-01 → présent
    agsi.gie.eu  = stockage souterrain de gaz : % de remplissage, stock (TWh),
                   capacité, injection/soutirage, couverture de consommation.
    alsi.gie.eu  = terminaux GNL : inventaire, envois vers le réseau (GWh/j),
                   capacité utilisée. C'est le baromètre de la crise gazière.
    Clé gratuite, transmise par EN-TÊTE `x-key` (jamais dans l'URL, donc jamais
    dans un log). Historique récupéré une fois puis mis à jour en INCRÉMENTAL.

────────────────────────────────────────────────────────────────────────────────
TROUS ASSUMÉS (surfacés dans l'UI, jamais comblés par une estimation maison)
────────────────────────────────────────────────────────────────────────────────
  - La Chine ne publie AUCUN niveau de stock (JODI CLOSTLV vide pour CN). Ses
    réserves stratégiques n'existent qu'en estimations privées payantes.
  - L'Iran déclare à JODI historiquement mais RIEN en 2026 sur production et
    exports → on bascule sur l'estimation EIA, en le disant.
  - La série longue de réserves prouvées s'arrête en 2020 (l'Energy Institute a
    cessé de la publier) ; au-delà = millésime Factbook.

Sortie : hydro_cache.json + hydro_cache.js (window.__HYDRO__)
"""
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

REPO = os.path.expanduser("~/Desktop/Site_Crypto_Finance")
CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
APPSUP = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# PIÈGE DUAL-DIRS (déjà vécu sur fetch_atlas_econ.py) : sous launchd le script
# tourne depuis App Support, où les chemins relatifs au dépôt ne résolvent pas,
# et l'accès à ~/Desktop est soumis à TCC. Toute ressource lue passe donc par une
# résolution multi-chemins, et l'écriture PRIMAIRE va dans Library/Caches
# (snapshot_site.sh propage ensuite vers le dépôt et public/).
def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


META_PATH = _first_existing(
    os.environ.get("ATLAS_META_PATH"),
    os.path.join(REPO, "assets/js/atlas/atlas_countries_meta.js"),
    os.path.join(SCRIPT_DIR, "assets/js/atlas/atlas_countries_meta.js"),
    os.path.join(APPSUP, "atlas_countries_meta.js"),
    os.path.join(CACHE_DIR, "atlas_countries_meta.js"),
) or os.path.join(REPO, "assets/js/atlas/atlas_countries_meta.js")
# Cache de travail : les gros CSV JODI sont conservés entre deux runs pour ne pas
# retélécharger 23 Mo + 1,7 Mo quand une seule composante a échoué.
WORK_DIR = os.path.join(CACHE_DIR, "hydro_work")

OUT_BASENAME = "hydro_cache"
OUT_DIRS = [CACHE_DIR, REPO, os.path.join(REPO, "public")]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

JODI_OIL_URL = ("https://www.jodidata.org/_resources/files/downloads/oil-data/"
                "world_primary_csv.zip")
JODI_GAS_URL = "https://www.jodidata.org/jodi-publisher/gas/24/GAS_world_NewFormat.zip"
OWID_ENERGY_URL = ("https://raw.githubusercontent.com/owid/energy-data/master/"
                   "owid-energy-data.csv")
OWID_OILRES_URL = ("https://ourworldindata.org/grapher/oil-proved-reserves.csv"
                   "?useColumnShortNames=true")
EIA_BASE = "https://api.eia.gov/v2"
EUROSTAT = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
            "{ds}?format=JSON&{q}")
FACTBOOK_TREE = ("https://api.github.com/repos/factbook/factbook.json/git/trees/"
                 "master?recursive=1")
FACTBOOK_RAW = "https://raw.githubusercontent.com/factbook/factbook.json/master/{path}"

# Flux JODI retenus → clé courte du cache.
OIL_FLOWS = {"INDPROD": "prod", "TOTIMPSB": "imp", "TOTEXPSB": "exp",
             "CLOSTLV": "stk", "REFINOBS": "ref"}
GAS_FLOWS = {"INDPROD": "prod", "IMPPIP": "imp_pip", "IMPLNG": "imp_lng",
             "EXPPIP": "exp_pip", "EXPLNG": "exp_lng", "TOTIMPSB": "imp",
             "TOTEXPSB": "exp", "CLOSTLV": "stk", "TOTDEMC": "dem"}
# Un stock est un NIVEAU (kbbl), un flux est un DÉBIT (kb/j) : unités distinctes.
OIL_UNIT = {"prod": "KBD", "imp": "KBD", "exp": "KBD", "ref": "KBD", "stk": "KBBL"}

PRICE_TICKERS = [
    ("brent", "BZ=F", "Brent", "USD/bbl"),
    ("wti", "CL=F", "WTI", "USD/bbl"),
    ("ttf", "TTF=F", "TTF Pays-Bas", "EUR/MWh"),
    ("hh", "NG=F", "Henry Hub", "USD/MMBtu"),
    ("rbob", "RB=F", "Essence RBOB", "USD/gal"),
    ("gasoil", "HO=F", "Gazole/fioul", "USD/gal"),
    ("coal", "MTF=F", "Charbon API2", "USD/t"),
]

VERBOSE = "--quiet" not in sys.argv


def log(msg):
    if VERBOSE:
        print(f"[hydro] {msg}", file=sys.stderr, flush=True)


# ── réseau ───────────────────────────────────────────────────────────────────

def http_bytes(url, timeout=180, tries=3, headers=None):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(2 + 4 * i)
    log(f"ÉCHEC {url[:96]} : {last}")
    return None


def http_json(url, timeout=90, tries=3, headers=None):
    b = http_bytes(url, timeout=timeout, tries=tries, headers=headers)
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception as e:
        log(f"JSON illisible {url[:80]} : {e}")
        return None


def eia_key():
    """env EIA_API_KEY → ~/.eia_api_key → DEMO_KEY (qui renvoie OVER_RATE_LIMIT)."""
    k = os.environ.get("EIA_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.eia_api_key")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                k = f.read().strip()
            if k:
                return k
        except Exception:
            pass
    log("AUCUNE clé EIA → DEMO_KEY (rate-limited). Déposer une ligne dans "
        "~/.eia_api_key (gratuit : eia.gov/opendata/register.php)")
    return "DEMO_KEY"


EIA_KEY = eia_key()


def eia(route, params, timeout=120):
    """Appel EIA v2. ⚠ Les crochets des params EIA (data[0], facets[x][]) sont
    envoyés tels quels : c'est urllib, pas curl (curl exige --globoff)."""
    q = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" if not isinstance(v, list)
                 else "&".join(f"{k}={urllib.parse.quote(str(x), safe='')}" for x in v)
                 for k, v in params)
    url = f"{EIA_BASE}/{route}?api_key={EIA_KEY}&{q}"
    d = http_json(url, timeout=timeout)
    if not d:
        return []
    if "error" in d:
        log(f"EIA erreur {route} : {str(d['error'])[:120]}")
        return []
    return d.get("response", {}).get("data", []) or []


def num(v):
    """OBS_VALUE JODI/EIA : '', '.', '-', 'w' (withheld), 'NA' → None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in (".", "-", "--", "NA", "n.a.", "w", "W", ":"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── méta pays (ISO2 → ISO3, noms FR) ─────────────────────────────────────────

def load_meta():
    with open(META_PATH, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"window\.__ATLAS_META__\s*=\s*", src)
    if not m:
        sys.stderr.write(f"[fatal] __ATLAS_META__ introuvable dans {META_PATH}\n")
        sys.exit(2)
    meta = json.loads(src[m.end():].strip().rstrip(";"))
    countries = meta["countries"]
    a2 = {}
    for a3, c in countries.items():
        if c.get("a2"):
            a2[c["a2"]] = a3
    return countries, a2


# JODI/Eurostat utilisent quelques codes non-ISO ou historiques.
A2_EXTRA = {
    "EL": "GRC",     # Eurostat : Grèce
    "UK": "GBR",     # Eurostat : Royaume-Uni
    "XK": "XKX",     # Kosovo
    "EU": None, "EU27_2020": None, "EU28": None, "EA19": None, "EA20": None,
    "OECD": None, "WORLD": None, "TOTAL": None,
}


def to_a3(code, a2map):
    if code in A2_EXTRA:
        return A2_EXTRA[code]
    if len(code) == 3 and code.isalpha() and code.upper() == code:
        return code                      # déjà ISO3 (EIA)
    return a2map.get(code)


# ── séries packées {s: index de départ, v: [...]} ────────────────────────────

def month_index(months):
    return {m: i for i, m in enumerate(months)}


def pack(idx_map, series, rnd=3):
    """{index: valeur} → {'s': premier index, 'v': [...]}. Trous = null.
    Bords trimmés. Une série < 2 points utiles est jetée (bruit)."""
    if not series:
        return None
    ks = [k for k, v in series.items() if v is not None]
    if len(ks) < 2:
        return None
    lo, hi = min(ks), max(ks)
    v = []
    for i in range(lo, hi + 1):
        x = series.get(i)
        v.append(None if x is None else round(x, rnd))
    return {"s": lo, "v": v}


def last_val(packed, months=None):
    """Dernière valeur non nulle d'une série packée → (valeur, période)."""
    if not packed:
        return None, None
    v = packed["v"]
    for j in range(len(v) - 1, -1, -1):
        if v[j] is not None:
            i = packed["s"] + j
            return v[j], (months[i] if months and i < len(months) else i)
    return None, None


def trailing_mean(packed, n=12):
    """Moyenne des n derniers points non nuls (lisse la saisonnalité et les
    trous de déclaration ; un mois isolé n'est PAS représentatif)."""
    if not packed:
        return None
    vals = [x for x in packed["v"] if x is not None]
    if not vals:
        return None
    tail = vals[-n:]
    return sum(tail) / len(tail)


# ── 1/2. JODI ────────────────────────────────────────────────────────────────

def jodi_download(url, name):
    """Télécharge le zip JODI et renvoie le contenu CSV décompressé (bytes).
    Conserve une copie dans WORK_DIR : si le téléchargement échoue au run
    suivant, on repart de la copie plutôt que de perdre tout le bloc."""
    os.makedirs(WORK_DIR, exist_ok=True)
    keep = os.path.join(WORK_DIR, f"{name}.csv")
    raw = http_bytes(url, timeout=600, tries=2)
    if raw:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                inner = [n for n in z.namelist() if n.lower().endswith(".csv")]
                if inner:
                    data = z.read(inner[0])
                    with open(keep, "wb") as f:
                        f.write(data)
                    log(f"JODI {name} : {inner[0]} ({len(data)/1e6:.0f} Mo)")
                    return data
        except Exception as e:
            log(f"JODI {name} zip illisible : {e}")
    if os.path.exists(keep):
        age = (time.time() - os.path.getmtime(keep)) / 86400
        log(f"JODI {name} : téléchargement KO → copie locale ({age:.1f} j)")
        with open(keep, "rb") as f:
            return f.read()
    return None


def parse_jodi(data, flows, unit_of, a2map, product_filter):
    """CSV JODI → {a3: {clé: {index_mois: val}}}, months triés.

    Le CSV pétrole fait 283 Mo / 6,9 M lignes : parcours en flux, aucune
    structure intermédiaire par ligne.
    """
    if not data:
        return {}, []
    txt = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8-sig", newline="")
    rd = csv.reader(txt)
    head = next(rd, None)
    if not head:
        return {}, []
    col = {c.strip().upper(): i for i, c in enumerate(head)}
    need = ("REF_AREA", "TIME_PERIOD", "ENERGY_PRODUCT", "FLOW_BREAKDOWN",
            "UNIT_MEASURE", "OBS_VALUE")
    if any(c not in col for c in need):
        log(f"JODI : colonnes inattendues {head}")
        return {}, []
    iA, iT, iP, iF, iU, iV = (col[c] for c in need)

    raw = {}
    periods = set()
    for row in rd:
        try:
            if row[iP] not in product_filter:
                continue
            fk = flows.get(row[iF])
            if not fk:
                continue
            if row[iU] != unit_of(fk):
                continue
            x = num(row[iV])
            if x is None:
                continue
            a3 = to_a3(row[iA], a2map)
            if not a3:
                continue
            per = row[iT]
            periods.add(per)
            raw.setdefault(a3, {}).setdefault(fk, {})[per] = x
        except IndexError:
            continue

    months = sorted(periods)
    mi = month_index(months)
    out = {}
    for a3, byk in raw.items():
        d = {}
        for k, permap in byk.items():
            packed = pack(mi, {mi[p]: v for p, v in permap.items() if p in mi})
            if packed:
                d[k] = packed
        if d:
            out[a3] = d
    return out, months


def fetch_jodi_oil(a2map):
    data = jodi_download(JODI_OIL_URL, "jodi_oil_primary")
    out, months = parse_jodi(data, OIL_FLOWS, lambda k: OIL_UNIT[k], a2map,
                             product_filter={"CRUDEOIL"})
    log(f"JODI-Oil : {len(out)} pays · {len(months)} mois "
        f"({months[0] if months else '?'} → {months[-1] if months else '?'})")
    return out, months


def fetch_jodi_gas(a2map):
    # Gaz : tout en M3 (millions de m³) pour rester homogène ; TJ ignoré.
    data = jodi_download(JODI_GAS_URL, "jodi_gas")
    out, months = parse_jodi(data, GAS_FLOWS, lambda k: "M3", a2map,
                             product_filter={"NATGAS"})
    log(f"JODI-Gas : {len(out)} pays · {len(months)} mois "
        f"({months[0] if months else '?'} → {months[-1] if months else '?'})")
    return out, months


# ── 3. OWID (le siècle) ──────────────────────────────────────────────────────

OWID_COLS = {
    "oil_prod": "oil_production", "oil_cons": "oil_consumption",
    "gas_prod": "gas_production", "gas_cons": "gas_consumption",
    "oil_share": "oil_share_energy", "gas_share": "gas_share_energy",
    "oil_pc": "oil_energy_per_capita", "gas_pc": "gas_energy_per_capita",
    "prim": "primary_energy_consumption",
}


def fetch_owid(known_a3):
    b = http_bytes(OWID_ENERGY_URL, timeout=300)
    if not b:
        return {}, []
    rd = csv.DictReader(io.TextIOWrapper(io.BytesIO(b), encoding="utf-8"))
    if not rd.fieldnames:
        return {}, []
    raw, years = {}, set()
    for r in rd:
        a3 = (r.get("iso_code") or "").strip()
        if not a3 or len(a3) != 3:
            continue
        try:
            y = int(r["year"])
        except (KeyError, ValueError, TypeError):
            continue
        for k, col in OWID_COLS.items():
            x = num(r.get(col))
            if x is None:
                continue
            raw.setdefault(a3, {}).setdefault(k, {})[y] = x
            years.add(y)
    if not years:
        return {}, []
    y0, y1 = min(years), max(years)
    out = {}
    for a3, byk in raw.items():
        d = {}
        for k, ymap in byk.items():
            packed = pack(None, {y - y0: v for y, v in ymap.items()}, rnd=2)
            if packed:
                packed["s"] += y0            # index absolu = année
                d[k] = packed
        if d:
            out[a3] = d
    log(f"OWID énergie : {len(out)} pays · {y0} → {y1}")
    return out, [y0, y1]


def fetch_owid_oil_reserves():
    b = http_bytes(OWID_OILRES_URL, timeout=120)
    if not b:
        return {}
    rd = csv.DictReader(io.StringIO(b.decode("utf-8", "replace")))
    col = next((c for c in (rd.fieldnames or []) if "reserv" in c.lower()), None)
    if not col:
        return {}
    raw, years = {}, set()
    for r in rd:
        a3 = (r.get("code") or "").strip()
        x = num(r.get(col))
        if not a3 or len(a3) != 3 or x is None:
            continue
        try:
            y = int(r["year"])
        except (KeyError, ValueError):
            continue
        raw.setdefault(a3, {})[y] = x
        years.add(y)
    out = {}
    for a3, ymap in raw.items():
        y0 = min(ymap)
        packed = pack(None, {y - y0: v for y, v in ymap.items()}, rnd=2)
        if packed:
            packed["s"] += y0
            out[a3] = packed
    log(f"OWID réserves pétrole : {len(out)} pays · "
        f"{min(years) if years else '?'} → {max(years) if years else '?'}")
    return out


# ── 4. EIA ───────────────────────────────────────────────────────────────────

def eia_intl(product, activity, unit, freq="annual", start=None, end=None):
    """international/data : renvoie {a3: {année: valeur}} (+ agrégats World, OPEC…)."""
    p = [("frequency", freq), ("data[0]", "value"),
         ("facets[productId][]", product), ("facets[activityId][]", activity),
         ("facets[unit][]", unit), ("length", 5000)]
    if start:
        p.append(("start", start))
    if end:
        p.append(("end", end))
    rows = eia("international/data/", p)
    out = {}
    for r in rows:
        x = num(r.get("value"))
        if x is None:
            continue
        cid = r.get("countryRegionId")
        if not cid:
            continue
        out.setdefault(cid, {})[r["period"]] = x
    return out


def fetch_eia_intl():
    """Production / consommation / imports de pétrole, annuel 1980+, 254 entités.
    C'est la seule source qui ESTIME les non-déclarants (Iran, Venezuela…)."""
    out, agg = {}, {}
    # ⚠ activityId=3 (« Imports ») de cette route sert surtout l'ÉLECTRICITÉ et
    # renvoie 0 ligne en TBPD : les imports/exports de pétrole viennent de JODI
    # (mensuel, 96 déclarants) — plus fin et plus frais. Ici : production et
    # consommation seulement, mais pour TOUS les pays, y compris les non-déclarants.
    spec = [("prod", "57", "1", "TBPD"),     # brut + condensat de puits
            ("cons", "5", "2", "TBPD")]      # pétrole et autres liquides
    got = {}
    for key, prod, act, unit in spec:
        d = eia_intl(prod, act, unit, freq="annual", start="1980", end="2026")
        got[key] = d
        log(f"EIA international {key} : {len(d)} entités")
        time.sleep(0.4)
    for key, d in got.items():
        for cid, ymap in d.items():
            years = {int(y): v for y, v in ymap.items() if y.isdigit()}
            if not years:
                continue
            y0 = min(years)
            packed = pack(None, {y - y0: v for y, v in years.items()}, rnd=2)
            if not packed:
                continue
            packed["s"] += y0
            tgt = out if (len(cid) == 3 and cid.isalpha()) else agg
            tgt.setdefault(cid, {})[key] = packed
    return out, agg


def fetch_eia_oecd_stocks():
    """Stocks totaux de pétrole des pays de l'OCDE, mensuel, en millions de barils.
    42 entités (dont agrégats OECD/OEEU et entités historiques DDR/CSK)."""
    # PIÈGE VÉCU (deux fois) : la pagination par `offset` de l'EIA ne fonctionne
    # PAS ici — même avec un `sort` explicite, on ne récupérait que 15 entités sur
    # 42, les pages se recouvrant. FIX : découper par TRANCHES DE TEMPS de 4 ans
    # (≈ 42 entités × 48 mois ≈ 2 000 lignes, bien sous le plafond de 5 000), ce
    # qui rend la collecte déterministe et complète, sans dépendre de l'offset.
    allrows, seen = [], set()
    for y0 in range(1980, 2028, 4):
        rows = eia("international/data/", [
            ("frequency", "monthly"), ("data[0]", "value"),
            ("facets[activityId][]", "5"), ("facets[unit][]", "MBBL"),
            ("start", f"{y0}-01"), ("end", f"{min(y0 + 3, 2027)}-12"),
            ("length", 5000),
        ])
        for r in rows:
            k = (r.get("countryRegionId"), r.get("period"), r.get("productId"))
            if k in seen:
                continue
            seen.add(k)
            allrows.append(r)
        if len(rows) >= 5000:
            log(f"  ⚠ tranche {y0} saturée à 5000 lignes — réduire la fenêtre")
        time.sleep(0.25)
    raw, periods = {}, set()
    for r in allrows:
        x = num(r.get("value"))
        cid = r.get("countryRegionId")
        if x is None or not cid:
            continue
        raw.setdefault(cid, {})[r["period"]] = x
        periods.add(r["period"])
    months = sorted(periods)
    mi = month_index(months)
    out = {}
    for cid, permap in raw.items():
        packed = pack(mi, {mi[p]: v for p, v in permap.items() if p in mi}, rnd=2)
        if packed:
            out[cid] = packed
    log(f"EIA stocks OCDE : {len(out)} entités · {len(months)} mois "
        f"({months[0] if months else '?'} → {months[-1] if months else '?'})")
    return out, months


def fetch_spr():
    """SPR américain, hebdomadaire depuis 1982 (WCSSTUS1, milliers de barils)."""
    rows = eia("petroleum/stoc/wstk/data/", [
        ("frequency", "weekly"), ("data[0]", "value"),
        ("facets[series][]", "WCSSTUS1"),
        ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
        ("length", 5000),
    ])
    pts = sorted(((r["period"], num(r.get("value"))) for r in rows if num(r.get("value")) is not None))
    if not pts:
        return None
    # Stock commercial de brut (hors SPR) pour le contexte : WCESTUS1.
    com = eia("petroleum/stoc/wstk/data/", [
        ("frequency", "weekly"), ("data[0]", "value"),
        ("facets[series][]", "WCESTUS1"),
        ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
        ("length", 5000),
    ])
    cmap = {r["period"]: num(r.get("value")) for r in com if num(r.get("value")) is not None}
    log(f"SPR : {len(pts)} semaines · {pts[0][0]} → {pts[-1][0]} · "
        f"{pts[-1][1]/1000:.1f} Mbbl")
    return {
        "dates": [p for p, _ in pts],
        "mbbl": [round(v / 1000, 2) for _, v in pts],
        "commercial": [round(cmap[p] / 1000, 2) if p in cmap else None for p, _ in pts],
        "unit": "millions de barils",
        "src": "EIA WCSSTUS1 (SPR) + WCESTUS1 (commercial)",
        "audit": "https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=pet&s=wcsstus1&f=w",
    }


def fetch_us_gas_storage():
    """Stockage souterrain de gaz US, hebdomadaire, en Bcf (total Lower 48)."""
    rows = eia("natural-gas/stor/wkly/data/", [
        ("frequency", "weekly"), ("data[0]", "value"),
        ("facets[series][]", "NW2_EPG0_SWO_R48_BCF"),
        ("sort[0][column]", "period"), ("sort[0][direction]", "asc"),
        ("length", 5000),
    ])
    pts = sorted(((r["period"], num(r.get("value"))) for r in rows
                  if num(r.get("value")) is not None))
    if not pts:
        log("stockage gaz US : série NW2_EPG0_SWO_R48_BCF vide")
        return None
    log(f"Stockage gaz US : {len(pts)} semaines · {pts[0][0]} → {pts[-1][0]} · "
        f"{pts[-1][1]:.0f} Bcf")
    return {"dates": [p for p, _ in pts], "bcf": [round(v, 1) for _, v in pts],
            "unit": "Bcf", "src": "EIA NW2_EPG0_SWO_R48_BCF (Lower 48)",
            "audit": "https://ir.eia.gov/ngs/ngs.html"}


# ── 5. Eurostat ──────────────────────────────────────────────────────────────

def eurostat(ds, query, timeout=180):
    return http_json(EUROSTAT.format(ds=ds, q=query), timeout=timeout)


def es_unpack(js, dims_keep):
    """JSON-stat Eurostat → [(clé_tuple, période, valeur)]. Décode l'index
    linéaire en coordonnées (l'ordre des dimensions fait foi)."""
    if not js or "value" not in js:
        return []
    dim = js.get("dimension", {})
    order = js.get("id") or list(dim.keys())
    sizes = js.get("size") or [len(dim[d]["category"]["index"]) for d in order]
    cats = []
    for d in order:
        idx = dim[d]["category"]["index"]
        inv = [None] * len(idx)
        for code, i in idx.items():
            inv[i] = code
        cats.append(inv)
    out = []
    for flat, val in js["value"].items():
        if val is None:
            continue
        i = int(flat)
        coord = {}
        for k in range(len(sizes) - 1, -1, -1):
            coord[order[k]] = cats[k][i % sizes[k]]
            i //= sizes[k]
        key = tuple(coord.get(d) for d in dims_keep)
        out.append((key, coord.get("time"), float(val)))
    return out


def fetch_eu_oil_stocks(a2map):
    """nrg_stk_oilm : stocks pétroliers mensuels UE.
    STKCL_EUE = réserves d'urgence UE · STKCL_NAT = territoire national ·
    STK_CL = stock de clôture total. Produit : brut+NGL+feedstocks."""
    got = {}
    for flow, key in (("STK_CL", "total"), ("STKCL_EUE", "urgence"),
                      ("STKCL_NAT", "national")):
        js = eurostat("nrg_stk_oilm",
                      f"siec=O4000&unit=THS_T&stk_flow={flow}&sinceTimePeriod=2013-01")
        rows = es_unpack(js, ("geo",))
        if not rows:
            log(f"Eurostat stocks {flow} : vide")
            continue
        for (geo,), per, val in rows:
            a3 = to_a3(geo, a2map)
            if not a3 or not per:
                continue
            got.setdefault(a3, {}).setdefault(key, {})[per] = val
        time.sleep(0.4)
    periods = sorted({p for d in got.values() for s in d.values() for p in s})
    mi = month_index(periods)
    out = {}
    for a3, byk in got.items():
        d = {}
        for k, permap in byk.items():
            packed = pack(mi, {mi[p]: v for p, v in permap.items() if p in mi}, rnd=1)
            if packed:
                d[k] = packed
        if d:
            out[a3] = d
    log(f"Eurostat stocks pétroliers : {len(out)} pays · {len(periods)} mois "
        f"({periods[0] if periods else '?'} → {periods[-1] if periods else '?'})")
    return out, periods


def fetch_eu_partners(a2map, ds, siec, label, unit, unit_lab):
    """nrg_ti_oilm / nrg_ti_gasm : imports par pays partenaire, 12 derniers mois.
    Répond à « qui achète à qui » pour les pays de l'UE.

    ⚠ PIÈGE D'UNITÉ VÉCU : `nrg_ti_oilm` n'a QU'UNE unité (THS_T), mais
    `nrg_ti_gasm` en a DEUX — `MIO_M3` (millions de m³) et `TJ_GCV` (térajoules).
    Sans filtre `unit=`, l'API renvoie les deux et la somme additionnait des mètres
    cubes avec des joules : la France ressortait avec 819 « Mt » de gaz algérien
    alors qu'elle importe ~35 bcm en tout. Le filtre d'unité est OBLIGATOIRE.
    """
    js = eurostat(ds, f"siec={siec}&unit={unit}&lastTimePeriod=13")
    rows = es_unpack(js, ("geo", "partner"))
    if not rows:
        log(f"Eurostat {ds} : vide")
        return {}
    agg = {}
    periods = set()
    for (geo, partner), per, val in rows:
        a3 = to_a3(geo, a2map)
        pa3 = to_a3(partner, a2map)
        if not a3 or not per:
            continue
        periods.add(per)
        # partner peut être un agrégat (EU27, TOTAL) → on garde le libellé brut.
        agg.setdefault(a3, {}).setdefault(pa3 or partner, {})[per] = val
    out = {}
    for a3, byp in agg.items():
        tot = {}
        for p, permap in byp.items():
            s = sum(permap.values())
            if s > 0:
                tot[p] = round(s, 1)
        if tot:
            out[a3] = dict(sorted(tot.items(), key=lambda kv: -kv[1])[:18])
    log(f"Eurostat {label} par partenaire : {len(out)} pays · "
        f"{min(periods) if periods else '?'} → {max(periods) if periods else '?'}")
    return {"by_country": out, "months": sorted(periods), "unit": unit_lab,
            "unit_code": unit, "ds": ds}


# ── 6. CIA World Factbook (réserves prouvées) ────────────────────────────────

RES_PATTERNS = [
    (r"([\d.,]+)\s*(?:million|billion|trillion)?\s*(?:bbl|barrels)", None),
]


def parse_qty(text):
    """« 208.6 billion barrels (2021 est.) » → (208.6e9, 2021).
    « 265.088 billion cubic meters (2023 est.) » → (265.088e9, 2023)."""
    if not text:
        return None, None
    t = text.replace(",", "")
    m = re.search(r"([\d.]+)\s*(thousand|million|billion|trillion)?", t)
    if not m:
        return None, None
    try:
        v = float(m.group(1))
    except ValueError:
        return None, None
    mult = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
    v *= mult.get((m.group(2) or "").lower(), 1.0)
    y = re.search(r"\((\d{4})", t)
    return v, (int(y.group(1)) if y else None)


# GEC/FIPS qui diffèrent de la table geonames (repris du fetcher de l'Atlas).
GEC_ISO_EXTRA = {
    "vm": "VNM", "gm": "DEU", "uk": "GBR", "ee": "EST", "ei": "IRL", "sw": "SWE",
    "sz": "CHE", "au": "AUT", "as": "AUS", "ja": "JPN", "ks": "KOR", "sn": "SGP",
    "ni": "NGA", "cg": "COD", "cf": "CAF", "iz": "IRQ", "ir": "IRN", "rs": "RUS",
    "up": "UKR", "da": "DNK", "bo": "BLR", "lo": "SVK", "si": "SVN", "ez": "CZE",
    "hr": "HRV", "gg": "GEO", "am": "ARM", "aj": "AZE", "kz": "KAZ", "kg": "KGZ",
    "ti": "TJK", "tx": "TKM", "uz": "UZB", "sf": "ZAF", "wa": "NAM", "wz": "SWZ",
    "bc": "BWA", "mi": "MWI", "za": "ZMB", "zi": "ZWE", "mz": "MOZ", "ke": "KEN",
    "tz": "TZA", "ug": "UGA", "et": "ETH", "su": "SDN", "od": "SSD", "tu": "TUR",
    "sa": "SAU", "ae": "ARE", "mu": "OMN", "ku": "KWT", "qa": "QAT", "ba": "BHR",
    "ve": "VEN", "ca": "CAN", "ch": "CHN", "in": "IDN", "no": "NOR", "br": "BRA",
}


def load_fips2iso3():
    """FIPS/GEC (minuscule) → **ISO3**, depuis geonames countryInfo.txt.

    Le nom des fichiers factbook.json EST le code GEC (≈ FIPS 10-4), qui diffère
    souvent de l'ISO (Allemagne = gm, Iran = ir, Arabie saoudite = sa…) → un
    appariement par NOM de pays ne matchait que 21 fiches sur 261 (mesuré).

    ⚠ PIÈGE DE COLONNE (vécu) : countryInfo.txt est
    `ISO2 \t ISO3 \t ISO-Numeric \t fips \t Country …`, donc la colonne 1 est
    l'**ISO3**, pas l'ISO2. Traiter c[1] comme un ISO2 puis le chercher dans la
    table ISO2→ISO3 échouait pour presque tout : 201 codes GEC non mappés et
    seulement 48 pays de réserves au lieu de ~200.
    """
    b = http_bytes("https://download.geonames.org/export/dump/countryInfo.txt",
                   timeout=90)
    out = {}
    if not b:
        log("geonames countryInfo KO → seule la table d'exceptions servira")
        return out
    for line in b.decode("utf-8", "replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        c = line.split("\t")
        if len(c) > 3 and c[1] and c[3]:
            out[c[3].strip().lower()] = c[1].strip()      # fips → ISO3
    log(f"geonames : {len(out)} codes FIPS → ISO3")
    return out


def fetch_factbook(countries, a2map):
    """Réserves prouvées de brut ET de gaz, ~230 pays, domaine public."""
    tree = http_json(FACTBOOK_TREE, timeout=90)
    if not tree:
        return {}
    paths = [t["path"] for t in tree.get("tree", [])
             if t.get("path", "").endswith(".json") and "/" in t["path"]
             and not t["path"].startswith(("_", "."))]
    fips2 = load_fips2iso3()
    out = {}
    fetched = 0
    unmapped = []
    for p in paths:
        gec = os.path.basename(p)[:-5].lower()
        if len(gec) != 2:
            continue
        # fips2 donne déjà un ISO3 ; le GEC peut aussi ÊTRE un ISO2 valide (fr, it…).
        a3 = (GEC_ISO_EXTRA.get(gec) or fips2.get(gec)
              or a2map.get(gec.upper()))
        if not a3 or a3 not in countries:
            unmapped.append(gec)
            continue
        d = http_json(FACTBOOK_RAW.format(path=p), timeout=45, tries=2)
        fetched += 1
        if not d:
            continue
        en = d.get("Energy", {})
        pet = en.get("Petroleum", {}) or {}
        gas = en.get("Natural gas", {}) or {}
        # ⚠ Les intitulés Factbook diffèrent entre pétrole et gaz, et ne sont PAS
        # « reserves » pour le gaz : c'est « proven reserves ». Vérifié sur la fiche
        # Iran — avec la mauvaise clé, AUCUNE réserve gazière n'était captée.
        def _txt(d_, *keys):
            for k in keys:
                v = d_.get(k)
                if isinstance(v, dict) and v.get("text"):
                    return v["text"]
            return None

        oil_v, oil_y = parse_qty(_txt(pet, "crude oil estimated reserves",
                                      "crude oil proved reserves", "proven reserves"))
        gas_v, gas_y = parse_qty(_txt(gas, "proven reserves", "reserves",
                                      "estimated reserves"))
        rec = {}
        if oil_v:
            rec["oil_bbl"] = oil_v
            rec["oil_yr"] = oil_y
        if gas_v:
            rec["gas_m3"] = gas_v
            rec["gas_yr"] = gas_y
        if rec:
            rec["gec"] = gec
            out[a3] = rec
        if fetched % 40 == 0:
            time.sleep(0.5)
    log(f"Factbook réserves : {len(out)} pays (sur {fetched} fiches lues, "
        f"{len(unmapped)} codes GEC non mappés)")
    if len(out) < 120:
        log(f"  ⚠ couverture faible — GEC non mappés : {sorted(unmapped)[:30]}")
    return out


# ── 8. GIE AGSI+ (stockage gaz) & ALSI (terminaux GNL) ───────────────────────

GIE_COUNTRIES = ["AT", "BE", "BG", "HR", "CZ", "DK", "FR", "DE", "HU", "IT",
                 "LV", "NL", "PL", "PT", "RO", "SK", "ES", "SE", "UA", "GB"]
GIE_AGG = ["eu", "ne"]          # ne = Europe non-UE incluse (agrégat GIE)


def gie_key():
    k = os.environ.get("GIE_API_KEY", "").strip()
    if k:
        return k
    p = os.path.expanduser("~/.gie_api_key")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


GIE_KEY = gie_key()


def gie_fetch_window(host, country, frm, to):
    """Une fenêtre de jours gaziers. La clé passe par l'en-tête `x-key`.
    ⚠ L'API renvoie du non-JSON (HTML de rate-limit) si on tape trop vite →
    temporisation obligatoire entre appels."""
    url = (f"https://{host}.gie.eu/api?country={urllib.parse.quote(country)}"
           f"&from={frm}&to={to}&size=300")
    d = http_json(url, timeout=60, tries=3, headers={"x-key": GIE_KEY})
    return (d or {}).get("data") or []


def gie_series(host, country, fields, start="2011-01-01"):
    """Historique quotidien complet, mis en cache sur disque puis complété en
    incrémental (sinon 20 pays × 19 pages = 380 requêtes à chaque run)."""
    os.makedirs(WORK_DIR, exist_ok=True)
    store = os.path.join(WORK_DIR, f"gie_{host}_{country}.json")
    hist = {}
    if os.path.exists(store):
        try:
            with open(store, encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            hist = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frm = max(hist) if hist else start
    # Fenêtres d'un an : l'API plafonne à 300 lignes par réponse.
    y0 = int(frm[:4])
    y1 = int(today[:4])
    for y in range(y0, y1 + 1):
        a = max(frm, f"{y}-01-01")
        b = min(today, f"{y}-12-31")
        if a > b:
            continue
        # Une année = ~365 jours > 300 → on découpe en deux semestres.
        for aa, bb in ((a, min(b, f"{y}-06-30")), (max(a, f"{y}-07-01"), b)):
            if aa > bb:
                continue
            rows = gie_fetch_window(host, country, aa, bb)
            for r in rows:
                day = r.get("gasDayStart")
                if not day:
                    continue
                rec = {}
                for f_ in fields:
                    v = r.get(f_)
                    if isinstance(v, dict):
                        v = v.get("gwh")
                    x = num(v)
                    if x is not None:
                        rec[f_] = x
                if rec:
                    hist[day] = rec
            time.sleep(1.1)
    if hist:
        tmp = store + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(hist, f, separators=(",", ":"))
        os.replace(tmp, store)
    return hist


def _daily_pack(hist, fields, days_index, series_only=None):
    """Séries quotidiennes packées.

    BUDGET DE TAILLE : 22 entités × 6 champs × 5 689 jours ≈ 750 000 valeurs, soit
    ~4,5 Mo pour le seul stockage gaz — inacceptable pour un cache web. On ne garde
    en SÉRIE QUOTIDIENNE COMPLÈTE que les champs réellement tracés (`series_only`) ;
    les autres ne conservent que leur dernière valeur dans `now`. Aucune perte
    d'historique sur ce qui est affiché, et le fichier de travail sur disque garde
    de toute façon tous les champs si on en veut d'autres un jour.
    """
    out, now = {}, {}
    keep = set(series_only or fields)
    for f_ in fields:
        s = {days_index[d]: rec[f_] for d, rec in hist.items()
             if f_ in rec and d in days_index}
        if not s:
            continue
        if f_ in keep:
            p = pack(days_index, s, rnd=2)
            if p:
                out[f_] = p
        else:
            k = max(s)
            now[f_] = s[k]
    if now:
        out["now"] = now
    return out


def fetch_gie(a2map):
    """AGSI+ (stockage) + ALSI (GNL), quotidien 2011+."""
    if not GIE_KEY:
        log("AUCUNE clé GIE → stockage gaz quotidien indisponible "
            "(déposer une ligne dans ~/.gie_api_key, gratuit : agsi.gie.eu)")
        return None
    agsi_fields = ["full", "gasInStorage", "workingGasVolume",
                   "injection", "withdrawal", "consumptionFull"]
    # ALSI n'expose PAS de champ `full` (vérifié sur la fiche France) : le taux de
    # remplissage d'un terminal se déduit de inventory ÷ dtmi (capacité maximale
    # déclarée). On stocke les deux plutôt que d'inventer un pourcentage.
    alsi_fields = ["inventory", "sendOut", "dtmi"]
    stor, lng = {}, {}
    days = set()
    for c in GIE_COUNTRIES + GIE_AGG:
        h = gie_series("agsi", c, agsi_fields)
        if h:
            stor[c] = h
            days |= set(h)
            last = max(h)
            log(f"AGSI {c} : {len(h)} jours → {last} "
                f"({h[last].get('full', '?')} % plein)")
    for c in GIE_COUNTRIES + GIE_AGG:
        h = gie_series("alsi", c, alsi_fields)
        if h:
            lng[c] = h
            days |= set(h)
    if not days:
        return None
    all_days = sorted(days)
    di = {d: i for i, d in enumerate(all_days)}

    # AGRÉGAT EUROPÉEN — CALCULÉ, PAS OFFICIEL.
    # L'API AGSI n'expose AUCUN agrégat : `country=eu` renvoie 0 ligne (c'est une
    # vue du site web seulement, vérifié). On le dérive donc de façon transparente :
    #   remplissage = Σ(gaz en stock) ÷ Σ(capacité utile) × 100, sur les pays suivis
    # et l'UI l'étiquette « agrégat calculé » pour ne pas le faire passer pour un
    # chiffre publié par GIE.
    agg = {}
    for day in all_days:
        tot_s = tot_c = 0.0
        n = 0
        for c, h in stor.items():
            rec = h.get(day)
            if not rec:
                continue
            s, cap = rec.get("gasInStorage"), rec.get("workingGasVolume")
            if s is None or not cap:
                continue
            tot_s += s
            tot_c += cap
            n += 1
        if n >= 8 and tot_c > 0:          # seuil : pas d'agrégat sur 2 pays
            agg[day] = {"gasInStorage": round(tot_s, 2),
                        "workingGasVolume": round(tot_c, 2),
                        "full": round(100.0 * tot_s / tot_c, 2),
                        "n": n}
    if agg:
        log(f"agrégat stockage calculé : {len(agg)} jours · dernier {max(agg)} = "
            f"{agg[max(agg)]['full']} % sur {agg[max(agg)]['n']} pays")
        stor["EUCALC"] = agg
    out_stor, out_lng = {}, {}
    for c, h in stor.items():
        a3 = to_a3(c, a2map) if len(c) == 2 else c
        out_stor[a3 or c] = _daily_pack(h, agsi_fields, di,
                                        series_only=("full", "gasInStorage"))
    for c, h in lng.items():
        a3 = to_a3(c, a2map) if len(c) == 2 else c
        out_lng[a3 or c] = _daily_pack(h, alsi_fields, di,
                                       series_only=("sendOut", "inventory"))
    log(f"GIE : {len(out_stor)} entités stockage · {len(out_lng)} entités GNL · "
        f"{len(all_days)} jours ({all_days[0]} → {all_days[-1]})")
    return {"days": all_days, "storage": out_stor, "lng": out_lng,
            "src": "GIE AGSI+ / ALSI (déclarations des opérateurs de stockage)",
            "audit": "https://agsi.gie.eu/"}


# ── 7. prix live (référence bakée ; le front rafraîchit lui-même) ────────────

def fetch_prices():
    """Historique LONG des prix, via FRED (quotidien, décennies).

    ⚠ Yahoo n'est PAS utilisable ici : urllib se fait renvoyer 429 systématiquement
    (il faut `curl_cffi` avec empreinte Chrome, qui n'existe que dans
    /usr/local/bin/python3 — pas dans l'anaconda qui exécute les launchd).
    Ce n'est pas une perte : FRED donne Brent depuis 1987 et WTI depuis 1986,
    contre 2 ans chez Yahoo. Le TICK LIVE, lui, est récupéré côté navigateur via
    le proxy maison `/live/quotes` (déjà en place, CORS *), donc la page affiche
    bien un prix live — voir _hydrocarbures_body.html.
    """
    # _fred_helpers vit à côté du fetcher (les 3 copies l'ont) : on cherche d'abord
    # le dossier du script, sinon le dépôt — jamais l'inverse (sous launchd le
    # Desktop peut être inaccessible).
    for d in (SCRIPT_DIR, APPSUP, REPO):
        if d not in sys.path:
            sys.path.insert(0, d)
    try:
        from _fred_helpers import fetch_fred
    except Exception as e:
        log(f"_fred_helpers indisponible : {e}")
        return {}
    spec = [
        ("brent", "DCOILBRENTEU", "Brent (Europe)", "USD/bbl", "BZ=F"),
        ("wti", "DCOILWTICO", "WTI (Cushing)", "USD/bbl", "CL=F"),
        ("hh", "DHHNGSP", "Henry Hub", "USD/MMBtu", "NG=F"),
        ("rbob", "DGASNYH", "Essence RBOB NY", "USD/gal", "RB=F"),
        ("gasoil", "DDFUELNYH", "Distillat ULSD NY", "USD/gal", "HO=F"),
    ]
    out = {}
    for key, sid, label, unit, tk in spec:
        obs = fetch_fred(sid, start="1980-01-01")
        if not obs or not obs.get("values"):
            log(f"prix {key} ({sid}) : KO")
            continue
        ds, vs = obs["dates"], obs["values"]
        pts = [(d, v) for d, v in zip(ds, vs) if v is not None]
        if not pts:
            continue
        out[key] = {
            "fred": sid, "ticker": tk, "label": label, "unit": unit,
            "dates": [d for d, _ in pts], "vals": [round(v, 4) for _, v in pts],
            "last": pts[-1][1], "last_date": pts[-1][0],
            "audit": f"https://fred.stlouisfed.org/series/{sid}",
        }
        log(f"prix {key} : {len(pts)} pts · {pts[0][0]} → {pts[-1][0]} · "
            f"{pts[-1][1]} {unit}")
        time.sleep(0.2)
    # TTF (gaz européen) n'existe pas chez FRED → seul le live navigateur le sert.
    out["_ttf_note"] = ("TTF: pas de série FRED — servi en direct par /live/quotes "
                        "(ticker TTF=F)")
    log(f"prix : {len([k for k in out if not k.startswith('_')])}/{len(spec)} séries FRED")
    return out


# ── valeurs courantes : résolution de source + FILTRE DE FRAÎCHEUR ───────────
#
# PIÈGE VÉCU (2026-07-30) : prendre « la dernière valeur non nulle » de JODI est
# FAUX pour les pays qui ont cessé de déclarer. Vérifié : l'Iran s'arrête en
# 2018-07, les Émirats en 2018-12, la Russie en 2023-03 (production), la Libye en
# 2014-03, Oman en 2016-02. Seuls 53 des 105 pays JODI sont à jour. Afficher
# « Iran 3 806 kb/j » sans dire « juillet 2018 » serait un mensonge.
# RÈGLE : une valeur JODI ne sert de valeur COURANTE que si son millésime est à
# moins de STALE_MONTHS du dernier mois du jeu de données. Sinon → repli sur
# l'estimation annuelle EIA (qui couvre les non-déclarants), et le millésime ET
# la source voyagent avec CHAQUE valeur jusqu'à l'écran.
STALE_MONTHS = 8


def _months_between(a, b):
    """Écart en mois entre deux 'YYYY-MM'."""
    try:
        ya, ma = int(a[:4]), int(a[5:7])
        yb, mb = int(b[:4]), int(b[5:7])
        return (yb - ya) * 12 + (mb - ma)
    except Exception:
        return 999


def V(v, unit, src, date, note=None):
    """Fabrique une entrée `latest` : valeur + unité + source + millésime.

    GARDE-FOU (point unique) : une valeur non numérique est ignorée plutôt que de
    faire tomber tout le run. Vécu : la population lue dans le cache de l'Atlas est
    un TRIPLET [valeur, année, drapeau], et la division a levé « float / list » —
    un seul pays mal formé faisait échouer les 200 autres.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if v != v or v in (float("inf"), float("-inf")):     # NaN / infini
        return None
    d = {"v": round(v, 4), "u": unit, "src": src, "d": date}
    if note:
        d["n"] = note
    return d


def derive_latest(a3, rec, oil_months, gas_months, pop,
                  oecd_months=None, eu_months=None):
    """Construit rec['latest'] : une entrée par métrique de la carte, chacune
    porteuse de sa valeur, son unité, sa source et son millésime."""
    oil, gas = rec.get("oil") or {}, rec.get("gas") or {}
    eia = rec.get("eia") or {}
    res = rec.get("res") or {}
    ann = rec.get("ann") or {}
    omax = oil_months[-1] if oil_months else None
    gmax = gas_months[-1] if gas_months else None
    L = {}

    def jodi(series, months, mx):
        """(valeur, millésime, frais?) d'une série mensuelle JODI."""
        v, p = last_val(series, months)
        if v is None:
            return None, None, False
        return v, p, (mx is not None and _months_between(p, mx) <= STALE_MONTHS)

    # ── pétrole : production ────────────────────────────────────────────────
    jv, jp, fresh = jodi(oil.get("prod"), oil_months, omax)
    ev, ey = last_val(eia.get("prod"))
    if fresh:
        L["oil_prod"] = V(jv, "kb/j", "JODI", jp)
    elif ev is not None:
        L["oil_prod"] = V(ev, "kb/j", "EIA", str(ey),
                          f"JODI figé en {jp}" if jp else None)
    elif jv is not None:
        L["oil_prod"] = V(jv, "kb/j", "JODI", jp, "série arrêtée")

    # ── pétrole : consommation (EIA seule couvre tout le monde) ─────────────
    cv, cy = last_val(eia.get("cons"))
    if cv is not None:
        L["oil_cons"] = V(cv, "kb/j", "EIA", str(cy))

    # ── pétrole : commerce (JODI seulement, sinon rien — pas d'invention) ───
    for key, lab in (("imp", "oil_imp"), ("exp", "oil_exp")):
        jv2, jp2, fr2 = jodi(oil.get(key), oil_months, omax)
        if jv2 is not None and fr2:
            L[lab] = V(jv2, "kb/j", "JODI", jp2)

    # Exportateur net : le signe est ce qui compte (carte divergente).
    if "oil_imp" in L and "oil_exp" in L:
        L["oil_net"] = V(L["oil_exp"]["v"] - L["oil_imp"]["v"], "kb/j", "JODI",
                         L["oil_exp"]["d"], "exports − imports")
    elif "oil_prod" in L and "oil_cons" in L:
        L["oil_net"] = V(L["oil_prod"]["v"] - L["oil_cons"]["v"], "kb/j",
                         L["oil_prod"]["src"] + "/EIA", L["oil_prod"]["d"],
                         "production − consommation")

    # Dépendance aux importations : part de la conso couverte par l'import net.
    if "oil_cons" in L and "oil_imp" in L and L["oil_cons"]["v"] > 0:
        net_imp = L["oil_imp"]["v"] - L.get("oil_exp", {"v": 0})["v"]
        L["oil_dep"] = V(max(0.0, min(100.0, 100.0 * net_imp / L["oil_cons"]["v"])),
                         "%", "JODI/EIA", L["oil_imp"]["d"],
                         "imports nets ÷ consommation")

    # ── stocks de brut + COUVERTURE EN JOURS ───────────────────────────────
    sv, sp, sfresh = jodi(oil.get("stk"), oil_months, omax)
    rv, rp_, rfresh = jodi(oil.get("ref"), oil_months, omax)
    if sv is not None and sfresh:
        L["oil_stk"] = V(sv / 1000.0, "Mbbl", "JODI", sp)
        # Couverture = stock de brut ÷ intrants quotidiens des raffineries.
        # Un stock nul déclaré (Iran) n'est pas une couverture nulle : on écarte.
        if rv and rv > 0 and sv > 0:
            L["stk_days"] = V(sv / rv, "jours", "JODI", sp,
                              "stocks de brut ÷ intrants raffinerie")
    # Les pays de l'OCDE ont en plus la série TOUS produits pétroliers (EIA).
    # ⚠ sans la liste des mois, last_val renvoie l'INDEX : « EIA 555 » à l'écran.
    ov, op_ = last_val(rec.get("oecd_stk"), oecd_months)
    if ov is not None:
        L["oecd_stk"] = V(ov, "Mbbl", "EIA (OCDE)", op_ if isinstance(op_, str) else str(op_))
        if cv and cv > 0:
            L["oecd_days"] = V(ov * 1000.0 / cv, "jours", "EIA",
                               op_ if isinstance(op_, str) else str(op_),
                               "stocks tous produits ÷ consommation")

    # ── réserves d'urgence UE (Eurostat, milliers de tonnes) ────────────────
    eus = rec.get("eu_stk") or {}
    for k, lab in (("urgence", "eu_emerg"), ("total", "eu_stk_tot")):
        v, p = last_val(eus.get(k), eu_months)
        if v is not None:
            L[lab] = V(v / 1000.0, "Mt", "Eurostat", p if isinstance(p, str) else str(p))

    # ── gaz ────────────────────────────────────────────────────────────────
    gv, gp, gfresh = jodi(gas.get("prod"), gas_months, gmax)
    if gv is not None and gfresh:
        # M3 JODI = millions de m³/mois → milliards de m³/an (bcm) ×12/1000.
        L["gas_prod"] = V(gv * 12.0 / 1000.0, "bcm/an", "JODI", gp,
                          "dernier mois annualisé")
    for a, b in (("imp", "gas_imp"), ("exp", "gas_exp")):
        v2, p2, f2 = jodi(gas.get(a), gas_months, gmax)
        if v2 is not None and f2:
            L[b] = V(v2 * 12.0 / 1000.0, "bcm/an", "JODI", p2)
    if "gas_imp" in L and "gas_exp" in L:
        L["gas_net"] = V(L["gas_exp"]["v"] - L["gas_imp"]["v"], "bcm/an", "JODI",
                         L["gas_exp"]["d"], "exports − imports")
    # Part du GNL dans les imports de gaz = exposition au marché mondial.
    ip, _, fip = jodi(gas.get("imp_pip"), gas_months, gmax)
    il, _, fil = jodi(gas.get("imp_lng"), gas_months, gmax)
    # SEUIL DE MATÉRIALITÉ : un ratio calculé sur un volume infime n'apprend rien.
    # Sans lui, la Norvège et la Malaisie — deux EXPORTATEURS nets de gaz dont les
    # importations résiduelles sont anecdotiques — apparaissaient à « 100 % de GNL »
    # en tête du classement. On exige au moins 100 Mm³/mois (~1,2 bcm/an) d'imports.
    tot_imp = (ip or 0) + (il or 0)
    if fip and fil and tot_imp >= 100.0:
        L["lng_share"] = V(100.0 * (il or 0) / tot_imp, "%", "JODI", gmax,
                           "GNL ÷ (GNL + pipeline) · imports ≥ 100 Mm³/mois")

    # ── réserves prouvées + R/P ────────────────────────────────────────────
    if res.get("oil_bbl"):
        gbbl = res["oil_bbl"] / 1e9
        L["oil_res"] = V(gbbl, "Gbbl", "CIA Factbook", str(res.get("oil_yr") or ""))
        pv = L.get("oil_prod", {}).get("v")
        if pv and pv > 0:
            # prod kb/j → Gbbl/an : ×365 ×1e3 /1e9 = ×3,65e-4
            L["oil_rp"] = V(gbbl / (pv * 3.65e-4), "ans", "Factbook/JODI",
                            L["oil_prod"]["d"], "réserves ÷ production annuelle")
    if res.get("gas_m3"):
        L["gas_res"] = V(res["gas_m3"] / 1e9, "bcm", "CIA Factbook",
                         str(res.get("gas_yr") or ""))

    # ── par habitant (l'intensité pétrolière d'un mode de vie) ─────────────
    if cv and pop:
        L["oil_pc"] = V(cv * 365.0 * 1000.0 / pop, "bbl/hab/an", "EIA/BM", str(cy))

    # ── part du pétrole et du gaz dans l'énergie primaire (OWID, le siècle) ─
    for k, lab in (("oil_share", "oil_share"), ("gas_share", "gas_share")):
        v3, y3 = last_val(ann.get(k))
        if v3 is not None:
            L[lab] = V(v3, "%", "OWID/EI", str(y3))
    return L


# ── assemblage ───────────────────────────────────────────────────────────────

def load_population():
    """Population par ISO3, reprise du cache de l'Atlas Économique (déjà
    rafraîchi toutes les 6 h) : évite un appel Banque mondiale de plus et
    garantit que « pétrole par habitant » utilise le MÊME dénominateur que le
    reste du site (cohérence entre onglets)."""
    for p in (os.path.join(CACHE_DIR, "atlas_econ_cache.json"),
              os.path.join(REPO, "atlas_econ_cache.json"),
              os.path.join(SCRIPT_DIR, "atlas_econ_cache.json")):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        out = {}
        for a3, c in (d.get("countries") or {}).items():
            v = (c.get("latest") or {}).get("pop")
            # PIÈGE VÉCU : dans le cache de l'Atlas, `latest[clé]` est un TRIPLET
            # [valeur, année, drapeau], pas un scalaire (le front lit toujours
            # `lat(a3,k)[0]`). Sans déballage : « float / list » à la division.
            if isinstance(v, (list, tuple)):
                v = v[0] if v else None
            if isinstance(v, (int, float)) and v > 0:
                out[a3] = float(v)
        if out:
            log(f"population : {len(out)} pays (cache Atlas)")
            return out
    log("population : cache Atlas introuvable → « par habitant » désactivé")
    return {}


def build(countries, a2map):
    oil, oil_months = fetch_jodi_oil(a2map)
    gas, gas_months = fetch_jodi_gas(a2map)
    owid, owid_span = fetch_owid(set(countries))
    oilres = fetch_owid_oil_reserves()
    eia_c, eia_agg = fetch_eia_intl()
    oecd_stk, oecd_months = fetch_eia_oecd_stocks()
    eu_stk, eu_months = fetch_eu_oil_stocks(a2map)
    fb = fetch_factbook(countries, a2map)
    spr = fetch_spr()
    usgas = fetch_us_gas_storage()
    prices = fetch_prices()
    eu_oil_p = fetch_eu_partners(a2map, "nrg_ti_oilm", "O4100_TOT", "imports pétrole",
                                 "THS_T", "milliers de tonnes")
    eu_gas_p = fetch_eu_partners(a2map, "nrg_ti_gasm", "G3000", "imports gaz",
                                 "MIO_M3", "millions de m³")
    gie = fetch_gie(a2map)
    pop = load_population()

    a3s = set(oil) | set(gas) | set(owid) | set(eia_c) | set(fb) | set(eu_stk)
    a3s &= set(countries)
    out = {}
    for a3 in sorted(a3s):
        c = countries[a3]
        rec = {"name": c.get("name"), "region": c.get("region")}
        if a3 in oil:
            rec["oil"] = oil[a3]
        if a3 in gas:
            rec["gas"] = gas[a3]
        if a3 in owid:
            rec["ann"] = owid[a3]
        if a3 in eia_c:
            rec["eia"] = eia_c[a3]
        if a3 in oecd_stk:
            rec["oecd_stk"] = oecd_stk[a3]
        if a3 in eu_stk:
            rec["eu_stk"] = eu_stk[a3]
        res = dict(fb.get(a3) or {})
        if a3 in oilres:
            res["oil_hist"] = oilres[a3]
        if res:
            rec["res"] = res
        rec["latest"] = derive_latest(a3, rec, oil_months, gas_months, pop.get(a3),
                                      oecd_months, eu_months)
        out[a3] = rec

    meta = {
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "oil_months": oil_months,
        "gas_months": gas_months,
        "oecd_months": oecd_months,
        "eu_months": eu_months,
        "owid_span": owid_span,
        "n_countries": len(out),
    }
    payload = {
        "meta": meta,
        "countries": out,
        "eia_agg": eia_agg,
        "spr": spr,
        "us_gas": usgas,
        "prices": prices,
        "eu_partners": {"oil": eu_oil_p, "gas": eu_gas_p},
        "gie": gie,
    }
    return payload


def read_previous():
    """Relit le cache précédent (vide si absent/illisible).

    GARDE-FOU (règle maison : un bug corrigé exige un garde-fou anti-régression).
    Chaque source de cette page peut tomber indépendamment — JODI a déjà servi un
    zip illisible, l'EIA renvoie OVER_RATE_LIMIT, Eurostat rend des 404
    intermittents. Sans ce filet, un seul run dégradé remplacerait un historique
    complet par un cache amputé, et la page afficherait des trous là où la donnée
    existe. On repart donc du cache précédent et on n'écrase QUE ce qu'on a
    réellement réussi à rafraîchir.
    """
    for d in (CACHE_DIR, REPO, SCRIPT_DIR):
        p = os.path.join(d, OUT_BASENAME + ".json")
        try:
            with open(p, encoding="utf-8") as f:
                prev = json.load(f)
            if prev and prev.get("countries"):
                log(f"cache précédent relu : {len(prev['countries'])} pays "
                    f"({(prev.get('meta') or {}).get('updated_at', '?')})")
                return prev
        except Exception:
            continue
    return {}


def merge_previous(payload, prev):
    """Réinjecte les blocs que ce run n'a PAS su produire."""
    if not prev:
        return payload
    kept = []
    # Blocs de premier niveau (séries autonomes).
    for k in ("spr", "us_gas", "prices", "eu_partners", "gie", "eia_agg"):
        if not payload.get(k) and prev.get(k):
            payload[k] = prev[k]
            kept.append(k)
    # Pays : si le run courant en a nettement moins, c'est un run dégradé →
    # on complète pays par pays plutôt que de perdre l'historique.
    pc, qc = payload.get("countries") or {}, prev.get("countries") or {}
    if qc and len(pc) < 0.8 * len(qc):
        log(f"⚠ run dégradé : {len(pc)} pays contre {len(qc)} au run précédent "
            f"→ complétion depuis le cache précédent")
        for a3, rec in qc.items():
            if a3 not in pc:
                pc[a3] = rec
                continue
            for sub in ("oil", "gas", "ann", "eia", "res", "eu_stk", "oecd_stk", "latest"):
                if not pc[a3].get(sub) and rec.get(sub):
                    pc[a3][sub] = rec[sub]
        payload["countries"] = pc
        kept.append(f"countries(+{len(qc) - len(pc)})")
    # Les index de mois servent d'axe aux séries : ne jamais les perdre.
    pm, qm = payload.get("meta") or {}, prev.get("meta") or {}
    for k in ("oil_months", "gas_months", "oecd_months", "eu_months", "owid_span"):
        if not pm.get(k) and qm.get(k):
            pm[k] = qm[k]
            kept.append("meta." + k)
    if kept:
        log("conservé du run précédent : " + ", ".join(kept))
    payload["meta"]["n_countries"] = len(payload.get("countries") or {})
    return payload


def audit(payload):
    """Contrôles de vraisemblance. On n'écrit pas un cache qui échoue au socle."""
    cs = payload.get("countries") or {}
    errs, warns = [], []
    if len(cs) < 100:
        errs.append(f"seulement {len(cs)} pays")

    def lv(a3, key):
        e = ((cs.get(a3) or {}).get("latest") or {}).get(key)
        return e["v"] if e else None

    # Bornes larges : on veut attraper une erreur d'unité ou de parsing, pas
    # arbitrer la géopolitique (la production varie beaucoup d'un mois à l'autre).
    checks = [
        ("USA", "oil_prod", 8000, 20000, "production US en kb/j"),
        ("USA", "oil_cons", 14000, 26000, "consommation US en kb/j"),
        ("SAU", "oil_prod", 3000, 13000, "production saoudienne en kb/j"),
        ("CHN", "oil_imp", 4000, 15000, "imports de brut chinois en kb/j"),
        ("FRA", "oil_prod", 1, 100, "production française (très faible, en kb/j)"),
    ]
    for a3, key, lo, hi, what in checks:
        v = lv(a3, key)
        if v is None:
            warns.append(f"{a3}.{key} absent ({what})")
        elif not (lo <= v <= hi):
            errs.append(f"{a3}.{key} = {v} hors de [{lo},{hi}] — {what}")

    # Trous ATTENDUS : leur disparition signalerait un mélange de sources.
    if lv("CHN", "oil_stk") is not None:
        warns.append("la Chine a soudain un stock déclaré — vérifier la source, "
                     "elle n'en publie pas")
    spr = payload.get("spr")
    if spr and spr.get("mbbl"):
        last = spr["mbbl"][-1]
        if not (100 <= last <= 800):
            errs.append(f"SPR = {last} Mbbl, hors plage plausible")
    for w in warns:
        log(f"[audit] ATTENTION {w}")
    for e in errs:
        log(f"[audit] ERREUR {e}")
    return errs


def main():
    countries, a2map = load_meta()
    log(f"méta : {len(countries)} pays, {len(a2map)} codes ISO2")
    prev = read_previous()
    payload = merge_previous(build(countries, a2map), prev)
    errs = audit(payload)
    if errs and prev:
        log(f"[audit] {len(errs)} erreur(s) → cache précédent CONSERVÉ, rien n'est écrit")
        sys.exit(1)
    if errs:
        log(f"[audit] {len(errs)} erreur(s) et aucun cache précédent → écriture quand même "
            f"(mieux qu'une page vide), à corriger")

    js = ("window.__HYDRO__ = "
          + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n")
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    wrote = []
    for d in OUT_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
            for name, content in ((OUT_BASENAME + ".json", blob),
                                  (OUT_BASENAME + ".js", js)):
                tmp = os.path.join(d, name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp, os.path.join(d, name))   # écriture atomique
            wrote.append(d)
        except Exception as e:
            log(f"écriture KO {d} : {e}")
    log(f"OK · {len(payload['countries'])} pays · {len(blob)/1e6:.2f} Mo · "
        f"écrit dans {len(wrote)} dossiers")


if __name__ == "__main__":
    main()
