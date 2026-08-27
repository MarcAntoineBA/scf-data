#!/usr/bin/env python3
"""Bake LOCAL des logos actions (4 zones US/CN/EU/IN) — remplace le DDG runtime.

POURQUOI (2026-07-20) : icons.duckduckgo.com renvoie pour ~74/100 domaines chinois un
404 dont le CORPS est une image placeholder 48x48 (globe generique). Le navigateur
ignore le statut HTTP d'un <img> et affiche le corps -> onload se declenche, la cascade
de fallback croit avoir un vrai logo -> "beaucoup de mauvais logos" sur la carte Chine.
Un <img> ne peut PAS voir le statut 404 ; seul un fetch serveur le peut. Donc on resout
les logos ICI (statuts HTTP reels), on les bake en PNG locaux versionnes dans le repo,
et le client n'a plus AUCUNE dependance logo au runtime.

Sources par ticker (1re image valide gagne) :
  1. DDG ip3 du domaine   — accepte UNIQUEMENT si HTTP 200 (vrai logo)
  2. DDG ip3 du domaine parent (ir.baidu.com -> baidu.com)
  3. https://<domaine>/favicon.ico (puis parent, puis http://)
  4. <link rel="icon"> parse sur la homepage (puis parent)
  5. Wikidata P154 (logo officiel, rasterise par Commons) — accepte seulement si le
     site officiel P856 de l'entite matche notre domaine (anti-homonyme)
  6. Google s2 avec rejet par HASH du globe generique (validation cote serveur ;
     interdit cote client car le navigateur ne peut pas distinguer le globe)

Validation : PIL decode, min 16px, >=2 couleurs opaques distinctes (anti-blanc).
Sortie : Site_Crypto_Finance/stock_logos/<ticker _ >.png (64x64 RGBA, pad carre)
       + stock_logos_manifest.js -> window.__STOCK_LOGOS__ = {"0700.HK":1,...}

Usage : python3 fetch_stock_logos.py [--only-missing]
A relancer apres chaque regeneration du pool (build_stock_universe.py).
"""
import io, json, re, sys, urllib.parse, warnings
from concurrent.futures import ThreadPoolExecutor

# ── Un cache devant la resolution de noms ────────────────────────────────
# La cascade interroge, pour CHAQUE societe : icons.duckduckgo.com, le domaine,
# son parent, puis wikidata.org — souvent deux fois. Sur onze mille six cents
# societes cela fait plus de cinquante mille resolutions, dont l'ecrasante
# majorite portent sur trois noms d'hote identiques.
#
# Sous WSL le resolveur est un relais NAT vers l'hote Windows. Mesure du
# 27/08/2026, a vingt-quatre fils PUIS a huit : `getent hosts` finit par
# echouer et le collecteur s'arrete vers deux mille pastilles EN SILENCE, parce
# qu'un echec de resolution est indiscernable d'un domaine sans favicon.
#
# Ce n'est pas un contournement de la limite : c'est cesser de poser cinquante
# mille fois une question dont on a deja la reponse.
import socket as _socket
import threading as _threading
import time as _time

_CACHE_DNS = {}
_VERROU_DNS = _threading.Lock()
_TTL_DNS = 900.0        # une reponse valide sert quinze minutes
_TTL_DNS_NEG = 120.0    # un echec n'en condamne pas le domaine pour tout le run
_getaddrinfo_reel = _socket.getaddrinfo


def _getaddrinfo_cache(host, port, *a, **kw):
    cle = (host, port, a, tuple(sorted(kw.items())))
    maintenant = _time.time()
    with _VERROU_DNS:
        e = _CACHE_DNS.get(cle)
        if e and maintenant < e[0]:
            if e[1] is None:
                raise _socket.gaierror(_socket.EAI_NONAME, "cache negatif : " + str(host))
            return e[1]
    try:
        r = _getaddrinfo_reel(host, port, *a, **kw)
    except Exception:
        with _VERROU_DNS:
            _CACHE_DNS[cle] = (maintenant + _TTL_DNS_NEG, None)
        raise
    with _VERROU_DNS:
        _CACHE_DNS[cle] = (maintenant + _TTL_DNS, r)
    return r


_socket.getaddrinfo = _getaddrinfo_cache

from pathlib import Path

from curl_cffi import requests   # TLS Chrome : les sites corporate (CF & co) bloquent python-requests
from PIL import Image

warnings.filterwarnings("ignore")
try:
    import urllib3; urllib3.disable_warnings()
except Exception:
    pass

SITE = Path.home() / "Desktop" / "Site_Crypto_Finance"
UNIVERSE = SITE / "scripts" / "SiteCryptoFinance" / "stock_universe.json"
EARNINGS_CACHE = SITE / "earnings_calendar_cache.json"   # tickers du calendrier resultats
REPO_DIR = SITE / "stock_logos"                 # logos versionnes dans le repo
MANIFEST = SITE / "stock_logos_manifest.js"

# ── Ecriture TCC-safe (2026-07-29) ──────────────────────────────────────────
# Ce script doit pouvoir etre declenche automatiquement par le radar de nouvelles
# cotations (fetch_new_listings.py, launchd). Or launchd n'a pas toujours le droit
# d'ecrire dans ~/Desktop (TCC). On ecrit donc dans le repo quand c'est possible,
# sinon dans ~/Library/Caches/, d'ou snapshot_site.sh publie ensuite les PNG
# (meme schema que les logos de l'Atlas Economique).
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_LOGOS = CACHE_DIR / "stock_logos"


def _writable(d):
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / ".w"
        p.write_text("x")
        p.unlink()
        return True
    except Exception:
        return False


