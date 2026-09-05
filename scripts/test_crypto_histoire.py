#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_crypto_histoire.py — GARDE-FOU du cache `crypto_histoire_cache`.

POURQUOI CE TEST EXISTE
    Une série datée fausse ne se voit pas. Elle a la bonne allure, le bon
    ordre de grandeur, et le bon nombre de points : rien, à l'œil, ne
    distingue une courbe décalée d'un mois d'une courbe juste. Quatre pannes
    de cette famille ont été rencontrées en construisant le collecteur, et
    aucune ne se serait signalée toute seule :

    1. LA SOURCE ÉTAIT MUETTE PARCE QU'ON LUI DEMANDAIT DE L'ÊTRE. Le
       collecteur passait `excludeTotalDataChart` : la même requête, sans ce
       paramètre, rend 3 085 jours de détail quotidien. Le cache avait zéro
       série datée, et personne ne pouvait le déduire de sa lecture.

    2. UN PAS DE TRENTE JOURS ENJAMBE UN MOIS COURT. Mesuré sur Bitcoin, la
       grille passait du 2018-10-31 au 2018-12-01 : novembre 2018 sortait à
       `null`, c'est-à-dire « la source ne publie pas » — alors que la source
       publie et que c'était notre échantillonnage qui était trop lâche. Le
       pas est passé à quinze jours.

    3. DEUX SLUGS DE CHAÎNE SUR TRENTE-SEPT NE SONT PAS CEUX QU'ON CROIT.
       /summary/fees/optimism et /summary/fees/cosmos rendent HTTP 400 ; il
       faut `op-mainnet` et `cosmoshub`. Et /overview/fees/dogecoin rend 500
       là où /summary/fees/doge rend douze ans de frais. Une chaîne au mauvais
       slug ne lève pas d'erreur : elle disparaît, simplement.

    4. LE CONTRÔLE COMPARAIT DEUX CHOSES DIFFÉRENTES. Sushi Perps et Sushi
       Launchpad annoncent un total annuel sans jamais paraître dans le
       détail quotidien : le total confronté à la série contenait 671 459 $
       que la série ne pouvait pas contenir, soit 4,16 % d'écart sur SUSHI
       pour une raison étrangère aux dates.

CE QUE LE TEST VÉRIFIE
    · qu'aucune série de référence n'est plus courte qu'un plancher mesuré ;
    · que le total annuel REFAIT depuis le tableau mensuel publié retombe à
      moins de 2 % du `total1y` que la source annonce. C'est le contrôle qui
      prouve qu'aucune date n'a glissé : décalée d'un seul jour, la même
      fenêtre fait sortir dix-sept protocoles de la barre des 2 % ;
    · qu'aucun mois n'est sauté dans le cours des jetons de référence ;
    · que toute absence porte sa raison, et qu'aucune raison ne contredit une
      série présente ;
    · qu'assez de jetons portent chaque série — un transfert de 26 Mo qui
      meurt en route rend un cache complet en apparence et vide de flux ;
    · qu'aucune série ne dépasse le mois de la collecte.

CE QUE LE TEST NE VÉRIFIE PAS
    Ni la justesse des chiffres chez DeFiLlama, ni la fraîcheur du cache
    (watchdog dédié), ni le rendu de la fiche.

USAGE
    python3 test_crypto_histoire.py
    python3 test_crypto_histoire.py --cache /chemin/vers/le/dossier
    Sortie non nulle = au moins un contrôle en échec.
