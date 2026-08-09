#!/usr/bin/env python3
"""Bake LOCAL des logos de venues perp — onglet Structure de marché, partie crypto.

Les 42 venues suivies par Loris sont pour l'essentiel des DEX perp récents. Aucune
n'a de logo dans les jeux d'icônes classiques, et CoinGecko n'en connaît que 13 :
son annuaire /exchanges recense des places d'échange, pas des protocoles.

MÊME DOCTRINE QUE fetch_stock_logos.py : on résout ICI, côté serveur, où les statuts
HTTP sont visibles, et on bake des PNG locaux versionnés. Un `<img>` ne peut pas voir
un 404 — il affiche le corps de la réponse, et un placeholder générique passe pour un
vrai logo. Le client n'a donc AUCUNE dépendance logo au runtime.

╔══════════════════════════════════════════════════════════════════════════════╗
║ La table est EXPLICITE, et c'est délibéré                                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
Apparier par ressemblance de nom produit des homonymes, et un homonyme est
indétectable une fois la page en ligne : le logo s'affiche, il est net, il est
simplement faux. DefiLlama recense quatre « Phoenix » et plusieurs « Paragon ».
Chaque venue est donc liée à une entrée NOMMÉE d'un registre, vérifiée à l'œil sur
la planche-contact (`--planche`), et non à un motif.

Deux registres, tous deux gratuits et sans clé :
  llama  → api.llama.fi/protocols   (champ `logo`) — couvre les DEX perp
  cg     → api.coingecko.com/api/v3/exchanges (champ `image`) — couvre les CEX

Validation avant bake : décodage PIL, ≥ 32 px de côté, ≥ 2 couleurs opaques
distinctes (un carré uni est un placeholder, pas un logo).

Sorties :
  Site_Crypto_Finance/venue_logos/<id>.png      64x64 RGBA, fond transparent
  Site_Crypto_Finance/venue_logos_manifest.js   window.__VENUE_LOGOS__ = {"binance":1,…}

Usage :
  python3 fetch_venue_logos.py              bake complet
  python3 fetch_venue_logos.py --planche    régénère la planche-contact d'audit
  python3 fetch_venue_logos.py --only-missing

Cadence hebdomadaire : le parc de venues bouge de quelques unités par trimestre,
et un logo de marque ne change presque jamais. Toute venue absente de la table est
signalée en fin d'exécution — c'est le seul moyen de voir arriver une nouveauté.
"""
import argparse
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

LLAMA = "https://api.llama.fi/protocols"
CG = "https://api.coingecko.com/api/v3/exchanges?per_page=250"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

SITE = Path.home() / "Desktop" / "Site_Crypto_Finance"
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
# Écriture TCC-safe, comme fetch_stock_logos.py : launchd n'a pas toujours le droit
# d'écrire dans ~/Desktop. On préfère le dépôt, on retombe sur les caches.
try:
    OUT = SITE / "venue_logos"
    OUT.mkdir(parents=True, exist_ok=True)
    MANIFEST = SITE / "venue_logos_manifest.js"
except OSError:
    OUT = CACHE_DIR / "venue_logos"
    OUT.mkdir(parents=True, exist_ok=True)
    MANIFEST = CACHE_DIR / "venue_logos_manifest.js"

