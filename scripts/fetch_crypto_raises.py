#!/usr/bin/env python3
"""Cache « Capital-Risque crypto » — levées de fonds, secteurs, fonds, stades.

À QUOI ÇA SERT
Le site mesure la valorisation des narratives crypto (mcap, TVL, P/S, momentum) mais
ne voyait pas d'où vient l'argent PRIMAIRE : ce que les fonds déploient, dans quel
secteur, à quel stade. Ce flux-là précède les prix — un secteur qui capte soudain
30 % du capital déployé se lit ici des mois avant de se lire sur une capitalisation.
D'où ce collecteur, qui alimente le bloc « Capital-Risque » de l'onglet Analyse
Fondamentale · Narratives Crypto.

POURQUOI CETTE SOURCE, ET PAS UNE API
Toutes les API de fundraising crypto sont payantes, sans exception vérifiée le
2026-08-25 : RootData (payante), DefiLlama /raises (HTTP 402 depuis le passage au
plan Pro à 300 $/mois), CryptoRank (le plan gratuit exclut explicitement le
fundraising), Messari (Enterprise). CoinGecko / CoinMarketCap / Coinpaprika n'en
servent à aucun tier.

MAIS la page publique https://defillama.com/raises est une application Next.js, et
Next.js embarque les données de la page dans le HTML, balise `__NEXT_DATA__`. Le jeu
complet — 7 185 levées de 2014 à aujourd'hui — arrive donc en UNE requête HTTP, sans
clé, sans compte. C'est la même donnée que l'API payante sert, publiée par le même
éditeur sur sa propre page publique. Une requête par jour : moins que ce que coûte un
visiteur qui ouvre la page.

⚠ CE QUE CETTE SOURCE NE DIT PAS — à respecter dans l'affichage
  · tous les tours ne publient pas leur montant : ~18 % des lignes des 12 derniers
    mois n'en ont pas (mesuré 2026-08-25), et la proportion est bien pire sur
    l'historique ancien. On ne dit donc JAMAIS « montant levé » mais « montant
    DIVULGUÉ », et on publie la part divulguée (`disclosed_share_12m`) pour que le
    chiffre soit lisible pour ce qu'il est. Les compteurs de DEALS, eux, portent sur
    la totalité — c'est la mesure d'activité robuste, celle qu'un mégadeal ne
    déforme pas, et c'est pour ça que le bloc expose systématiquement les deux.
  · les valorisations n'existent que sur ~5 % des lignes : jamais un agrégat, juste
    une médiane indicative assortie de son effectif.
  · couverture asiatique plus faible que RootData. Sous-estimation, pas biais aléatoire.

GARDE-FOUS (deux pannes déjà vues ailleurs dans le parc)
  1. Un front qui change de structure ne doit pas VIDER la page : sous un seuil
     plancher de lignes, on sort en échec sans écrire — le cache de la veille tient.
  2. Deux chemins d'extraction, pas un : `__NEXT_DATA__` d'abord, puis la route
     `_next/data/<buildId>/raises.json` (le buildId est lu dans la page). Si Next.js
     bascule un jour en flux RSC, le second chemin prend le relais sans intervention.

Sortie : crypto_raises_cache.json + .js (window.__CRYPTO_RAISES__), ~90 Ko.
"""

import base64
import json
import re
import shutil
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "crypto_raises_cache.json"
OUT_JS = CACHE_DIR / "crypto_raises_cache.js"

