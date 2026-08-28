#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_filet_captation.py — Le filet doit rendre les champs que la PAGE lit.

CE QU'IL EMPÊCHE DE REVENIR
Le 2026-08-28 à 23 h 30, la page Valorisation L1 a affiché « Feed health — 1 feed(s)
en N/A inattendu : Captation de valeur indisponible pour BTC, ETH, SOL, BNB, XRP,
ADA, AVAX, NEAR, SUI, APT, TON, TRX, HYPE, TAO » — soit tout le parc. Le bandeau
accusait DefiLlama. DefiLlama répondait parfaitement.

Deux défauts se sont additionnés :

1. LA CAUSE. Le binaire lancé par launchd n'était pas celui du dépôt : c'était la
   version du 20 août, d'avant la captation. Elle n'a rien perdu — elle ne sait pas
   écrire ces champs. Traité par tools/derive_collecteurs.py, qui compare ce qui
   TOURNE à ce qui est écrit.

2. LE FILET QUI NE RATTRAPE RIEN, testé ici. PRESERVE_FIELDS restaurait bien les
   ingrédients (capt_frais_m, capt_detenteurs_m) mais AUCUNE des trois grandeurs que
   la page affiche (capt_taux_pct, capt_rendement_pct, capt_nette_pct), calculées
   plus haut dans la boucle, avant le filet. Le filet se refermait sur les
   ingrédients pendant que le plat restait vide. Une simple panne DefiLlama de plus
   de quatre heures aurait donc produit EXACTEMENT le même écran.

Les valeurs de référence ci-dessous sont celles du cache réel du 28/08/2026 14 h 07,
le dernier complet avant la panne : le recalcul doit les retrouver au chiffre près,
sinon il ne recalcule pas la même chose que la collecte.

Lancement : python3 tools/test_filet_captation.py
"""

import importlib.util
import os
import sys

CHEMIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "..", "scripts", "fetch_l1_valuation.py")
spec = importlib.util.spec_from_file_location("fetch_l1_valuation", CHEMIN)
fl1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fl1)

échecs = []


def verifie(nom, obtenu, attendu):
    ok = obtenu == attendu
    if not ok:
        échecs.append(f"{nom}\n     attendu : {attendu!r}\n     obtenu  : {obtenu!r}")
    print(f"  {'✓' if ok else '✗'} {nom}")


def apres_panne(ingredients):
    """L'état d'une entrée au sortir du filet : ingrédients restaurés du cache
    précédent, grandeurs affichées encore vides — puisque la source s'est tue."""
    entry = dict(ingredients)
    for champ in ("capt_taux_pct", "capt_rendement_pct", "capt_nette_pct",
                  "capt_pente_cents", "capt_r2"):
        entry[champ] = None
    fl1._recalculer_captation_derivee(entry)
    return entry


