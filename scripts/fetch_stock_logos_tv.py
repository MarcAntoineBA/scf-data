#!/usr/bin/env python3
"""Logos actions — source PRIMAIRE : les logos vectoriels TradingView, par place et code.

POURQUOI (2026-08-10, demande user : « les logos des cartes Chine, Europe et Inde sont
moins nombreux et moins esthetiques que ceux de la carte US »)
------------------------------------------------------------------------------------
La cascade historique (fetch_stock_logos.py) part du DOMAINE de l'entreprise : DDG,
favicon du site, <link rel=icon>, Wikidata, Google s2. Elle tient aux Etats-Unis et
s'effondre ailleurs, pour des raisons qui ne sont PAS reparables en ajoutant des
sources du meme genre :
  - le domaine du pool est parfois mort ou faux (sinopec.com ne resout meme pas en DNS,
    powergrid.in ne repond pas depuis la France, trentlimited.com renvoie 404 partout) ;
  - quand il repond, on recupere une icone de 16 a 32 px, floue une fois agrandie ;
  - beaucoup de sites asiatiques servent un mot-logo horizontal illisible en vignette.
Mesure avant : logos presents us 303/306, cn 273/330, in 308/330 ; et parmi les presents,
des dizaines d'icones 16 px agrandies.

TradingView publie pour chaque titre cote un logo VECTORIEL, indexe par (place, code
boursier) — donc sans ambiguite d'homonyme, et rasterisable a la taille qu'on veut.
Mesure apres : 1288/1296 tickers, en 128 px nets.

CHAINE
------
1. Resolution  : symbol_search v3 -> `logoid`. On n'accepte QUE si le code rendu est
                 exactement le notre ET que `source_id` est la place attendue.
2. SVG         : s3-symbol-logo.tradingview.com/<logoid>--big.svg (cache disque).
3. Rasterisation: aucun rasteriseur SVG sur ce Mac (ni cairosvg, ni rsvg, ni inkscape)
                 -> Chrome headless, par PLANCHES de 64 logos (une capture pour 64,
                 pas un lancement de Chrome par logo).
4. Normalisation: pastille RONDE quand la vignette est pleine (la carte dessine des
                 bulles : un carre de couleur dedans fait autocollant), rognage +
                 recentrage sinon, et disque clair derriere une marque sombre sans fond
                 (invisible sur le fond sombre de la carte).
5. Poids        : palette 128 couleurs avec alpha quand elle est plus legere -> le
                 dossier passe de 8,8 Mo (64 px) a 3,7 Mo (128 px).

Usage :
    /usr/local/bin/python3 fetch_stock_logos_tv.py            # incrementiel
    …  --refresh        # rejoue la resolution TradingView (ignore le cache d'ids)
    …  --only ZONE[,…]  # limite aux zones (us,cn,eu,in)
A relancer apres chaque regeneration du pool (build_stock_universe.py), AVANT
fetch_stock_logos.py --only-missing qui comble ce que TradingView ne couvre pas.

PIEGE INTERPRETEUR : lancer avec /usr/local/bin/python3 (celui qui porte curl_cffi),
pas le python3 d'anaconda. Cf project_yahoo_curl_cffi_required.
"""
import base64, json, math, os, re, shutil, subprocess, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path

from curl_cffi import requests
from PIL import Image, ImageDraw

SITE = Path.home() / "Desktop" / "Site_Crypto_Finance"
UNIVERSE = SITE / "scripts" / "SiteCryptoFinance" / "stock_universe.json"
REPO_DIR = SITE / "stock_logos"
MANIFEST = SITE / "stock_logos_manifest.js"
TV_MAP = SITE / "stock_logos_tv.json"          # ticker -> logoid : audit + garde-fou
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
SVG_DIR = CACHE_DIR / "tv_svg"
IDS_CACHE = CACHE_DIR / "tv_logoids.json"

SIZE = 128            # cote du PNG final (la carte dessine jusqu'a ~100 px reels en plein ecran)
CELL, COLS, ROWS = 160, 8, 8          # planche de rasterisation
REFRESH = "--refresh" in sys.argv
ZONES = ["us", "cn", "eu", "in"]
for a in sys.argv[1:]:
    if a.startswith("--only"):
        ZONES = (a.split("=", 1)[1] if "=" in a else sys.argv[sys.argv.index(a) + 1]).split(",")

