#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_series_cours_narratifs.py — Garde-fou : une serie de cours porte l'identite
du jeton qu'elle pretend decrire, et un constituant mort cesse de peser.

CE QU'IL EMPECHE DE REVENIR — trois pannes mesurees le 05/09/2026

1. DES COURS D'AUTRES ACTIFS, RANGES SOUS LE BON NOM.
   Le collecteur DEVINAIT le ticker Yahoo (« {SYM}-USD ») et n'acceptait la
   serie que si son DERNIER cours tombait entre la moitie et le double du prix
   de reference. Un point sur mille huit cents : quatre series sur soixante et
   une portaient donc l'identite d'un autre actif. `canton-network`, jeton ne en
   novembre 2025, affichait quarante-deux points de 2021 a 2023.

2. UNE SERIE ENTIERE EFFACEE PAR UN SEUL POINT ABERRANT.
   Le filtre anti-preouverture ecartait tout cours sous 1 % du MAXIMUM de la
   serie. Un point a 7 149 $ chez `celestia` a donc fixe le seuil a 71 $ et
   emporte les mille huit cents points reels. Le narratif « Modulaire » se
   retrouvait sans indice — et classe treizieme sur vingt-cinq quand meme.

3. UN CONSTITUANT MORT QUI PESE ENCORE.
   Le report en avant recopiait le dernier cours connu sur toute la grille. Un
   jeton gele contribuait ainsi un rendement de ZERO a chaque barre, avec tout
   son poids : quatre indices sur vingt-cinq etaient amortis par un constituant
   arrete depuis des annees — BUIDL, vingt pour cent du panier « Actifs reels »,
   fige depuis mille quatre cent quatre-vingt-deux jours.

CE QUE LE CONTROLE FAIT
Il interroge la source pour DEUX cas nommes dont on connait la reponse, puis
verifie sur des series FABRIQUEES que le report en avant s'arrete et que le
repli sur cache refuse une serie perimee. Les contre-epreuves comptent autant
que les cas reels : un controle qui dit oui a tout ne protege de rien.
"""

import importlib.util
import os
import sys
import time
from datetime import datetime, timedelta

ICI = os.path.dirname(os.path.abspath(__file__))
COLLECTEUR = os.path.join(ICI, "..", "scripts", "fetch_narratives.py")


def charger():
    spec = importlib.util.spec_from_file_location("fn_test", COLLECTEUR)
    m = importlib.util.module_from_spec(spec)
    sys.modules["fn_test"] = m
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def jours(n):
    return (datetime.utcnow() - timedelta(days=n)).strftime("%Y-%m-%d")


def main():
    echecs = []
    reseau = "--sans-reseau" not in sys.argv
    m = charger()

    # ── 1. LES DEUX CAS REELS ────────────────────────────────────────────
    if reseau:
        meta = {
            "canton-network": {"symbol": "CC", "name": "Canton", "price": None},
            "celestia": {"symbol": "TIA", "name": "Celestia", "price": None},
        }
        try:
            h = m.fetch_token_histories(meta, days=1825)
        except Exception as e:
            h = {}
            echecs.append(f"la collecte a leve {type(e).__name__}: {e}")

        cc = h.get("canton-network") or []
        if not cc:
            echecs.append("canton-network : aucune serie rendue")
        else:
            plus_vieux = min(ts for ts, _ in cc)
            ne_le = datetime(2025, 6, 1).timestamp()
            if plus_vieux < ne_le:
                echecs.append(
                    "canton-network : un point date du "
                    + datetime.utcfromtimestamp(plus_vieux).strftime("%Y-%m-%d")
                    + " alors que le jeton est ne fin 2025 — c'est la serie d'un AUTRE actif")

        tia = h.get("celestia") or []
        if len(tia) < 100:
            echecs.append(f"celestia : {len(tia)} points, moins des cent attendus "
                          "(la serie avait ete detruite par un point aberrant)")
    else:
        print("(controle reseau saute : --sans-reseau)")

    # ── 2. LE REPORT EN AVANT S'ARRETE ───────────────────────────────────
    # Deux constituants de meme poids, l'un vivant, l'autre arrete il y a un an.
    # Si le mort pesait encore, il diviserait par deux le rendement de chaque
    # barre ; l'indice final serait a mi-chemin entre 100 et le vrai.
    grille = [jours(d) for d in range(400, -1, -5)]   # 81 jours distincts
    vivant = {g: 100.0 * (1.0 + 0.02 * i) for i, g in enumerate(grille)}
    mort = {g: 50.0 for g in grille[:32]}         # 32 points, puis plus rien depuis ~245 j
    narr = {"tokens": [
        {"id": "vivant", "symbol": "VIV", "mcap": 1e9, "is_stock": False},
        {"id": "mort",   "symbol": "MRT", "mcap": 1e9, "is_stock": False},
    ]}
    hist = {
        "vivant": [(int(datetime.strptime(g, "%Y-%m-%d").timestamp()), v)
                   for g, v in sorted(vivant.items())],
        "mort":   [(int(datetime.strptime(g, "%Y-%m-%d").timestamp()), v)
                   for g, v in sorted(mort.items())],
    }
    idx = m.compute_narrative_index(narr, hist, n_top=0)
    if not idx or not idx.get("values"):
        echecs.append("l'indice de controle n'a pas ete calcule")
    else:
        fin = idx["values"][-1]
        # MESURE, pas intuition. Le meme jeu de donnees rend 194,2 quand le
        # constituant arrete sort des barres apres quarante-cinq jours, et
        # 161,7 quand il est reporte indefiniment — il amortit alors la moitie
        # de chaque rendement. Le seuil est place entre les deux.
        if fin < 180.0:
            echecs.append(f"le constituant arrete pese encore sur l'indice : "
                          f"final {fin:.1f} au lieu de ~194 (161,7 = report sans borne)")

    # ── 3. CONTRE-EPREUVE : un constituant VIVANT ne doit PAS etre ecarte ─
    hist2 = {"vivant": hist["vivant"], "mort": hist["vivant"]}
    idx2 = m.compute_narrative_index(narr, hist2, n_top=0)
    if not idx2 or len(idx2.get("tokens", [])) != 2:
        echecs.append("deux constituants vivants devraient etre retenus tous les deux, "
                      f"or l'indice en retient {len(idx2.get('tokens', [])) if idx2 else 0}")

    # ── 4. LE SEUIL DE PEREMPTION DU REPLI EXISTE ────────────────────────
    src = open(COLLECTEUR, encoding="utf-8").read()
    if "45 * 86400" not in src:
        echecs.append("le repli sur cache n'a plus de seuil de peremption : "
                      "une serie morte serait reinjectee indefiniment")
    if "TOLERANCE_REPORT_J" not in src:
        echecs.append("le report en avant n'a plus de borne")
    if "query1.finance.yahoo.com" in src.split("def fetch_stock_histories")[0]:
        echecs.append("les cours des JETONS repassent par Yahoo : la devinette de "
                      "ticker et ses collisions sont de retour")

    print()
    if echecs:
        print(f"✗ {len(echecs)} controle(s) en echec :")
        for e in echecs:
            print(f"   · {e}")
        return 1
    print("✓ tous les controles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