def main():
    print("\n— Le parc réel, ramené depuis ses seuls ingrédients —")

    # ETH : le cas ordinaire. Une chaîne qui capte une part modeste de ses frais.
    eth = apres_panne({"mcap_b": 301.751, "inflation": 0.55,
                       "capt_frais_m": 5037.1, "capt_detenteurs_m": 351.57})
    verifie("ETH taux de captation", eth["capt_taux_pct"], 6.98)
    verifie("ETH rendement de captation", eth["capt_rendement_pct"], 0.117)
    verifie("ETH captation nette", eth["capt_nette_pct"], -0.43)

    # HYPE : l'autre bout du parc, 55 % de captation et une dilution qui l'avale.
    # Si l'échelle M$/G$ était fausse d'un facteur mille, c'est ici que ça crèverait
    # les yeux — et c'est la ligne qui porte l'argument de la page.
    hype = apres_panne({"mcap_b": 18.769, "inflation": 12.0,
                        "capt_frais_m": 1306.5, "capt_detenteurs_m": 728.64})
    verifie("HYPE taux de captation", hype["capt_taux_pct"], 55.77)
    verifie("HYPE rendement de captation", hype["capt_rendement_pct"], 3.882)
    verifie("HYPE captation nette", hype["capt_nette_pct"], -8.12)

    # BTC : captation NULLE par construction. Le piège du zéro — `if h_m` l'aurait
    # traité comme une absence et rendu un N/A là où 0 est la réponse juste, celle
    # qui porte tout l'argument de la prime monétaire.
    btc = apres_panne({"mcap_b": 1601.702, "inflation": 1.68,
                       "capt_frais_m": 84.6, "capt_detenteurs_m": 0.0})
    verifie("BTC taux nul restitué (et non N/A)", btc["capt_taux_pct"], 0.0)
    verifie("BTC rendement nul restitué", btc["capt_rendement_pct"], 0.0)
    verifie("BTC captation nette = -inflation", btc["capt_nette_pct"], -1.68)

    print("\n— Ce que le recalcul n'a pas le droit de faire —")

    # Sans ingrédient, on n'invente pas. Un N/A honnête vaut mieux qu'un chiffre.
    vide = apres_panne({"mcap_b": 12.0, "inflation": 3.0,
                        "capt_frais_m": None, "capt_detenteurs_m": None})
    verifie("aucun ingrédient : taux reste N/A", vide["capt_taux_pct"], None)
    verifie("aucun ingrédient : rendement reste N/A", vide["capt_rendement_pct"], None)
    verifie("aucun ingrédient : nette reste N/A", vide["capt_nette_pct"], None)

    # DOT : la source est muette sur Polkadot depuis toujours. Des frais sans revenu
    # détenteurs ne font pas un taux — ils feraient une division sur du vide.
    dot = apres_panne({"mcap_b": 1.435, "inflation": 7.0,
                       "capt_frais_m": None, "capt_detenteurs_m": None})
    verifie("DOT reste N/A (exclusion structurelle)", dot["capt_taux_pct"], None)

    # Une capitalisation absente ne doit pas produire un rendement : le taux, lui,
    # ne dépend que des deux flux et reste calculable.
    sans_mcap = apres_panne({"mcap_b": None, "inflation": 2.0,
                             "capt_frais_m": 100.0, "capt_detenteurs_m": 25.0})
    verifie("sans capitalisation : taux calculé quand même", sans_mcap["capt_taux_pct"], 25.0)
    verifie("sans capitalisation : rendement reste N/A", sans_mcap["capt_rendement_pct"], None)
    verifie("sans capitalisation : nette reste N/A", sans_mcap["capt_nette_pct"], None)

    # Une valeur FRAÎCHE gagne toujours : le recalcul ne repasse jamais derrière la
    # collecte du jour. Sans cette règle, le filet écraserait la donnée qu'il protège.
    frais = {"mcap_b": 301.751, "inflation": 0.55,
             "capt_frais_m": 5037.1, "capt_detenteurs_m": 351.57,
             "capt_taux_pct": 42.0, "capt_rendement_pct": 9.9, "capt_nette_pct": 1.1,
             "capt_pente_cents": None, "capt_r2": None}
    fl1._recalculer_captation_derivee(frais)
    verifie("valeur fraîche non écrasée (taux)", frais["capt_taux_pct"], 42.0)
    verifie("valeur fraîche non écrasée (rendement)", frais["capt_rendement_pct"], 9.9)
    verifie("valeur fraîche non écrasée (nette)", frais["capt_nette_pct"], 1.1)

    print("\n— Le filet couvre bien les champs que la page lit —")

    # La liste des champs préservés doit contenir les INGRÉDIENTS de la captation.
    # Les grandeurs affichées, elles, se recalculent : les préserver les
    # rapporterait à la capitalisation d'aujourd'hui, ce qui serait faux.
    for champ in ("capt_frais_m", "capt_detenteurs_m",
                  "capt_frais_mensuel", "capt_detenteurs_mensuel"):
        verifie(f"{champ} est préservé", champ in fl1.PRESERVE_FIELDS, True)

    # L'empreinte de capacités : elle seule permet de dire « collecteur périmé »
    # au lieu d'accuser la source à tort, comme le bandeau l'a fait le 28/08.
    verifie("le collecteur annonce savoir faire la captation",
            "captation" in fl1.COLLECTEUR_CAPACITES, True)

    print()
    if échecs:
        print("ÉCHEC — %d cas :\n" % len(échecs))
        for e in échecs:
            print("  · " + e)
        return 1
    print("Tout passe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
