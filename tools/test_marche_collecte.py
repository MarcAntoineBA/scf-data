#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_marche_collecte.py — Deux garde-fous de la collecte de marché
(fetch_marche_actions.py) : une PLACE entière qui quitte la source ne fait plus
refuser toute la collecte, un bridage si ; et les devises que le serveur n'a
jamais eues (riyal, dirham, dollar de Taïwan) ont toujours un taux.

CE QU'IL EMPÊCHE DE REVENIR
Le 30/09/2026, le hors-cote américain (OTCMKTS, 3 993 sociétés publiées la
veille) a quitté d'un bloc le point d'entrée de stockanalysis : 74 107 lignes
brutes contre 85 182, soit 13 % de moins. Le garde-fou « plus de 8 % de lignes
brutes en moins = bridage » a refusé la collecte ENTIÈRE et gardé les fragments
de la veille. Les quatre-vingt-cinq autres places étaient pourtant là au titre
près. Effet en ligne : l'onglet Secteurs « daté de 29 h » et toutes les fiches
figées d'un jour — sans qu'aucun bilan ne le dise autrement que par « 34/40 ».

CE QUI DOIT RESTER VRAI
  · une place sortie en bloc (≥ 200 sociétés publiées, zéro ligne) → collecte
    PUBLIÉE sans elle, la place NOMMÉE dans l'index jusqu'à son retour ;
  · un bridage (toutes les places amputées) → toujours REFUSÉ ;
  · une place disparue + un pays muet → REFUSÉ (le muet explique la place) ;
  · une place disparue qui pèse plus de 15 % → REFUSÉ (la source change) ;
  · une place disparue + une autre amputée → REFUSÉ.