# ── Table explicite : venue Loris → (registre, NOM EXACT dans ce registre) ────
# Vérifiée à l'œil le 2026-08-09 sur la planche-contact. Les deux entrées marquées
# « À REVOIR » ont bien été trouvées dans le registre mais leur logo ne ressemble
# pas à la marque attendue — homonyme probable. Elles sont volontairement laissées
# hors du bake : pas de logo vaut mieux qu'un faux logo, le monogramme de repli du
# front fera le travail.
TABLE = {
    "aster":       ("llama", "Aster Perps"),
    "binance":     ("cg",    "Binance"),
    "bingx":       ("llama", "BingX"),
    "bitget":      ("llama", "Bitget"),
    "bluefin":     ("llama", "Bluefin Spot"),
    "bullet":      ("llama", "Bullet Perps"),
    "bybit":       ("llama", "Bybit"),
    "coinbase":    ("cg",    "Coinbase International Exchange"),
    # Coinbase US et Coinbase International sont deux places de la MÊME maison, donc
    # une seule marque. CoinGecko sert pour l'une une vignette aplatie que le contrôle
    # anti-placeholder rejette — à juste titre. On réutilise le logo de l'autre plutôt
    # que d'aller chercher un visuel différent pour une entreprise qui n'en a qu'un.
    "coinbaseus":  ("alias", "coinbase"),
    "cryptocom":   ("llama", "Crypto-com"),
    "decibel":     ("llama", "Decibel"),
    "edgex":       ("llama", "edgeX Perps"),
    "ethereal":    ("llama", "Ethereal DEX"),
    "extended":    ("llama", "Extended Perps"),
    "gateio":      ("llama", "Gate Perp"),
    "grvt":        ("llama", "Grvt Perps"),
    "hibachi":     ("llama", "Hibachi"),
    "hotstuff":    ("llama", "Hotstuff Perps"),
    "huobi":       ("llama", "HTX"),
    "hyena":       ("llama", "HYENA"),
    "hyperliquid": ("llama", "Hyperliquid Perps"),
    "kalshi":      ("llama", "Kalshi"),
    "kinetiq":     ("llama", "Kinetiq Earn"),
    "kucoin":      ("llama", "KuCoin"),
    "lighter":     ("llama", "Lighter Perps"),
    "mexc":        ("llama", "MEXC"),
    "nado":        ("llama", "Nado Perps"),
    "okx":         ("llama", "OKX"),
    "ondo":        ("llama", "Ondo Perps"),
    "pacifica":    ("llama", "Pacifica Perps"),
    "paradex":     ("llama", "Paradex Perps"),
    "paragon":     ("llama", "Paragon"),
    "phemex":      ("llama", "Phemex"),
    "qfex":        ("llama", "QFEX"),
    "reya":        ("llama", "Reya Perps"),
    "risex":       ("llama", "RISEx"),
    "tradexyz":    ("llama", "tradeXYZ"),
    "variational": ("llama", "Variational"),
    "vest":        ("llama", "Vest Markets"),
    "woofipro":    ("llama", "WooFi Pro Perps"),
    # Deribit ne fait pas partie des venues de perps suivies par Loris : il est
    # ajouté ici pour le module d'options, qui a besoin de la même vignette.
    "deribit":     ("llama", "Deribit"),
    # À REVOIR — trouvés, mais le visuel ne correspond pas à la marque :
    # "phoenix": ("llama", "Phoenix DEX"),   volute bleue, quatre « Phoenix » au registre
    # "zo":      ("llama", "01"),            visuel photographique, pas une marque
}


def get_json(url, essais=3):
    """CoinGecko plafonne son palier gratuit et répond 429 par rafales. On patiente
    au lieu d'insister — et l'appelant sait encaisser un registre absent."""
    dernier = None
    for n in range(1, essais + 1):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=60).read())
        except Exception as e:                                    # noqa: BLE001
            dernier = e
            if n < essais:
                time.sleep(15 * n)
    raise RuntimeError(f"{url} injoignable : {dernier}")


def registre(nom, url, requis):
    """Charge un registre, ou rend un dictionnaire vide en le disant.

    Un registre muet ne doit JAMAIS faire tomber le collecteur : les venues qu'il
    sert gardent simplement le logo baké au passage précédent. Effacer un logo
    valide parce qu'un annuaire tiers a hoqueté serait la pire des réactions.
    """
    if not requis:
        return {}, None
    try:
        d = get_json(url)
        return ({e["name"]: e for e in d if isinstance(e, dict)}, None)
    except Exception as e:                                        # noqa: BLE001
        return {}, str(e)[:120]


def valider(data):
    """Renvoie l'image si elle est un vrai logo, sinon None avec la raison."""
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:                                        # noqa: BLE001
        return None, f"illisible ({type(e).__name__})"
    if min(im.size) < 32:
        return None, f"trop petit ({im.size[0]}x{im.size[1]})"
    rgba = im.convert("RGBA")
    couleurs = {p[:3] for p in rgba.getdata() if p[3] > 40}
    if len(couleurs) < 2:
        return None, "aplat uni (placeholder probable)"
    return rgba, None