TV_EX = {'.HK': 'HKEX', '.SS': 'SSE', '.SZ': 'SZSE', '.NS': 'NSE', '.BO': 'BSE',
         '.L': 'LSE', '.SW': 'SIX', '.PA': 'EURONEXT', '.AS': 'EURONEXT',
         '.BR': 'EURONEXT', '.LS': 'EURONEXT', '.DE': 'XETR', '.F': 'FWB',
         '.MC': 'BME', '.MI': 'MIL', '.CO': 'OMXCOP', '.ST': 'OMXSTO',
         '.OL': 'OSL', '.HE': 'OMXHEX', '.VI': 'VIE', '.WA': 'GPW'}
US_SRC = {"NASDAQ", "NYSE", "AMEX", "BATS", "ARCA"}
HDR = {"Origin": "https://www.tradingview.com", "Referer": "https://www.tradingview.com/"}
EM = re.compile(r"</?em>")


def log(m):
    sys.stderr.write(m + "\n")
    sys.stderr.flush()


# ══ 1. RESOLUTION ═══════════════════════════════════════════════════════════
def variants(t):
    """Codes acceptables chez TradingView pour un ticker Yahoo, + place attendue.

    Trois ecarts d'ecriture rencontres : Hong Kong sans zeros de tete (0700 -> 700),
    le LSE qui suffixe d'un point (BP.L -> « BP. »), et les lignes A/B ecrites avec
    un point ou un tiret bas selon la place (NOVO-B -> NOVO_B, BT-A -> BT.A).
    """
    dot = t.rfind(".")
    base, suf = (t[:dot], t[dot:]) if dot >= 0 else (t, "")
    if suf == ".HK":
        base = base.lstrip("0") or base
    v = {base, base.replace("-", "."), base.replace("-", "_"), base.replace("-", "")}
    if suf == ".L":
        v.add(base + ".")
    return {x.upper() for x in v}, (TV_EX.get(suf) if suf else None)


def _search(txt, ex):
    u = ("https://symbol-search.tradingview.com/symbol_search/v3/"
         f"?text={txt}&exchange={ex or ''}&hl=1&lang=en&search_type=undefined&domain=production")
    for a in range(3):
        try:
            r = requests.get(u, impersonate="chrome120", timeout=20, headers=HDR)
            if r.status_code == 200:
                return r.json().get("symbols") or []
            time.sleep(2 + 2 * a)
        except Exception:
            time.sleep(1.5 + a)
    return []


def _pick(hits, want, src_ok):
    for h in hits:
        if EM.sub("", h.get("symbol") or "").upper() not in want:
            continue
        if not h.get("logoid") or h.get("type") not in ("stock", "dr", "fund"):
            continue
        if src_ok and (h.get("source_id") or "").upper() not in src_ok:
            continue
        return h
    return None


def resolve_one(job):
    t, name = job
    want, ex = variants(t)
    # Sans place connue (US), on interroge NASDAQ puis NYSE explicitement : sans filtre,
    # un code tres recherche (HON, DD) est noye dans les 50 resultats maximum.
    tries = ([(v, ex, {ex}) for v in sorted(want)] if ex
             else [(v, e, US_SRC) for v in sorted(want) for e in ("NASDAQ", "NYSE", "AMEX")])
    for txt, exq, src_ok in tries:
        h = _pick(_search(txt, exq), want, src_ok)
        if h:
            desc = EM.sub("", h.get("description") or "")
            return t, {"logoid": h["logoid"], "tv_name": desc, "source": h.get("source_id"),
                       "sim": round(SequenceMatcher(None, desc.lower(), (name or "").lower()).ratio(), 2)}
    return t, None


def resolve_all(pool):
    ids = {}
    if IDS_CACHE.exists() and not REFRESH:
        try:
            ids = json.loads(IDS_CACHE.read_text())
        except Exception:
            ids = {}
    todo = [(t, n) for t, n in pool if t not in ids]
    if todo:
        log(f"[TVLogos] resolution TradingView : {len(todo)} ticker(s) "
            f"({len(ids)} deja en cache)")
        done = 0
        with ThreadPoolExecutor(max_workers=5) as ex:
            for t, v in ex.map(resolve_one, todo):
                ids[t] = v or {"logoid": None}
                done += 1
                if done % 100 == 0:
                    log(f"    … {done}/{len(todo)}")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        IDS_CACHE.write_text(json.dumps(ids, ensure_ascii=False))
    # Audit : le nom TradingView doit ressembler au notre. Aucune exclusion
    # automatique (nos noms de pool sont tronques : « VGT » pour Victory Giant),
    # mais tout ecart est ECRIT pour etre relu.
    names = dict(pool)
    doubt = [(t, v) for t, v in ids.items()
             if v.get("logoid") and v.get("sim", 1) < 0.45 and t in names]
    if doubt:
        log(f"[TVLogos] {len(doubt)} correspondance(s) a relire (nom TV != notre nom) :")
        for t, v in sorted(doubt, key=lambda x: x[1]["sim"])[:30]:
            log(f"    {v['sim']:.2f} {t:14} nous={str(names[t])[:28]:30} TV={v['tv_name'][:38]}")
    return ids