if _writable(REPO_DIR):
    OUT_DIR = REPO_DIR
else:
    CACHE_LOGOS.mkdir(parents=True, exist_ok=True)
    OUT_DIR = CACHE_LOGOS
    MANIFEST = CACHE_DIR / "stock_logos_manifest.js"
    sys.stderr.write("[StockLogos] repo non inscriptible (TCC) -> ecriture dans Caches\n")

# Repertoires consultes pour SAVOIR si un logo existe deja. Le manifeste doit
# etre l'UNION des deux : le construire depuis le seul dossier d'ecriture ferait
# disparaitre du manifeste les centaines de logos deja bakes dans le repo, et donc
# de la carte (le client affiche "logo local ou RIEN").
LOOKUP_DIRS = [d for d in (REPO_DIR, CACHE_LOGOS) if d.exists()] or [OUT_DIR]


def logo_exists(t):
    fn = _nom_fichier(t) + ".png"
    return any((d / fn).exists() for d in LOOKUP_DIRS)


def read_manifest():
    """Manifeste deja publie (repo ou Caches), {} s'il est absent/illisible."""
    for p in (MANIFEST, REPO_DIR.parent / "stock_logos_manifest.js"):
        try:
            raw = p.read_text().split("=", 1)[1].strip().rstrip(";")
            return json.loads(raw)
        except Exception:
            continue
    return {}

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
ONLY_MISSING = "--only-missing" in sys.argv
RETRY_FAILED = "--retry-failed" in sys.argv
FORCE_REFETCH = "--force-refetch" in sys.argv

# ── Priorite a TradingView (2026-08-10) ─────────────────────────────────────
# fetch_stock_logos_tv.py bake des pastilles VECTORIELLES 128 px pour ~99 % du pool
# (cf son en-tete : les favicons d'entreprise s'effondrent hors des Etats-Unis).
# Ce script-ci ne garde donc qu'un role de REPLI. Sans ce garde-fou, un run sans
# --only-missing re-resolvait tout par la cascade favicon et remplacait une pastille
# nette par une icone 16 px agrandie — la regression exacte qu'on vient de corriger.
TV_SOURCED = {}
for _p in (SITE / "stock_logos_tv.json", CACHE_DIR / "stock_logos_tv.json"):
    try:
        TV_SOURCED = json.loads(_p.read_text())
        break
    except Exception:
        continue

# ── Memoire des echecs (2026-07-29) ─────────────────────────────────────────
# ~22 domaines corporate chinois n'exposent aucune icone exploitable. Sans
# memoire, CHAQUE run les retentait via les 6 strategies de resolution, chacune
# avec ses timeouts : 15 a 20 min par run pour zero gain. Le radar declenchant ce
# script automatiquement, on note la date du dernier echec et on ne retente qu'au
# bout de FAIL_TTL_DAYS (ou immediatement avec --retry-failed). Un site qui
# publie enfin un favicon est donc bien recupere, mais sans marteler.
FAILED_FILE = CACHE_DIR / "stock_logos_failed.json"
FAIL_TTL_DAYS = 14


def load_failed():
    if RETRY_FAILED:
        return {}
    try:
        import datetime
        raw = json.loads(FAILED_FILE.read_text())
        out = {}
        for t, d in raw.items():
            try:
                age = (datetime.datetime.now() - datetime.datetime.strptime(d, "%Y-%m-%d")).days
            except ValueError:
                continue
            if age <= FAIL_TTL_DAYS:
                out[t] = d
        return out
    except Exception:
        return {}


def save_failed(prev, new_kos):
    import datetime
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    prev = dict(prev)
    for t in new_kos:
        prev[t] = today
    try:
        FAILED_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAILED_FILE.write_text(json.dumps(prev, indent=1))
    except Exception as e:
        log(f"[StockLogos] memoire des echecs non ecrite : {e}")

def log(m): sys.stderr.write(m + "\n"); sys.stderr.flush()

def _nom_fichier(ticker):
    """Le nom de fichier d'un logo, pour un symbole du monde entier.

    Le point devient un souligne (NVDA.SW -> NVDA_SW). La BARRE OBLIQUE aussi :
    `bvl/CVERDEC1` designait un sous-dossier inexistant, et le run entier
    s'arretait sur cette seule cotation liménienne. La page applique exactement
    la meme regle, dans le meme ordre.
    """
    return str(ticker).replace(".", "_").replace("/", "_")


def parent_domain(dom):
    """ir.baidu.com -> baidu.com ; en.sungrowpower.com -> sungrowpower.com.
    Garde 3 labels pour les TLD composes (.com.cn, .com.hk, .co.uk, .net.cn...)."""
    parts = dom.split(".")
    if len(parts) <= 2: return None
    if parts[-2] in ("com", "co", "net", "org", "gov", "edu") and len(parts[-1]) == 2:
        keep = 3
    else:
        keep = 2
    if len(parts) <= keep: return None
    return ".".join(parts[-keep:])

# Images GENERIQUES a rejeter, identifiees par empreinte du PNG bake 64x64.
# Ajout 2026-07-29 : Grasim Industries et Union Bank of India portaient tous deux
# la MEME icone "document" generique (un placeholder de CMS), qui passait toutes
# les validations existantes — 641 couleurs, 64px, non monochrome. C'est le genre
# de faux logo que l'oeil repere immediatement sur la carte, mais qu'aucun controle
# automatique ne voyait. Le signal qui l'a demasque : deux domaines SANS RAPPORT
# aboutissant a des octets identiques (cf. rapport de fin de run).
BAD_HASHES = {
    # Relevés le 27/08/2026 sur le parc mondial : icônes par défaut de
    # gestionnaires de contenu, servies pour des societes sans aucun rapport.
    "d47dc2bc3e8d",  # Knight-Swift, Tidewater, InMode, Camil, Gokul Agro
    "f5fe4dfa85b6",  # Sterling Infra, Guoco, Guardian, Caltagirone

    "b1afaf0d51399d22615951babd47bed6",   # icone "document" generique (CMS)
}