ET LES TAUX (même jour, même cause d'invisibilité)
Sur le serveur, aucun cache de taux n'est présent au passage quotidien ; la BCE
ne cote ni SAR, ni AED, ni TWD. Depuis début septembre, 7 lignes en TWD au lieu
de 997 et 6 en SAR au lieu de 206 : Saudi Aramco, TSMC en cotation locale,
MediaTek, Hon Hai absents du tableau des métiers.
  · sans aucun cache ni BCE → SAR, AED (parités fixes) et TWD (Yahoo) présents ;
  · un taux du cache n'est JAMAIS écrasé par une parité ou par Yahoo ;
  · un cours Yahoo hors fourchette est écarté.

ET LES CAPITALISATIONS DONT ON IGNORE LA DEVISE (toujours le 30/09)
Les lignes en dollars de Buenos Aires n'ont pas de nombre d'actions et portent
une capitalisation en PESOS ; leur facteur se mesurait sur les cotations
hors-cote. Sans elles : Cablevisión à 1 678 Md$ au lieu de 1,1. Passage COMPLET
du collecteur (main), source simulée, sans réseau :
  · ligne invérifiable sans facteur mesuré → NON publiée, et comptée ;
  · ligne invérifiable dont la place a un facteur mesuré → publiée.

Lancer : python3 tools/test_marche_collecte.py   (aucun accès réseau)
"""
import importlib.util
import json
import os
import signal
import sys
import tempfile
import types
from pathlib import Path

ICI = os.path.dirname(os.path.abspath(__file__))
COLLECTEUR = os.environ.get("SCF_COLLECTEUR_MARCHE",
                            os.path.join(ICI, "..", "scripts", "fetch_marche_actions.py"))

spec = importlib.util.spec_from_file_location("fetch_marche_actions", COLLECTEUR)
fma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fma)
signal.alarm(0)                  # l'import arme le délai global de 20 min du collecteur

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-58s %-10s %s" % (quoi, obtenu, "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


# Le passage précédent, à l'échelle du 29/09 divisée par dix environ :
# 9 000 sociétés publiées dont 1 000 hors-cote (11 %), 12 000 lignes brutes.
PUBLIEES_AVANT = {"NYSE": 4000, "Frankfurt Stock Exchange": 4000, "OTCMKTS": 1000}
BRUTES_AVANT = {"NYSE": 5000, "Frankfurt Stock Exchange": 4000, "OTCMKTS": 3000}


def passage_precedent(dossier, publiees=PUBLIEES_AVANT, brutes=BRUTES_AVANT,
                      index=True, index_autre_jour=False, disparues_notees=None):
    """Deux fragments et, au choix, l'index — à la forme réelle."""
    total = sum(brutes.values())
    lignes = [("%s%05d" % (p[:3], i), p) for p, n in sorted(publiees.items()) for i in range(n)]
    for k, part in enumerate((lignes[::2], lignes[1::2])):
        with open(os.path.join(dossier, "marche_%02d.json" % k), "w", encoding="utf-8") as h:
            json.dump({"lignes_brutes": total, "genere_le": "2026-09-29 11:44 UTC",
                       "champs": ["name", "exchange"],
                       "societes": {s: [s, p] for s, p in part}}, h)
    if index:
        # Un index d'un AUTRE jour, qui ne connaissait pas encore le hors-cote :
        # le croire ferait retirer 0 ligne brute et refuser à tort.
        ex = {"lignes_brutes": total + (1 if index_autre_jour else 0),
              "lignes_brutes_par_place": ({p: n for p, n in brutes.items() if p != "OTCMKTS"}
                                          if index_autre_jour else brutes)}
        if disparues_notees:
            ex["places_disparues"] = disparues_notees
        with open(os.path.join(dossier, "marche_actions_index.json"), "w", encoding="utf-8") as h:
            json.dump({"exhaustivite": ex}, h)


def brut_du_jour(brutes):
    return {"%s%06d" % (p[:3], i): {"exchange": p} for p, n in brutes.items() for i in range(n)}


def juger(dossier, brutes_jour, publiees_jour, muets=()):
    """(accepté ?, places_disparues de l'index)."""
    try:
        _, notees = fma._juger_collecte(Path(dossier), brut_du_jour(brutes_jour),
                                        publiees_jour, list(muets), 86, "2026-09-30 11:30 UTC")
        return True, notees
    except SystemExit:
        return False, None


def cas(titre, attendu, brutes_jour, publiees_jour, muets=(), **precedent):
    with tempfile.TemporaryDirectory() as d:
        passage_precedent(d, **precedent)
        ok, notees = juger(d, brutes_jour, publiees_jour, muets)
    verifie(titre, "publié" if ok else "refusé", attendu)
    return notees


SANS_OTC = {"NYSE": 5000, "Frankfurt Stock Exchange": 4000}


def faux_yahoo(par_dollar):
    """Un module curl_cffi de substitution : Yahoo répond `par_dollar` unités
    pour un dollar, sans réseau."""
    rep = types.SimpleNamespace(json=lambda: {"chart": {"result": [
        {"meta": {"regularMarketPrice": par_dollar}}]}})
    req = types.SimpleNamespace(get=lambda *a, **k: rep)
    return types.SimpleNamespace(requests=req)


def taux_du_serveur():
    cache_reel, bce_reelle = fma.CACHE_DIR, fma.taux_bce
    fma.taux_bce = lambda: {"EUR": 1.17, "JPY": 0.0067}         # la BCE : ni SAR, ni AED, ni TWD
    try:
        with tempfile.TemporaryDirectory() as d:
            fma.CACHE_DIR = Path(d)                               # aucun cache de taux, comme sur le serveur
            sys.modules["curl_cffi"] = faux_yahoo(31.9)
            t = fma.charger_taux()
            verifie("SAR par parité fixe (1/3,75)", round(t.get("SAR", 0), 6), round(1 / 3.75, 6))
            verifie("AED par parité fixe (1/3,6725)", round(t.get("AED", 0), 6), round(1 / 3.6725, 6))
            verifie("TWD au marché (1/31,9)", round(t.get("TWD", 0), 6), round(1 / 31.9, 6))
            verifie("l'euro de la BCE reste celui de la BCE", t.get("EUR"), 1.17)

            sys.modules["curl_cffi"] = faux_yahoo(0.319)          # cent fois trop petit
            verifie("TWD hors fourchette : écarté", "TWD" in fma.charger_taux(), False)

            with open(os.path.join(d, "tradfi_fx_cache.json"), "w") as h:
                json.dump({"SAR": {"2026-09-29": 0.2663}, "TWD": {"2026-09-29": 0.0314}}, h)
            sys.modules["curl_cffi"] = faux_yahoo(31.9)
            t = fma.charger_taux()
            verifie("le cache gagne sur la parité fixe", t.get("SAR"), 0.2663)
            verifie("le cache gagne sur Yahoo", t.get("TWD"), 0.0314)
    finally:
        fma.CACHE_DIR, fma.taux_bce = cache_reel, bce_reelle
        sys.modules.pop("curl_cffi", None)


def ligne(nom, place, dev, px, actions, capi, pays):
    return {"name": nom, "exchange": place, "priceCurrency": dev, "price": px,
            "sharesOut": actions, "marketCap": capi, "country": pays}


SOURCE = {
    None: {"AAPL": ligne("Apple Inc.", "NASDAQ", "USD", 250.0, 15e9, 3.75e12, "United States"),
           # Invérifiable, mais NASDAQ/USD a un facteur mesuré (≈ 1, sur Apple).
           "NOSH": ligne("Sans Actions Inc.", "NASDAQ", "USD", 10.0, None, 5e9, "United States")},
    # Invérifiable, et AUCUNE ligne de Buenos Aires en dollars pour mesurer un
    # facteur : la capitalisation est en pesos, rien ne permet de le savoir.
    "AR": {"bcba/CVHD": ligne("Cablevisión Holding S.A.", "Buenos Aires Stock Exchange",
                              "USD", 5.86, None, 1678169568200, "Argentina")},
}


def passage_complet():
    """Rend (fragments publiés {symbole: ligne}, index) d'un main() hors ligne."""
    reel = {n: getattr(fma, n) for n in ("CACHE_DIR", "OUT_DIR", "PAYS", "bulk", "taux_bce")}
    reel_yahoo = getattr(fma, "taux_yahoo", None)
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "univers_actions.json"), "w", encoding="utf-8") as h:
            json.dump({"titres": [{"sa": "AAPL", "yahoo": "AAPL"}, {"sa": "NOSH", "yahoo": "NOSH"},
                                  {"sa": "bcba/CVHD", "yahoo": "CVHD.BA"}]}, h)
        fma.CACHE_DIR = fma.OUT_DIR = Path(d)
        fma.PAYS = ["AR"]
        fma.bulk = lambda champs, pays=None: SOURCE.get(pays, {})
        fma.taux_bce = lambda: {"EUR": 1.17}
        fma.taux_yahoo = lambda devises: {}
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                fma.main()
            publies = {}
            for f in Path(d).glob("marche_[0-9]*.json"):
                x = json.load(open(f, encoding="utf-8"))
                for k, r in x["societes"].items():
                    publies[k] = dict(zip(x["champs"], r))
            index = json.load(open(os.path.join(d, "marche_actions_index.json"), encoding="utf-8"))
        finally:
            for n, v in reel.items():
                setattr(fma, n, v)
            if reel_yahoo is not None:
                fma.taux_yahoo = reel_yahoo
    return publies, index


