#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CAPTATION DE VALEUR, ÂGE ET HISTOIRE — le socle des fiches crypto détaillées.

POURQUOI CE COLLECTEUR EXISTE
------------------------------
Les fiches crypto jugeaient chaque jeton sur des multiples — prix/revenus,
capi/TVL — et se taisaient sur la seule question qui décide de la valeur d'un
jeton : EST-CE QUE CE QUE LE PROTOCOLE ENCAISSE ARRIVE JUSQU'AU JETON ?

La mesure montre que la question n'est pas rhétorique. Le cache amont
`narratives_fundamentals_cache` appelle « revenus » ce que DeFiLlama sert sous
`dataType=dailyFees` : ce que les UTILISATEURS PAIENT. Ce n'est pas ce que le
protocole garde, et encore moins ce qui revient au détenteur du jeton. Trois
grandeurs distinctes, que DeFiLlama publie séparément :

    dailyFees           ce que les utilisateurs versent          (le haut)
    dailyRevenue        ce que le protocole garde                (le milieu)
    dailyHoldersRevenue ce qui revient aux détenteurs du jeton   (le bas)

Le cas qui décide de la conception est Akash — déjà celui qui avait décidé de
la note par famille : 1,93 M$ de frais payés, zéro revenu protocole, zéro pour
le détenteur. Tout va aux fournisseurs de machines. Un « prix/revenus » y est
muet, mais la CHAÎNE de captation, elle, raconte exactement ce qui se passe :
les utilisateurs paient, et le jeton n'en voit rien.

Bitcoin dit la même chose autrement : 82,3 M$ de frais par an, qui rémunèrent
les mineurs (le budget de sécurité). Aucun mécanisme ne les reverse au
détenteur. BTC ne vaut pas pour ce qu'il capte — il vaut pour sa rareté. La
fiche doit pouvoir dire les deux, et ne peut le dire que si on mesure les deux.

CE QU'IL AJOUTE, ET POUR QUI
-----------------------------
1. LA CHAÎNE DE CAPTATION (les trois étages ci-dessus, par jeton), d'où
   dérivent deux taux qui, eux, se comparent entre jetons :
     - taux de captation  = revenu protocole / frais payés
     - part du détenteur  = revenu détenteurs / frais payés
     - rendement de captation = revenu détenteurs / capitalisation
   Le dernier est le seul homogène à un rendement d'action : ce que rapporte
   un dollar investi, hors mouvement de cours.

2. L'ÂGE ET L'HISTOIRE. Un jeton qui a traversé un cycle n'est pas un jeton
   listé il y a six mois, et c'est vrai SURTOUT là où il n'y a rien d'autre à
   mesurer : sur un memecoin, l'ancienneté et la survie sont les seules
   grandeurs fondamentales disponibles. On collecte donc la date de genèse,
   la première cotation observée, la date du plus haut et du plus bas.

3. L'HISTORIQUE DE COURS du top 200, pour que la fiche crypto porte le même
   graphe que la fiche action — avec ses horizons et sa régression.