def bake(rgba):
    """64x64, fond transparent, ratio conservé, centré."""
    rgba.thumbnail((64, 64), Image.LANCZOS)
    toile = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    toile.paste(rgba, ((64 - rgba.width) // 2, (64 - rgba.height) // 2), rgba)
    return toile


def planche(faits):
    """Planche-contact d'audit : le SEUL moyen de repérer un homonyme. Une table
    d'appariement ne se relit pas, elle se regarde."""
    cols, cell, pad, lbl = 8, 96, 14, 18
    lignes = (len(faits) + cols - 1) // cols
    from PIL import ImageDraw, ImageFont
    sheet = Image.new("RGB", (cols * (cell + pad) + pad,
                              lignes * (cell + pad + lbl) + pad), (14, 17, 22))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 11)
    except Exception:                                             # noqa: BLE001
        font = ImageFont.load_default()
    for i, vid in enumerate(sorted(faits)):
        x = pad + (i % cols) * (cell + pad)
        y = pad + (i // cols) * (cell + pad + lbl)
        im = Image.open(OUT / f"{vid}.png").convert("RGBA")
        fond = Image.new("RGBA", (cell, cell), (255, 255, 255, 10))
        fond.paste(im, ((cell - im.width) // 2, (cell - im.height) // 2), im)
        sheet.paste(fond.convert("RGB"), (x, y))
        dr.text((x, y + cell + 3), vid[:14], fill=(190, 198, 210), font=font)
    # La planche est un outil d'AUDIT, pas un actif du site : elle reste dans les
    # caches. Publiée, elle serait servie au visiteur pour rien et pèserait autant
    # que les quarante logos réunis.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chemin = CACHE_DIR / "venue_logos_planche_audit.png"
    sheet.save(chemin)
    return chemin


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--planche", action="store_true",
                    help="régénère seulement la planche-contact d'audit")
    args = ap.parse_args()

    if args.planche:
        faits = [p.stem for p in OUT.glob("*.png") if not p.stem.startswith("_")]
        print(f"planche : {planche(faits)} ({len(faits)} logos)")
        return 0

    # On ne sollicite un registre que si une venue à baker en dépend : CoinGecko
    # ne sert que trois entrées, il n'a pas à être appelé quand elles sont déjà là.
    a_faire = [(v, s, n) for v, (s, n) in sorted(TABLE.items())
               if not (args.only_missing and (OUT / f"{v}.png").exists())]
    besoin = {s for _, s, _ in a_faire}   # « alias » ne sollicite aucun registre

    print("registres…")
    llama, err_l = registre("llama", LLAMA, "llama" in besoin)
    cg, err_c = registre("cg", CG, "cg" in besoin)
    print(f"  DefiLlama {len(llama)} protocoles · CoinGecko {len(cg)} places")
    for nom_reg, err in (("DefiLlama", err_l), ("CoinGecko", err_c)):
        if err:
            print(f"  ! {nom_reg} muet ({err}) — ses venues gardent leur logo précédent")

    faits = [v for v in TABLE if (OUT / f"{v}.png").exists()]
    rates = []
    # Les alias passent en dernier : la source dont ils copient le logo doit exister.
    for vid, src, nom in sorted(a_faire, key=lambda t: t[1] == "alias"):
        cible = OUT / f"{vid}.png"
        if src == "alias":
            source = OUT / f"{nom}.png"
            if not source.exists():
                rates.append(f"{vid} : marque « {nom} » pas encore bakée")
                continue
            cible.write_bytes(source.read_bytes())
            if vid not in faits:
                faits.append(vid)
            continue
        entree = (llama if src == "llama" else cg).get(nom)
        if not entree:
            if cible.exists():
                continue          # registre muet : on conserve, sans bruit inutile
            rates.append(f"{vid} : « {nom} » absent de {src}")
            continue
        url = entree.get("logo") or entree.get("image")
        if not url:
            rates.append(f"{vid} : entrée sans logo")
            continue
        try:
            data = urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=30).read()
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{vid} : {type(e).__name__}")
            continue
        rgba, pourquoi = valider(data)
        if rgba is None:
            rates.append(f"{vid} : {pourquoi}")
            continue
        bake(rgba).save(cible)
        if vid not in faits:
            faits.append(vid)

    MANIFEST.write_text("window.__VENUE_LOGOS__=" +
                        json.dumps({v: 1 for v in sorted(faits)},
                                   separators=(",", ":")) + ";\n")
    print(f"\n{len(faits)}/{len(TABLE)} logos bakés → {OUT}")
    if rates:
        print("échecs :")
        for r in rates:
            print(f"  {r}")
    print(f"planche d'audit : {planche(faits)}")
    print("\nÀ REVOIR (hors table, homonymes probables) : phoenix, zo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