"""

import argparse
import json
import os
import sys

RACINE = os.path.dirname(os.path.abspath(__file__))

# ── Les planchers ─────────────────────────────────────────────────────────
# (jeton, série, nombre minimal de mois, premier mois AU PLUS TARD)
#
# Les nombres viennent d'une collecte réelle du 2026-09-05, rabaissés de 5 à
# 8 % : une série ne PERD pas de mois avec le temps, elle en gagne un par
# mois. Un plancher qui casse veut donc dire qu'on a cessé de collecter ce
# qu'on collectait, jamais que le temps a passé.
#
# Le choix des jetons n'est pas décoratif : `optimism` et `cosmos` gardent les
# deux slugs corrigés, `dogecoin` garde la chaîne récupérée sous le slug
# `doge`, `bitcoin` et `litecoin` gardent l'accès aux frais d'avant 2018 que
# le bulk ne connaît pas, `sushi` garde le contrôle réparé.
PLANCHERS = [
    ("bitcoin",       "frais",      180, "2011-03"),
    ("bitcoin",       "cours",      155, "2013-07"),
    ("bitcoin",       "tvl",         62, "2021-05"),
    ("ethereum",      "frais",      128, "2015-10"),
    ("ethereum",      "revenu",     128, "2015-10"),
    ("ethereum",      "detenteurs", 128, "2015-10"),
    ("ethereum",      "tvl",        104, "2017-11"),
    ("ethereum",      "cours",      128, "2015-10"),
    ("dogecoin",      "frais",      146, "2014-03"),
    ("dogecoin",      "cours",      147, "2014-02"),
    ("litecoin",      "frais",      154, "2013-06"),
    ("solana",        "frais",       65, "2021-03"),
    ("solana",        "cours",       74, "2020-06"),
    ("uniswap",       "frais",       90, "2019-01"),
    ("uniswap",       "cours",       69, "2020-11"),
    ("aave",          "frais",       66, "2021-02"),
    ("lido-dao",      "frais",       62, "2021-06"),
    ("chainlink",     "frais",       34, "2023-10"),
    ("chainlink",     "cours",      101, "2018-01"),
    ("hyperliquid",   "frais",       17, "2025-04"),
    ("hyperliquid",   "cours",       19, "2025-02"),
    ("akash-network", "cours",       68, "2020-12"),
    ("optimism",      "frais",       55, "2022-01"),
    ("cosmos",        "frais",       15, "2025-07"),
    ("sushi",         "frais",       69, "2020-11"),
    ("tron",          "frais",       76, "2020-04"),
    ("cardano",       "frais",       92, "2018-11"),
]

# Le cours de ces jetons-là ne doit AUCUN trou : ils sont cotés en continu
# depuis leur naissance, et un `null` intérieur y signalerait le retour du
# pas de trente jours (cf. panne nº 2). D'autres jetons en ont de vrais —
# mesuré : bitget-token n'a aucun prix chez la source du 2020-06-24 au
# 2021-03-21, soit 270 jours. Leurs trous sont la vérité, pas un défaut.
COURS_SANS_TROU = ["bitcoin", "ethereum", "dogecoin", "chainlink", "solana",
                   "litecoin", "cardano"]

ECART_MAX_PCT = 2.0


def lire_cache(dossier):
    for nom in ("crypto_histoire_cache.json", "crypto_histoire_cache.js"):
        chemin = os.path.join(dossier, nom)
        if not os.path.exists(chemin):
            continue
        t = open(chemin, encoding="utf-8").read()
        if nom.endswith(".json"):
            return json.loads(t), chemin
        i = t.find("{")
        return json.loads(t[i:t.rindex("}") + 1]), chemin
    return None, None


def _mois_suivant(m):
    a, mo = int(m[:4]), int(m[5:7])
    return "%04d-%02d" % (a + 1, 1) if mo == 12 else "%04d-%02d" % (a, mo + 1)


def _rang(depart, mois):
    """Index d'un mois dans un tableau qui commence à `depart`."""
    return (int(mois[:4]) - int(depart[:4])) * 12 + \
           (int(mois[5:7]) - int(depart[5:7]))