def bake_hash(im):
    """Empreinte de l'image APRES normalisation 64x64 (celle qui sera ecrite)."""
    import hashlib
    buf = io.BytesIO()
    # MEMES parametres que bake() : sans optimize=True les octets different et
    # l'empreinte ne correspond a aucune entree de BAD_HASHES (le placeholder
    # "document" est repasse malgre le rejet lors du run du 29/07/2026).
    _square64(im).save(buf, "PNG", optimize=True)
    return hashlib.md5(buf.getvalue()).hexdigest()


def valid_image(data):
    """PIL decode + min 16px + pas monochrome. Retourne l'Image ou None."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if hasattr(im, "n_frames") and getattr(im, "n_frames", 1) > 1:
            pass  # ICO multi-frame : PIL ouvre la plus grande par defaut
        if min(im.size) < 16: return None
        rgba = im.convert("RGBA")
        px = list(rgba.resize((24, 24)).getdata())
        opaque = [(r, g, b) for r, g, b, a in px if a > 40]
        if not opaque: return None                                  # tout transparent
        colors = {(r >> 4, g >> 4, b >> 4) for r, g, b in opaque}
        # Rejet "carre uniforme" seulement : 1 couleur SANS forme decoupee par l'alpha.
        # Un glyphe monochrome sur fond transparent (ex. AMD noir 16px) est LEGITIME.
        if len(colors) < 2 and len(opaque) > len(px) * 0.95: return None
        return rgba
    except Exception:
        return None

# Mode masse : une seule source, un delai court. Voir resolve().
RAPIDE = "--rapide" in sys.argv
DELAI = 6 if RAPIDE else 15


def get(url, **kw):
    try:
        return requests.get(url, headers=UA, timeout=DELAI, verify=False,
                            impersonate="chrome120", **kw)
    except Exception:
        return None

def try_ddg(dom):
    r = get(f"https://icons.duckduckgo.com/ip3/{dom}.ico")
    if r is not None and r.status_code == 200:      # 404 DDG = placeholder MENTEUR, jamais accepte
        return valid_image(r.content)
    return None

def try_favicon(dom):
    for scheme in ("https", "http"):
        r = get(f"{scheme}://{dom}/favicon.ico")
        if r is not None and r.status_code == 200:
            im = valid_image(r.content)
            if im: return im
    return None

_LINK_RE = re.compile(r'<link[^>]+rel=["\'][^"\']*icon[^"\']*["\'][^>]*>', re.I)
_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_SIZE_RE = re.compile(r'sizes=["\'](\d+)x', re.I)

def try_homepage(dom):
    for scheme in ("https", "http"):
        r = get(f"{scheme}://{dom}/")
        if r is None or r.status_code != 200: continue
        links = _LINK_RE.findall(r.text[:200_000])
        cands = []
        for lk in links:
            href = _HREF_RE.search(lk)
            if not href: continue
            u = href.group(1)
            if u.lower().endswith(".svg") or u.startswith("data:"): continue
            sz = _SIZE_RE.search(lk)
            cands.append((int(sz.group(1)) if sz else 32, urllib.parse.urljoin(r.url, u)))
        for _, u in sorted(cands, reverse=True):
            r2 = get(u)
            if r2 is not None and r2.status_code == 200:
                im = valid_image(r2.content)
                if im: return im
        break
    return None

def try_wikidata(name, dom):
    """Logo officiel Wikidata (P154), rasterise en PNG par Commons (gere les SVG).
    GARDE-FOU anti-mauvais-logo : l'entite n'est acceptee que si son site officiel
    (P856) correspond a notre domaine (ou domaine parent) — un homonyme ne passe pas."""
    par = parent_domain(dom) or dom
    try:
        r = get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                "&language=en&type=item&limit=5&search=" + urllib.parse.quote(name))
        if r is None or r.status_code != 200: return None
        for ent in r.json().get("search", []):
            qid = ent.get("id")
            r2 = get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
            if r2 is None or r2.status_code != 200: continue
            claims = r2.json()["entities"][qid].get("claims", {})
            sites = [c["mainsnak"]["datavalue"]["value"]
                     for c in claims.get("P856", []) if c["mainsnak"].get("datavalue")]
            if not any(par in s or dom in s for s in sites): continue
            logos = [c["mainsnak"]["datavalue"]["value"]
                     for c in claims.get("P154", []) if c["mainsnak"].get("datavalue")]
            for f in logos:
                r3 = get("https://commons.wikimedia.org/wiki/Special:FilePath/"
                         + urllib.parse.quote(f) + "?width=128")
                if r3 is not None and r3.status_code == 200:
                    im = valid_image(r3.content)
                    if im: return im
    except Exception:
        pass
    return None

def domain_from_wikidata(name, ticker):
    """Trouve le domaine officiel (P856) d'une societe a partir de son NOM.

    Sert uniquement aux tickers du calendrier des resultats qui ne sont ni dans
    stock_universe.json ni dans EARNINGS_DOMAINS : chaque trimestre amene de
    nouvelles societes (>= 200 Md$ ou watchlist), et sans ca elles resteraient
    sans logo jusqu'a une intervention manuelle.

    GARDE-FOU anti-homonyme, non negociable : l'entite n'est retenue que si son
    symbole boursier Wikidata (P249) matche EXACTEMENT notre ticker. « Circle »,
    « ON » ou « TM » ramenent sinon n'importe quoi. Aucun match = None (le ticker
    est alors journalise comme SANS DOMAINE, et on n'affiche pas de logo plutot
    qu'un mauvais logo)."""
    want = ticker.upper().strip()
    try:
        r = get("https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json"
                "&language=en&type=item&limit=6&search=" + urllib.parse.quote(name))
        if r is None or r.status_code != 200:
            return None
        for ent in r.json().get("search", []):
            qid = ent.get("id")
            r2 = get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
            if r2 is None or r2.status_code != 200:
                continue
            claims = r2.json()["entities"][qid].get("claims", {})
            syms = set()
            for c in claims.get("P249", []):
                v = (c["mainsnak"].get("datavalue") or {}).get("value")
                if isinstance(v, str):
                    syms.add(v.upper().split(":")[-1].strip())
            if want not in syms:
                continue
            for c in claims.get("P856", []):
                v = (c["mainsnak"].get("datavalue") or {}).get("value")
                if not isinstance(v, str):
                    continue
                host = urllib.parse.urlparse(v).netloc.lower()
                if host.startswith("www."):
                    host = host[4:]
                if host:
                    return host
    except Exception:
        pass
    return None


