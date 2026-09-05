#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FICHES CRYPTO — le top 200, chaque jeton jugé sur ce qu'il prétend être.

CE QUI A CHANGÉ, ET POURQUOI
-----------------------------
La version précédente notait chaque jeton selon SA FAMILLE : six familles, six
grilles. C'était déjà mieux qu'une grille unique — le cas Akash l'imposait :
revenus nuls, réseau à 59 % d'occupation, une grille commune l'aurait déclaré
sans valeur. Mais six grilles pour deux cents jetons laissaient passer trois
choses que la mesure met en évidence :

1. LA CAPTATION N'ÉTAIT PAS MESURÉE. Le cache amont appelle « revenus » ce que
   DeFiLlama sert sous `dailyFees` : ce que les UTILISATEURS PAIENT. Ce n'est
   pas ce que le protocole garde, ni ce qui revient au détenteur du jeton.
   Mesuré sur le top 200 : Uniswap facture 872 M$ par an et 4,4 % atteignent
   UNI ; Hyperliquid facture 1 309 M$ et en reverse 55 %. Deux protocoles que
   l'ancienne note plaçait dans la même famille, avec les mêmes critères, alors
   que le fait qui les sépare est précisément celui-là.

2. LES ENVELOPPES ÉTAIENT NOTÉES COMME DES PROTOCOLES. stETH, wstETH, WBTC,
   cbBTC, WETH, eETH, frxETH — huit jetons du top 200 — n'ont ni revenus, ni
   TVL, ni économie propre : ils représentent un autre actif, un pour un. On
   leur reprochait de n'avoir pas de « prix / revenus ». De même XAUT et PAXG
   (de l'or en coffre) et STRCX (une action tokenisée).

3. LE TEMPS N'ÉTAIT NULLE PART. Un jeton qui a traversé deux cycles et une
   chute de 90 % n'est pas un jeton listé il y a six mois — et sur un memecoin,
   qui n'a ni revenus ni TVL, c'est la SEULE grandeur fondamentale disponible.

D'où la conception actuelle : un PROFIL par jeton (`crypto_profils.py`), qui
dit ses six axes de radar, ses critères notés, et en une phrase ce que ce jeton
prétend être. Les jetons notables portent un profil écrit à la main ; les
autres héritent d'un archétype raisonné — onze archétypes, là où il y avait six
familles, parce que les familles mélangeaient des choses qui ne se mesurent pas
pareil.

CE QUE CE COLLECTEUR NE FAIT TOUJOURS PAS
------------------------------------------
Aucune requête réseau. Il dérive de caches déjà collectés — dont le nouveau
`crypto_capture_cache`, qui porte la captation et l'âge. Si l'un manque, la
grandeur devient muette — jamais fausse.

Il lit aussi `crypto_histoire_cache`, mais n'en RECOPIE rien : seulement
l'index (combien de mois, depuis quand, quelles séries, et la raison de
chaque absence). Les séries, elles, restent dans leur cache, que la page
charge à part. Mesuré sur les 200 jetons du 05/09/2026 : l'index pèse 96 916
octets — 485 par jeton —, les séries 748 725, soit 3 744 par jeton. Les
recopier ajouterait 636 Ko à un `crypto_fiches.js` qui en pèse déjà 1 273,
pour une information que la page aurait alors en double.

LA MÉCANIQUE DE NOTE, INCHANGÉE (elle avait fait ses preuves)
--------------------------------------------------------------
Trois états par critère :
  - MUET            : la source ne publie pas. Exclu du dénominateur.
  - MUET ATTENDU    : la grandeur n'a pas de sens pour ce profil. Exclu aussi,
                      et la fiche dit pourquoi plutôt que d'afficher un trou.
  - NOTÉ            : la valeur existe, on la note (1 pt / ½ pt / 0).
La note est RAMENÉE aux seuls critères que le jeton pouvait obtenir, et refusée
si moins de 60 % des critères APPLICABLES sont servis.
"""

import json
import os
import sys
import math
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crypto_profils import GRANDEURS, ARCHETYPES, PROFILS, profil_de  # noqa: E402

RACINE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("SCF_CACHE", RACINE)
TOP_N = int(os.environ.get("SCF_TOP_CRYPTO", "200"))


# ── Lecture des caches ────────────────────────────────────────────────────
def lire_cache(nom):
    """Rend l'objet d'un cache .js ou .json.

    Plusieurs caches du depot ne sont PAS `window.X = {...}` mais des IIFE
    `(function(){var d={...};window.__X__=d;})()`. On cherche donc le premier
    objet equilibre, sans se fier au prefixe.
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