def _fin(depart, n):
    r = (int(depart[5:7]) - 1) + n - 1
    return "%04d-%02d" % (int(depart[:4]) + r // 12, r % 12 + 1)


def controle_planchers(jetons):
    echecs = []
    for cid, cle, mini, debut_max in PLANCHERS:
        j = jetons.get(cid)
        if not j:
            echecs.append("%s : absent du cache" % cid)
            continue
        b = (j.get("mensuel") or {}).get(cle)
        if not b:
            raison = (j.get("muet") or {}).get(cle)
            echecs.append("%s / %s : série absente%s"
                          % (cid, cle, (" — la source dit : « %s »" % raison)
                             if raison else " et sans raison publiée"))
            continue
        n = len(b.get("valeurs") or [])
        if n < mini:
            echecs.append("%s / %s : %d mois, plancher %d"
                          % (cid, cle, n, mini))
        if b.get("debut", "9999-99") > debut_max:
            echecs.append("%s / %s : commence en %s, attendu au plus tard %s"
                          % (cid, cle, b.get("debut"), debut_max))
    return echecs


def controle_annuel(jetons):
    """Refait le total annuel de la source depuis le TABLEAU MENSUEL publié.

    La fenêtre de 365 jours ne tombe pas sur des mois entiers : le collecteur
    publie donc les deux bords tronqués, et on additionne bord + mois entiers
    lus dans le tableau + bord. Un mois hors du tableau vaut zéro — c'est le
    cas d'un jeton né après le début de la fenêtre, dont les deux bords valent
    zéro eux aussi.
    """
    echecs, verifies, pire = [], 0, (0.0, None)
    for cid, j in jetons.items():
        for cle, b in (j.get("mensuel") or {}).items():
            c = b.get("controle")
            if not c:
                continue
            verifies += 1
            depart, valeurs = b["debut"], b["valeurs"]
            somme, m = 0.0, c["premier_mois_entier"]
            garde = 0
            while garde < 600:
                garde += 1
                i = _rang(depart, m)
                if 0 <= i < len(valeurs) and valeurs[i] is not None:
                    somme += valeurs[i]
                if m == c["dernier_mois_entier"]:
                    break
                m = _mois_suivant(m)
            total = somme + (c.get("bord_debut") or 0) + (c.get("bord_fin") or 0)
            attendu = c.get("total1y_source") or 0
            if attendu <= 0:
                continue
            ecart = abs(total - attendu) / attendu * 100.0
            if ecart > pire[0]:
                pire = (ecart, "%s / %s" % (cid, cle))
            if ecart > ECART_MAX_PCT:
                echecs.append(
                    "%s / %s : total refait %.6g contre %.6g annoncé — %.2f %% "
                    "d'écart (fenêtre %s → %s)"
                    % (cid, cle, total, attendu, ecart,
                       c["fenetre"][0], c["fenetre"][1]))
    return echecs, verifies, pire


def controle_trous(jetons):
    echecs = []
    for cid in COURS_SANS_TROU:
        b = ((jetons.get(cid) or {}).get("mensuel") or {}).get("cours")
        if not b:
            echecs.append("%s : pas de cours mensuel" % cid)
            continue
        trous = [i for i, v in enumerate(b["valeurs"]) if v is None]
        if trous:
            noms = [_fin(b["debut"], i + 1) for i in trous[:5]]
            echecs.append("%s / cours : %d mois sautés (%s%s) — le pas "
                          "d'échantillonnage est redevenu trop lâche"
                          % (cid, len(trous), ", ".join(noms),
                             " …" if len(trous) > 5 else ""))
    return echecs


# Les cinq séries qu'une fiche complète porte. Chacune est soit là, soit
# expliquée — jamais simplement absente.
SERIES_ATTENDUES = ("frais", "revenu", "detenteurs", "tvl", "cours")


def controle_absences(jetons):
    """Toute absence porte sa raison, et aucune raison ne ment.

    ⚠ CE CONTRÔLE A DÉJÀ ÉTÉ TROP INDULGENT UNE FOIS, ET ÇA S'EST VU SUR
    AKASH. Il ne réclamait une raison que pour un jeton SANS AUCUNE série.
    Akash en avait une — son cours — et ses quatre autres cases (frais,
    revenu, détenteurs, TVL) sortaient vides et muettes : la fiche montrait
    quatre trous sans un mot, ce qui se lit comme une panne de collecte alors
    que c'est la source qui se tait (vérifié le 05/09/2026 :
    /summary/fees/akash-network rend 400, /overview/fees/akash-network rend
    500, et son nom n'est dans aucun jour du détail quotidien). Les deux
    situations appellent des gestes opposés — relancer la collecte, ou écrire
    que la donnée n'existe pas —, donc le cache doit les distinguer.
    On exige désormais une raison POUR CHAQUE série manquante.
    """
    echecs = []
    for cid, j in jetons.items():
        men = j.get("mensuel") or {}
        muet = j.get("muet") or {}
        if not men and not muet:
            echecs.append("%s : aucune série et aucune raison publiée" % cid)
        for cle in muet:
            if cle in men:
                echecs.append("%s / %s : déclaré non publié ALORS QUE la série "
                              "est là — la fiche afficherait un trou par-dessus "
                              "une donnée" % (cid, cle))
        if "tout" in muet:
            continue
        for cle in SERIES_ATTENDUES:
            if cle not in men and cle not in muet:
                echecs.append("%s / %s : absente ET sans raison — la fiche "
                              "montrerait un trou muet, qui se lit comme une "
                              "panne de collecte" % (cid, cle))
    return echecs


# ── Le plancher de COUVERTURE ────────────────────────────────────────────
# ⚠ LA PANNE QUE CE CONTRÔLE ATTRAPE, ET QUE LES PLANCHERS PAR JETON
# LAISSAIENT PASSER.
# Le bulk /overview/fees fait 26,7 Mo pour dailyFees, 22,3 pour dailyRevenue
# et 10,7 pour dailyHoldersRevenue — mesuré le 05/09/2026. Sans en-tête
# `Accept-Encoding: gzip`, ce transfert a été mesuré à plus de sept minutes
# avant de mourir sur `IncompleteRead(20 730 855 octets lus)`. Or `_get`
# rend `None` après ses quatre essais, et le collecteur écrit alors un cache
# COMPLET EN APPARENCE : deux cents jetons, tous leurs cours (qui viennent
# d'un autre endpoint, léger, qui n'échoue pas), et presque aucune série de
# flux. Rien dans le document ne dit qu'une source a manqué.
# Les planchers par jeton ne suffisent pas : ils ne surveillent qu'une
# vingtaine de jetons nommés, et un effondrement qui épargne Bitcoin et
# Ethereum passerait entier. On compte donc combien de jetons portent chaque
# série, et on refuse une chute franche.
#
# Les nombres viennent de la collecte du 05/09/2026 (frais 91, revenu 89,
# détenteurs 64, TVL 46, cours 200 sur 200 jetons), abaissés d'environ 15 %.
# Cette marge est là pour absorber ce que la SOURCE peut légitimement retirer
# d'un jour à l'autre ; en dessous, c'est nous qui avons cessé de collecter.
COUVERTURE_MINI = {"frais": 77, "revenu": 75, "detenteurs": 54,
                   "tvl": 39, "cours": 190}


def controle_couverture(doc, jetons):
    echecs = []
    couv = doc.get("couverture") or {}
    for cle, mini in COUVERTURE_MINI.items():
        # On recompte depuis les séries plutôt que de croire l'en-tête : un
        # compteur juste posé sur un cache faux ne prouverait rien.
        n = sum(1 for j in jetons.values() if cle in (j.get("mensuel") or {}))
        if n < mini:
            echecs.append(
                "%s : %d jetons servis, plancher %d — une source a manqué et "
                "le cache n'en dit rien (l'en-tête en annonce %s)"
                % (cle, n, mini, couv.get(cle)))
    return echecs


def controle_horizon(doc, jetons):
    """Aucune série ne dépasse le mois de la collecte.

    Un décalage de dates vers l'avant produit des mois qui n'ont pas encore
    eu lieu. C'est le seul symptôme visible d'un décalage, et il est gratuit
    à vérifier.
    """
    echecs, atteint = [], False
    limite = doc.get("mois_incomplet")
    if not limite:
        return ["le document ne nomme pas son mois incomplet : la fiche ne "
                "peut pas distinguer un mois entamé d'un effondrement"], False
    for cid, j in jetons.items():
        for cle, b in (j.get("mensuel") or {}).items():
            f = _fin(b["debut"], len(b["valeurs"]))
            if f > limite:
                echecs.append("%s / %s : va jusqu'à %s, après le mois de "
                              "collecte %s" % (cid, cle, f, limite))
            elif f == limite:
                atteint = True
    if not atteint:
        echecs.append("aucune série n'atteint le mois de collecte %s : la "
                      "collecte a-t-elle vraiment eu lieu ?" % limite)
    return echecs, True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=os.environ.get("SCF_CACHE", RACINE),
                    help="dossier où lire crypto_histoire_cache")
    args = ap.parse_args()

    doc, chemin = lire_cache(args.cache)
    if not doc:
        print("ÉCHEC · crypto_histoire_cache introuvable dans %s" % args.cache)
        return 1
    jetons = doc.get("jetons") or {}
    if not jetons:
        print("ÉCHEC · %s ne porte aucun jeton" % chemin)
        return 1

    blocs = [("planchers de longueur", controle_planchers(jetons))]
    ec_annuel, verifies, pire = controle_annuel(jetons)
    blocs.append(("total annuel refait depuis la série", ec_annuel))
    blocs.append(("mois sautés dans le cours", controle_trous(jetons)))
    blocs.append(("absences expliquées", controle_absences(jetons)))
    blocs.append(("couverture", controle_couverture(doc, jetons)))
    ec_h, _ = controle_horizon(doc, jetons)
    blocs.append(("horizon", ec_h))

    total = sum(len(e) for _, e in blocs)
    if total:
        print("ÉCHEC · crypto_histoire_cache — %d contrôle(s) en échec\n"
              "         (%s)" % (total, chemin))
        for titre, ec in blocs:
            if ec:
                print("\n  · %s :" % titre)
                for x in ec:
                    print("      %s" % x)
        return 1

    n_series = sum(len(j.get("mensuel") or {}) for j in jetons.values())
    n_pts = sum(sum(1 for v in b["valeurs"] if v is not None)
                for j in jetons.values()
                for b in (j.get("mensuel") or {}).values())
    print("OK · crypto_histoire_cache · %d jetons · %d séries mensuelles · "
          "%d points datés" % (len(jetons), n_series, n_pts))
    print("     %d totaux annuels refaits depuis le tableau publié, "
          "écart maximal %.4f %% (%s)" % (verifies, pire[0], pire[1]))
    print("     %s" % chemin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