MÉTHODE, ET CE QU'ELLE REFUSE DE FAIRE
---------------------------------------
Aucune grandeur n'est inventée. Quand une source se taît, le champ reste nul
et la fiche le déclare non publié — jamais zéro. La distinction est capitale
ici : « ce protocole ne reverse rien » (mesuré : holders_revenue = 0 alors que
fees > 0) et « on ne sait pas ce qu'il reverse » (DeFiLlama ne publie pas la
série) sont deux phrases différentes, et les confondre ferait passer un réseau
non mesuré pour un réseau qui ne partage rien.
"""

import gzip
import http.client
import json
import os
import sys
import time
import math
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

RACINE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("SCF_CACHE", RACINE)
TOP_N = int(os.environ.get("SCF_TOP_CRYPTO", "200"))
# L'historique de cours coûte un appel par jeton, et CoinGecko limite le débit
# du palier gratuit. On le rend débranchable pour pouvoir rafraîchir la
# captation seule, qui ne demande que trois appels.
AVEC_HIST = os.environ.get("SCF_CAPTURE_HIST", "1") not in ("0", "", "non")
# ⚠ TRENTE-SIX MOIS N'EXISTENT PLUS SUR LE PALIER GRATUIT.
# Mesuré le 03/09/2026, sans autre trafic concurrent :
#     days=max  → HTTP 401      days=3650 → HTTP 401
#     days=1825 → HTTP 401      days=730  → HTTP 401
#     days=365  → OK, 366 points
# CoinGecko a fait passer le plein historique derrière un abonnement. On prend
# donc ce qui reste servi, et — c'est le point important — la fiche ne PRÉTEND
# pas avoir davantage : le sélecteur d'horizon n'offre que ce que la série
# couvre, et l'ancienneté comme les cycles se calculent sur la GENÈSE publiée
# par `/coins/{id}`, pas sur le premier point de cette série d'un an. Sans quoi
# Bitcoin serait déclaré vieux d'un an.
HIST_JOURS = os.environ.get("SCF_CAPTURE_HIST_JOURS", "365")
# L'HISTOIRE LONGUE (voir la section dédiée, plus bas) coûte le TRANSFERT du
# détail quotidien — mesuré : 26,6 Mo pour dailyFees, 22,3 pour dailyRevenue,
# 10,6 pour dailyHoldersRevenue — et une centaine de requêtes de chaînes. On
# la débranche pour rafraîchir la captation seule.
HISTOIRE = os.environ.get("SCF_CAPTURE_HISTOIRE", "1") not in ("0", "", "non")

UA = "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0; +https://github.com)"
CG = "https://api.coingecko.com/api/v3"
DL = "https://api.llama.fi"


def _get(url, timeout=60, essais=4, pause=2.0, pause_debit=35.0):
    """GET JSON, avec la patience qu'impose le palier gratuit de CoinGecko.

    Le 429 n'est pas une erreur à retenter tout de suite : la fenêtre de débit
    est d'une minute. On attend franchement plutôt que de brûler les essais.

    ⚠ LA COMPRESSION N'EST PAS UN CONFORT, C'EST CE QUI REND LA COLLECTE
    POSSIBLE. `urllib` n'annonce AUCUN `Accept-Encoding`, et DeFiLlama sert
    alors le JSON brut. Mesuré le 05/09/2026 sur /overview/fees?dataType=
    dailyFees, depuis cette machine :
        sans en-tête   26 693 845 octets sur le fil ;
        Accept-Encoding: gzip
                        6 372 277 octets sur le fil — corps identique une fois
                       décompressé.
    Mesuré sur les trois séries du bulk, le même jour :
        dailyFees             6 372 277 sur le fil / 26 693 845 décompressés
        dailyRevenue          4 447 113            / 22 321 506
        dailyHoldersRevenue   1 351 822            / 10 666 313
    Soit 12,2 Mo transférés au lieu de 59,7 : quatre fois moins de données.

    ⚠ CE QUE CETTE MESURE NE DIT PAS, ET QU'UNE PREMIÈRE RÉDACTION AFFIRMAIT.
    Elle annonçait « sept minutes sans finir, puis IncompleteRead » comme un
    fait établi. Une relecture a rejoué le même appel par le même chemin de code,
    quatre fois : 3,6 s, 15,4 s, 3,4 s, 4,6 s, corps complet à chaque essai,
    aucune troncature. L'échec observé pendant l'écriture était donc lié à
    l'état du réseau de ce moment-là, pas au poids en soi.
    Ce qui reste vrai et suffit : quatre fois moins d'octets à transférer, et
    une fenêtre de troncature d'autant plus étroite. Ce qui n'est pas vrai :
    prétendre que sans gzip la collecte ne peut pas aboutir. Un chiffre non
    reproductible dans un commentaire est un chiffre qui sera cru.

    ⚠ ET LA TRONCATURE SE RATTRAPE, ELLE NE SE DEVINE PAS. Un corps découpé
    en `chunks` peut s'arrêter en route : le serveur a répondu 200, on a lu la
    moitié du JSON, et `json.loads` échoue sur une erreur de syntaxe qui ne
    dit rien de la vraie cause. On attrape donc `IncompleteRead` sous son nom,
    pour que l'avertissement nomme la panne — et parce qu'elle mérite un
    nouvel essai, contrairement à un JSON réellement mal formé.
    """
    dernier = None
    for i in range(essais):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                brut = r.read()
                if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                    brut = gzip.decompress(brut)
                return json.loads(brut.decode("utf-8"))
        except urllib.error.HTTPError as e:
            dernier = e
            if e.code == 429:
                time.sleep(pause_debit)
                continue
            # 404 sur un jeton : la source ne le connaît pas. Inutile d'insister.
            if e.code in (400, 404):
                return None
            time.sleep(pause * (i + 1))
        except (http.client.IncompleteRead, EOFError, gzip.BadGzipFile) as e:
            # Corps tronqué en vol : on a bien parlé au bon serveur, c'est le
            # transfert qui a lâché. Réessayer a un sens.
            dernier = "corps tronqué (%s)" % type(e).__name__
            time.sleep(pause * (i + 1))
        except Exception as e:
            dernier = e
            time.sleep(pause * (i + 1))
    print("[warn] GET %s : %s" % (url[:110], dernier), file=sys.stderr)
    return None


def lire_cache(nom):
    """Rend l'objet d'un cache .js ou .json du dépôt.

    Repris de `fetch_crypto_fiches.py` : plusieurs caches ne sont pas
    `window.X = {...}` mais une IIFE, et le préfixe n'est donc pas fiable.
    """
    chemin = os.path.join(CACHE, nom)
    if not os.path.exists(chemin):
        return None
    t = open(chemin, encoding="utf-8").read()
    if nom.endswith(".json"):
        try:
            return json.loads(t)
        except Exception:
            return None
    depart = max(t.find("="), t.find("(function"))
    i = t.find("{", depart if depart > 0 else 0)
    if i < 0:
        return None
    prof, j, dans, ech = 0, i, False, False
    while j < len(t):
        c = t[j]
        if ech:
            ech = False
        elif c == "\\":
            ech = True
        elif c == '"':
            dans = not dans
        elif not dans:
            if c == "{":
                prof += 1
            elif c == "}":
                prof -= 1
                if prof == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except Exception:
                        return None
        j += 1
    return None


# ─── La jointure DeFiLlama → CoinGecko ────────────────────────────────────
# Reprise de `fetch_narratives_fundamentals.build_dlid_to_gecko`, et pour la
# raison qui y est écrite : seuls ~30 % des protocoles DeFiLlama portent un
# `gecko_id` direct. Sans la remontée par parent et par slug, AAVE V3 — le
# gros du revenu d'Aave — serait ignoré au profit de V2, cent fois plus petit.
CATEGORIES_EXCLUES = {"CEX", "Chain"}

PARENT_VERS_GECKO = {
    "parent#jito": "jito-governance-token",
    "parent#ether-fi": "ether-fi",
    "parent#stader": "stader-labs",
    "parent#pyth": "pyth-network",
    "parent#chainlink": "chainlink",
    "parent#pancakeswap": "pancakeswap-token",
    "parent#aerodrome": "aerodrome-finance",
    "parent#raydium": "raydium",
    "parent#meteora": "meteora",
    "parent#fluid": "instadapp",
    "parent#drift": "drift-protocol",
    "parent#gmx": "gmx",
    "parent#lighter": "lighter",
    "parent#pump": "pump-fun",
    "parent#bonkfun": "bonk",
    "parent#sky": "sky",
    "parent#maker": "sky",
    "parent#ena": "ethena",
    "parent#ethena": "ethena",
    "parent#ondo-finance": "ondo-finance",
    "parent#hyperliquid": "hyperliquid",
    "parent#morpho": "morpho",
    "parent#pendle": "pendle",
    "parent#kamino": "kamino",
    "parent#jupiter": "jupiter-exchange-solana",
    "parent#curve-finance": "curve-dao-token",
    "parent#aave": "aave",
    "parent#uniswap": "uniswap",
    "parent#lido": "lido-dao",
    "parent#thorchain": "thorchain",
    "parent#aerodrome-finance": "aerodrome-finance",
    "parent#syrup": "syrup",
    "parent#maple": "syrup",
    "parent#venice": "venice-token",
    "parent#virtuals": "virtual-protocol",
}

SLUG_VERS_GECKO = {
    "uniswap": "uniswap", "uniswap-labs": "uniswap", "uniswap-v2": "uniswap",
    "uniswap-v3": "uniswap", "uniswap-v4": "uniswap",
    "aave": "aave", "aave-v1": "aave", "aave-v2": "aave", "aave-v3": "aave",
    "compound": "compound-governance-token", "compound-v3": "compound-governance-token",
    "compound-finance": "compound-governance-token",
    "curve": "curve-dao-token", "curve-finance": "curve-dao-token",
    "curve-dex": "curve-dao-token",
    "pancakeswap-amm": "pancakeswap-token", "pancakeswap": "pancakeswap-token",
    "pancakeswap-amm-v3": "pancakeswap-token",
    "morpho-blue": "morpho", "morpho-aave-v3": "morpho", "morpho": "morpho",
    "aerodrome-slipstream": "aerodrome-finance", "aerodrome-v1": "aerodrome-finance",
    "raydium": "raydium", "raydium-amm": "raydium", "raydium-clmm": "raydium",
    "jupiter": "jupiter-exchange-solana", "jupiter-aggregator": "jupiter-exchange-solana",
    "jupiter-perpetual-exchange": "jupiter-exchange-solana",
    "balancer": "balancer", "balancer-v2": "balancer", "balancer-v3": "balancer",
    "1inch-aggregation-protocol": "1inch", "1inch-network": "1inch",
    "ethena": "ethena", "ethena-usde": "ethena",
    "frax-finance": "frax-share", "frax-ether": "frax-share", "frax": "frax-share",
    "rocket-pool": "rocket-pool", "lido": "lido-dao", "convex-finance": "convex-finance",
    "jito": "jito-governance-token", "jito-restaking": "jito-governance-token",
    "jito-liquid-staking": "jito-governance-token",
    "pyth-network": "pyth-network", "chainlink-ccip": "chainlink",
    "chainlink-data-feeds": "chainlink", "chainlink": "chainlink",
    "liquity": "liquity", "liquity-v2": "liquity",
    "ondo-finance": "ondo-finance", "ondo-flux-finance": "ondo-finance",
    "sky-lending": "sky", "sky": "sky", "sky-money": "sky", "sky-rwa": "sky",
    "makerdao": "sky",
    "pendle": "pendle", "kamino-lend": "kamino", "kamino": "kamino",
    "hyperliquid-spot-orderbook": "hyperliquid", "hyperliquid-perps": "hyperliquid",
    "thorchain": "thorchain", "gmx-v1": "gmx", "gmx-v2": "gmx",
    "lighter": "lighter", "aster": "aster-2", "edgex": "edgex",
    "derive": "derive", "synthetix": "havven", "ether-fi-stake": "ether-fi",
    "ether-fi-liquid": "ether-fi", "eigenlayer": "eigenlayer",
    "the-graph": "the-graph", "arweave": "arweave", "filecoin": "filecoin",
    "akash-network": "akash-network", "render-network": "render-token",
    "grass": "grass", "bittensor": "bittensor", "helium": "helium",
    "pump-fun": "pump-fun", "meteora": "meteora", "orca": "orca",
    "ens": "ethereum-name-service", "sushiswap": "sushi",
    "spark": "sky", "usual": "usual", "resolv": "resolv",
    "falcon-finance": "falcon-finance-ff", "stables-labs-usdx": "stable-2",
}

# Les chaînes : leur revenu natif ne vient pas du bulk /overview/fees (qui le
# rend partiel — mesuré dans le collecteur amont : ETH 129 M$ contre 1 156 M$
# réels) mais de l'endpoint dédié par slug.
#
# ⚠ UNE CHAÎNE PAR JETON, ET RIEN D'AUTRE. Une première version rattachait
# Base, Linea, Scroll et Blast à `ethereum`, faute de jeton propre. Mesuré :
# ETH recevait 275 M$ de frais dont 137 venaient de Base et 11,6 de Linea —
# la moitié de sa « captation » appartenait à des chaînes dont les frais
# reviennent à Coinbase et à ConsenSys, pas au détenteur d'ETH. Le collecteur
# amont porte le même avertissement, à la même ligne.
#
# Ces chaînes ne sont donc PAS collectées : leurs frais n'ont pas de jeton à
# créditer. Ce n'est pas une perte d'information — c'est le refus d'en
# fabriquer une fausse.
CHAINES = {
    "ethereum": ("ethereum", "Ethereum"),
    "solana": ("solana", "Solana"),
    "bsc": ("binancecoin", "BSC"),
    "tron": ("tron", "Tron"),
    "arbitrum": ("arbitrum", "Arbitrum"),
    "optimism": ("optimism", "OP Mainnet"),
    "polygon": ("polygon-ecosystem-token", "Polygon"),
    "avalanche": ("avalanche-2", "Avalanche"),
    "sui": ("sui", "Sui"),
    "aptos": ("aptos", "Aptos"),
    "near": ("near", "Near"),
    "ton": ("the-open-network", "TON"),
    "sei": ("sei-network", "Sei"),
    "celestia": ("celestia", "Celestia"),
    "starknet": ("starknet", "Starknet"),
    "stellar": ("stellar", "Stellar"),
    "xrpl": ("ripple", "Ripple"),
    "canton": ("canton-network", "Canton"),
    "hyperliquid-l1": ("hyperliquid", "Hyperliquid L1"),
    "hedera": ("hedera-hashgraph", "Hedera"),
    "algorand": ("algorand", "Algorand"),
    "cosmos": ("cosmos", "Cosmos"),
    "polkadot": ("polkadot", "Polkadot"),
    "cardano": ("cardano", "Cardano"),
    "bitcoin": ("bitcoin", "Bitcoin"),
    "litecoin": ("litecoin", "Litecoin"),
    "bitcoin-cash": ("bitcoin-cash", "Bitcoin Cash"),
    "monero": ("monero", "Monero"),
    "zcash": ("zcash", "Zcash"),
    "tezos": ("tezos", "Tezos"),
    # ⚠ `dogecoin` et `kaspa` rendent un 500 sur cet endpoint : DeFiLlama ne
    # publie pas de série de frais pour ces chaînes. Les laisser dans la table
    # coûtait douze appels perdus et douze avertissements par passage, sans
    # jamais rien rapporter. Leur captation reste donc non publiée — ce qui
    # est la vérité, et non un zéro.
    "injective": ("injective-protocol", "Injective"),
    "mantle": ("mantle", "Mantle"),
    "zksync-era": ("zksync", "zkSync Era"),
    "sonic": ("sonic-3", "Sonic"),
    "berachain": ("berachain-bera", "Berachain"),
    "plasma": ("plasma", "Plasma"),
    "monad": ("monad", "Monad"),
}


def construire_dlid(protocoles):
    """dlid (str) -> gecko_id, par résolution directe → parent → slug."""
    parent = {}
    for p in protocoles:
        par, gid = p.get("parentProtocol"), p.get("gecko_id")
        if par and gid and par not in parent:
            parent[par] = gid
    for par, gid in PARENT_VERS_GECKO.items():
        parent.setdefault(par, gid)

    out = {}
    for p in protocoles:
        if (p.get("category") or "") in CATEGORIES_EXCLUES:
            continue
        did = p.get("id")
        if did is None:
            continue
        gid = p.get("gecko_id")
        if not gid:
            par = p.get("parentProtocol")
            if par:
                gid = parent.get(par)
        if not gid:
            gid = SLUG_VERS_GECKO.get((p.get("slug") or "").lower())
        if gid:
            out[str(did)] = gid
    return out


def somme_serie(dtype, dlid, paquet=None):
    """gecko_id -> total 1 an de la série demandée, pour les PROTOCOLES.

    Somme et non maximum : chaque version d'un protocole contribue au même
    jeton. L'entrée de chaîne est écartée ici — elle est traitée à part, son
    total dans le bulk étant partiel.

    `paquet` : la réponse déjà téléchargée quand l'histoire longue a demandé
    la version COMPLÈTE de cette même requête. Elle porte exactement les mêmes
    totaux, plus le détail quotidien : les deux paramètres `exclude…` ne
    retirent que les graphes. Sans ce partage, un passage complet
    téléchargerait deux fois les 59 Mo des trois séries.
    """
    d = paquet
    if d is None:
        d = _get(DL + "/overview/fees?dataType=%s"
                      "&excludeTotalDataChart=true"
                      "&excludeTotalDataChartBreakdown=true" % dtype)
    if not d:
        return {}, {}
    out, detail = {}, {}
    for p in (d.get("protocols") or []):
        did = str(p.get("defillamaId") or "")
        if did.startswith("chain#") or p.get("category") == "Chain":
            continue
        gid = dlid.get(did)
        if not gid:
            continue
        v = p.get("total1y")
        if v is None or v <= 0:
            # Un protocole lancé il y a trois mois n'a pas de total1y. Son
            # rythme sur trente jours annualisé vaut mieux qu'un silence, mais
            # il est marqué comme tel pour que la fiche puisse le dire.
            r30 = p.get("total30d") or 0
            if r30 > 0:
                v = r30 * 365.0 / 30.0
                detail.setdefault(gid, {})["annualise"] = True
        if v is None or v <= 0:
            continue
        out[gid] = out.get(gid, 0.0) + v
        detail.setdefault(gid, {}).setdefault("lignes", []).append(
            {"nom": p.get("name"), "slug": p.get("slug"),
             "cat": p.get("category"), "usd": int(round(v))})
    return out, detail


def somme_chaines(dtype):
    """gecko_id -> total 1 an de la série, pour le NATIF des chaînes."""
    out, detail = {}, {}
    for slug, (gid, web) in CHAINES.items():
        d = _get(DL + "/overview/fees/%s?excludeTotalDataChart=true"
                      "&excludeTotalDataChartBreakdown=true&dataType=%s"
                 % (slug, dtype), timeout=40)
        if not d:
            continue
        natif, entree = 0.0, None
        for p in (d.get("protocols") or []):
            pid = str(p.get("defillamaId") or "")
            if pid == "chain#" + slug or (p.get("name") or "").lower() == slug.lower():
                natif = p.get("total1y") or 0
                if natif <= 0:
                    r30 = p.get("total30d") or 0
                    if r30 > 0:
                        natif = r30 * 365.0 / 30.0
                entree = p
                break
        if natif <= 0:
            # Stellar, XRPL : le natif EST le seul « protocole » de la chaîne.
            natif = d.get("total1y") or 0
            if natif <= 0:
                r30 = d.get("total30d") or 0
                if r30 > 0:
                    natif = r30 * 365.0 / 30.0
        if natif > 0:
            out[gid] = out.get(gid, 0.0) + natif
            detail.setdefault(gid, {}).setdefault("lignes", []).append(
                {"nom": (entree.get("name") if entree else web) or web,
                 "slug": slug, "cat": "Chain", "usd": int(round(natif))})
        time.sleep(0.25)
    return out, detail


def fusion(proto, chaine):
    """La chaîne PRIME sur le protocole pour un même jeton.

    Pour un L1, seul le revenu de la chaîne est attribuable à son jeton : les
    frais d'Aave déployé sur Ethereum reviennent aux détenteurs d'AAVE, pas à
    ceux d'ETH. Le collecteur amont fait le même choix, pour la même raison.
    """
    out = dict(proto)
    out.update(chaine)
    return out


# ─── Le point de reprise ──────────────────────────────────────────────────
# Les deux passes CoinGecko font quatre cents appels sur un palier gratuit qui
# limite à trente par minute — et qui, mesuré ici, répond souvent 429 bien
# avant. La collecte dure donc des heures, et perdre le tout parce qu'une
# coupure réseau survient à la trois-centième requête serait absurde.
#
# On écrit donc l'avancement au fur et à mesure dans un fichier de reprise, et
# on le relit au démarrage : un second passage ne redemande que ce qui manque.
# C'est aussi ce qui rend le collecteur relançable sans scrupule.
REPRISE = os.path.join(CACHE, ".crypto_capture_reprise.json")


def _lire_reprise():
    if not os.path.exists(REPRISE):
        return {"identite": {}, "cours": {}}
    try:
        d = json.load(open(REPRISE, encoding="utf-8"))
        return {"identite": d.get("identite") or {},
                "cours": d.get("cours") or {}}
    except Exception:
        return {"identite": {}, "cours": {}}


def _ecrire_reprise(etat):
    tmp = REPRISE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(etat, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, REPRISE)
    except Exception as e:
        print("[warn] point de reprise non écrit : %s" % e, file=sys.stderr)


# ─── L'âge, et ce qu'il faut pour le dire ────────────────────────────────
def fiche_identite(ids, etat=None):
    """gecko_id -> {genesis, ath_date, atl_date, ath, atl, …}

    Un appel par jeton (`/coins/{id}`), c'est le seul endroit où CoinGecko
    publie `genesis_date`. On coupe les blocs inutiles de la réponse pour ne
    pas transporter des mégaoctets de tickers.
    """
    out = dict((etat or {}).get("identite") or {})
    reste = [c for c in ids if c not in out]
    if out:
        print("[cg] identité : %d déjà connues, %d à collecter"
              % (len(out), len(reste)))
    n = len(reste)
    for i, cid in enumerate(reste, 1):
        d = _get(CG + "/coins/" + urllib.parse.quote(cid) +
                 "?localization=false&tickers=false&market_data=true"
                 "&community_data=true&developer_data=true&sparkline=false",
                 timeout=45)
        if not d:
            time.sleep(2.5)
            continue
        md = d.get("market_data") or {}

        def _u(bloc, dev="usd"):
            v = (md.get(bloc) or {})
            v = v.get(dev)
            return v if isinstance(v, (int, float)) else None

        dev = d.get("developer_data") or {}
        com = d.get("community_data") or {}
        out[cid] = {
            "genesis": d.get("genesis_date"),
            "pays": d.get("country_origin") or None,
            "categories": [c for c in (d.get("categories") or []) if c],
            "ath": _u("ath"),
            "ath_date": ((md.get("ath_date") or {}).get("usd")),
            "atl": _u("atl"),
            "atl_date": ((md.get("atl_date") or {}).get("usd")),
            "ath_chg_pct": _u("ath_change_percentage"),
            "atl_chg_pct": _u("atl_change_percentage"),
            # Le rang du jeton par capitalisation selon la source elle-même :
            # utile pour repérer un jeton que notre univers classe très
            # différemment (signe d'une capitalisation périmée d'un côté).
            "rang_cg": d.get("market_cap_rank"),
            # L'activité de développement. Sur un jeton sans revenus ni TVL,
            # c'est parfois la seule mesure d'activité réelle qui existe.
            "dev_etoiles": dev.get("stars"),
            "dev_forks": dev.get("forks"),
            "dev_contributeurs": dev.get("pull_request_contributors"),
            "dev_commits_4s": dev.get("commit_count_4_weeks"),
            "dev_pr_fusionnees": dev.get("closed_issues"),
            "com_twitter": com.get("twitter_followers"),
            "com_reddit": com.get("reddit_subscribers"),
            # `watchlist_portfolio_users` mesure l'attention portée au jeton
            # sur CoinGecko : ni un fondamental, ni du bruit — un signal de
            # notoriété, qu'on étiquette comme tel dans la fiche.
            "suivi_cg": d.get("watchlist_portfolio_users"),
        }
        if i % 10 == 0 or i == n:
            print("[cg] identité %d/%d" % (i, n), flush=True)
            if etat is not None:
                etat["identite"] = out
                _ecrire_reprise(etat)
        time.sleep(2.5)   # palier gratuit : ~30 appels/minute annoncés
    if etat is not None:
        etat["identite"] = out
        _ecrire_reprise(etat)
    return out


def historique_cours(ids, etat=None):
    """gecko_id -> [[ts_ms, prix], …] quotidien, toute la vie cotée.

    `days=max&interval=daily` rend un point par jour depuis la première
    cotation. C'est ce qui permet à la fiche de proposer les mêmes horizons
    que la fiche action — jusqu'à « Max », qui est ici la vraie information :
    le jeton a-t-il déjà vécu un cycle ?
    """
    out = dict((etat or {}).get("cours") or {})
    reste = [c for c in ids if c not in out]
    if out:
        print("[cg] historique : %d déjà connus, %d à collecter"
              % (len(out), len(reste)))
    n = len(reste)
    for i, cid in enumerate(reste, 1):
        d = _get(CG + "/coins/" + urllib.parse.quote(cid) +
                 "/market_chart?vs_currency=usd&days=%s&interval=daily" % HIST_JOURS,
                 timeout=60)
        if d and isinstance(d.get("prices"), list) and d["prices"]:
            pts = [[int(p[0]), round(float(p[1]), 8)]
                   for p in d["prices"]
                   if isinstance(p, list) and len(p) >= 2
                   and isinstance(p[1], (int, float)) and math.isfinite(p[1])]
            if pts:
                out[cid] = pts
        if i % 10 == 0 or i == n:
            print("[cg] historique %d/%d (%d servis)" % (i, n, len(out)), flush=True)
            if etat is not None:
                etat["cours"] = out
                _ecrire_reprise(etat)
        time.sleep(2.5)
    if etat is not None:
        etat["cours"] = out
        _ecrire_reprise(etat)
    return out



# ══════════════════════════════════════════════════════════════════════════
#  L'HISTOIRE LONGUE — le compte de résultat pluriannuel d'un jeton
# ══════════════════════════════════════════════════════════════════════════
# CE QUE CE BLOC RÉPARE
# ---------------------
# `crypto_fiches.js` ne portait AUCUNE série datée : zéro tableau daté sur
# deux cents jetons, là où une fiche action en porte jusqu'à trente-quatre
# exercices. La seule série existante — le cours — s'arrête à 366 jours,
# plafond du palier gratuit de CoinGecko (cf. HIST_JOURS plus haut).
#
# LA CAUSE N'ÉTAIT PAS LA SOURCE : C'EST LE COLLECTEUR QUI DEMANDAIT À LA
# SOURCE DE NE PAS LUI ENVOYER L'HISTOIRE. `somme_serie` et `somme_chaines`
# passent `excludeTotalDataChart` et `excludeTotalDataChartBreakdown`, puis ne
# lisent que `total1y`. Mesuré le 05/09/2026, la même requête SANS ces deux
# paramètres rend 26 652 301 octets en 2,6 s : le détail QUOTIDIEN par
# protocole sur 3 085 jours depuis le 2018-03-26. La réponse porte 2 655
# protocoles, dont 2 188 apparaissent dans le détail quotidien, et AUCUN nom
# du détail n'est sans fiche protocole — la jointure par nom est totale.
# Zéro requête de plus : on ne paie que le transfert.
#
# CE QU'ON PUBLIE, ET POURQUOI MENSUEL
# -------------------------------------
# Le quotidien sur 3 085 jours × 200 jetons × 3 étages ne tient pas dans une
# page : on agrège au MOIS. Un flux (frais, revenu, revenu détenteurs) est
# SOMMÉ sur le mois — c'est un compte de résultat. Un stock (TVL, cours) prend
# la DERNIÈRE valeur du mois — c'est un bilan. Confondre les deux donnerait
# une TVL cumulée qui n'existe nulle part.
#
# Le mois COURANT est incomplet par construction : le document le nomme
# (`mois_incomplet`) au lieu de le retrancher, pour que la fiche le grise
# plutôt que de faire croire à un effondrement du dernier mois.
#
# LE CONTRÔLE QUI PROUVE QUE LES DATES NE SONT PAS DÉCALÉES
# ----------------------------------------------------------
# Une série datée fausse d'un jour reste plausible : rien ne la trahit à l'œil.
# On publie donc, pour chaque flux, de quoi RECALCULER le total annuel que la
# source annonce, et l'écart mesuré.
#
# Mesuré : `total1y` est la somme des 365 jours se terminant LA VEILLE du
# dernier point du graphe global. Sur les 577 protocoles au-dessus d'un
# million de dollars, l'écart médian avec cette fenêtre est de 0,000 % et
# l'écart maximal de 1,3e-14 % — la réconciliation est exacte à la précision
# machine. Décalée d'un seul jour, la même fenêtre donne 0,285 % d'écart
# médian et fait sortir 17 protocoles de la barre des 2 %. C'est ce contraste
# qui fait du contrôle un vrai garde-fou et non une formalité.
#
# La fenêtre de 365 jours ne coïncide pas avec des mois entiers. On publie
# donc les deux BORDS (la fin du premier mois, le début du dernier) : le
# vérificateur additionne bord + mois entiers du tableau publié + bord, et
# retrouve `total1y` exactement. Sans ces deux nombres, le contrôle serait
# obligé de comparer douze mois civils à une fenêtre glissante — deux choses
# différentes, dont l'écart légitime aurait masqué un vrai décalage.

JOUR = 86400

# ⚠ DEUX ENDPOINTS, DEUX DICTIONNAIRES DE SLUGS.
# Le natif d'une chaîne (ses frais à elle, pas ceux des protocoles qui
# tournent dessus) se lit sur /summary/fees/{slug}. Mesuré : 35 des 37
# chaînes de CHAINES répondent, `optimism` et `cosmos` rendent HTTP 400
# « not found » — alors que /overview/fees/{slug}, utilisé par
# `somme_chaines`, les accepte tous les deux. Sans ces deux corrections, OP
# et ATOM auraient été les seuls jetons de chaîne sans histoire, et rien ne
# l'aurait signalé.
SLUG_SOMMAIRE = {
    "optimism": "op-mainnet",
    "cosmos": "cosmoshub",
}

# ⚠ UNE CHAÎNE QUE `CHAINES` A DÛ ABANDONNER, ET QUE CELLE-CI RÉCUPÈRE.
# `CHAINES` a retiré `dogecoin` parce que /overview/fees/dogecoin rend un 500.
# Mesuré : le slug de DeFiLlama n'est pas `dogecoin` mais `doge`, et
# /summary/fees/doge rend 4 607 jours depuis le 2014-01-23, pour 504 635 $ de
# frais sur un an. `CHAINES` n'est pas touchée — elle alimente un autre cache,
# dont les valeurs sont déjà publiées et lues ailleurs — mais l'histoire, elle,
# n'a aucune raison de se priver de douze ans de frais de Dogecoin.
#
# Akash, lui, reste muet, et ce n'est pas un oubli : /summary/fees/akash-network
# rend 400 et /overview/fees/akash-network rend 500. Ses 1,93 M$ de frais
# annuels existent bien dans la liste du bulk (entrée `chain#akash-network`),
# mais AUCUNE série datée n'est servie — pas plus par le détail quotidien, où
# son nom n'apparaît jamais. On le déclare non publié plutôt que d'inventer.
CHAINES_HISTOIRE = {
    "doge": ("dogecoin", "Dogecoin"),
}


def _chaines_histoire():
    d = dict(CHAINES)
    d.update(CHAINES_HISTOIRE)
    return d


def _jour_iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def _sig(x, n=6):
    """Arrondit à n chiffres significatifs.

    POURQUOI PAS UN NOMBRE FIXE DE DÉCIMALES. Les grandeurs publiées vont du
    cours d'un memecoin (0,00000522 $ pour SHIB) à la TVL d'Ethereum
    (49 163 M$) : dix ordres de grandeur. Un arrondi à deux décimales
    écraserait le premier à zéro — une donnée FABRIQUÉE, celle qui dirait
    « ce jeton ne vaut rien » —, et un arrondi à dix décimales ferait porter
    au cache dix chiffres de bruit sur la seconde. Six chiffres significatifs gardent le dixième de dollar sur un
    cours à cinq chiffres et la centaine de milliers de dollars sur une TVL de
    49 milliards. Mesuré : cet arrondi introduit au plus 0,0005 % d'écart sur
    le contrôle annuel des 208 séries de flux — trois ordres de grandeur sous
    le seuil de 2 % que le garde-fou tolère.
    """
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    if x == 0:
        return 0
    r = round(x, -int(math.floor(math.log10(abs(x)))) + (n - 1))
    return int(r) if r == int(r) and abs(r) < 1e15 else r


def _mensuel(quotidien, mode):
    """{'AAAA-MM-JJ': v} → {'AAAA-MM': v}, sommé (flux) ou DERNIER POINT du mois.

    ⚠ « DERNIER POINT DU MOIS » N'EST PAS « FIN DE MOIS », et l'écart n'est pas
    théorique. La grille de cours longue de `coins.llama.fi` ne sert que DEUX
    jours par mois : décembre 2017 ne contient que le 6 et le 21, et la valeur
    retenue est celle du 21. Mesuré sur Bitcoin, 79 % des points de cours sont
    donc des points de MILIEU de mois, pas de clôture — pour décembre 2017,
    16 355 $ au lieu du prix du 31.
    Cela ne fausse aucune courbe : une série de cours mensuelle échantillonnée
    au même rang chaque mois se lit exactement comme une série de fin de mois.
    Mais l'étiquette publiée doit dire ce que la donnée EST, sinon quelqu'un
    finira par recouper le point de décembre avec une clôture officielle et
    conclura à une erreur qui n'existe pas.
    """
    out = {}
    if mode == "somme":
        for j, v in quotidien.items():
            out[j[:7]] = out.get(j[:7], 0.0) + v
    else:
        dernier = {}
        for j, v in quotidien.items():
            m = j[:7]
            if m not in dernier or j > dernier[m]:
                dernier[m] = j
                out[m] = v
    return out


def _mois_suivant(m):
    a, mo = int(m[:4]), int(m[5:7])
    return "%04d-%02d" % (a + 1, 1) if mo == 12 else "%04d-%02d" % (a, mo + 1)


def _mois_precedent(m):
    a, mo = int(m[:4]), int(m[5:7])
    return "%04d-%02d" % (a - 1, 12) if mo == 1 else "%04d-%02d" % (a, mo - 1)


def _compacter(mensuel, echelle=1.0):
    """{'AAAA-MM': v} → {'debut': 'AAAA-MM', 'valeurs': [...]}.

    On ne publie pas les mois : on publie le PREMIER, et le lecteur avance
    d'un mois par case. Sur deux cents jetons et cinq séries, écrire chaque
    étiquette « 2019-07 » coûterait 10 octets par point pour une information
    qui se déduit. Les trous INTÉRIEURS restent des `null` — un mois où la
    source n'a rien publié n'est pas un mois à zéro.
    """
    if not mensuel:
        return None
    mois = sorted(mensuel)
    debut, fin = mois[0], mois[-1]
    valeurs, m = [], debut
    while True:
        v = mensuel.get(m)
        valeurs.append(_sig(v / echelle) if isinstance(v, (int, float)) else None)
        if m == fin:
            break
        m = _mois_suivant(m)
    return {"debut": debut, "valeurs": valeurs}


def _compacter_jours(quotidien, jours, fin_iso, echelle=1.0):
    """Les N derniers jours, en tableau plein calé sur une date de départ."""
    if not quotidien:
        return None
    fin = datetime.strptime(fin_iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    debut = fin - timedelta(days=jours - 1)
    valeurs, vus = [], 0
    for k in range(jours):
        j = (debut + timedelta(days=k)).strftime("%Y-%m-%d")
        v = quotidien.get(j)
        if v is None:
            valeurs.append(None)
        else:
            valeurs.append(_sig(v / echelle))
            vus += 1
    if not vus:
        return None
    return {"debut": debut.strftime("%Y-%m-%d"), "valeurs": valeurs}


def _controle_annuel(quotidien, total1y_source, jour_reference, echelle=1e6):
    """De quoi refaire le total annuel de la source depuis la série publiée.

    Rend la fenêtre exacte, les deux bords que la grille mensuelle ne peut pas
    exprimer, et l'écart mesuré. Voir l'en-tête de section : c'est ce bloc qui
    prouve qu'aucune date n'a glissé.
    """
    if not quotidien or not isinstance(total1y_source, (int, float)) or total1y_source <= 0:
        return None
    fin = datetime.strptime(jour_reference, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    debut = fin - timedelta(days=364)
    d_iso, f_iso = debut.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")
    somme = bord_d = bord_f = 0.0
    fin_mois_debut = _mois_suivant(d_iso[:7])
    for j, v in quotidien.items():
        if j < d_iso or j > f_iso:
            continue
        somme += v
        if j[:7] == d_iso[:7]:
            bord_d += v
        elif j[:7] == f_iso[:7]:
            bord_f += v
    return {
        "fenetre": [d_iso, f_iso],
        # Les mois ENTIERS de la fenêtre : ni celui du premier jour ni celui du
        # dernier, tous deux tronqués par les bornes. Ce sont ceux-là, et eux
        # seuls, que le vérificateur lit dans le tableau mensuel publié ; les
        # deux moitiés manquantes sont `bord_debut` et `bord_fin`.
        "premier_mois_entier": fin_mois_debut,
        "dernier_mois_entier": _mois_precedent(f_iso[:7]),
        "bord_debut": _sig(bord_d / echelle),
        "bord_fin": _sig(bord_f / echelle),
        "somme_serie": _sig(somme / echelle),
        "total1y_source": _sig(total1y_source / echelle),
        "ecart_pct": round(abs(somme - total1y_source) / total1y_source * 100.0, 4),
    }


# ─── Étage 1 : les trois flux, par protocole, sans une requête de plus ────
def flux_protocoles(paquet, dlid, vise=None):
    """Le détail quotidien du bulk /overview/fees, replié sur le gecko_id.

    Le détail est indexé par NOM d'affichage du protocole, pas par
    identifiant : on repasse donc par la liste `protocols` de la même réponse
    pour retrouver le `defillamaId`, puis la jointure `dlid`. Mesuré : les
    2 188 noms du détail ont tous leur fiche — la jointure par nom ne perd
    rien, et les noms sont uniques (2 655 protocoles, 2 655 noms distincts).

    Les entrées de CHAÎNE sont écartées ici, comme dans `somme_serie` et pour
    la même raison : le natif d'une chaîne se lit sur son endpoint dédié, le
    bulk le rend partiel.

    `vise` borne la construction aux jetons de l'univers. Ce n'est pas une
    optimisation de confort : sans elle on garderait 2 575 séries de 3 085
    jours en mémoire pour n'en publier que deux cents.
    """
    if not paquet:
        return {}, {}, {}, None
    fiches = {}
    for p in (paquet.get("protocols") or []):
        nom = p.get("name")
        if nom:
            fiches[nom] = p
    quotidien, total1y, hors_detail = {}, {}, {}
    presents = set()
    detail = paquet.get("totalDataChartBreakdown") or []
    for ligne in detail:
        if not isinstance(ligne, list) or len(ligne) < 2 or not isinstance(ligne[1], dict):
            continue
        j = _jour_iso(ligne[0])
        for nom, v in ligne[1].items():
            presents.add(nom)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                continue
            p = fiches.get(nom)
            if not p:
                continue
            did = str(p.get("defillamaId") or "")
            if did.startswith("chain#") or p.get("category") == "Chain":
                continue
            gid = dlid.get(did)
            if not gid or (vise is not None and gid not in vise):
                continue
            d = quotidien.setdefault(gid, {})
            d[j] = d.get(j, 0.0) + v
    # ⚠ ON NE COMPARE QUE CE QUI EST COMPARABLE.
    # Le total annuel du contrôle ne somme QUE les protocoles dont le détail
    # quotidien est réellement publié. Mesuré : Sushi Perps (294 289 $) et
    # Sushi Launchpad (377 170 $) annoncent un `total1y` sans jamais paraître
    # dans le détail. Les compter contre une série qui ne les contient pas
    # donnait 4,16 % d'écart sur SUSHI — le garde-fou sonnait pour une raison
    # qui n'a rien à voir avec un décalage de dates, c'est-à-dire pour rien.
    # Ce que la source annonce sans le détailler n'est pas perdu : il est
    # publié à part, dans `total1y_sans_detail`.
    for nom, p in fiches.items():
        did = str(p.get("defillamaId") or "")
        if did.startswith("chain#") or p.get("category") == "Chain":
            continue
        gid = dlid.get(did)
        if not gid or (vise is not None and gid not in vise):
            continue
        t = p.get("total1y")
        if not isinstance(t, (int, float)) or t <= 0:
            continue
        if nom in presents:
            total1y[gid] = total1y.get(gid, 0.0) + t
        else:
            hors_detail[gid] = hors_detail.get(gid, 0.0) + t
    # Le jour de référence du contrôle : la VEILLE du dernier point du graphe
    # global. Mesuré : c'est la fenêtre sur laquelle `total1y` est calculé,
    # exactement (écart médian 0,000 % sur 577 protocoles).
    chart = paquet.get("totalDataChart") or []
    ref = None
    if chart:
        ref = _jour_iso(chart[-1][0] - JOUR)
    return quotidien, total1y, hors_detail, ref


# ─── Étage 2 : les trois flux, pour le natif des chaînes ──────────────────
def flux_chaines(dtype, seulement=None):
    """gecko_id → série quotidienne du natif de la chaîne, et son total1y.

    /summary/fees/{slug} remonte BEAUCOUP plus loin que le bulk, qui démarre
    au 2018-03-26 : mesuré, Bitcoin rend 5 653 points depuis le 2011-01-31 et
    Ethereum 4 047 depuis le 2015-08-07. C'est la différence entre « ce jeton
    a une histoire » et « ce jeton a huit ans d'histoire ».

    Un HTTP 400 ici n'est pas une panne : il dit que la série n'existe pas
    pour cette chaîne (mesuré : dailyHoldersRevenue de Bitcoin). On le note
    comme non publié, on n'invente pas un zéro.
    """
    quotidien, total1y, muets = {}, {}, {}
    for slug, (gid, web) in _chaines_histoire().items():
        if seulement and gid not in seulement:
            continue
        api = SLUG_SOMMAIRE.get(slug, slug)
        d = _get(DL + "/summary/fees/%s?dataType=%s" % (api, dtype), timeout=45)
        if not d:
            muets[gid] = "série %s non publiée par DeFiLlama pour %s" % (dtype, web)
            continue
        chart = d.get("totalDataChart") or []
        if not chart:
            muets[gid] = "série %s vide pour %s" % (dtype, web)
            continue
        s = {}
        for pt in chart:
            if not isinstance(pt, list) or len(pt) < 2:
                continue
            v = pt[1]
            if isinstance(v, (int, float)) and math.isfinite(v):
                s[_jour_iso(pt[0])] = s.get(_jour_iso(pt[0]), 0.0) + v
        if s:
            quotidien[gid] = s
            t = d.get("total1y")
            if isinstance(t, (int, float)) and t > 0:
                total1y[gid] = t
        time.sleep(0.15)
    return quotidien, total1y, muets


# ─── Étage 3 : la TVL ─────────────────────────────────────────────────────
def tvl_chaines(seulement=None):
    """gecko_id → TVL quotidienne de la chaîne.

    Mesuré : 36 des 37 chaînes répondent, 1,8 Mo et 13 s au total — Ethereum
    seul rend 3 265 points depuis le 2017-09-27 pour 120 Ko. `monero` rend un
    404 : il n'y a pas de finance décentralisée sur Monero, et son absence est
    donc la vérité, pas une panne.
    """
    out, muets = {}, {}
    for slug, (gid, web) in _chaines_histoire().items():
        if seulement and gid not in seulement:
            continue
        d = _get(DL + "/v2/historicalChainTvl/" + slug, timeout=45)
        if not isinstance(d, list) or not d:
            muets[gid] = "TVL de chaîne non publiée pour %s" % web
            continue
        s = {}
        for pt in d:
            v = (pt or {}).get("tvl")
            if isinstance(v, (int, float)) and math.isfinite(v):
                s[_jour_iso(pt["date"])] = v
        if s:
            out[gid] = s
        time.sleep(0.15)
    return out, muets


def tvl_protocoles(cibles, protos, dlid):
    """TVL quotidienne des protocoles, pour le HAUT du classement seulement.

    ⚠ POURQUOI ON S'ARRÊTE AU TOP 50, ET AUX DÉPLOIEMENTS QUI PÈSENT.
    Il n'existe pas d'endpoint léger pour la TVL historique d'un protocole :
    /v2/historicalProtocolTvl rend 404, et `?excludeTokens=true` est ignoré.
    La seule voie est /protocol/{slug}, qui transporte aussi la composition
    en jetons de chaque jour. Mesuré : aave-v3 pèse 29,0 Mo, pumpswap 35,2 Mo,
    morpho-blue 30,7 Mo. Prendre les 116 protocoles du top 200 coûterait de
    l'ordre du gigaoctet par passage, pour une grandeur que la fiche n'affiche
    qu'en second rang.
    On se limite donc aux jetons du top 50 (mesuré : 11 d'entre eux sont des
    protocoles, 21 autres du haut de tableau étant des chaînes, déjà servies),
    et pour chacun aux déploiements pesant au moins 5 % de sa TVL actuelle —
    14 slugs, 139 Mo, 21 s. La part réellement couverte est publiée avec la
    série (`couverture_tvl_pct` : mesuré, 100 % pour Chainlink et Lido, 99,9 %
    pour Uniswap, 95,8 % pour Aave), pour que la fiche puisse dire « cette
    courbe couvre 96 % de la TVL du protocole » au lieu de laisser croire à un
    total. SCF_HISTOIRE_TVL_TOP descend plus bas dans le classement, en
    connaissance du prix.
    """
    par_gecko = {}
    for p in protos:
        gid = dlid.get(str(p.get("id")))
        if not gid or gid not in cibles:
            continue
        tvl = p.get("tvl")
        if not isinstance(tvl, (int, float)) or tvl <= 0:
            continue
        par_gecko.setdefault(gid, []).append((p.get("slug"), tvl))

    out, couverture = {}, {}
    for gid, lignes in par_gecko.items():
        total = sum(t for _, t in lignes)
        if total <= 0:
            continue
        gardes = [(s, t) for s, t in lignes if s and t >= 0.05 * total]
        if not gardes:
            continue
        s_acc, pris = {}, 0.0
        for slug, t in sorted(gardes, key=lambda x: -x[1]):
            d = _get(DL + "/protocol/" + urllib.parse.quote(slug), timeout=180)
            serie = (d or {}).get("tvl") or []
            if not serie:
                continue
            pris += t
            for pt in serie:
                v = (pt or {}).get("totalLiquidityUSD")
                if isinstance(v, (int, float)) and math.isfinite(v):
                    j = _jour_iso(pt["date"])
                    s_acc[j] = s_acc.get(j, 0.0) + v
            time.sleep(0.2)
        if s_acc:
            out[gid] = s_acc
            couverture[gid] = round(100.0 * pris / total, 1)
    return out, couverture


# ─── Étage 4 : le cours long, sans CoinGecko ──────────────────────────────
# ⚠ LE PIÈGE DE coins.llama.fi, ET CE QU'IL EST VRAIMENT.
# On lit souvent que l'endpoint rend `{"coins":{}}` — objet vide, code 200 —
# « quand start précède la naissance du jeton ». Mesuré, ce n'est pas ça :
#     hyperliquid, start=2010, span=160  →  {"coins":{}}
#     hyperliquid, start=2012, span=160  →  2 points
#     hyperliquid, start=2013, span=160  →  15 points
#     hyperliquid, start=2013, span=167  →  22 points, depuis le 2024-11-29
# L'API avance de `span` pas de `period` À PARTIR de `start` et ne rend que
# les cases où un prix existe. Ce n'est donc pas la naissance qui décide,
# c'est le fait que LA GRILLE ATTEIGNE LE PRÉSENT : trop courte, elle
# s'arrête avant la vie du jeton et rend zéro point sans le dire.
# La règle de construction est donc : start + span × period > aujourd'hui.
#
# Et le plafond est DUR : span × nombre de jetons ≤ 500 par requête. Mesuré
# au point de bascule — 2 jetons × span 250 = 500 → HTTP 200 ; 2 × 251 = 502
# → HTTP 400 ; 1 × 500 → HTTP 200 ; 1 × 501 → HTTP 400. D'où les paliers :
# plus le jeton est vieux, plus la grille est longue, moins on en met par
# requête.
#
# ⚠ ET POURQUOI UN PAS DE QUINZE JOURS, PAS DE TRENTE.
# Trente jours semblait suffire pour une série mensuelle. Mesuré sur Bitcoin,
# ça sautait des mois entiers : la grille passe du 2018-10-31 au 2018-12-01,
# et du 2019-01-30 au 2019-03-01. Un pas de trente jours ENJAMBE un mois de
# vingt-huit ou de trente jours. Novembre 2018 et février 2019 ressortaient
# donc à `null` — c'est-à-dire « la source ne publie pas », alors que la
# source publie et que c'est notre échantillonnage qui était trop lâche. Une
# donnée absente doit dire pourquoi elle est absente ; celle-là aurait menti.
# Mesuré avec un pas de quinze jours, même jeton, même départ : 326 points,
# 161 mois, AUCUN mois sauté, et la série va jusqu'à la veille de la collecte
# au lieu de s'arrêter deux semaines avant. Le prix est de doubler `span`,
# donc de diviser les lots par deux.
PERIODE_COURS = "15d"
PAS_COURS_JOURS = 15
PALIERS_COURS = ["2013-01-01", "2015-01-01", "2017-01-01", "2019-01-01",
                 "2020-07-01", "2021-07-01", "2022-07-01", "2023-07-01",
                 "2024-07-01", "2025-07-01"]
LLAMA_COINS = "https://coins.llama.fi"


def _plan_paliers(maintenant):
    """[(palier, timestamp, span, taille de lot)] — la marge est délibérée."""
    plan = []
    for p in PALIERS_COURS:
        d = datetime.strptime(p, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Huit périodes de marge — quatre mois : la grille doit DÉPASSER
        # aujourd'hui, sinon les jetons récents du lot rendent zéro point (cf.
        # l'avertissement ci-dessus). Huit, et pas une, parce que le premier
        # point d'un lot se cale sur la première case où un prix existe, ce qui
        # décale toute la grille.
        span = int((maintenant - d).days // PAS_COURS_JOURS) + 8
        plan.append((p, int(d.timestamp()), span, max(1, 500 // span)))
    return plan


def cours_longs(ids, naissances, maintenant):
    """gecko_id → {'AAAA-MM-JJ': prix}, un point tous les quinze jours.

    Deux passes. La première place chaque jeton au palier qui précède sa
    naissance estimée ; les suivantes RECULENT d'un palier ceux dont le
    premier point tombe à moins de deux périodes du départ de la grille —
    signe que la série commençait probablement avant, et que l'âge estimé la
    sous-estimait. Cette correction n'est pas un luxe : mesuré, 172 des 200
    jetons n'ont pas de genèse déclarée, et leur âge n'est alors qu'une BORNE
    INFÉRIEURE (cf. `age_source` dans crypto_capture_cache).
    """
    plan = _plan_paliers(maintenant)
    palier_de = {}
    for cid in ids:
        n = naissances.get(cid)
        i = 0
        if n:
            # Deux cents jours de marge : un âge tiré du plus bas ou du plus
            # haut connu est une borne inférieure, jamais la naissance.
            n = n - timedelta(days=200)
            for k, (p, _t, _s, _l) in enumerate(plan):
                if datetime.strptime(p, "%Y-%m-%d").replace(tzinfo=timezone.utc) <= n:
                    i = k
        palier_de[cid] = i

    out, requetes = {}, 0
    a_traiter = list(ids)
    for _tour in range(4):
        if not a_traiter:
            break
        lots = {}
        for cid in a_traiter:
            lots.setdefault(palier_de[cid], []).append(cid)
        suivant = []
        for i in sorted(lots):
            p, t, span, taille = plan[i]
            depart = datetime.strptime(p, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            membres = lots[i]
            for k in range(0, len(membres), taille):
                lot = membres[k:k + taille]
                url = (LLAMA_COINS + "/chart/" +
                       ",".join("coingecko:" + urllib.parse.quote(c) for c in lot) +
                       "?period=%s&span=%d&start=%d" % (PERIODE_COURS, span, t))
                d = _get(url, timeout=90)
                requetes += 1
                coins = (d or {}).get("coins") or {}
                for cid in lot:
                    pr = (coins.get("coingecko:" + cid) or {}).get("prices") or []
                    s = {}
                    for pt in pr:
                        v = (pt or {}).get("price")
                        if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
                            s[_jour_iso(pt["timestamp"])] = v
                    if not s:
                        continue
                    if len(s) > len(out.get(cid) or {}):
                        out[cid] = s
                    d0 = datetime.strptime(min(s), "%Y-%m-%d").replace(
                        tzinfo=timezone.utc)
                    if i > 0 and (d0 - depart).days <= 2 * PAS_COURS_JOURS:
                        palier_de[cid] = i - 1
                        suivant.append(cid)
                time.sleep(0.2)
        a_traiter = suivant
    print("[llama] cours longs : %d jetons servis en %d requêtes"
          % (len(out), requetes), flush=True)
    return out


# ─── L'assemblage ─────────────────────────────────────────────────────────
FLUX = (("frais", "dailyFees", "ce que paient les utilisateurs"),
        ("revenu", "dailyRevenue", "ce que garde le protocole"),
        ("detenteurs", "dailyHoldersRevenue", "ce qui revient au jeton"))

# QUATRE-VINGT-DIX JOURS DE QUOTIDIEN, ET PAS TROIS CENT SOIXANTE-CINQ.
# Ce quotidien ne coûte AUCUNE requête : il est découpé dans les séries déjà
# téléchargées pour construire le mensuel. Il ne coûte que du poids, et on l'a
# mesuré sur les deux cents jetons : 90 jours pèsent 355 Ko sur un cache de
# 650 Ko — 55 % du poids —, et 365 jours portent le cache à 1 657 Ko. Les deux
# tiennent sous les 3 Mo visés ; on garde 90 jours parce que c'est la fenêtre
# où le mensuel ne dit rien d'utile (le trimestre écoulé, c'est deux points),
# et parce qu'au-delà le mensuel raconte déjà l'histoire. Le réglage reste
# ouvert : SCF_HISTOIRE_JOURS_FINS=365 pour l'année pleine.
JOURS_FINS = int(os.environ.get("SCF_HISTOIRE_JOURS_FINS", "90"))
TVL_PROTO = os.environ.get("SCF_HISTOIRE_TVL_PROTO", "1") not in ("0", "", "non")
# La profondeur de la TVL de protocole, en rang de capitalisation. Voir
# `tvl_protocoles` : c'est la seule grandeur dont le coût de collecte se
# compte en centaines de mégaoctets, donc la seule qu'on borne par le rang.
TVL_TOP = int(os.environ.get("SCF_HISTOIRE_TVL_TOP", "50"))

MUET_CAPITALISATION = (
    "Aucune capitalisation historique n'est publiée. Mesuré : coins.llama.fi/"
    "mcaps IGNORE le paramètre `timestamp` — interrogé au 1er juin 2022, il "
    "rend la capitalisation du jour même. Et on refuse de la reconstruire en "
    "multipliant le cours passé par l'offre d'aujourd'hui : l'offre a changé "
    "entre-temps, et le produit serait un chiffre inventé, pas une mesure.")


def collecte_histoire(classes, cours_recents, naissances, protos, dlid,
                      maintenant, pre_protocoles=None):
    """Construit le document `crypto_histoire_cache`.

    `pre_protocoles` permet à `main()` de passer le détail quotidien qu'il a
    DÉJÀ extrait de la réponse /overview/fees : la même réponse sert à la
    captation (ses totaux) et à l'histoire (son détail). Sans ce partage, un
    passage complet téléchargerait deux fois les 59 Mo des trois séries.
    """
    ids = [t["id"] for t in classes]
    vise = set(ids)
    quotidien = {}      # cle -> {gecko_id: {jour: usd}}
    total1y = {}        # cle -> {gecko_id: usd}
    hors = {}           # cle -> {gecko_id: usd annoncé mais jamais détaillé}
    muets = {}          # gecko_id -> {cle: raison}
    origine = {}        # gecko_id -> {cle: d'où vient la série}
    jour_ref = None

    for cle, dtype, _quoi in FLUX:
        pret = (pre_protocoles or {}).get(dtype)
        if pret is None:
            print("[llama] %s : détail quotidien (bulk) ..." % dtype, flush=True)
            pret = flux_protocoles(
                _get(DL + "/overview/fees?dataType=%s" % dtype, timeout=180),
                dlid, vise)
        qp, tp, hd, ref = pret
        jour_ref = jour_ref or ref
        print("[llama] %s : chaînes (natif) ..." % dtype, flush=True)
        qc, tc, mc = flux_chaines(dtype, seulement=vise)
        # La chaîne PRIME sur le protocole, comme dans `fusion` et pour la
        # même raison : les frais d'Aave déployé sur Ethereum reviennent aux
        # détenteurs d'AAVE, pas à ceux d'ETH.
        q = {g: s for g, s in qp.items() if g in vise}
        t1 = {g: v for g, v in tp.items() if g in vise}
        for g in ids:
            if g in qc:
                q[g] = qc[g]
                origine.setdefault(g, {})[cle] = "chaîne (/summary/fees)"
                if g in tc:
                    t1[g] = tc[g]
                elif g in t1:
                    del t1[g]
            elif g in q:
                origine.setdefault(g, {})[cle] = "protocoles (/overview/fees)"
        for g, r in mc.items():
            if g in vise and g not in q:
                muets.setdefault(g, {})[cle] = r
        quotidien[cle] = q
        total1y[cle] = t1
        hors[cle] = {g: v for g, v in (hd or {}).items()
                     if g in vise and g not in qc}
        print("[llama]   %s : %d jetons ont une série datée" % (dtype, len(q)),
              flush=True)

    print("[llama] TVL des chaînes ...", flush=True)
    tvl, muets_tvl = tvl_chaines(seulement=vise)
    for g, r in muets_tvl.items():
        if g in vise:
            muets.setdefault(g, {})["tvl"] = r
    for g in tvl:
        origine.setdefault(g, {})["tvl"] = "chaîne (/v2/historicalChainTvl)"
    couv_tvl = {}
    if TVL_PROTO:
        cibles = set(ids[:TVL_TOP]) - set(tvl)
        print("[llama] TVL des protocoles du top %d (%d jetons visés) ..."
              % (TVL_TOP, len(cibles)), flush=True)
        tp2, couv_tvl = tvl_protocoles(cibles, protos, dlid)
        for g, s in tp2.items():
            tvl[g] = s
            origine.setdefault(g, {})["tvl"] = "protocoles (/protocol/{slug})"

    print("[llama] cours longs (coins.llama.fi) ...", flush=True)
    cours = cours_longs(ids, naissances, maintenant)

    # Le cours quotidien déjà collecté par la passe CoinGecko sert deux fois :
    # il fournit la fenêtre fine des derniers mois, ET il prolonge la série
    # mensuelle. Mesuré : coins.llama.fi s'arrête à la dernière case pleine de
    # sa grille de 30 jours — le 2026-08-21 pour une collecte du 2026-09-05.
    # Sans ce raccord, le dernier point mensuel du cours aurait deux semaines
    # de retard sur celui des frais, et les deux courbes ne se liraient plus
    # ensemble.
    cours_j = {}
    for g, pts in (cours_recents or {}).items():
        if g not in vise:
            continue
        s = {}
        for pt in pts:
            try:
                s[_jour_iso(pt[0] / 1000.0)] = float(pt[1])
            except Exception:
                continue
        if s:
            cours_j[g] = s

    fin_fine = maintenant.strftime("%Y-%m-%d")
    sortie, stats = {}, {"frais": 0, "revenu": 0, "detenteurs": 0,
                         "tvl": 0, "cours": 0}
    # Le RANG par capitalisation, énuméré ici : c'est lui qui décide si la TVL
    # d'un protocole a été collectée ou délibérément laissée de côté, et donc
    # laquelle des deux phrases d'absence la fiche doit afficher.
    for rang, t in enumerate(classes, 1):
        g = t["id"]
        mensuel, journalier = {}, {}
        for cle, _dt, _q in FLUX:
            q = (quotidien.get(cle) or {}).get(g)
            if not q:
                continue
            bloc = _compacter(_mensuel(q, "somme"), 1e6)
            if not bloc:
                continue
            ctrl = _controle_annuel(q, (total1y.get(cle) or {}).get(g), jour_ref)
            if ctrl:
                sd = (hors.get(cle) or {}).get(g)
                if sd:
                    ctrl["total1y_sans_detail"] = _sig(sd / 1e6)
                    ctrl["note"] = (
                        "La source annonce en plus ce montant sur un an pour "
                        "des déploiements dont elle ne publie aucun détail "
                        "quotidien : il n'est donc ni dans la série, ni dans "
                        "le total confronté à la série.")
                bloc["controle"] = ctrl
            mensuel[cle] = bloc
            stats[cle] += 1
            fin = _compacter_jours(q, JOURS_FINS, fin_fine, 1e6)
            if fin:
                journalier[cle] = fin

        q = tvl.get(g)
        if q:
            bloc = _compacter(_mensuel(q, "fin"), 1e6)
            if bloc:
                if g in couv_tvl:
                    bloc["couverture_tvl_pct"] = couv_tvl[g]
                mensuel["tvl"] = bloc
                stats["tvl"] += 1
                fin = _compacter_jours(q, JOURS_FINS, fin_fine, 1e6)
                if fin:
                    journalier["tvl"] = fin

        c = dict(cours.get(g) or {})
        c.update(cours_j.get(g) or {})      # le quotidien prime : il est plus fin
        if c:
            bloc = _compacter(_mensuel(c, "fin"))
            if bloc:
                mensuel["cours"] = bloc
                stats["cours"] += 1
                origine.setdefault(g, {})["cours"] = (
                    "coins.llama.fi (15 j) + CoinGecko quotidien"
                    if g in cours and g in cours_j else
                    "coins.llama.fi (15 j)" if g in cours else
                    "CoinGecko quotidien (366 j)")
        if cours_j.get(g):
            fin = _compacter_jours(cours_j[g], JOURS_FINS, fin_fine)
            if fin:
                journalier["cours"] = fin

        # ── TOUTE SÉRIE MANQUANTE DOIT DIRE POURQUOI ─────────────────────
        # Le cas qui l'impose est Akash : il ressort avec un cours et RIEN
        # d'autre — ni frais, ni revenu, ni TVL — et, avant cette boucle,
        # aucune raison publiée. Vérifié à la source le 05/09/2026 :
        # /summary/fees/akash-network rend 400, /overview/fees/akash-network
        # rend 500, et son nom n'apparaît dans aucun jour du détail quotidien.
        # L'absence est donc réelle. Mais une fiche qui affiche quatre cases
        # vides sans une ligne d'explication laisse croire à une panne de
        # collecte, alors que c'est la source qui se tait — et les deux
        # appellent des gestes opposés.
        #
        # Trois absences distinctes, trois phrases distinctes :
        #   · la source annonce un total annuel mais ne détaille aucun jour
        #     (mesuré : Sushi Perps, Sushi Launchpad) ;
        #   · nous avons délibérément renoncé à collecter (TVL de protocole
        #     sous le rang TVL_TOP — voir `tvl_protocoles`, c'est une question
        #     de centaines de mégaoctets) ;
        #   · la source ne publie rien du tout.
        # La deuxième est la seule qui soit de notre fait, et c'est justement
        # celle qu'il serait malhonnête de faire passer pour un silence de la
        # source.
        # ⚠ UNE RAISON D'ABSENCE QUI SURVIT À LA SÉRIE EST UN MENSONGE.
        # `muets` se remplit AVANT le repêchage : la TVL d'une chaîne peut
        # manquer (Monero rend 404) puis être retrouvée côté protocole pour le
        # même jeton, et l'entrée « TVL non publiée » resterait à côté d'une
        # courbe bien présente — la fiche afficherait un trou par-dessus une
        # donnée. On purge donc toute raison que la série dément. Le test
        # `controle_absences` monte la garde sur ce même invariant.
        manque = {k: v for k, v in (muets.get(g) or {}).items()
                  if k not in mensuel}
        for cle, dtype, _q in FLUX:
            if cle in mensuel or cle in manque:
                continue
            sd = (hors.get(cle) or {}).get(g)
            if sd:
                manque[cle] = (
                    "DeFiLlama annonce %.3g M$ sur un an sous %s pour ce jeton, "
                    "mais ne publie aucun détail quotidien : il n'y a donc pas "
                    "de série à afficher." % (sd / 1e6, dtype))
            else:
                manque[cle] = (
                    "DeFiLlama ne publie aucune série %s pour ce jeton, ni par "
                    "protocole ni par chaîne." % dtype)
        if "tvl" not in mensuel and "tvl" not in manque:
            if not TVL_PROTO:
                manque["tvl"] = (
                    "TVL de protocole non collectée : la collecte en a été "
                    "débranchée pour ce passage (SCF_HISTOIRE_TVL_PROTO=0).")
            elif rang > TVL_TOP:
                manque["tvl"] = (
                    "TVL historique non collectée : elle n'existe que sur "
                    "/protocol/{slug}, qui transporte aussi la composition en "
                    "jetons de chaque jour — mesuré le 05/09/2026, 29,1 Mo "
                    "pour aave-v3 et 67,6 Mo pour curve-dex. Elle est donc "
                    "réservée aux %d premières capitalisations ; ce jeton est "
                    "%de." % (TVL_TOP, rang))
            else:
                manque["tvl"] = (
                    "DeFiLlama ne publie pas de TVL historique pour ce jeton : "
                    "ni comme chaîne, ni comme protocole identifié.")
        if "cours" not in mensuel:
            manque["cours"] = (
                "Aucun cours daté : coins.llama.fi ne sert pas ce jeton, et la "
                "passe CoinGecko n'en a rapporté aucun point.")

        n_pts = sum(len(b["valeurs"]) for b in mensuel.values())
        e = {"symbole": t.get("symbol"),
             "mensuel": mensuel or None,
             "quotidien": journalier or None,
             "points_mensuels": n_pts,
             "origine": origine.get(g) or None}
        if manque:
            e["muet"] = manque
        if not mensuel:
            e["muet"] = dict(e.get("muet") or {})
            e["muet"]["tout"] = (
                "Aucune série datée : ni DeFiLlama ni coins.llama.fi ne "
                "publient d'historique pour ce jeton.")
        sortie[g] = e

    doc = {
        "genere_le": maintenant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "univers": len(sortie),
        "jour_reference": jour_ref,
        "mois_incomplet": maintenant.strftime("%Y-%m"),
        "jours_fins": JOURS_FINS,
        "unites": {"frais": "M$ par mois", "revenu": "M$ par mois",
                   "detenteurs": "M$ par mois",
                   # La TVL vient d'une série QUOTIDIENNE : son dernier point du
                   # mois est bien le dernier jour. Le cours, lui, vient d'une
                   # grille à quinze jours au-delà de la fenêtre récente : son
                   # dernier point du mois n'est presque jamais le 31.
                   "tvl": "M$ au dernier jour du mois",
                   "cours": "$ au dernier point servi du mois — la grille longue "
                            "ne sert que deux jours par mois, ce n'est donc pas "
                            "un cours de clôture"},
        "sources": [
            "DefiLlama /overview/fees?dataType=… — détail quotidien par protocole depuis 2018-03-26",
            "DefiLlama /summary/fees/{chaîne} — natif des chaînes, jusqu'à 2011-01-31 (Bitcoin)",
            "DefiLlama /v2/historicalChainTvl/{chaîne} — TVL quotidienne des chaînes",
            "DefiLlama /protocol/{slug} — TVL quotidienne, jetons du top %d seulement" % TVL_TOP,
            "coins.llama.fi /chart/coingecko:… — cours par pas de 15 jours, toute la vie cotée",
            "CoinGecko /coins/{id}/market_chart — cours quotidien des 366 derniers jours (déjà collecté)",
        ],
        "methode": (
            "Un flux (frais, revenu, revenu détenteurs) est SOMMÉ sur le mois : "
            "c'est un compte de résultat. Un stock (TVL, cours) prend sa "
            "dernière valeur du mois : c'est un bilan. Chaque flux porte un "
            "bloc `controle` qui permet de refaire le total annuel annoncé par "
            "la source à partir de la série publiée — c'est ce contrôle qui "
            "prouve qu'aucune date n'a glissé."),
        "avertissement": (
            "Le mois nommé par `mois_incomplet` n'est pas terminé : ses flux "
            "sont partiels par construction, et le lire comme les autres ferait "
            "voir un effondrement là où il n'y a qu'un mois entamé. "
            "Un `null` à l'intérieur d'une série est un mois que la source n'a "
            "pas publié, jamais un mois à zéro."),
        "muet_capitalisation": MUET_CAPITALISATION,
        "couverture": stats,
        "jetons": sortie,
    }
    return doc


def ecrire_histoire(doc, force=False):
    """Publie l'histoire — sauf si elle est plus PAUVRE que celle qu'elle remplace.

    ⚠ LA PANNE QUE CE DÉPÔT A DÉJÀ PAYÉE. Un passage sur quatre cents sociétés
    avait effacé les paquets de quatre cent trente-cinq autres. Le même chemin
    était rouvert ici : `SCF_HISTOIRE_JETONS=bitcoin,ethereum` — une mise au
    point de deux jetons, que le commentaire du `__main__` recommande lui-même —
    remplaçait un cache de deux cents jetons (757 435 octets) par un cache de
    deux (17 952 octets), sans condition ni avertissement. Vérifié en bac à
    sable pendant la relecture, et c'est ainsi que le défaut a été trouvé.

    On refuse donc de publier un univers qui a fondu de plus d'un quart, à moins
    que l'appelant ne le demande explicitement. Le refus est BRUYANT : un cache
    qu'on n'écrit pas doit se dire, sinon il se lit comme un cache écrit.

    ⚠ LES DEUX FICHIERS S'ÉCRIVENT DE LA MÊME FAÇON. Le `.js` passait par un
    fichier temporaire et un remplacement atomique, le `.json` était écrit
    directement — or c'est le `.json` que le contrôle lit en premier. Un passage
    interrompu laissait donc un `.json` tronqué à côté d'un `.js` valide, et le
    garde-fou tombait sur le fichier que le site ne sert pas.
    """
    js = os.path.join(CACHE, "crypto_histoire_cache.js")
    jsonf = os.path.join(CACHE, "crypto_histoire_cache.json")
    if not force and os.path.exists(jsonf):
        try:
            with open(jsonf, encoding="utf-8") as fh:
                ancien = json.load(fh)
            avant = len(ancien.get("jetons") or {})
            apres = len(doc.get("jetons") or {})
            if avant >= 20 and apres < avant * 0.75:
                print("[refus] histoire NON écrite : %d jetons contre %d dans le "
                      "cache existant. Une mise au point ne doit pas remplacer une "
                      "collecte. Relancer sans SCF_HISTOIRE_JETONS, ou passer "
                      "force=True si l'appauvrissement est voulu."
                      % (apres, avant), file=sys.stderr)
                return None
        except Exception as e:
            print("[warn] cache d'histoire précédent illisible (%s) : on écrit "
                  "sans pouvoir comparer" % e, file=sys.stderr)
    tmp = js + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.__CRYPTO_HISTOIRE__=")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(tmp, js)
    tmpj = jsonf + ".tmp"
    with open(tmpj, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmpj, jsonf)
    s = doc["couverture"]
    print("[ok] histoire : %d jetons" % doc["univers"])
    print("     frais / revenu / détenteurs : %3d / %3d / %3d"
          % (s["frais"], s["revenu"], s["detenteurs"]))
    print("     TVL / cours                 : %3d / %3d" % (s["tvl"], s["cours"]))
    print("     écrit %s (%.0f Ko)" % (js, os.path.getsize(js) / 1024.0))
    return js


def _univers_depuis_capture():
    """Relit l'univers, les âges et le cours fin du cache de captation.

    Sert au mode `--histoire`, qui reconstruit la seule histoire sans refaire
    les quatre cents appels CoinGecko de la captation.
    """
    cap = lire_cache("crypto_capture_cache.json") or lire_cache("crypto_capture_cache.js")
    if not cap:
        raise SystemExit("[fatal] crypto_capture_cache introuvable : lancer "
                         "d'abord le collecteur complet.")
    jetons = cap.get("jetons") or {}
    classes = [{"id": g, "symbol": v.get("symbole")} for g, v in jetons.items()]
    filtre = [s.strip() for s in os.environ.get("SCF_HISTOIRE_JETONS", "").split(",")
              if s.strip()]
    if filtre:
        classes = [t for t in classes if t["id"] in filtre]
        print("[info] SCF_HISTOIRE_JETONS : univers borné à %d jetons"
              % len(classes))
    return classes, (cap.get("cours") or {}), jetons


def naissances_depuis(jetons, maintenant):
    """gecko_id → date de naissance estimée, pour choisir le palier de cours."""
    out = {}
    for g, v in (jetons or {}).items():
        a = v.get("age_jours")
        if isinstance(a, (int, float)) and a > 0:
            out[g] = maintenant - timedelta(days=int(a))
    return out


def main_histoire():
    """Reconstruit `crypto_histoire_cache` seul, sans toucher à la captation."""
    maintenant = datetime.now(timezone.utc)
    classes, cours_recents, jetons = _univers_depuis_capture()
    print("[info] univers : %d jetons" % len(classes))
    protos = _get(DL + "/protocols", timeout=90) or []
    dlid = construire_dlid(protos)
    print("[info] %d jointures dlid → gecko" % len(dlid))
    doc = collecte_histoire(classes, cours_recents,
                            naissances_depuis(jetons, maintenant),
                            protos, dlid, maintenant)
    ecrire_histoire(doc)
    return 0


def main():
    nf = lire_cache("narratives_fundamentals_cache.js")
    if not nf:
        raise SystemExit("[fatal] narratives_fundamentals_cache.js illisible.")

    # L'univers : les jetons du cache amont, classés par capitalisation. Même
    # tri que `fetch_crypto_fiches.py`, pour que les deux caches parlent des
    # mêmes jetons — sans quoi la fiche demanderait une captation pour un
    # jeton absent, et l'afficherait comme non publiée alors qu'elle existe.
    jetons = {}
    for n in nf.get("narratives", []):
        for t in n.get("tokens", []):
            if t.get("is_stock"):
                continue
            jetons.setdefault(t["id"], dict(t))
    classes = sorted(
        [t for t in jetons.values() if isinstance(t.get("mcap_b"), (int, float))],
        key=lambda t: -t["mcap_b"])[:TOP_N]
    ids = [t["id"] for t in classes]
    print("[info] univers : %d jetons" % len(ids))

    print("[info] DeFiLlama /protocols (jointure) ...")
    protos = _get(DL + "/protocols", timeout=90) or []
    dlid = construire_dlid(protos)
    print("[info] %d jointures dlid → gecko" % len(dlid))

    # Les trois étages, chacun en deux temps : les protocoles, puis le natif
    # des chaînes. L'ordre importe — voir `fusion`.
    etages = {}
    # Le détail quotidien des trois séries est extrait ICI, dans la même
    # descente que les totaux : la réponse complète sert les deux phases, puis
    # on la relâche. La garder pour plus tard tiendrait les trois payloads
    # (59 Mo de JSON) en mémoire en même temps — sur un portable, c'est ce qui
    # transforme une collecte en échange sur disque.
    pre_protocoles = {}
    for cle, dtype in (("frais", "dailyFees"),
                       ("revenu", "dailyRevenue"),
                       ("detenteurs", "dailyHoldersRevenue")):
        print("[info] DeFiLlama %s ..." % dtype)
        paquet = None
        if HISTOIRE:
            paquet = _get(DL + "/overview/fees?dataType=%s" % dtype, timeout=180)
            pre_protocoles[dtype] = flux_protocoles(paquet, dlid, set(ids))
        p, dp = somme_serie(dtype, dlid, paquet)
        paquet = None
        c, dc = somme_chaines(dtype)
        etages[cle] = fusion(p, c)
        det = dict(dp)
        det.update(dc)
        etages[cle + "_detail"] = det
        print("[info]   %s : %d jetons servis (%d chaînes)" % (dtype, len(etages[cle]), len(c)))

    ident = {}
    hist = {}
    if AVEC_HIST:
        etat = _lire_reprise()
        print("[info] CoinGecko : fiche d'identité de %d jetons "
              "(≈ %d min) ..." % (len(ids), int(len(ids) * 2.6 / 60) + 1))
        ident = fiche_identite(ids, etat)
        print("[info] CoinGecko : historique de cours (≈ %d min) ..."
              % (int(len(ids) * 2.6 / 60) + 1))
        hist = historique_cours(ids, etat)
    else:
        print("[info] SCF_CAPTURE_HIST=0 : identité et historique non collectés.")
        # On ne perd pas ce qu'un passage précédent avait obtenu : sans cela,
        # un rafraîchissement de la captation seule effacerait les âges.
        vieux = lire_cache("crypto_capture_cache.js") or {}
        ident = vieux.get("identite") or {}
        hist = vieux.get("cours") or {}
        if ident or hist:
            print("[info]   %d identités et %d historiques repris du cache "
                  "précédent." % (len(ident), len(hist)))

    maintenant = datetime.now(timezone.utc)
    sortie = {}
    for t in classes:
        cid = t["id"]
        mcap_usd = (t.get("mcap_b") or 0) * 1e9 or None
        fdv_usd = (t.get("fdv_b") or 0) * 1e9 or None

        frais = etages["frais"].get(cid)
        revenu = etages["revenu"].get(cid)
        detenteurs = etages["detenteurs"].get(cid)

        def _m(v):
            return round(v / 1e6, 3) if isinstance(v, (int, float)) else None

        # ── Les deux taux, et pourquoi ils ne se calculent pas toujours ──
        # Un taux n'a de sens que si son dénominateur est réel. Sous cent
        # mille dollars de frais annuels, le rapport mesure le bruit de la
        # collecte, pas un partage de valeur : on se taît.
        PLANCHER_FRAIS = 100000.0
        taux_capt = None
        part_det = None
        if (isinstance(frais, (int, float)) and frais >= PLANCHER_FRAIS):
            if isinstance(revenu, (int, float)):
                taux_capt = round(100.0 * revenu / frais, 2)
            if isinstance(detenteurs, (int, float)):
                part_det = round(100.0 * detenteurs / frais, 2)

        # ── La garde de cohérence des trois étages ───────────────────────
        # Les trois séries sont publiées SÉPARÉMENT par DeFiLlama, et rien
        # n'y impose que l'une soit inférieure à l'autre. Mesuré sur le top
        # 200 : six jetons rendent une part supérieure à 100 % — OP à 373 %,
        # Sonic à 1 980 %. La cause est connue et légitime côté source : ce
        # qui revient au détenteur peut inclure des ÉMISSIONS (incitations
        # versées en jetons neufs) qui ne sont pas prélevées sur les frais.
        #
        # Mais « 1 980 % de ce que paient les utilisateurs » est une phrase
        # fausse, et l'afficher ferait passer une subvention pour une
        # captation record — exactement à l'envers de ce que la fiche doit
        # dire. On ne corrige pas le chiffre (il n'est pas à nous) : on
        # refuse le TAUX, on garde les montants, et on marque le jeton pour
        # que la fiche explique pourquoi le taux manque.
        incoherent = None
        if (isinstance(part_det, (int, float)) and part_det > 100.5) or \
           (isinstance(taux_capt, (int, float)) and taux_capt > 100.5):
            incoherent = (
                "Ce qui revient au détenteur dépasse ce que paient les "
                "utilisateurs : la rémunération inclut des jetons émis, et non "
                "seulement une part des frais. Le taux n’est donc pas calculé — "
                "il ne mesurerait pas un partage.")
            taux_capt = None
            part_det = None

        # Le rendement de captation : la seule de ces grandeurs homogène au
        # rendement d'une action. Il se calcule sur la capitalisation en
        # circulation ET sur la FDV — l'écart entre les deux EST la dilution
        # que le détenteur paiera.
        rend_det = None
        rend_det_fdv = None
        if isinstance(detenteurs, (int, float)) and mcap_usd:
            rend_det = round(100.0 * detenteurs / mcap_usd, 4)
        if isinstance(detenteurs, (int, float)) and fdv_usd:
            rend_det_fdv = round(100.0 * detenteurs / fdv_usd, 4)
        rend_rev = None
        if isinstance(revenu, (int, float)) and mcap_usd:
            rend_rev = round(100.0 * revenu / mcap_usd, 4)

        # ── L'âge ────────────────────────────────────────────────────────
        idt = ident.get(cid) or {}
        pts = hist.get(cid) or []
        premiere = None
        if pts:
            premiere = datetime.fromtimestamp(pts[0][0] / 1000.0, timezone.utc)
        genesis = None
        g = idt.get("genesis")
        if g:
            try:
                genesis = datetime.strptime(g, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                genesis = None
        # ── L'ÂGE NE SE LIT PAS DANS LA SÉRIE DE COURS ────────────────────
        # La série ne couvre plus qu'un an (le palier gratuit refuse au-delà,
        # cf. HIST_JOURS). Sa première date ne dit donc RIEN de l'ancienneté :
        # prise pour telle, elle déclarerait Bitcoin vieux d'un an et lui
        # donnerait zéro cycle traversé — l'inverse exact de ce que sa fiche
        # doit dire.
        #
        # Deux sources solides restent, dans cet ordre :
        #   1. la GENÈSE publiée par `/coins/{id}` — la bonne réponse ;
        #   2. à défaut, la date du PLUS BAS historique. C'est une borne
        #      INFÉRIEURE de l'âge (le jeton existait déjà à cette date), pas
        #      l'âge exact ; la fiche le dit en nommant sa source.
        # La première cotation observée n'est retenue que si elle est plus
        # ancienne que la fenêtre servie, c'est-à-dire jamais aujourd'hui —
        # la ligne reste pour le jour où l'horizon se rouvrira.
        def _date(x):
            if not x:
                return None
            try:
                return datetime.strptime(str(x)[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc)
            except Exception:
                return None

        atl_d = _date(idt.get("atl_date"))
        ath_d = _date(idt.get("ath_date"))
        fenetre = None
        if pts:
            fenetre = (maintenant - premiere).days if premiere else None
        depart, source = None, None
        if genesis:
            depart, source = genesis, "genèse déclarée"
        else:
            # Le plus ancien repère daté dont on dispose.
            reperes = [d for d in (atl_d, ath_d) if d]
            if premiere and fenetre and fenetre > 400:
                reperes.append(premiere)
            if reperes:
                depart = min(reperes)
                source = "au moins depuis son plus bas ou son plus haut connu"
        age_j = int((maintenant - depart).days) if depart else None
        # A-t-il traversé un cycle ? Le halving de Bitcoin cadence le marché
        # crypto depuis quinze ans ; quatre ans est donc la mesure qui compte,
        # bien plus qu'un âge en années brut.
        cycles = round(age_j / 1461.0, 2) if age_j else None

        # ── LA PIRE CHUTE : sur toute la vie, pas sur la fenêtre servie ────
        # Calculée sur une série d'un an, elle dirait « pire chute −22 % » pour
        # un jeton qui en a perdu 94 % en 2022. C'est faux, et c'est faux dans
        # le sens rassurant — le pire des deux. `ath_chg_pct`, lui, mesure
        # l'écart au record de TOUTE la vie cotée : c'est la seule mesure
        # honnête de la chute encaissée dont on dispose encore.
        #
        # Le repli sur la fenêtre n'est donc PAS conservé : mieux vaut un champ
        # nul, que la fiche déclare non mesuré, qu'un chiffre trois fois trop
        # doux affiché comme une vérité.
        pire_chute = None
        chg = idt.get("ath_chg_pct")
        if isinstance(chg, (int, float)) and math.isfinite(chg) and chg <= 0:
            pire_chute = round(chg, 1)

        sortie[cid] = {
            "symbole": t.get("symbol"),
            # ── la chaîne de captation, en millions de dollars ──
            "frais_m": _m(frais),
            "revenu_m": _m(revenu),
            "detenteurs_m": _m(detenteurs),
            "taux_captation_pct": taux_capt,
            "part_detenteurs_pct": part_det,
            "rendement_detenteurs_pct": rend_det,
            "rendement_detenteurs_fdv_pct": rend_det_fdv,
            "rendement_revenu_pct": rend_rev,
            "captation_incoherente": incoherent,
            "captation_detail": (etages["detenteurs_detail"].get(cid)
                                 or etages["revenu_detail"].get(cid)
                                 or etages["frais_detail"].get(cid) or None),
            # ── l'âge et l'histoire ──
            "genesis": idt.get("genesis"),
            "premiere_cotation": (premiere.strftime("%Y-%m-%d") if premiere else None),
            "age_jours": age_j,
            "age_source": source,
            "cycles": cycles,
            "pire_chute_pct": pire_chute,
            "ath": idt.get("ath"),
            "ath_date": idt.get("ath_date"),
            "ath_chg_pct": idt.get("ath_chg_pct"),
            "atl": idt.get("atl"),
            "atl_date": idt.get("atl_date"),
            "atl_chg_pct": idt.get("atl_chg_pct"),
            "pays": idt.get("pays"),
            "categories_cg": idt.get("categories"),
            "rang_cg": idt.get("rang_cg"),
            # ── l'activité, faute de mieux ──
            "dev_etoiles": idt.get("dev_etoiles"),
            "dev_forks": idt.get("dev_forks"),
            "dev_contributeurs": idt.get("dev_contributeurs"),
            "dev_commits_4s": idt.get("dev_commits_4s"),
            "com_twitter": idt.get("com_twitter"),
            "com_reddit": idt.get("com_reddit"),
            "suivi_cg": idt.get("suivi_cg"),
            "n_points_cours": len(pts),
        }

    doc = {
        "genere_le": maintenant.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_updated": nf.get("updated"),
        "univers": len(sortie),
        "sources": [
            "DefiLlama /overview/fees?dataType=dailyFees (ce que paient les utilisateurs)",
            "DefiLlama /overview/fees?dataType=dailyRevenue (ce que garde le protocole)",
            "DefiLlama /overview/fees?dataType=dailyHoldersRevenue (ce qui revient au jeton)",
            "CoinGecko /coins/{id} (genèse, plus haut, plus bas, dépôt de code)",
            "CoinGecko /coins/{id}/market_chart (cours quotidien, toute la vie cotée)",
        ],
        "methode": (
            "La captation se lit en trois étages : les utilisateurs paient des "
            "FRAIS, le protocole en garde un REVENU, et une partie seulement "
            "revient aux DÉTENTEURS du jeton. Les taux ne sont calculés qu'au-"
            "delà de cent mille dollars de frais annuels : en dessous, le "
            "rapport mesure le bruit de la collecte."),
        "avertissement": (
            "Un étage nul et un étage non publié ne sont pas la même chose. "
            "« Ce protocole ne reverse rien » est une mesure ; « on ne sait pas "
            "ce qu'il reverse » est une absence. Le cache laisse le champ nul "
            "dans le second cas, et la fiche l'affiche comme non publié."),
        "jetons": sortie,
        "cours": hist,
        "identite": ident,
    }

    js = os.path.join(CACHE, "crypto_capture_cache.js")
    tmp = js + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.__CRYPTO_CAPTURE__=")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(tmp, js)
    with open(os.path.join(CACHE, "crypto_capture_cache.json"), "w",
              encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))

    n_frais = sum(1 for v in sortie.values() if v["frais_m"] is not None)
    n_det = sum(1 for v in sortie.values() if v["detenteurs_m"] is not None)
    n_taux = sum(1 for v in sortie.values() if v["taux_captation_pct"] is not None)
    n_age = sum(1 for v in sortie.values() if v["age_jours"] is not None)
    n_h = sum(1 for v in sortie.values() if v["n_points_cours"] > 0)
    print("[ok] %d jetons" % len(sortie))
    print("     frais servis        : %3d" % n_frais)
    print("     revenu détenteurs   : %3d" % n_det)
    print("     taux de captation   : %3d" % n_taux)
    print("     âge connu           : %3d" % n_age)
    print("     historique de cours : %3d" % n_h)
    print("     écrit %s (%.0f Ko)" % (js, os.path.getsize(js) / 1024.0))

    # ── L'HISTOIRE LONGUE, dans un cache SÉPARÉ ──────────────────────────
    # `crypto_capture_cache` n'est pas touché : la fiche qui le lit
    # aujourd'hui continue de lire la même chose. L'histoire s'AJOUTE.
    if HISTOIRE:
        print("[info] histoire longue ...")
        ecrire_histoire(collecte_histoire(
            classes, hist, naissances_depuis(sortie, maintenant),
            protos, dlid, maintenant, pre_protocoles=pre_protocoles))
    else:
        print("[info] SCF_CAPTURE_HISTOIRE=0 : histoire longue non collectée.")
    return 0


if __name__ == "__main__":
    # `--histoire` refait la seule histoire longue en relisant l'univers et le
    # cours fin du cache de captation : indispensable pour la mettre au point
    # sans relancer les quatre cents appels CoinGecko qui durent des heures.
    sys.exit(main_histoire() if "--histoire" in sys.argv else main())