# ══ 2. SVG ══════════════════════════════════════════════════════════════════
def fetch_svgs(logoids):
    SVG_DIR.mkdir(parents=True, exist_ok=True)

    def dl(lid):
        p = SVG_DIR / (lid.replace("/", "_") + ".svg")
        if p.exists() and p.stat().st_size > 80:
            return lid, True
        for url in (f"https://s3-symbol-logo.tradingview.com/{lid}--big.svg",
                    f"https://s3-symbol-logo.tradingview.com/{lid}.svg"):
            try:
                r = requests.get(url, impersonate="chrome120", timeout=20,
                                 headers={"Referer": "https://www.tradingview.com/"})
                if r.status_code == 200 and b"<svg" in r.content[:400]:
                    p.write_bytes(r.content)
                    return lid, True
            except Exception:
                pass
        return lid, False
    ko = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for lid, ok in ex.map(dl, sorted(logoids)):
            if not ok:
                ko.append(lid)
    if ko:
        log(f"[TVLogos] {len(ko)} SVG introuvable(s) : {', '.join(ko[:10])}")
    return ko


# ══ 3. RASTERISATION ════════════════════════════════════════════════════════
def chrome_bin():
    cands = list((Path.home() / "Library/Caches/ms-playwright").glob(
        "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"))
    cands.sort(reverse=True)
    cands.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def rasterize(jobs, tmp):
    """jobs : [(ticker, chemin_svg)] -> {ticker: Image RGBA CELLxCELL}."""
    chrome = chrome_bin()
    if not chrome:
        log("[TVLogos] ABANDON : aucun Chrome trouve pour rasteriser les SVG")
        return {}
    out = {}
    per = COLS * ROWS
    pages = [jobs[i:i + per] for i in range(0, len(jobs), per)]
    for pi, page in enumerate(pages):
        cells = []
        for t, svg in page:
            b64 = base64.b64encode(Path(svg).read_bytes()).decode()
            cells.append(f'<div class="c"><img src="data:image/svg+xml;base64,{b64}"></div>')
        rows = math.ceil(len(page) / COLS)
        html = (f'<!doctype html><meta charset="utf-8"><style>'
                f'html,body{{margin:0;padding:0;background:transparent}}'
                f'body{{display:grid;grid-template-columns:repeat({COLS},{CELL}px);'
                f'grid-auto-rows:{CELL}px}}'
                f'.c{{width:{CELL}px;height:{CELL}px;display:flex;align-items:center;'
                f'justify-content:center;overflow:hidden}}'
                f'img{{width:{CELL}px;height:{CELL}px;object-fit:contain}}'
                f'</style>{"".join(cells)}')
        hp = tmp / f"p{pi}.html"
        sp = tmp / f"p{pi}.png"
        hp.write_text(html)
        subprocess.run([chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000",
                        f"--window-size={COLS * CELL},{rows * CELL}",
                        f"--screenshot={sp}", f"file://{hp}"],
                       capture_output=True, timeout=120)
        if not sp.exists():
            log(f"[TVLogos] planche {pi} : capture manquante, ignoree")
            continue
        sheet = Image.open(sp).convert("RGBA")
        for i, (t, _) in enumerate(page):
            x, y = (i % COLS) * CELL, (i // COLS) * CELL
            out[t] = sheet.crop((x, y, x + CELL, y + CELL))
        log(f"[TVLogos] planche {pi + 1}/{len(pages)} rasterisee")
    return out


# ══ 4. NORMALISATION ════════════════════════════════════════════════════════
def _mask():
    ss = 4
    m = Image.new("L", (SIZE * ss, SIZE * ss), 0)
    ImageDraw.Draw(m).ellipse([0, 0, SIZE * ss - 1, SIZE * ss - 1], fill=255)
    return m.resize((SIZE, SIZE), Image.LANCZOS)


MASK = _mask()


def _trim(im, pad_ratio=0.02):
    bb = im.split()[3].point(lambda v: 255 if v > 8 else 0).getbbox()
    if not bb:
        return im
    im = im.crop(bb)
    w, h = im.size
    s = max(w, h)
    pad = int(s * pad_ratio)
    out = Image.new("RGBA", (s + 2 * pad, s + 2 * pad), (0, 0, 0, 0))
    out.paste(im, ((s - w) // 2 + pad, (s - h) // 2 + pad))
    return out


def normalise(cell):
    alpha = list(cell.split()[3].getdata())
    plein = sum(1 for v in alpha if v > 200) / len(alpha) > 0.93
    if plein:
        im = cell.resize((SIZE, SIZE), Image.LANCZOS)
        im.putalpha(MASK)
        return im, "pastille"
    im = _trim(cell).resize((SIZE, SIZE), Image.LANCZOS)
    px = [p for p in im.getdata() if p[3] > 120]
    lum = (sum(0.2126 * p[0] + 0.7152 * p[1] + 0.0722 * p[2] for p in px) / (255 * len(px))) if px else 1.0
    if lum < 0.26:
        bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(bg).ellipse([0, 0, SIZE - 1, SIZE - 1], fill=(238, 242, 247, 240))
        bg.alpha_composite(im)
        return bg, "contraste"
    return im, "detoure"


def save_png(im, path):
    """Ecrit la version la plus LEGERE entre RGBA 32 bits et palette 128 couleurs."""
    q = im.quantize(colors=128, method=Image.FASTOCTREE)
    with tempfile.TemporaryDirectory() as d:
        a, b = Path(d) / "a.png", Path(d) / "b.png"
        im.save(a, optimize=True)
        q.save(b, optimize=True)
        shutil.copyfile(b if b.stat().st_size < a.stat().st_size else a, path)


# ══ 5. MAIN ═════════════════════════════════════════════════════════════════
def main():
    uni = json.loads(UNIVERSE.read_text())
    pool, seen = [], set()
    for z in ZONES:
        for r in uni.get(z, {}).get("pool", []):
            if r["t"] not in seen:
                seen.add(r["t"])
                pool.append((r["t"], r.get("n", "")))
    log(f"[TVLogos] {len(pool)} tickers ({', '.join(ZONES)})")

    ids = resolve_all(pool)
    have = {t: v["logoid"] for t, v in ids.items() if v.get("logoid") and t in seen}
    log(f"[TVLogos] {len(have)}/{len(pool)} logos TradingView disponibles")

    fetch_svgs(set(have.values()))
    jobs = []
    for t, lid in sorted(have.items()):
        p = SVG_DIR / (lid.replace("/", "_") + ".svg")
        if p.exists():
            jobs.append((t, str(p)))

    REPO_DIR.mkdir(parents=True, exist_ok=True)
    kinds, written = {}, []
    with tempfile.TemporaryDirectory() as d:
        cells = rasterize(jobs, Path(d))
        for t, cell in cells.items():
            im, kind = normalise(cell)
            save_png(im, REPO_DIR / (t.replace(".", "_") + ".png"))
            kinds[kind] = kinds.get(kind, 0) + 1
            written.append(t)
    log(f"[TVLogos] {len(written)} PNG {SIZE}px ecrits · {kinds}")

    # Carte ticker -> logoid : trace d'audit ET garde-fou lu par fetch_stock_logos.py,
    # qui ne doit pas re-ecraser une pastille TradingView par un favicon 16 px.
    prev = {}
    if TV_MAP.exists():
        try:
            prev = json.loads(TV_MAP.read_text())
        except Exception:
            prev = {}
    prev.update({t: have[t] for t in written})
    blob = json.dumps(prev, indent=0, sort_keys=True)
    TV_MAP.write_text(blob)
    # Doublon dans Caches : sous launchd, la lecture de ~/Desktop peut etre refusee
    # (TCC). Sans cette copie, le garde-fou de fetch_stock_logos.py tomberait
    # silencieusement a vide et la cascade favicon pourrait ecraser les pastilles.
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / "stock_logos_tv.json").write_text(blob)
    except Exception:
        pass

    # Manifeste : union avec l'existant, regle inchangee = « le PNG est sur disque ».
    man = {}
    if MANIFEST.exists():
        try:
            man = json.loads(MANIFEST.read_text().split("=", 1)[1].strip().rstrip(";"))
        except Exception:
            man = {}
    for t in written:
        man[t] = 1
    man = {t: 1 for t in man if (REPO_DIR / (t.replace(".", "_") + ".png")).exists()}
    MANIFEST.write_text("window.__STOCK_LOGOS__=" + json.dumps(man, separators=(",", ":")) + ";\n")
    log(f"[TVLogos] manifeste : {len(man)} tickers")

    manque = [t for t, _ in pool if t not in have]
    if manque:
        log(f"[TVLogos] {len(manque)} sans logo TradingView (repli sur fetch_stock_logos.py "
            f"--only-missing) : {', '.join(manque)}")


if __name__ == "__main__":
    main()