_S2_DEFAULT_HASHES = set()
def _s2_default_hashes():
    """Hashes du globe generique s2 (reponse pour un domaine bidon) -> a rejeter."""
    import hashlib
    if not _S2_DEFAULT_HASHES:
        for probe in ("domaine-inexistant-xyzzy-42.com", "aucunsite-bidon-777.net"):
            r = get(f"https://www.google.com/s2/favicons?domain={probe}&sz=64")
            if r is not None and r.status_code == 200:
                _S2_DEFAULT_HASHES.add(hashlib.md5(r.content).hexdigest())
    return _S2_DEFAULT_HASHES

def try_s2(dom):
    """Google s2 en DERNIER recours, cote serveur seulement : on rejette par hash le
    globe generique (impossible cote navigateur — origine des anciens logos bugues)."""
    import hashlib
    r = get(f"https://www.google.com/s2/favicons?domain={dom}&sz=64")
    if r is None or r.status_code != 200: return None
    if hashlib.md5(r.content).hexdigest() in _s2_default_hashes(): return None
    return valid_image(r.content)

# Domaines ALTERNATIFS cures (le domaine du pool = site corporate/IR sans favicon
# exploitable ; l'alternatif est un domaine officiel de la MEME societe) :
ALT_DOMAINS = {
    "NTES":          ["163.com"],              # NetEase = portail 163.com
    "PDD":           ["pinduoduo.com"],        # PDD Holdings = Pinduoduo
    "NAUKRI.NS":     ["naukri.com"],           # Info Edge = naukri.com
    "600028.SS":     ["sinopecgroup.com"],     # Sinopec groupe
    "601919.SS":     ["coscoshipping.com"],    # COSCO Shipping
    "POWERGRID.NS":  ["powergridindia.com"],   # Power Grid Corp of India
    "BANKBARODA.NS": ["bankofbaroda.in"],      # Bank of Baroda (.in)
    "GAIL.NS":       ["gail.co.in"],           # GAIL India
    "000858.SZ":     ["wuliangye.com"],        # Wuliangye
}

# ── Calendrier des resultats (2026-08-02) ───────────────────────────────────
# Le widget « Calendrier macro & resultats » affiche un logo par societe. Ses
# tickers viennent d'Investing.com et NE SONT PAS tous dans stock_universe.json :
# ADR (TCEHY, SIEGY, MUFG, NVO...), societes < seuil du pool (CRCL, CRWV), et
# les deux classes de Berkshire (BRKa/BRKb, notees BRK-B dans le pool). On les
# bake ici avec les MEMES garde-fous (statut HTTP reel, rejet du globe generique)
# pour que le front n'ait aucune dependance logo au runtime.
EARNINGS_DOMAINS = {
    "ANET":  "arista.com",          "BHP":   "bhp.com",
    "BRKa":  "berkshirehathaway.com", "BRKb": "berkshirehathaway.com",
    "CRCL":  "circle.com",          "CRWV":  "coreweave.com",
    "DELL":  "dell.com",            "HSBC":  "hsbc.com",
    "IDEXY": "inditex.com",         "MRVL":  "marvell.com",
    "MUFG":  "mufg.jp",             "NVO":   "novonordisk.com",
    "ON":    "onsemi.com",          "RY":    "rbc.com",
    "SIEGY": "siemens.com",         "SMCI":  "supermicro.com",
    "SNOW":  "snowflake.com",       "TCEHY": "tencent.com",
    "TM":    "toyota.com",          "TD":    "td.com",
    "SFTBF": "group.softbank",      "SFTBY": "group.softbank",
}