# ── Les familles, réduites à leur seul rôle restant ───────────────────────
# Elles ne décident plus des critères — c'est le profil qui le fait — mais
# servent encore de REPLI pour un jeton dont le narratif n'évoque aucun profil
# connu, et d'étiquette de regroupement dans le tableau.
FAMILLES = {
    "L1 Smart Contracts": "chaine",
    "Ethereum": "chaine",
    "Ethereum L2s": "chaine",
    "Solana Ecosystem": "chaine",
    "Modular / DA": "chaine",
    "BTC L2s & Ordinals": "chaine",
    "DEX & AMM": "protocole",
    "Lending & Yield": "protocole",
    "Perp DEX": "protocole",
    "Liquid Staking": "protocole",
    "Restaking": "protocole",
    "RWA": "protocole",
    "Prediction Markets": "protocole",
    "Stablecoins": "protocole",
    "DePIN": "reseau",
    "AI & Agents": "reseau",
    "Memecoins": "speculatif",
    "Gaming / SocialFi": "speculatif",
    "NFT": "speculatif",
    "Payment Coins": "monnaie",
    "Bitcoin Institutional": "monnaie",
    "ZK / Privacy": "monnaie",
    "Exchange Tokens": "plateforme",
    "Web3 Exchanges & Fintech": "plateforme",
    "Bitcoin Miners": "plateforme",
}


def noter(valeur, haut, bas, sens):
    """Un point au seuil haut, un demi au seuil bas, zero sinon."""
    if valeur is None or (isinstance(valeur, float) and not math.isfinite(valeur)):
        return None
    if sens == "haut":
        return 1.0 if valeur >= haut else (0.5 if valeur >= bas else 0.0)
    return 1.0 if valeur <= haut else (0.5 if valeur <= bas else 0.0)


# ── Le denominateur degenere ──────────────────────────────────────────────
# Le P/S d'XRP vaut 836 443. Le calcul n'est PAS faux : 82,7 Md$ de
# capitalisation pour 0,1 M$ de revenus annuels. Mais un ratio dont le
# denominateur est effondre ne mesure plus rien — il mesure la petitesse du
# denominateur.
#
# Ce qui rend un multiple absurde n'est pas sa HAUTEUR, c'est l'effondrement
# de son DENOMINATEUR. L'ancien garde-fou testait le ratio (> 500x) et jetait
# donc Ethereum — 215,3 M$ de revenus annuels reels — dans le meme panier
# qu'Algorand — 0,01 M$ de revenus. Le premier chiffre est une information, le
# second n'en est pas une.
PLAFOND_MULTIPLE = {"ps_ttm": 20000.0, "mc_tvl": 20000.0, "fdv_tvl": 20000.0}
PLANCHER_DENOMINATEUR = {
    "ps_ttm":  ("rev_m_1y", 10.0),    # revenus annuels, en M$
    "mc_tvl":  ("tvl_b",    0.010),   # valeur immobilisee, en Md$ (10 M$)
    "fdv_tvl": ("tvl_b",    0.010),
}


# ── Les parts, qui ne peuvent pas dépasser cent ──────────────────────────
# `part_detenteurs_pct` et `taux_captation_pct` sont des PARTS de ce que
# paient les utilisateurs. Au-delà de cent, elles ne mesurent plus un partage :
# la rémunération inclut des jetons émis, non prélevés sur les frais.
#
# Le collecteur de captation refuse déjà ces taux. Mais ce collecteur-ci lit un
# CACHE, qui peut avoir été écrit avant cette règle — mesuré : cinq jetons du
# cache en place portaient de 123 % à 1 980 %, et Sonic obtenait le point plein
# ET le centième centile du radar sur une assiette de frais de 0,2 M$. Refaire
# la vérification ici coûte deux comparaisons et rend la note indépendante de
# l'âge du cache.
PART_MAX = 100.5
CLES_PART = {"part_detenteurs_pct", "taux_captation_pct"}


def denominateur_degenere(cle, valeur, jeton=None):
    """Vrai quand le ratio est arithmetiquement juste mais vide de sens."""
    if not isinstance(valeur, (int, float)):
        return False
    if cle in CLES_PART and (valeur > PART_MAX or valeur < -0.5):
        return True
    if jeton is not None and cle in PLANCHER_DENOMINATEUR:
        champ, plancher = PLANCHER_DENOMINATEUR[cle]
        d = jeton.get(champ)
        if isinstance(d, (int, float)):
            return d < plancher
    pl = PLAFOND_MULTIPLE.get(cle)
    return pl is not None and valeur > pl