PAGE = "https://defillama.com/raises"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-Raises/1.0"
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/json,*/*"}

# Plancher de vraisemblance : le jeu comptait 7 185 lignes le 2026-08-25 et ne fait
# que croître. Sous 4 000, ce n'est pas une collecte partielle, c'est une page qui a
# changé de forme — on refuse d'écrire plutôt que de publier un graphe amputé.
MIN_DEALS = 4000

MONTH_START = "2018-01"   # avant 2018 le jeu est trop clairsemé pour une série mensuelle
TOP_SECTORS = 14
TOP_INVESTORS = 20
TOP_CHAINS = 10
N_LATEST = 25

# Les 40 libellés de tours de DefiLlama, ramenés à des familles lisibles. Un stade dit
# où en est le CYCLE : un marché dominé par le pre-seed amorce, un marché dominé par
# les séries C+ et les tours de dette consolide.
#
# « Stratégique » et « Non précisé » sont SÉPARÉS, et ce n'est pas cosmétique : sur les
# 12 derniers mois ce sont les deux plus gros paquets (132 tours « Strategic » et 176
# lignes sans libellé au 2026-08-25). Les fondre en un « Autre » de 40 % des deals
# aurait donné une répartition par stade qui ne dit plus rien. Un tour stratégique est
# un fait — c'est un industriel qui entre au capital, pas un fonds ; une ligne sans
# libellé est une lacune de la source, et le graphe doit les distinguer.
STAGE_MAP = [
    (("pre-seed", "angel"), "Pre-Seed / Angel"),
    (("seed",), "Seed"),
    (("series a", "pre-series a", "series a+"), "Série A"),
    (("series b", "pre-series b", "series b1"), "Série B"),
    (("series c", "series d", "series e", "series f", "series c-1"), "Série C+"),
    (("token", "ico", "public sale", "private sale", "private token"), "Token sale / ICO"),
    (("debt", "loan", "convertible", "post-ipo", "ipo", "pre-ipo", "private equity",
      "secondary"), "Dette / Marchés publics"),
    (("grant", "ecosystem"), "Grant / Écosystème"),
    (("strategic", "corporate"), "Stratégique / Corporate"),
]
STAGE_ORDER = ["Pre-Seed / Angel", "Seed", "Série A", "Série B", "Série C+",
               "Token sale / ICO", "Stratégique / Corporate", "Grant / Écosystème",
               "Dette / Marchés publics", "Non précisé"]


def stage_of(round_label):
    r = (round_label or "").strip().lower()
    for keys, label in STAGE_MAP:
        for k in keys:
            if k in r:
                return label
    return "Non précisé"


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ── Logos de chaînes ─────────────────────────────────────────────────────────
# EMBARQUÉS EN data: URI dans le cache, pas référencés par URL. Deux raisons, et
# les deux sont des règles maison payées par des incidents :
#   · « logo local ou rien » — un service d'icônes externe qui ne connaît pas un
#     nom peut répondre 200 avec un placeholder générique, et la page affiche un
#     globe en croyant montrer une marque. Ici on vérifie le code ET la taille à
#     la COLLECTE : ce qui entre dans le cache a été vu.
#   · « ne jamais demander un fichier absent » — sur Cloudflare Pages un chemin
#     inconnu renvoie 200 + la page d'accueil (~540 Ko). Un logo manquant coûterait
#     un demi-mégaoctet avant même que `onerror` ne se déclenche. Embarqué, il n'y
#     a aucune requête à l'affichage, donc aucun trou possible.
# Coût mesuré : 10 chaînes ≈ 25 Ko de base64. Couverture mesurée : 10/10.
# (Les logos de PROJETS ont été écartés : 4 sur 25 seulement existent chez
# DefiLlama — les startups fraîchement financées n'y sont pas encore des
# protocoles. Une liste dont 21 lignes sur 25 tombent en repli n'est pas un
# ornement, c'est un défaut visible.)
def _slug(name):
    s = (name or "").lower().strip()
    s = re.sub(r"\s*\(.*?\)\s*", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


def chain_logo(name):
    url = "https://icons.llamao.fi/icons/chains/rsz_%s?w=48&h=48" % _slug(name)
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status != 200:
                return ""
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if not ctype.startswith("image/"):
                return ""
            raw = r.read()
            # Un « logo » de 40 octets n'est pas un logo : plancher explicite.
            if len(raw) < 100 or len(raw) > 60000:
                return ""
            return "data:%s;base64,%s" % (ctype, base64.b64encode(raw).decode("ascii"))
    except Exception:
        return ""


def load_raises():
    """Deux chemins d'extraction. Retourne (lignes, chemin_utilise)."""
    html = fetch(PAGE)

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            rows = data.get("props", {}).get("pageProps", {}).get("raises")
            if rows:
                return rows, "__NEXT_DATA__"
        except json.JSONDecodeError as e:
            sys.stderr.write("[warn] __NEXT_DATA__ illisible : %s\n" % e)

    b = re.search(r'"buildId":"([^"]+)"', html)
    if b:
        url = "https://defillama.com/_next/data/%s/raises.json" % b.group(1)
        try:
            rows = json.loads(fetch(url)).get("pageProps", {}).get("raises")
            if rows:
                return rows, "_next/data"
        except Exception as e:
            sys.stderr.write("[warn] route _next/data en echec : %s\n" % e)

    raise RuntimeError("aucun chemin d'extraction n'a rendu de lignes")


def usd(row):
    """Montant divulgué en USD, 0 si non divulgué."""
    v = row.get("amountUsd")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def build_payload(rows):
    now = datetime.now(timezone.utc)
    t_now = now.timestamp()
    d90 = t_now - 90 * 86400
    d12m = t_now - 365 * 86400
    d24m = t_now - 730 * 86400
    year_start = datetime(now.year, 1, 1, tzinfo=timezone.utc).timestamp()

    clean = []
    for r in rows:
        try:
            ts = float(r.get("date") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or ts > t_now + 86400 * 30:
            continue
        clean.append({
            "ts": ts,
            "name": (r.get("name") or "").strip(),
            "usd": usd(r),
            "round": (r.get("round") or "").strip(),
            "stage": stage_of(r.get("round")),
            "cat": (r.get("category") or "").strip() or "Non classé",
            "grp": (r.get("categoryGroup") or "").strip() or "Non classé",
            "chains": r.get("chains") or [],
            "lead": (r.get("leadInvestors") or []),
            "other": (r.get("otherInvestors") or []),
            "val": r.get("valuationUsd") or 0,
        })
    clean.sort(key=lambda x: x["ts"])

    def win(lo, hi=None):
        return [d for d in clean if d["ts"] >= lo and (hi is None or d["ts"] < hi)]

    r90, r12m, r24m, rytd = win(d90), win(d12m), win(d24m, d12m), win(year_start)

    def total(rs):
        return sum(d["usd"] for d in rs)

    def median(vals):
        v = sorted(x for x in vals if x > 0)
        if not v:
            return 0
        n = len(v)
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    disclosed_12m = [d for d in r12m if d["usd"] > 0]
    vals_12m = [d["val"] for d in r12m if d["val"]]

    kpi = {
        "raised_12m": round(total(r12m)),
        "deals_12m": len(r12m),
        "raised_12m_prev": round(total(r24m)),
        "deals_12m_prev": len(r24m),
        "raised_ytd": round(total(rytd)),
        "deals_ytd": len(rytd),
        "raised_90d": round(total(r90)),
        "deals_90d": len(r90),
        "median_deal_12m": round(median([d["usd"] for d in r12m])),
        "median_deal_12m_prev": round(median([d["usd"] for d in r24m])),
        "disclosed_share_12m": round(len(disclosed_12m) / len(r12m) * 100, 1) if r12m else 0,
        "median_valuation_12m": round(median(vals_12m)),
        "n_valuation_12m": len(vals_12m),
        "raised_all": round(total(clean)),
        "deals_all": len(clean),
    }

    # ── Série mensuelle : le robinet du capital ───────────────────────────────
    by_month = defaultdict(lambda: {"usd": 0.0, "n": 0, "nd": 0})
    for d in clean:
        key = datetime.fromtimestamp(d["ts"], timezone.utc).strftime("%Y-%m")
        if key < MONTH_START:
            continue
        b = by_month[key]
        b["usd"] += d["usd"]
        b["n"] += 1
        if d["usd"] > 0:
            b["nd"] += 1
    monthly = [{"m": k, "usd": round(v["usd"]), "n": v["n"], "nd": v["nd"]}
               for k, v in sorted(by_month.items())]
    # Le mois courant est incomplet par construction : on le marque au lieu de le
    # retirer (le retirer ferait croire à un effondrement du dernier point).
    cur = now.strftime("%Y-%m")
    for p in monthly:
        p["partial"] = 1 if p["m"] == cur else 0

    # ── Rotation sectorielle : 90 jours vs 12 mois ────────────────────────────
    # LE signal du bloc. Un secteur qui pèse 8 % du capital sur 12 mois et 22 % sur
    # 90 jours est en train de capter la rotation — et ça se voit ici avant de se
    # voir sur les prix. On publie capital ET nombre de deals : le premier bouge sur
    # un seul mégadeal, le second dit si c'est un mouvement de fond.
    def agg(rs, key="cat"):
        out = defaultdict(lambda: {"usd": 0.0, "n": 0})
        for d in rs:
            o = out[d[key]]
            o["usd"] += d["usd"]
            o["n"] += 1
        return out

    a90, a12 = agg(r90), agg(r12m)
    tot90, tot12 = total(r90) or 1, total(r12m) or 1
    n90, n12 = len(r90) or 1, len(r12m) or 1
    sectors = []
    for cat in set(a12) | set(a90):
        s90 = a90.get(cat, {"usd": 0, "n": 0})
        s12 = a12.get(cat, {"usd": 0, "n": 0})
        sectors.append({
            "name": cat,
            "usd_90d": round(s90["usd"]), "n_90d": s90["n"],
            "usd_12m": round(s12["usd"]), "n_12m": s12["n"],
            "share_90d": round(s90["usd"] / tot90 * 100, 2),
            "share_12m": round(s12["usd"] / tot12 * 100, 2),
            "nshare_90d": round(s90["n"] / n90 * 100, 2),
            "nshare_12m": round(s12["n"] / n12 * 100, 2),
        })
    for s in sectors:
        s["delta_pp"] = round(s["share_90d"] - s["share_12m"], 2)
        s["ndelta_pp"] = round(s["nshare_90d"] - s["nshare_12m"], 2)
    # On garde les plus gros sur 12 mois (base stable) — pas les plus gros sur 90 j,
    # qui feraient entrer un secteur né d'un seul deal et sortir un pilier.
    sectors.sort(key=lambda s: -s["usd_12m"])
    sectors = sectors[:TOP_SECTORS]

    groups = []
    ag12 = agg(r12m, "grp")
    for g, v in sorted(ag12.items(), key=lambda kv: -kv[1]["usd"]):
        groups.append({"name": g, "usd_12m": round(v["usd"]), "n_12m": v["n"]})

    # ── Stades : où en est le cycle ───────────────────────────────────────────
    st12, st24 = agg(r12m, "stage"), agg(r24m, "stage")
    stages = []
    for s in STAGE_ORDER:
        a, b = st12.get(s), st24.get(s)
        if not a and not b:
            continue
        stages.append({
            "name": s,
            "usd_12m": round(a["usd"]) if a else 0, "n_12m": a["n"] if a else 0,
            "usd_prev": round(b["usd"]) if b else 0, "n_prev": b["n"] if b else 0,
        })

    # ── Fonds : qui déploie encore ────────────────────────────────────────────
    # Un fonds actif sur 12 mois puis silencieux sur 90 jours s'est retiré ; c'est
    # une information au moins aussi utile que le classement lui-même. D'où les deux
    # fenêtres côte à côte, et la date du dernier deal connu.
    inv = defaultdict(lambda: {"n_all": 0, "n_12m": 0, "n_90d": 0, "lead_12m": 0,
                               "usd_12m": 0.0, "last": 0})
    for d in clean:
        pairs = [(x, True) for x in d["lead"]] + [(x, False) for x in d["other"]]
        for i, is_lead in pairs:
            name = (i or "").strip()
            if not name:
                continue
            o = inv[name]
            o["n_all"] += 1
            o["last"] = max(o["last"], d["ts"])
            if d["ts"] >= d12m:
                o["n_12m"] += 1
                o["usd_12m"] += d["usd"]
                if is_lead:
                    o["lead_12m"] += 1
            if d["ts"] >= d90:
                o["n_90d"] += 1
    investors = [{
        "name": k,
        "n_all": v["n_all"], "n_12m": v["n_12m"], "n_90d": v["n_90d"],
        "lead_12m": v["lead_12m"], "usd_12m": round(v["usd_12m"]),
        "last": datetime.fromtimestamp(v["last"], timezone.utc).strftime("%Y-%m-%d"),
    } for k, v in inv.items()]
    investors.sort(key=lambda x: (-x["n_12m"], -x["n_all"]))
    investors = investors[:TOP_INVESTORS]

    # ── Chaînes ───────────────────────────────────────────────────────────────
    ch = defaultdict(lambda: {"usd": 0.0, "n": 0})
    for d in r12m:
        for c in d["chains"]:
            o = ch[c]
            o["usd"] += d["usd"]
            o["n"] += 1
    chains = sorted(({"name": k, "usd_12m": round(v["usd"]), "n_12m": v["n"]}
                     for k, v in ch.items()), key=lambda x: -x["n_12m"])[:TOP_CHAINS]
    for c in chains:
        c["logo"] = chain_logo(c["name"])
    n_logos = sum(1 for c in chains if c["logo"])

    # ── Dernières levées ──────────────────────────────────────────────────────
    latest = [{
        "d": datetime.fromtimestamp(d["ts"], timezone.utc).strftime("%Y-%m-%d"),
        "name": d["name"], "usd": round(d["usd"]), "round": d["round"],
        "cat": d["cat"],
        "lead": d["lead"][0] if d["lead"] else "",
        "ni": len(d["lead"]) + len(d["other"]),
    } for d in clean[-N_LATEST:][::-1]]

    last_ts = clean[-1]["ts"] if clean else 0
    return {
        "meta": {
            "updated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "DefiLlama — page publique /raises",
            "source_url": PAGE,
            "deals_total": len(clean),
            "chain_logos": n_logos,
            "investors_total": len(inv),
            "last_deal": datetime.fromtimestamp(last_ts, timezone.utc).strftime("%Y-%m-%d") if last_ts else "",
            "note": ("Montants DIVULGUÉS uniquement : environ deux tours sur trois ne "
                     "publient pas leur montant. Les compteurs de deals portent sur la "
                     "totalité des lignes."),
        },
        "kpi": kpi,
        "monthly": monthly,
        "sectors": sectors,
        "groups": groups,
        "stages": stages,
        "investors": investors,
        "chains": chains,
        "latest": latest,
    }


def write_outputs(payload):
    OUT_JSON.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    OUT_JS.write_text(
        "/* crypto_raises_cache.js — generated %s */\nwindow.__CRYPTO_RAISES__ = %s;\n"
        % (payload["meta"]["updated_at"],
           json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    )
    # COPIE, jamais de lien symbolique — contrairement à d'autres collecteurs du
    # dépôt. Ces deux fichiers sont VERSIONNÉS dans Site_Crypto_Finance (comme
    # narratives_fundamentals_cache.js et ses voisins). Un symlink posé à leur
    # place fait basculer git en « typechange », et `auto_sync.sh`, qui committe
    # tout ce qu'il trouve toutes les 5 minutes, publierait un fichier de 72
    # octets à la place du cache. Mesuré le 2026-08-25 sur ce collecteur même.
    site_dir = Path.home() / "Desktop" / "Site_Crypto_Finance"
    if site_dir.exists():
        for name in ("crypto_raises_cache.json", "crypto_raises_cache.js"):
            dest, target = site_dir / name, CACHE_DIR / name
            try:
                if dest.is_symlink():
                    dest.unlink()
                shutil.copy2(target, dest)
            except OSError as e:
                sys.stderr.write("[COPIE %s] %s\n" % (name, e))


def main():
    t0 = time.time()
    try:
        rows, path = load_raises()
    except Exception as e:
        sys.stderr.write("[FATAL] extraction impossible : %s\n" % e)
        sys.exit(2)

    if len(rows) < MIN_DEALS:
        sys.stderr.write(
            "[GUARD] %d lignes < plancher %d — structure de page probablement "
            "changee, cache precedent conserve\n" % (len(rows), MIN_DEALS))
        sys.exit(1)

    payload = build_payload(rows)
    if payload["kpi"]["deals_12m"] == 0:
        sys.stderr.write("[GUARD] aucune levee sur 12 mois — refus d'ecrire\n")
        sys.exit(1)

    write_outputs(payload)
    k = payload["kpi"]
    sys.stdout.write(
        "[crypto_raises] OK · %d levees via %s · 12m : %d deals / %.1f Md$ · %.1fs · %s\n"
        % (len(rows), path, k["deals_12m"], k["raised_12m"] / 1e9,
           time.time() - t0, OUT_JSON))


if __name__ == "__main__":
    main()