# ── Alias : MEME societe deja bakee sous un autre ticker (2026-08-07) ─────────
# Le calendrier des resultats emploie les tickers d'Investing.com (BRKa/BRKb) ou
# les ADR americains (BACHY, CICHY, IDCBY), la ou le pool a bake la ligne locale
# (BRK-B, 601988.SS, 601939.SS, 1398.HK). Resoudre une 2e fois le meme domaine
# etait au mieux du gaspillage, au pire un echec : berkshirehathaway.com n'expose
# aucune icone exploitable, BRKa/BRKb figuraient donc dans la memoire des echecs
# depuis le 02/08 et la 1re capi du calendrier s'affichait en monogramme « BRK »
# alors que BRK-B.png dormait dans le depot. On COPIE le PNG deja valide : c'est
# la meme marque, pas un logo devine.
# BRK.A/BRK.B : orthographe Nasdaq. La collecte tourne sur GitHub Actions, ou
# Investing.com refuse l'IP du datacenter (403) et le repli Nasdaq prend la main :
# la MEME societe arrive alors sous un autre ticker. Sans alias, elle repasserait
# en monogramme des que le repli se declenche.
EARNINGS_ALIAS = {
    "BRKa":  "BRK-B",       "BRKb":  "BRK-B",
    "BRK.A": "BRK-B",       "BRK.B": "BRK-B",
    "BACHY": "601988.SS",   # Bank of China ADR        -> ligne Shanghai
    "CICHY": "601939.SS",   # China Construction Bank   -> ligne Shanghai
    "IDCBY": "1398.HK",     # ICBC ADR                  -> ligne Hong Kong
}


def apply_aliases():
    """Copie le PNG de la societe mere pour chaque alias qui n'en a pas encore.
    Retourne la liste des tickers desormais couverts."""
    faits = []
    for cible, source in EARNINGS_ALIAS.items():
        if logo_exists(cible):
            faits.append(cible)
            continue
        src_fn = _nom_fichier(source) + ".png"
        src = next((d / src_fn for d in LOOKUP_DIRS if (d / src_fn).exists()), None)
        if src is None:
            log(f"[StockLogos] alias {cible} -> {source} : source absente, ignore")
            continue
        try:
            (OUT_DIR / (_nom_fichier(cible) + ".png")).write_bytes(src.read_bytes())
            log(f"[StockLogos] alias {cible} <- {source} (meme societe)")
            faits.append(cible)
        except Exception as e:
            log(f"[StockLogos] alias {cible} echoue : {e}")
    return faits

# Tickers dont le favicon du site est un FAUX logo connu (audit visuel 2026-07-20) :
# YUMC = yumchina.com sert le favicon par defaut de Vue.js. 0288.HK = le favicon DDG
# de whgroupltd.com est une rangee de chiffres (artefact). Pour eux : uniquement les
# sources sures (DDG 200 du domaine pool, Wikidata P154 valide P856) — jamais le
# favicon du site. Mieux vaut PAS de logo (orbe + ticker) qu'un logo faux.
UNTRUSTED_SITE_FAVICON = {"YUMC", "0288.HK"}

def resolve(ticker, dom, name=""):
    if RAPIDE:
        # La masse d'abord : DuckDuckGo repond pour la grande majorite des
        # societes reelles, et c'est UNE requete. Ce qu'on ne trouve pas ici
        # part dans la memoire des echecs — un run ulterieur SANS --rapide lui
        # appliquera la cascade complete, sur une liste devenue courte.
        im = try_ddg(dom)
        if im:
            return im
        par = parent_domain(dom)
        return try_ddg(par) if par else None
    if ticker in UNTRUSTED_SITE_FAVICON:
        im = try_ddg(dom)
        if im: return im
        return try_wikidata(name, dom) if name else None
    par = parent_domain(dom)
    www = None if dom.startswith("www.") else "www." + (par or dom)
    for fn, arg in ((try_ddg, dom), (try_ddg, par),
                    (try_favicon, dom), (try_favicon, par), (try_favicon, www),
                    (try_homepage, dom), (try_homepage, par), (try_homepage, www)):
        if arg is None: continue
        im = fn(arg)
        if im: return im
    if name:
        # Variantes de nom : brut, sans suffixes corporate ("Alibaba Holding" -> "Alibaba"),
        # sinon wbsearchentities rate le label Wikidata ("Alibaba Group").
        base = re.sub(r"\b(Holding[s]?|Group|Limited|Ltd\.?|Inc\.?|Corp\.?|Co\.?|PLC|N\.?V\.?|S\.?A\.?|AG|SE)\b\.?", "", name).strip(" ,.")
        for nm in dict.fromkeys([name, base] + ([base.split()[0]] if base and len(base.split()[0]) > 3 else [])):
            if not nm: continue
            im = try_wikidata(nm, dom)
            if im: return im
    for arg in (dom, par):
        if arg is None: continue
        im = try_s2(arg)
        if im: return im
    for alt in ALT_DOMAINS.get(ticker, []):
        im = resolve("", alt, name)          # ticker vide -> pas de re-recursion ALT
        if im: return im
    return None