def main():
    nf = lire_cache("narratives_fundamentals_cache.js")
    if not nf:
        raise SystemExit(
            "[fatal] narratives_fundamentals_cache.js illisible. ATTENTION : le "
            "jumeau .json est un fossile (mesure : 17 jours de retard, et PLUS "
            "gros que le .js). Ne pas basculer dessus.")

    # Les gisements specialises, facultatifs : absents, ils rendent la grandeur
    # muette, jamais fausse.
    iadec = lire_cache("ai_dec_revenue_cache.json") or {}
    ia_projets = {p.get("id") or p.get("cgid"): p
                  for p in (iadec.get("projects") or [])}
    akash = iadec.get("akash") or {}
    l1 = lire_cache("l1_valuation_cache.js") or {}
    # Le nouveau gisement : captation, âge, historique. Son absence n'est pas
    # fatale — les axes correspondants deviennent muets — mais elle prive la
    # fiche de ce qui fait son intérêt, alors on le dit franchement.
    cap = lire_cache("crypto_capture_cache.js") or {}
    capj = cap.get("jetons") or {}
    if not capj:
        print("[warn] crypto_capture_cache absent : ni captation ni âge. "
              "Lancer fetch_crypto_capture.py.", file=sys.stderr)

    # ── CE QUE LA FICHE DOIT SAVOIR DE SON PROPRE HISTORIQUE ─────────────
    # Les séries elles-mêmes vivent dans `crypto_histoire_cache` : les
    # recopier ici doublerait un cache de plusieurs centaines de kilo-octets
    # pour rien. Mais la fiche ne peut pas ANNONCER un onglet « historique »
    # sans savoir s'il y a quelque chose derrière — pas plus qu'une fiche
    # action n'affiche « 34 exercices » sans les avoir comptés. On greffe donc
    # l'INDEX, et lui seul : combien de mois, depuis quand, quelles séries.
    # Mesuré sur les 200 jetons du 05/09/2026 : 485 octets par jeton pour
    # l'index, 3 744 pour les séries — huit fois plus.
    hist = lire_cache("crypto_histoire_cache.js") or {}
    histj = hist.get("jetons") or {}
    if not histj:
        print("[warn] crypto_histoire_cache absent : les fiches n'annonceront "
              "aucun historique. Lancer fetch_crypto_capture.py --histoire.",
              file=sys.stderr)

    # Un jeton peut appartenir a plusieurs narratifs : on garde le premier, et
    # l'on note tous ses narratifs pour l'affichage.
    jetons, narratifs = {}, {}
    for n in nf.get("narratives", []):
        for t in n.get("tokens", []):
            if t.get("is_stock"):
                continue
            jetons.setdefault(t["id"], dict(t))
            narratifs.setdefault(t["id"], []).append(n["narrative"])

    classes = sorted([t for t in jetons.values()
                      if isinstance(t.get("mcap_b"), (int, float))],
                     key=lambda t: -t["mcap_b"])[:TOP_N]

    sortie = []
    for rang, t in enumerate(classes, 1):
        cid = t["id"]
        fams = narratifs.get(cid, [])
        famille = None
        for f in fams:
            if f in FAMILLES:
                famille = FAMILLES[f]
                break
        famille = famille or "speculatif"

        # ── Un jeton qui porte sa propre chaine est une chaine ────────────
        # Le narratif dit ce que le jeton FAIT ; le cache L1 dit ce qu'il EST.
        # HYPE n'etait « Protocole DeFi » que parce que son seul narratif est
        # « Perp DEX », alors que Hyperliquid porte 6,65 Md$ de TVL. On ne
        # promeut pas tout ce que le cache L1 connait : Bitcoin y figure aussi,
        # avec 4 Md$ de TVL pour 1 550 Md$ de capitalisation. Le depart se fait
        # donc sur le POIDS de la chaine.
        if famille in ("protocole", "plateforme"):
            _l1t = next((v for v in (l1.get("tokens") or {}).values()
                         if v.get("coingecko_id") == cid), None)
            if _l1t:
                _tvl, _mc = _l1t.get("tvl_b"), t.get("mcap_b")
                if (isinstance(_tvl, (int, float)) and isinstance(_mc, (int, float))
                        and _mc > 0 and _tvl / _mc > 0.01):
                    famille = "chaine"

        # Grandeurs derivees, calculees ici pour etre rattrapables hors ligne.
        mcap, fdv = t.get("mcap_b"), t.get("fdv_b")
        # Borne a 1 : la capitalisation ne peut pas depasser la valeur
        # pleinement diluee, qui compte les memes jetons plus ceux a emettre.
        t["mcap_fdv"] = (min(round(mcap / fdv, 4), 1.0)
                         if (mcap and fdv and fdv > 0) else None)
        tvl = t.get("tvl_b")
        t["fdv_tvl"] = round(fdv / tvl, 2) if (fdv and tvl and tvl > 0) else None
        prix = t.get("price")

        # ── Le greffon « captation, âge et histoire » ────────────────────
        c = capj.get(cid) or {}
        for k in ("frais_m", "revenu_m", "detenteurs_m", "taux_captation_pct",
                  "part_detenteurs_pct", "rendement_detenteurs_pct",
                  "rendement_detenteurs_fdv_pct", "rendement_revenu_pct",
                  "cycles", "pire_chute_pct", "genesis", "premiere_cotation",
                  "age_jours", "age_source", "atl_chg_pct", "atl_date",
                  "ath_date", "pays", "rang_cg", "dev_commits_4s",
                  "dev_contributeurs", "dev_etoiles", "com_twitter",
                  "com_reddit", "suivi_cg", "captation_detail",
                  "captation_incoherente"):
            t[k] = c.get(k)
        t["age_annees"] = (round(c["age_jours"] / 365.25, 1)
                           if isinstance(c.get("age_jours"), int) else None)

        # ── LE CHIFFRE ÉCRIT DOIT ÊTRE CELUI DU GRAPHE ──────────────────
        # ⚠ La série mensuelle des frais porte un bloc `controle` fait pour
        # refaire le total annuel à partir des points publiés. Sur 197 jetons
        # sur 200, `frais_m` et ce total coïncident au centième près. Sur
        # trois, ils divergent d'un facteur : Hyperliquid écrivait 1 313,4 M$
        # là où sa propre série somme à 11,35 — cent seize fois — et son
        # `frais_m` valait EXACTEMENT son `l1_frais_m`, la grandeur de la
        # CHAÎNE, qui avait débordé dans le champ du protocole. Cosmos ×133,
        # XRP ×0,3.
        # La fiche affichait donc, à quinze centimètres d'écart, un montant et
        # un graphe qui se contredisent, sans qu'aucun des deux ne se dise
        # faux. Le contrôle est l'arbitre : il est calculé SUR les points
        # tracés. On lui donne raison, et on garde trace du montant écarté.
        ctrl = (((histj.get(cid) or {}).get("mensuel") or {})
                .get("frais") or {}).get("controle") or {}
        total_serie = ctrl.get("total1y_source")
        fm = t.get("frais_m")
        if (isinstance(total_serie, (int, float)) and total_serie > 0
                and isinstance(fm, (int, float)) and fm > 0):
            rapport = fm / total_serie
            if rapport > 1.5 or rapport < 0.67:
                t["frais_m"] = round(total_serie, 4)
                t["frais_m_ecarte"] = {
                    "valeur_ecartee": fm,
                    "raison": ("Le montant du cache de captation contredisait la "
                               "somme de la série mensuelle publiée (facteur "
                               "%.4g). C'est la série qui fait foi : elle est ce "
                               "que la fiche trace." % rapport),
                }
        # L'ATH vient du cache de captation quand il l'a, du cache L1 sinon :
        # les deux le publient, et le premier couvre deux cents jetons quand
        # le second en couvre quinze.
        t["ath_chg_pct"] = c.get("ath_chg_pct")

        # ── L'index de l'historique ──────────────────────────────────────
        # `histoire_mois` compte la série la PLUS LONGUE, et `histoire_depuis`
        # nomme le mois où elle commence. Prendre la plus courte ferait dire
        # « 17 mois » à Bitcoin, dont les frais remontent à 2011 mais dont la
        # TVL ne commence qu'en 2021 — l'onglet paraîtrait vide alors qu'il
        # porte quinze ans. Le détail par série reste publié, pour que la
        # fiche puisse dire laquelle va jusqu'où.
        h = histj.get(cid) or {}
        hm = h.get("mensuel") or {}
        t["histoire_series"] = sorted(hm) or None
        t["histoire_mois"] = max(
            (len(b.get("valeurs") or []) for b in hm.values()), default=None) or None
        t["histoire_depuis"] = min(
            (b["debut"] for b in hm.values() if b.get("debut")), default=None)
        # L'absence porte sa raison, comme partout ailleurs : « ce jeton n'a
        # pas d'historique publié » n'est pas la même phrase que « nous ne
        # l'avons pas collecté », et la fiche doit pouvoir dire laquelle.
        t["histoire_muet"] = (h.get("muet") or None) if h else (
            None if not histj else
            {"tout": "Ce jeton n'est pas dans le cache d'historique."})

        # L'usage physique, pour les reseaux. Aujourd'hui seul Akash porte le
        # detail ; les autres ont au moins leurs frais.
        t["usage_taux"] = None
        t["usage_frais_m"] = None
        pr = ia_projets.get(cid)
        if pr and isinstance(pr.get("fee1y"), (int, float)):
            t["usage_frais_m"] = round(pr["fee1y"] / 1e6, 3)
        if cid == "akash-network" and isinstance(akash.get("gpuUtil"), (int, float)):
            t["usage_taux"] = akash["gpuUtil"]
            t["usage_detail"] = {
                "gpu_actifs": akash.get("activeGPU"),
                "gpu_total": akash.get("totalGPU"),
                "fournisseurs": akash.get("providers"),
                "baux_actifs": akash.get("activeLeases"),
                "depense_jour_usd": akash.get("dailyUsdc"),
            }

        # ── Le greffon L1 ────────────────────────────────────────────
        # Le cache de la page « Valorisation d'une blockchain » couvre quinze
        # chaines, indexees par symbole mais porteuses du coingecko_id : c'est
        # LUI qui sert de jointure, le symbole etant ambigu.
        for k in ("capt_nette_pct", "real_yield", "nvt_ratio",
                  "adresses_actives_k", "l1_frais_m", "l1_inflation",
                  "l1_staking_apy", "l1_capt_base"):
            t.setdefault(k, None)
        for _sym, _l1 in (l1.get("tokens") or {}).items():
            if _l1.get("coingecko_id") != cid:
                continue

            def _n(x):
                v = _l1.get(x)
                return v if isinstance(v, (int, float)) else None
            t["capt_nette_pct"] = _n("capt_nette_pct")
            t["real_yield"] = _n("real_yield")
            t["nvt_ratio"] = _n("nvt_ratio")
            aa = _n("active_addresses_7d_avg")
            t["adresses_actives_k"] = round(aa / 1000.0, 1) if aa else None
            if t.get("ath_chg_pct") is None:
                t["ath_chg_pct"] = _n("ath_chg_pct")
            t["l1_frais_m"] = _n("fees_m")
            t["l1_inflation"] = _n("inflation")
            t["l1_staking_apy"] = _n("staking_apy")
            t["l1_capt_base"] = _l1.get("capt_base")
            break

        # ══ LE PROFIL — c'est ici que le cas par cas entre en jeu ═══════
        p = profil_de(cid, famille)
        muets = set(p.get("muets") or ())

        # La note, ramenee aux seuls criteres que ce jeton POUVAIT obtenir.
        criteres, total, notables = [], 0.0, 0
        for cle, haut, bas in p["criteres"]:
            g = GRANDEURS.get(cle)
            if not g:
                continue
            lib, sens, suf, note_lib = g
            v = t.get(cle)
            if denominateur_degenere(cle, v, t):
                criteres.append({
                    "cle": cle, "libelle": lib, "valeur": v, "point": None,
                    "statut": "degenere", "seuil_haut": haut, "seuil_bas": bas,
                    "sens": sens, "suffixe": suf, "note": note_lib,
                })
                continue
            pt = noter(v, haut, bas, sens)
            if pt is None:
                statut = "muet_attendu" if cle in muets else "muet"
            else:
                statut = "note"
                total += pt
                notables += 1
            criteres.append({
                "cle": cle, "libelle": lib, "valeur": v, "point": pt,
                "statut": statut, "seuil_haut": haut, "seuil_bas": bas,
                "sens": sens, "suffixe": suf, "note": note_lib,
            })

        # Refusee si trop peu de criteres : une note sur deux points n'est pas
        # une note. Le denominateur compte les criteres APPLICABLES, jamais
        # ceux que le profil enumere : un critere hors de portee ne doit pas
        # relever la barre de qui ne l'a jamais eu.
        applicables = sum(1 for x in criteres if x["statut"] in ("note", "muet"))
        assez = notables >= max(3, int(applicables * 0.6))
        note20 = round(20.0 * total / notables, 1) if (notables and assez) else None
        # Une enveloppe, une matière tokenisée, une action tokenisée : on ne
        # note PAS. Leur valeur est celle du sous-jacent, et une note laisserait
        # croire qu'on a jugé un actif quand on n'a jugé qu'un emballage.
        if p.get("sans_note"):
            note20 = None

        # ── Les six axes du radar ────────────────────────────────────────
        # Chacun porte sa valeur ET son percentile dans le groupe qui lui
        # ressemble. Le percentile se calcule plus bas, quand tous les jetons
        # sont connus : ici on ne fait que déclarer les axes retenus.
        axes = []
        for cle in p["axes"]:
            g = GRANDEURS.get(cle)
            if not g:
                continue
            lib, sens, suf, expl = g
            axes.append({"cle": cle, "libelle": lib, "sens": sens,
                         "suffixe": suf, "explication": expl,
                         "valeur": t.get(cle)})

        sortie.append({
            "id": cid, "symbole": t.get("symbol"), "nom": t.get("name"),
            "image": t.get("image"), "rang": rang,
            "famille": famille,
            "famille_lib": p.get("lib") or ARCHETYPES[p["archetype"]]["lib"],
            "archetype": p["archetype"],
            "profil_nomme": p.get("nomme", False),
            "these": p.get("these") or ARCHETYPES[p["archetype"]]["these"],
            "note_axes": p.get("note_axes"),
            "sousjacent": p.get("sousjacent"),
            "sans_note": bool(p.get("sans_note")),
            "narratifs": fams,
            "prix": prix, "mcap_b": mcap, "fdv_b": fdv,
            "vol_b": t.get("vol_b"), "circ_pct": t.get("circ_pct"),
            "tvl_b": tvl, "mc_tvl": t.get("mc_tvl"), "fdv_tvl": t.get("fdv_tvl"),
            "mcap_fdv": t.get("mcap_fdv"),
            "ps_ttm": t.get("ps_ttm"), "rev_m_1y": t.get("rev_m_1y"),
            "vol_mcap_pct": t.get("vol_mcap_pct"),
            "perf_7d": t.get("perf_7d"), "perf_30d": t.get("perf_30d"),
            "perf_1y": t.get("perf_1y"),
            "usage_taux": t.get("usage_taux"),
            "usage_frais_m": t.get("usage_frais_m"),
            "usage_detail": t.get("usage_detail"),
            # ── la captation, le cœur de la fiche ──
            "frais_m": t.get("frais_m"),
            "revenu_m": t.get("revenu_m"),
            "detenteurs_m": t.get("detenteurs_m"),
            "taux_captation_pct": t.get("taux_captation_pct"),
            "part_detenteurs_pct": t.get("part_detenteurs_pct"),
            "rendement_detenteurs_pct": t.get("rendement_detenteurs_pct"),
            "rendement_detenteurs_fdv_pct": t.get("rendement_detenteurs_fdv_pct"),
            "rendement_revenu_pct": t.get("rendement_revenu_pct"),
            "captation_detail": t.get("captation_detail"),
            "captation_incoherente": t.get("captation_incoherente"),
            # ── le temps ──
            "age_jours": t.get("age_jours"), "age_annees": t.get("age_annees"),
            "age_source": t.get("age_source"), "genesis": t.get("genesis"),
            "premiere_cotation": t.get("premiere_cotation"),
            "cycles": t.get("cycles"), "pire_chute_pct": t.get("pire_chute_pct"),
            "ath_chg_pct": t.get("ath_chg_pct"), "ath_date": t.get("ath_date"),
            "atl_chg_pct": t.get("atl_chg_pct"), "atl_date": t.get("atl_date"),
            # ── l'index de l'historique (les séries sont dans
            #    crypto_histoire_cache, pas ici) ──
            "histoire_mois": t.get("histoire_mois"),
            "histoire_depuis": t.get("histoire_depuis"),
            "histoire_series": t.get("histoire_series"),
            "histoire_muet": t.get("histoire_muet"),
            # ── l'économie du réseau (quinze chaînes) ──
            "capt_nette_pct": t.get("capt_nette_pct"),
            "real_yield": t.get("real_yield"),
            "nvt_ratio": t.get("nvt_ratio"),
            "adresses_actives_k": t.get("adresses_actives_k"),
            "l1_frais_m": t.get("l1_frais_m"),
            "l1_inflation": t.get("l1_inflation"),
            "l1_staking_apy": t.get("l1_staking_apy"),
            "l1_capt_base": t.get("l1_capt_base"),
            # ── l'activité, faute de mieux ──
            "dev_commits_4s": t.get("dev_commits_4s"),
            "dev_contributeurs": t.get("dev_contributeurs"),
            "com_twitter": t.get("com_twitter"),
            "suivi_cg": t.get("suivi_cg"),
            "pays": t.get("pays"),
            "chaine_slug": (t.get("rev_source_slug")
                            if t.get("rev_source_kind") == "chain" else None),
            "note20": note20, "note_total": round(total, 2),
            "note_notables": notables, "note_max": len(p["criteres"]),
            "criteres": criteres,
            "axes": axes,
            # Les grandeurs que CE profil déclare hors sujet. La fiche s'en sert
            # pour ne pas afficher, dans « Les chiffres », un ratio que le bloc
            # d'à côté vient de déclarer sans objet — Bitcoin montrait un
            # « capitalisation / TVL » de 376 × sous un profil qui dit
            # justement qu'une réserve de valeur n'immobilise rien.
            "muets": sorted(muets),
            "raison_muet": p.get("raison"),
        })

    # ══ LES PERCENTILES DU RADAR ═══════════════════════════════════════
    # Un axe ne dit rien seul : « 3,9 % de rendement » n'est une bonne nouvelle
    # que rapporté aux autres. On situe donc chaque valeur dans son ARCHÉTYPE —
    # comparer le Capi/TVL d'une chaîne à celui d'un protocole de prêt n'aurait
    # pas de sens, les ordres de grandeur n'étant pas les mêmes.
    #
    # Quand l'archétype compte trop peu de jetons servis pour qu'un rang veuille
    # dire quelque chose, on se rabat sur l'univers entier et l'axe le dit. Le
    # seuil est à six : en dessous, un rang saute de vingt points d'un cran à
    # l'autre, et le radar dessinerait du bruit.
    SEUIL_PAIRS = 6
    # ⚠ LA DISTRIBUTION SE CONSTRUIT SUR TOUS LES JETONS QUI PORTENT LA
    # GRANDEUR, et non sur les seuls dont le PROFIL en a fait un axe. La
    # première version ne comptait que les seconds, et le résultat se voyait :
    # `real_yield` n'avait qu'UNE valeur (seul le profil de Solana le trace)
    # alors que quinze chaînes la publient, et `adresses_actives_k` en avait
    # cinq. Sous le plancher de six, ces axes n'étaient donc jamais situés :
    # 126 fiches sur 200 portaient au moins un axe sans position, et 33 en
    # avaient quatre ou plus — des radars réduits à un triangle, qui donnent
    # une forme fausse à lire.
    #
    # On balaie donc les grandeurs elles-mêmes, sur tout l'univers.
    CLES_AXES = set()
    for j in sortie:
        for a in j["axes"]:
            CLES_AXES.add(a["cle"])

    par_arch, univers_vals = {}, {}
    for j in sortie:
        for cle in CLES_AXES:
            v = j.get(cle)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                continue
            # Les ratios dégénérés n'entrent pas dans la distribution. Ils la
            # ruinaient : l'échelle du « capitalisation / TVL » des blockchains
            # allait de 0,02 à 7 222 à cause de quatre jetons dont la TVL est
            # nulle, ce qui tassait tous les autres dans le premier centile.
            if denominateur_degenere(cle, v, j):
                continue
            par_arch.setdefault((j["archetype"], cle), []).append(v)
            univers_vals.setdefault(cle, []).append(v)
    for d in (par_arch, univers_vals):
        for k in d:
            d[k].sort()

    def percentile(vals, v, sens):
        if not vals or len(vals) < 2:
            return None
        i = 0
        while i < len(vals) and vals[i] < v:
            i += 1
        p = 100.0 * i / (len(vals) - 1)
        p = max(0.0, min(100.0, p))
        return round(100.0 - p if sens == "bas" else p, 1)

    for j in sortie:
        for a in j["axes"]:
            v = a["valeur"]
            a["percentile"] = None
            a["groupe"] = None
            a["groupe_n"] = None
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                continue
            # Un ratio dont le dénominateur est effondré n'est pas situable :
            # il ne mesure que la petiteur de ce dénominateur. Il était exclu
            # des CRITÈRES mais tracé sur le RADAR — mesuré : ATOM affichait un
            # « capitalisation / TVL » de 7 222 × sur une TVL nulle, et cette
            # valeur écrasait l'échelle des percentiles de tout son archétype.
            # Même règle des deux côtés, donc : l'axe reste affiché avec sa
            # valeur, mais sans position.
            if denominateur_degenere(a["cle"], v, j):
                a["degenere"] = True
                continue
            vals = par_arch.get((j["archetype"], a["cle"]), [])
            if len(vals) >= SEUIL_PAIRS:
                a["percentile"] = percentile(vals, v, a["sens"])
                a["groupe"] = ARCHETYPES[j["archetype"]]["lib"]
                a["groupe_n"] = len(vals)
                continue
            # Le repli sur l'univers entier applique LE MÊME plancher. Sans lui,
            # BTC recevait « 100ᵉ centile » sur une distribution de DEUX
            # valeurs, et SOL un rendement réel situé parmi… une seule. Un rang
            # calculé sur moins de six pairs ne dit rien : on préfère un axe
            # sans position à une position inventée.
            vals = univers_vals.get(a["cle"], [])
            if len(vals) >= SEUIL_PAIRS:
                a["percentile"] = percentile(vals, v, a["sens"])
                a["groupe"] = "les 200 premiers actifs"
                a["groupe_n"] = len(vals)

    par_arch_n = {}
    for j in sortie:
        par_arch_n[j["archetype"]] = par_arch_n.get(j["archetype"], 0) + 1

    doc = {
        "genere_le": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_updated": nf.get("updated"),
        "capture_updated": cap.get("genere_le"),
        "histoire_updated": hist.get("genere_le"),
        # Le mois entamé du cache d'historique, repris tel quel : la fiche en a
        # besoin pour griser le dernier point au lieu de laisser voir un
        # effondrement là où il n'y a qu'un mois qui n'est pas fini.
        "histoire_mois_incomplet": hist.get("mois_incomplet"),
        "univers": len(sortie),
        "archetypes": par_arch_n,
        "archetypes_lib": {k: v["lib"] for k, v in ARCHETYPES.items()},
        "n_profils_nommes": sum(1 for j in sortie if j["profil_nomme"]),
        "methode": (
            "Chaque jeton porte un PROFIL : six axes de radar, ses critères "
            "notés, et une phrase qui dit ce qu'il prétend être. Les jetons "
            "notables ont un profil écrit à la main ; les autres héritent d'un "
            "archétype. La note est RAMENÉE aux seuls critères que l'actif "
            "pouvait obtenir, et refusée si moins de 60 % des critères "
            "APPLICABLES sont servis. Les enveloppes (stETH, WBTC…), l'or "
            "tokenisé et les actions tokenisées ne sont pas notés : leur valeur "
            "est celle du sous-jacent."),
        "methode_captation": (
            "La captation se lit en trois étages : les utilisateurs paient des "
            "FRAIS, le protocole en garde un REVENU, une partie seulement "
            "revient aux DÉTENTEURS. C'est le dernier étage qui décide de la "
            "valeur d'un jeton, et c'est celui que la plupart des analyses "
            "omettent — Uniswap facture 872 M$ et en reverse 4,4 %."),
        "avertissement": (
            "Les seuils sont des reperes, pas des verites : ils viennent des "
            "ordres de grandeur observes dans les caches, et un marche entier "
            "peut se deplacer. La note dit « par rapport a ces reperes », pas "
            "« bon » ou « mauvais »."),
        "jetons": sortie,
    }

    js = os.path.join(CACHE, "crypto_fiches.js")
    tmp = js + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("window.__CRYPTO_FICHES__=")
        json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";\n")
    os.replace(tmp, js)

    notees = sum(1 for j in sortie if j["note20"] is not None)
    ncapt = sum(1 for j in sortie if j["part_detenteurs_pct"] is not None)
    nage = sum(1 for j in sortie if j["age_annees"] is not None)
    nhist = sum(1 for j in sortie if j.get("histoire_mois"))
    mois_tot = sum(j.get("histoire_mois") or 0 for j in sortie)
    print("[ok] %d jetons, %d notes (%.0f %%)"
          % (len(sortie), notees, 100.0 * notees / max(1, len(sortie))))
    print("     profils ecrits a la main : %d" % doc["n_profils_nommes"])
    print("     part revenant au jeton   : %d" % ncapt)
    print("     age connu                : %d" % nage)
    print("     historique disponible    : %d jetons, %d mois cumules "
          "(%.0f mois en moyenne)"
          % (nhist, mois_tot, mois_tot / max(1, nhist)))
    for f, n in sorted(par_arch_n.items(), key=lambda kv: -kv[1]):
        nn = sum(1 for j in sortie if j["archetype"] == f and j["note20"] is not None)
        print("     %-18s %3d jetons, %3d notes" % (f, n, nn))
    print("     ecrit %s (%.0f Ko)" % (js, os.path.getsize(js) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