def main():
    print("LES CAPITALISATIONS — une devise qu'on ne peut plus mesurer")
    publies, index = passage_complet()
    verifie("Cablevisión (pesos lus en dollars) : non publiée", "CVHD.BA" in publies, False)
    verifie("  → comptée dans l'index",
            (index.get("exhaustivite") or {}).get("capitalisations_devise_inconnue"), 1)
    verifie("ligne invérifiable, facteur mesuré : publiée",
            (publies.get("NOSH") or {}).get("marketCapUsd"), 5000000000)
    verifie("ligne vérifiable : publiée", (publies.get("AAPL") or {}).get("marketCapUsd"),
            3750000000000)
    print()

    if not hasattr(fma, "_juger_collecte"):
        print("✗ le collecteur ne sait pas juger une collecte place par place "
              "(pas de _juger_collecte) : le cas du 30/09 serait refusé en entier")
        return 1

    print("LA RÈGLE D'AVANT — ce qu'elle faisait du 30/09")
    try:
        fma._refuser_effondrement(37428, 33450, [], 86, 85182, 74107)
        ancien = "publié"
    except SystemExit:
        ancien = "refusé"
    verifie("sans notion de place, −13 % de brut est refusé", ancien, "refusé")

    print()
    print("LE CAS RÉEL — une place sort en bloc, les autres sont intactes")
    notees = cas("hors-cote disparu, index SANS brut par place (transition)",
                 "publié", SANS_OTC, 8000, index=False)
    verifie("  → la place est nommée dans l'index", sorted(notees or {}), ["OTCMKTS"])
    verifie("  → avec ses sociétés publiées la veille",
            (notees or {}).get("OTCMKTS", {}).get("publiees_avant"), 1000)
    cas("hors-cote disparu, index AVEC brut par place", "publié", SANS_OTC, 8000)
    cas("index d'un autre jour : ignoré, jugé comme en transition", "publié",
        SANS_OTC, 8000, index_autre_jour=True)
    cas("la place grossit ailleurs le même jour", "publié",
        {"NYSE": 5100, "Frankfurt Stock Exchange": 4050}, 8100)

    print()
    print("CE QUI DOIT RESTER REFUSÉ")
    cas("bridage partout : −13 % sur chaque place, aucune à zéro", "refusé",
        {"NYSE": 4350, "Frankfurt Stock Exchange": 3480, "OTCMKTS": 2610}, 7830)
    cas("place disparue + un pays muet", "refusé", SANS_OTC, 8000, muets=("JP",))
    cas("place disparue + une autre amputée de 10 % (publiées)", "refusé",
        {"NYSE": 4500, "Frankfurt Stock Exchange": 4000}, 7200, index=False)
    cas("place disparue + une autre amputée de 10 % (brut connu)", "refusé",
        {"NYSE": 4500, "Frankfurt Stock Exchange": 3600}, 7600)
    cas("place disparue pesant 44 % (au-delà de 15 %)", "refusé",
        {"NYSE": 5000, "OTCMKTS": 3000}, 5000)
    cas("petite place (< 200 sociétés) à zéro : règle ordinaire", "publié",
        {"NYSE": 5000, "Frankfurt Stock Exchange": 4000, "OTCMKTS": 3000,
         "Lima": 0}, 9000,
        publiees={**PUBLIEES_AVANT, "Lima": 150},
        brutes={**BRUTES_AVANT, "Lima": 180})

    print()
    print("LE CONSTAT PERSISTE TANT QUE LA PLACE N'EST PAS REVENUE")
    constat = {"OTCMKTS": {"publiees_avant": 3993, "constatee_le": "2026-09-30 11:30 UTC"}}
    notees = cas("lendemain : fragments déjà sans hors-cote", "publié", SANS_OTC, 8000,
                 publiees={"NYSE": 4000, "Frankfurt Stock Exchange": 4000},
                 brutes=SANS_OTC, disparues_notees=constat)
    verifie("  → constat reporté, date d'origine conservée",
            (notees or {}).get("OTCMKTS", {}).get("constatee_le"), "2026-09-30 11:30 UTC")
    notees = cas("retour de la place", "publié", BRUTES_AVANT, 9000,
                 publiees={"NYSE": 4000, "Frankfurt Stock Exchange": 4000},
                 brutes=SANS_OTC, disparues_notees=constat)
    verifie("  → constat retiré", notees, {})

    print()
    print("LES TAUX — un serveur sans cache de change, une BCE sans riyal")
    taux_du_serveur()

    print()
    if échecs:
        print("✗ %d contrôle(s) en échec :" % len(échecs))
        for e in échecs:
            print("   · %s" % e)
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