def _square64(im):
    """Normalise en PNG carre 64x64 RGBA (pad transparent, pas de deformation)."""
    im = im.copy()
    w, h = im.size
    if w > 256 or h > 256:                            # borne le cout du pad
        im.thumbnail((256, 256), Image.LANCZOS)
        w, h = im.size
    side = max(w, h)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(im, ((side - w) // 2, (side - h) // 2))
    return sq.resize((64, 64), Image.LANCZOS)


def bake(im, path):
    _square64(im).save(path, "PNG", optimize=True)

def earnings_jobs(already):
    """(ticker, domaine|None, nom) pour chaque societe du calendrier des resultats
    absente du pool. Domaine : EARNINGS_DOMAINS d'abord, sinon Wikidata verrouille
    par le symbole boursier. Un ticker sans domaine est remonte tel quel : il sera
    journalise comme SANS DOMAINE plutot que d'echouer en silence."""
    try:
        evs = json.loads(EARNINGS_CACHE.read_text()).get("events", [])
    except Exception as e:
        log(f"[StockLogos] calendrier resultats illisible ({e}) -> tickers earnings ignores")
        return []
    names = {}
    for e in evs:
        t = (e.get("ticker") or "").strip()
        if t and t not in already and t not in names:
            names[t] = (e.get("company") or "").strip()
    out = []
    for t, n in sorted(names.items()):
        d = EARNINGS_DOMAINS.get(t)
        if not d and t in EARNINGS_ALIAS:
            out.append((t, None, n))       # apply_aliases() s'en charge, pas Wikidata
            continue
        if not d and n and not (ONLY_MISSING and logo_exists(t)):
            d = domain_from_wikidata(n, t)
            if d:
                log(f"[StockLogos] domaine devine par Wikidata (P249={t}) : {t} -> {d}")
        out.append((t, d, n))
    if out:
        log(f"[StockLogos] +{len(out)} ticker(s) du calendrier des resultats "
            f"(hors pool) : {', '.join(t for t, _, _ in out)}")
    return out


SOURCE_MARCHE = "--source" in sys.argv and "marche" in sys.argv
PROFOND = "--profond" in sys.argv
LIMITE = 0
for _i, _a in enumerate(sys.argv):
    if _a == "--limite" and _i + 1 < len(sys.argv):
        LIMITE = int(sys.argv[_i + 1])


def jobs_marche():
    """Les domaines servis par la collecte de marche, en (ticker, domaine, nom).

    Trie par capitalisation decroissante : si le run est plafonne ou interrompu,
    ce sont les societes les plus regardees qui ont leur pastille.
    """
    import glob as _glob
    import re as _re
    from urllib.parse import urlparse as _up
    dossier = str(CACHE_DIR)
    frags = sorted(_glob.glob(dossier + "/marche_[0-9]*.json"))
    if not frags:
        log("[StockLogos] aucun fragment de marche dans " + dossier)
        return []
    profonds = set()
    if PROFOND:
        idx = CACHE_DIR / "intl_fundamentals_index.json"
        if idx.exists():
            try:
                profonds = set((json.loads(idx.read_text()) or {}).get("societes") or {})
            except Exception:
                profonds = set()
        sec = CACHE_DIR / "sec_fundamentals_index.json"
        if sec.exists():
            try:
                profonds |= set((json.loads(sec.read_text()) or {}).get("societes") or {})
            except Exception:
                pass
        log("[StockLogos] %d societes avec etats financiers" % len(profonds))
    out = []
    for f in frags:
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        ch = d.get("champs") or []
        if "site_web" not in ch:
            continue
        iw = ch.index("site_web")
        inom = ch.index("name") if "name" in ch else None
        ica = ch.index("marketCapUsd") if "marketCapUsd" in ch else None
        for sym, arr in (d.get("societes") or {}).items():
            if PROFOND and profonds and sym not in profonds:
                continue
            w = arr[iw] if iw < len(arr) else None
            if not isinstance(w, str) or not w:
                continue
            h = _up(w if "//" in w else "http://" + w).netloc.lower().split(":")[0]
            if not _re.match(r"^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", h):
                continue
            capi = arr[ica] if (ica is not None and ica < len(arr)) else 0
            out.append((capi if isinstance(capi, (int, float)) else 0,
                        sym, h, (arr[inom] if inom is not None and inom < len(arr) else "") or ""))
    out.sort(key=lambda x: -x[0])
    if LIMITE:
        out = out[:LIMITE]
    return [(t, d, n) for _, t, d, n in out]



# ── Ecarter les placeholders, sans emporter les fratries ─────────────────
# Une image identique sur plusieurs societes est SOUVENT legitime : les neuf
# Caisses regionales du Credit Agricole, les huit entites Brookfield, les cinq
# Tata, les quatre SK, les trois filiales CNOOC. Elle est parfois l'icone par
# defaut d'un gestionnaire de contenu, servie a des societes sans rapport.
#
# LE CRITERE, mesure sur ce parc : une fratrie laisse une trace, dans les NOMS
# ou dans les DOMAINES. Il faut les DEUX signaux — sur le seul nom, SK Telecom
# (deux lettres), LG Chem (deux lettres) et les trois filiales CNOOC (aucun mot
# commun) etaient condamnees a tort.
#
# Le doute profite au monogramme : a deux porteurs seulement on ne touche a rien,
# et un monogramme n'affirme rien de faux tandis qu'un logo emprunte, si.
_MOTS_VIDES = {
    "group", "groupe", "holding", "holdings", "limited", "ltd", "inc", "corp",
    "corporation", "company", "compagnie", "plc", "sa", "nv", "ag", "se", "spa",
    "as", "asa", "oyj", "ab", "co", "the", "and", "de", "du", "des", "la", "le",
    "les", "of", "for", "international", "global", "national", "industries",
    "industrial", "technologies", "technology", "tech", "systems", "solutions",
    "services", "capital", "financial", "finance", "bank", "banco", "banca",
    "insurance", "energy", "power", "resources", "mining", "properties",
    "property", "realty", "reit", "trust", "fund", "investment", "investments",
    "development", "enterprises", "electric", "electronics", "pharmaceutical",
    "pharmaceuticals", "pharma", "chemical", "chemicals", "motor", "motors",
    "bancorp", "first", "new", "united", "general", "public", "joint", "stock",
    "class", "ordinary", "shares", "share", "com", "www", "net", "org",
}
_TLD = re.compile(
    r"\.(com|net|org|co|io|ai|fr|de|jp|cn|hk|kr|tw|in|br|au|ca|ch|nl|se|no|fi"
    r"|dk|it|es|pt|be|at|pl|tr|za|mx|sg|my|th|id|ph|vn|ae|sa|il|ru|gr|cz|hu|ro"
    r"|ie|nz|cl|ar|pe|kz|ua|eu|uk|us|biz|info)(\.[a-z]{2})?$")


def _identite_marche():
    """(nom, domaine) par nom de fichier, depuis la collecte de marche."""
    import glob as _g
    noms, doms = {}, {}
    for f in sorted(_g.glob(str(CACHE_DIR / "marche_[0-9]*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        ch = d.get("champs") or []
        i_n = ch.index("name") if "name" in ch else None
        i_w = ch.index("site_web") if "site_web" in ch else None
        for sym, arr in (d.get("societes") or {}).items():
            k = _nom_fichier(sym)
            if i_n is not None and i_n < len(arr) and arr[i_n]:
                noms[k] = arr[i_n]
            if i_w is not None and i_w < len(arr) and arr[i_w]:
                doms[k] = arr[i_w]
    return noms, doms


def _mots_nom(nom):
    import unicodedata as _u
    t = _u.normalize("NFKD", str(nom or "")).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-zA-Z0-9 ]", " ", t).lower()
    # Deux lettres suffisent : « SK » et « LG » sont de vrais noms de groupe.
    return {m for m in t.split() if len(m) > 1 and m not in _MOTS_VIDES}


def _racine_domaine(dom):
    d = str(dom or "").lower().strip()
    d = re.sub(r"^https?://", "", d).split("/")[0].split(":")[0]
    d = re.sub(r"^www\d?\.", "", d)
    return re.sub(r"[^a-z0-9]", "", _TLD.sub("", d))


def _racine_commune(racines):
    racines = [r for r in racines if len(r) >= 4]
    if len(racines) < 2:
        return None
    besoin = max(2, len(racines) / 2.0)
    for r in racines:
        for n in range(len(r), 3, -1):
            for i in range(len(r) - n + 1):
                frag = r[i:i + n]
                if sum(1 for x in racines if frag in x) >= besoin:
                    return frag
    return None


def _ecarter_placeholders(by_hash):
    groupes = {h: v for h, v in by_hash.items() if len(v) > 2}
    if not groupes:
        return
    noms, doms = _identite_marche()
    if not noms:
        log("[StockLogos] identites de marche indisponibles -> aucun ecart automatique")
        return
    efface = 0
    for h, v in groupes.items():
        ens = [_mots_nom(noms.get(t)) for t in v if noms.get(t)]
        compte = {}
        for e in ens:
            for m in e:
                compte[m] = compte.get(m, 0) + 1
        par_nom = ([m for m, n in compte.items() if n >= max(2, len(ens) / 2.0)]
                   if len(ens) >= 2 else [])
        par_dom = _racine_commune([_racine_domaine(doms.get(t)) for t in v])
        if par_nom or par_dom:
            continue
        log(f"  PLACEHOLDER {h[:12]} porte par {len(v)} societes sans lien : "
            f"{', '.join((noms.get(t) or t)[:18] for t in v[:5])}")
        for t in v:
            for base in (OUT_DIR, REPO_DIR):
                f = base / (t + ".png")
                if f.exists():
                    f.unlink()
                    efface += 1
    if efface:
        log(f"[StockLogos] {efface} pastille(s) ecartee(s) : image partagee par des "
            f"societes sans lien de nom NI de domaine")


def main():
    uni = json.loads(UNIVERSE.read_text())
    jobs = []                                          # (ticker, domaine, nom)
    seen = set()
    nodomain = []
    for z in ("us", "cn", "eu", "in"):
        for r in uni.get(z, {}).get("pool", []):
            t, d = r["t"], r.get("d")
            if t in seen: continue
            seen.add(t)
            if not d:
                # Un titre sans domaine etait SILENCIEUSEMENT ignore : ni logo, ni
                # entree au manifeste, ni trace. C'est ainsi que CXMT (1re capi
                # chinoise, cotee le 27/07/2026) s'est retrouvee sans logo sur la
                # carte sans que rien ne le signale. On le dit desormais.
                nodomain.append((t, r.get("n", "")))
                continue
            jobs.append((t, d, r.get("n", "")))
    # La collecte de marche sert 37 697 domaines mesures. Ils s'AJOUTENT au pool
    # ecrit a la main : celui-ci porte des corrections curees (domaines
    # alternatifs, ADR, classes de Berkshire) qu'aucune source automatique ne
    # retrouverait, et il garde donc la priorite sur un meme ticker.
    if SOURCE_MARCHE:
        avant = len(jobs)
        for t, d, n in jobs_marche():
            if t in seen:
                continue
            seen.add(t)
            jobs.append((t, d, n))
        log("[StockLogos] +%d tickers depuis la collecte de marche" % (len(jobs) - avant))

    # Tickers du calendrier des resultats absents du pool (ADR, hors seuil, BRKa/b).
    earn_extra = earnings_jobs(seen)
    # Alias d'abord : une societe deja bakee sous un autre ticker n'a rien a
    # resoudre, et son PNG existant vaut mieux qu'une 2e tentative qui echoue.
    alias_ok = set(apply_aliases())
    for t, d, n in earn_extra:
        seen.add(t)
        if t in alias_ok:
            continue
        if d:
            jobs.append((t, d, n))
        else:
            nodomain.append((t, n))
    log(f"[StockLogos] {len(jobs)} tickers, sortie {OUT_DIR}")
    if nodomain:
        log(f"[StockLogos] {len(nodomain)} SANS DOMAINE -> aucun logo possible, a "
            f"renseigner dans stock_universe.json (pool) ou EARNINGS_DOMAINS (resultats) :")
        for t, n in nodomain[:40]:
            log(f"  ! {t} ({n})")
        if len(nodomain) > 40:
            log(f"  ... et {len(nodomain) - 40} autres")

    failed_mem = load_failed()
    if failed_mem:
        log(f"[StockLogos] {len(failed_mem)} ticker(s) en echec recent -> non retentes "
            f"(TTL {FAIL_TTL_DAYS} j ; --retry-failed pour forcer)")
    ok, ko = [], []
    def work(job):
        t, d, n = job
        path = OUT_DIR / (_nom_fichier(t) + ".png")
        if t in TV_SOURCED and logo_exists(t) and not FORCE_REFETCH:
            ok.append(t); return          # pastille TradingView deja bakee : on n'y touche pas
        if ONLY_MISSING and logo_exists(t):
            ok.append(t); return
        if ONLY_MISSING and t in failed_mem:
            return
        im = resolve(t, d, n)
        if im and bake_hash(im) in BAD_HASHES:
            # Mieux vaut PAS de logo qu'un faux logo : la carte affiche alors le
            # label du titre, ce qui reste juste.
            log(f"  REJET image generique pour {t} ({d})")
            im = None
        if im:
            bake(im, path); ok.append(t)
        else:
            ko.append((t, d))

    # Douze bras suffisaient pour mille cinq cents titres. Pour onze mille, la
    # tentation est d'en mettre plus — le cout est le reseau, pas le calcul.
    # Mesure du 27/08/2026 sous WSL : a vingt-quatre fils, le resolveur NAT
    # (172.22.240.1) rend la main, `getent hosts` echoue, et le collecteur
    # s'arrete a deux mille pastilles SANS RIEN DIRE — chaque echec de
    # resolution ressemble a un domaine sans favicon. Le run stoppe, la
    # resolution revient dans la seconde. On descend donc a huit : plus lent,
    # mais il va au bout, et un run qui s'arrete en silence ne vaut rien.
    # En mode masse chaque societe coute UNE requete a un seul hote, dont
    # l'adresse est en cache : le resolveur ne voit presque plus rien passer,
    # et on peut remonter. La cascade complete reste a huit fils.
    _bras = 20 if RAPIDE else (8 if len(jobs) > 3000 else 12)
    with ThreadPoolExecutor(max_workers=_bras) as ex:
        list(ex.map(work, jobs))

    # Manifest = UNIQUEMENT les PNG reellement presents sur disque. Les tickers du
    # calendrier des resultats SANS domaine y figurent quand meme si un PNG existe
    # (logo depose a la main) : sinon le widget afficherait un monogramme alors que
    # l'image est la.
    mapping = {t: 1 for t, _, _ in jobs if logo_exists(t)}
    mapping.update({t: 1 for t, _, _ in earn_extra if logo_exists(t)})
    # Les alias sont bakes d'avance, y compris pour une orthographe de ticker qui
    # n'apparait pas dans le calendrier D'AUJOURD'HUI (BRK.A/BRK.B ne surgissent
    # que les jours ou le repli Nasdaq prend la main). Sans cette ligne le PNG
    # existe mais le manifeste l'ignore, et le widget affiche un monogramme.
    mapping.update({t: 1 for t in EARNINGS_ALIAS if logo_exists(t)})
    # GARDE-FOU (2026-08-02) : on repart aussi du manifeste precedent, pour tout
    # ticker dont le PNG est TOUJOURS sur disque. Sans ca, un run ou la liste
    # d'entree est incomplete — calendrier des resultats illisible (TCC sur
    # ~/Desktop), pool en cours de regeneration — reecrivait un manifeste appauvri
    # et faisait disparaitre des logos deja bakes. La regle « manifeste = PNG
    # reellement present » reste vraie : c'est logo_exists qui tranche.
    for t in list(read_manifest()):
        if t not in mapping and logo_exists(t):
            mapping[t] = 1
    MANIFEST.write_text("window.__STOCK_LOGOS__=" + json.dumps(mapping, separators=(",", ":")) + ";\n")
    save_failed(failed_mem, [t for t, _ in ko])
    log(f"[StockLogos] OK {len(mapping)}/{len(jobs)} | echecs {len(ko)}")
    for t, d in ko[:40]: log(f"  KO {t} ({d})")
    if len(ko) > 40:
        log(f"  ... et {len(ko) - 40} autres echecs")
    log(f"[StockLogos] wrote {MANIFEST.name}")

    # ── Detection des faux logos : images IDENTIQUES sur plusieurs titres ────
    # Un placeholder generique (icone "document" d'un CMS, globe DDG…) se trahit
    # en revenant a l'identique pour des domaines sans rapport. Le partage est en
    # revanche LEGITIME au sein d'un groupe (Adani, Nestle SA / Nestle India, ABB
    # Suisse / ABB Inde, Zijin Mining / Zijin Gold) : on ne rejette donc rien
    # automatiquement, on SIGNALE, et un vrai placeholder rejoint BAD_HASHES.
    import hashlib, collections
    by_hash = collections.defaultdict(list)
    for t, _, _ in jobs:
        f = OUT_DIR / (_nom_fichier(t) + ".png")
        if not f.exists():
            f = REPO_DIR / (_nom_fichier(t) + ".png")
        if f.exists():
            by_hash[hashlib.md5(f.read_bytes()).hexdigest()].append(t)
    shared = {h: v for h, v in by_hash.items() if len(v) > 1}
    _ecarter_placeholders(by_hash)
    if shared:
        log(f"[StockLogos] {len(shared)} image(s) partagee(s) par plusieurs titres "
            f"(normal en groupe, SUSPECT sinon) :")
        for h, v in sorted(shared.items(), key=lambda x: -len(x[1]))[:25]:
            log(f"  {len(v)}x {h[:12]} : {', '.join(v[:8])}")
        if len(shared) > 25:
            log(f"  ... et {len(shared) - 25} autres groupes")

if __name__ == "__main__":
    main()
