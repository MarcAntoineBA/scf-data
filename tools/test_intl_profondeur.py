#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_intl_profondeur.py — Garde-fou de la profondeur des fondamentaux
internationaux, et de l'honnêteté de la couture entre leurs trois sources.

CE QU'IL EMPÊCHE DE REVENIR

`stockanalysis.com` ne sert que CINQ exercices à un visiteur anonyme, et cette
fenêtre GLISSE. Trois défauts en découlent, tous silencieux :

1. LA PERTE. `fusionner_paquets` remplaçait la société entière d'un passage à
   l'autre. L'exercice que la fenêtre laisse tomber disparaissait donc pour
   toujours — un par an, sans erreur, sans message, sur un fichier parfaitement
   valide. C'est une amputation qui ne se voit qu'en comparant deux passages.

2. LA COUTURE MUETTE. Les exercices viennent désormais de trois maisons :
   stockanalysis, le scanner TradingView, et nos propres paquets archivés. Deux
   d'entre elles ne définissent pas « résultat brut » de la même façon — mesuré,
   36 % d'écart chez Reliance et BHP. Une courbe qui saute de 36 % à la jonction
   ment davantage qu'une courbe absente. Chaque exercice doit donc DIRE d'où il
   vient, et une grandeur qui ne se raccorde pas doit être ÉCARTÉE.

3. LE FACTEUR DE CHANGE. ⚠ TradingView publie ses fondamentaux dans la devise de
   la LIGNE COTÉE, pas dans celle des états : `ASX:BHP` rend BHP en dollars
   australiens quand elle publie en dollars américains (+47 % à +55 % sur toutes
   les grandeurs), `HKEX:700` rend Tencent en dollars de Hong Kong quand elle
   publie en yuans. C'est exactement le genre de facteur qui passe inaperçu et
   qui fausse tout ce qui en dépend.

LES QUATRE FAMILLES DE CONTRÔLE, ET CE QU'ELLES PROUVENT

  A. HORS LIGNE, SUR PIÈCES FABRIQUÉES — la mécanique de couture elle-même :
     décalage d'étiquette d'exercice, conversion de devise, refus d'un symbole
     qui ne concorde pas, mise à l'écart d'une grandeur qui ne se raccorde pas,
     et fusion par année qui ne perd jamais un exercice.

  B. SUR LES PAQUETS ÉCRITS — exhaustif, pas par échantillon : tout exercice
     déclare sa source ; toute grandeur RETENUE d'un fournisseur secondaire
     concorde à 5 % près avec la source principale sur les exercices communs ;
     aucun exercice ne mélange deux fournisseurs dans la même ligne.

  C. LE CLIQUET DE PROFONDEUR — un témoin garde la plus grande profondeur jamais
     atteinte par chaque société. Une société qui en perd fait échouer le test.
     Le témoin ne monte jamais tout seul : il s'écrit à côté des paquets et se
     relit au passage suivant.

  D. EN LIGNE (option `--reseau`) — le contrôle de bout en bout sur huit
     sociétés de huit places et huit devises : on redemande les deux
     fournisseurs et on vérifie que, pour le MÊME exercice et la MÊME grandeur,
     l'écart reste sous 5 %. C'est ce contrôle-là qui prouve qu'on n'a mélangé
     ni deux définitions ni deux devises.

Lancement :
    python3 tools/test_intl_profondeur.py                    # A + B + C
    python3 tools/test_intl_profondeur.py --reseau           # tout, avec le réseau
    python3 tools/test_intl_profondeur.py --paquets <dir>    # sur un dossier d'essai
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "scripts"))

import fetch_intl_fundamentals as F  # noqa: E402

# Le seuil est celui du collecteur, LU chez lui et non recopié : deux constantes
# qui disent le même seuil finissent par diverger, et c'est le test qui aurait
# alors tort.
SEUIL = F.TV_ECART_RACCORD

# Les huit sociétés de contrôle : huit places, huit devises, deux calendriers
# décalés (Toyota et Reliance clôturent en mars, BHP en juin) et deux cas où la
# devise de cotation n'est PAS celle des états (Tencent, BHP).
TEMOINS = [
    ("MC.PA", "quote/epa/MC", "EURONEXT:MC"),
    ("ASML.AS", "quote/ams/ASML", "EURONEXT:ASML"),
    ("7203.T", "quote/tyo/7203", "TSE:7203"),
    ("NESN.SW", "quote/swx/NESN", "SIX:NESN"),
    ("0700.HK", "quote/hkg/0700", "HKEX:700"),
    ("RELIANCE.NS", "quote/nse/RELIANCE", "NSE:RELIANCE"),
    ("BHP.AX", "quote/asx/BHP", "ASX:BHP"),
    ("005930.KS", "quote/krx/005930", "KRX:005930"),
]

# Les grandeurs qu'un exercice TradingView ne peut PAS porter : ce fournisseur
# ne les sert pas. Si l'une d'elles a une valeur sur une ligne marquée
# `tradingview`, c'est que deux fournisseurs ont été mélangés dans la même ligne.
INTERDITS_TV = ("equity", "liabilities", "ocf", "shares_diluted", "shares_basic",
                "cash", "interest_expense", "operating_income", "dividends_paid")

échecs = []


def verifie(quoi, obtenu, attendu):
    ok = obtenu == attendu
    print("  %-62s %-14s %s" % (quoi, str(obtenu)[:14],
                                "✓" if ok else "✗ attendu " + str(attendu)))
    if not ok:
        échecs.append(quoi)


def exige(quoi, condition, detail=""):
    print("  %-62s %s" % (quoi, "✓" if condition else "✗ " + detail))
    if not condition:
        échecs.append("%s%s" % (quoi, (" — " + detail) if detail else ""))


# ══════════════════════════════════════════════════════════════════════════
# A. LA MÉCANIQUE DE COUTURE, SUR PIÈCES FABRIQUÉES
# ══════════════════════════════════════════════════════════════════════════
def _serie_fx(valeur, debut, fin):
    """Une série de change quotidienne — assez fournie pour la moyenne exigée."""
    out, j = {}, datetime.fromisoformat(debut)
    stop = datetime.fromisoformat(fin)
    while j <= stop:
        out[j.strftime("%Y-%m-%d")] = valeur
        j += timedelta(days=1)
    return out


def _tv_fabrique(annees, revenus, fin_fy, devise="EUR", **autres):
    d = {"fiscal_period_fy_h": list(annees),
         "total_revenue_fy_h": list(revenus),
         "currency": devise,
         "description": "Société de contrôle",
         "_symbole_tv": "TEST:X",
         "fiscal_period_end_fy": int(
             datetime.fromisoformat(fin_fy + "T00:00:00+00:00").timestamp())}
    d.update(autres)
    return d


def _sa_fabrique(paires, cle="revenue"):
    return [{"annee": a, "fin": "%d-12-31" % a, cle: v, "source": "stockanalysis"}
            for a, v in paires]


def controles_mecanique():
    print("A. LA MÉCANIQUE DE COUTURE — sur pièces fabriquées, sans réseau")

    # ── A1. Le cas nominal : mêmes devises, mêmes montants sur la fenêtre commune
    annees = list(range(2025, 2005, -1))
    revenus = [1000.0 - 10 * i for i in range(20)]
    tv = _tv_fabrique(annees, revenus, "2025-12-31")
    sa = _sa_fabrique([(a, r) for a, r in zip(annees[:5], revenus[:5])])
    lignes, diag = F.exercices_tradingview(tv, "EUR", {}, sa)
    verifie("A1 · exercices ajoutés (20 servis − 5 déjà connus)", len(lignes), 15)
    verifie("A1 · aucun refus", diag.get("refus"), None)
    exige("A1 · chaque ligne déclare sa source",
          all(l.get("source") == "tradingview" for l in lignes))
    exige("A1 · aucune ligne ne recouvre un exercice de la source principale",
          not ({l["annee"] for l in lignes} & {e["annee"] for e in sa}))
    exige("A1 · aucune ligne ne porte une grandeur que TradingView ne sert pas",
          all(l.get(c) is None for l in lignes for c in INTERDITS_TV))

    # ── A2. Le symbole qui n'est pas la bonne société : refus EN BLOC
    tv_faux = _tv_fabrique(annees, [r * 2.0 for r in revenus], "2025-12-31")
    lignes, diag = F.exercices_tradingview(tv_faux, "EUR", {}, sa)
    verifie("A2 · symbole douteux : aucune ligne", len(lignes), 0)
    exige("A2 · le refus est motivé", bool(diag.get("refus")),
          "un refus sans motif ne se corrige pas")
    exige("A2 · le motif nomme le chiffre d'affaires",
          "chiffre d'affaires" in (diag.get("refus") or ""))

    # ── A3. Aucun exercice commun : rien ne prouve l'identité, donc rien n'entre
    lignes, diag = F.exercices_tradingview(tv, "EUR", {}, [])
    verifie("A3 · sans exercice commun : aucune ligne", len(lignes), 0)
    exige("A3 · le refus dit que l'identité n'est pas prouvée",
          "identité" in (diag.get("refus") or ""))

    # ── A4. Une grandeur qui ne se raccorde pas est ÉCARTÉE, pas publiée
    brut_tv = [r * 0.64 for r in revenus]          # −36 %, le cas Reliance
    tv_gp = _tv_fabrique(annees, revenus, "2025-12-31",
                         gross_profit_fy_h=brut_tv,
                         net_income_fy_h=[r * 0.10 for r in revenus])
    sa_gp = [dict(e, gross_profit=revenus[i], net_income=revenus[i] * 0.10)
             for i, e in enumerate(_sa_fabrique(
                 [(a, r) for a, r in zip(annees[:5], revenus[:5])]))]
    lignes, diag = F.exercices_tradingview(tv_gp, "EUR", {}, sa_gp)
    exige("A4 · le résultat brut divergent est écarté",
          "gross_profit" in (diag.get("grandeurs_ecartees") or []))
    exige("A4 · et il est VIDE sur les lignes, pas approché",
          all(l.get("gross_profit") is None for l in lignes))
    exige("A4 · le résultat net, lui, concorde et reste",
          all(l.get("net_income") is not None for l in lignes))
    exige("A4 · l'écart mesuré est écrit, pour être relu",
          abs((diag["raccord"]["gross_profit"]["ecart_median_pct"] or 0) + 36.0) < 1.0,
          str(diag["raccord"]["gross_profit"]))

    # ── A5. L'étiquette d'exercice décalée : Toyota clôt en mars
    tv_mars = _tv_fabrique(annees, revenus, "2026-03-31", devise="JPY")
    sa_mars = [{"annee": a + 1, "fin": "%d-03-31" % (a + 1), "revenue": r,
                "source": "stockanalysis"}
               for a, r in zip(annees[:5], revenus[:5])]
    lignes, diag = F.exercices_tradingview(tv_mars, "JPY", {}, sa_mars)
    verifie("A5 · décalage d'étiquette mesuré, non deviné",
            diag.get("decalage_etiquette"), 1)
    verifie("A5 · l'exercice ajouté le plus récent", max(l["annee"] for l in lignes), 2021)
    exige("A5 · les clôtures reconstruites gardent le mois de mars",
          all(l["fin"].endswith("-03-31") for l in lignes))
    exige("A5 · et se déclarent reconstruites",
          all(l.get("fin_reconstruite") for l in lignes))

    # ── A6. LA DEVISE — le défaut le plus coûteux du dépôt
    # TradingView rend BHP en dollars australiens, les états sont en dollars
    # américains. Sans conversion, l'écart de raccord ferait +47 % et TOUT
    # serait refusé ; avec une conversion fausse d'un facteur, tout passerait.
    taux = 0.68
    fx = {"AUD": _serie_fx(taux, "2004-01-01", "2026-12-31")}
    tv_aud = _tv_fabrique(annees, [r / taux for r in revenus], "2025-12-31",
                          devise="AUD")
    lignes, diag = F.exercices_tradingview(tv_aud, "USD", fx, sa)
    verifie("A6 · conversion : exercices ajoutés", len(lignes), 15)
    exige("A6 · la conversion est déclarée sur la ligne",
          all(l.get("devise_convertie") == "AUD → USD" for l in lignes))
    plus_vieille = min(lignes, key=lambda l: l["annee"])
    attendu = revenus[annees.index(plus_vieille["annee"])]
    exige("A6 · et elle rend le montant des états, pas celui de la cotation",
          abs(plus_vieille["revenue"] / attendu - 1) < 0.005,
          "%.4f contre %.4f" % (plus_vieille["revenue"], attendu))

    # ── A7. Devise inconnue du cache de change : on refuse, on n'approxime pas
    lignes, diag = F.exercices_tradingview(tv_aud, "USD", {}, sa)
    verifie("A7 · devise absente du cache : aucune ligne", len(lignes), 0)
    exige("A7 · et le refus le dit", "devise" in (diag.get("refus") or "").lower())

    # ── A8. Sous-unité de cotation : GBX n'est pas une devise, GBP en est une
    verifie("A8 · GBX (pence) est ramené à GBP", F._devise_tv("GBX"), "GBP")
    verifie("A8 · ZAC (centimes) est ramené à ZAR", F._devise_tv("ZAC"), "ZAR")
    verifie("A8 · ILA (agorot) est ramené à ILS", F._devise_tv("ILA"), "ILS")

    print()


def controles_fusion():
    print("B. LA FUSION PAR ANNÉE — un passage ne détruit pas le précédent")
    ancienne = {"exercices": [{"annee": a, "fin": "%d-12-31" % a, "revenue": a,
                               "source": "stockanalysis"} for a in range(2006, 2021)],
                "resume": {"n_exercices": 15}}
    nouvelle = {"exercices": [{"annee": a, "fin": "%d-12-31" % a, "revenue": a * 2,
                               "source": "stockanalysis"} for a in range(2021, 2026)],
                "resume": {"n_exercices": 5}}
    fusion, repris = F.fusionner_societe(ancienne, nouvelle)
    verifie("B1 · exercices sauvés de la fenêtre glissante", repris, 15)
    verifie("B1 · profondeur après fusion", len(fusion["exercices"]), 20)
    exige("B1 · l'ordre chronologique tient",
          [e["annee"] for e in fusion["exercices"]] == list(range(2006, 2026)))
    exige("B1 · les exercices repris se déclarent archivés",
          all(e.get("archive") for e in fusion["exercices"] if e["annee"] < 2021))
    exige("B1 · le résumé est refait, pas hérité",
          fusion["resume"]["n_exercices"] == 20,
          "n_exercices=%s alors que la série en porte 20"
          % fusion["resume"].get("n_exercices"))

    # ⚠ À année égale, c'est la version FRAÎCHE qui gagne : elle porte les
    # retraitements. L'archive ne doit jamais ressusciter un chiffre corrigé.
    chevauche = {"exercices": [{"annee": 2020, "fin": "2020-12-31", "revenue": 999,
                                "source": "stockanalysis"}],
                 "resume": {}}
    fusion2, repris2 = F.fusionner_societe(ancienne, chevauche)
    verifie("B2 · une année encore servie n'est pas reprise de l'archive",
            [e["revenue"] for e in fusion2["exercices"] if e["annee"] == 2020], [999])
    print()


# ══════════════════════════════════════════════════════════════════════════
# C. LES PAQUETS ÉCRITS — exhaustif, jamais par échantillon
# ══════════════════════════════════════════════════════════════════════════
def charger_paquets(dossier):
    soc = {}
    for p in sorted(glob.glob(os.path.join(dossier, "intl_detail_*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                soc.update((json.load(fh) or {}).get("societes") or {})
        except Exception as e:
            échecs.append("paquet illisible : %s (%s)" % (os.path.basename(p), e))
    return soc


def controles_paquets(dossier, maj_temoin):
    print("C. LES PAQUETS ÉCRITS — %s" % dossier)
    soc = charger_paquets(dossier)
    if not soc:
        print("  (aucun paquet : rien à contrôler ici)")
        print()
        return
    print("  %d société(s), %d exercice(s)"
          % (len(soc), sum(len(v.get("exercices") or []) for v in soc.values())))

    # ── C1. Tout exercice déclare sa source ──
    muets, exemples = 0, []
    for sym, v in soc.items():
        for e in (v.get("exercices") or []):
            if not e.get("source"):
                muets += 1
                if len(exemples) < 5:
                    exemples.append("%s %s" % (sym, e.get("annee")))
    # ⚠ UN CACHE ANTÉRIEUR AU CORRECTIF N'EST PAS UNE RÉGRESSION.
    # Le champ `source` par exercice n'existe que depuis le 05/09/2026. Sur des
    # paquets écrits avant, il manque PARTOUT — 98 819 exercices sur 98 819 — et
    # le message « exercices muets » se lit alors comme une panne neuve. Un
    # contrôle qui accuse le collecteur d'un défaut qu'il vient de corriger se
    # fait désarmer au premier coup d'œil. On distingue donc les deux cas.
    total_ex = sum(len(v.get("exercices") or []) for v in soc.values())
    if muets and total_ex and muets == total_ex:
        exige("C1 · tout exercice déclare sa source", False,
              "AUCUN des %d exercices ne porte de source : ces paquets sont "
              "ANTÉRIEURS au correctif du 05/09/2026. Republier avec le "
              "collecteur courant avant de conclure quoi que ce soit." % total_ex)
    else:
        exige("C1 · tout exercice déclare sa source", muets == 0,
              "%d exercice(s) muets sur %d, dont %s"
              % (muets, total_ex, ", ".join(exemples)))

    # ── C2. Aucune ligne ne mélange deux fournisseurs ──
    melanges, ex2 = 0, []
    for sym, v in soc.items():
        for e in (v.get("exercices") or []):
            if e.get("source") != "tradingview":
                continue
            fautifs = [c for c in INTERDITS_TV if e.get(c) is not None]
            if fautifs:
                melanges += 1
                if len(ex2) < 5:
                    ex2.append("%s %s : %s" % (sym, e.get("annee"), ",".join(fautifs)))
    exige("C2 · aucune ligne TradingView ne porte de grandeur d'un autre",
          melanges == 0, "%d ligne(s) mélangées, dont %s" % (melanges, " ; ".join(ex2)))

    # ── C3. Toute grandeur RETENUE se raccorde à 5 % près ──
    # C'est le contrôle qui prouve qu'on n'a mélangé ni deux définitions ni deux
    # devises : le collecteur a mesuré l'écart sur les exercices COMMUNS et l'a
    # écrit dans le paquet ; ici on relit ce qu'il a écrit et on vérifie qu'il
    # n'a rien retenu au-delà du seuil.
    hors, ex3, mesures = 0, [], 0
    for sym, v in soc.items():
        rac = ((v.get("resume") or {}).get("tradingview") or {}).get("raccord") or {}
        for champ, d in rac.items():
            ec = d.get("ecart_median_pct")
            if ec is None:
                continue
            mesures += 1
            if d.get("retenu") and abs(ec) > SEUIL:
                hors += 1
                if len(ex3) < 5:
                    ex3.append("%s %s %.1f %%" % (sym, champ, ec))
    exige("C3 · aucune grandeur retenue au-delà de %.0f %% (%d écart(s) mesuré(s))"
          % (SEUIL, mesures), hors == 0,
          "%d grandeur(s) hors seuil, dont %s" % (hors, " ; ".join(ex3)))

    # ── C4. LE CLIQUET : personne ne perd d'exercice d'un passage à l'autre ──
    temoin_p = os.path.join(dossier, "intl_profondeur_temoin.json")
    temoin = {}
    if os.path.exists(temoin_p):
        try:
            with open(temoin_p, encoding="utf-8") as fh:
                temoin = (json.load(fh) or {}).get("profondeurs") or {}
        except Exception as e:
            échecs.append("témoin de profondeur illisible : %s" % e)
    pertes, ex4 = 0, []
    courant = {}
    for sym, v in soc.items():
        n = len(v.get("exercices") or [])
        courant[sym] = n
        av = temoin.get(sym)
        if isinstance(av, int) and n < av:
            pertes += 1
            if len(ex4) < 8:
                ex4.append("%s %d → %d" % (sym, av, n))
    if not temoin:
        print("  C4 · témoin absent : il est créé, le cliquet démarre au passage suivant")
    exige("C4 · aucune société ne perd d'exercice (témoin de %d société(s))"
          % len(temoin), pertes == 0,
          "%d société(s) amputées, dont %s" % (pertes, " ; ".join(ex4)))

    # ⚠ LE TÉMOIN NE SE MET À JOUR QU'À LA HAUSSE, et jamais quand un contrôle a
    # échoué. Un témoin qui suivrait la baisse effacerait la preuve de la perte
    # qu'il est censé détecter — c'est le même piège que le cache qui rajeunit
    # en se recopiant.
    if maj_temoin and not échecs:
        fusion = dict(temoin)
        for sym, n in courant.items():
            if n > int(fusion.get(sym) or 0):
                fusion[sym] = n
        try:
            with open(temoin_p, "w", encoding="utf-8") as fh:
                json.dump({"ecrit_le": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                           "profondeurs": fusion}, fh, ensure_ascii=False, indent=1)
            print("  C4 · témoin mis à jour : %d société(s)" % len(fusion))
        except Exception as e:
            échecs.append("témoin non écrit : %s" % e)
    print()


# ══════════════════════════════════════════════════════════════════════════
# D. LE CONTRÔLE DE BOUT EN BOUT, AVEC LE RÉSEAU
# ══════════════════════════════════════════════════════════════════════════
def controles_reseau():
    print("D. BOUT EN BOUT — les deux fournisseurs redemandés, huit places, huit devises")
    series, bilan = F.series_tv([t[0] for t in TEMOINS])
    exige("D0 · le scanner répond pour les huit témoins",
          bilan.get("repondus") == len(TEMOINS),
          "%s/%s" % (bilan.get("repondus"), len(TEMOINS)))
    fx = F.charger_fx()
    exige("D0 · le cache de change est chargé", len(fx) > 5,
          "%d devise(s)" % len(fx))

    for sym, chemin, attendu_tv in TEMOINS:
        brut = F.etats(chemin)
        if not brut:
            échecs.append("D · %s : la source principale ne répond pas" % sym)
            print("  %-13s ✗ source principale muette" % sym)
            continue
        devise = ((brut.get("contexte") or {}).get("currency") or "").upper() or None
        bati = F.construire(brut, devise=devise, fx_dev=fx.get(devise), fx=fx,
                            tv=series.get(sym))
        if not bati:
            échecs.append("D · %s : rien de construit" % sym)
            continue
        ex = bati["exercices"]
        diag = (bati["resume"].get("tradingview") or {})
        n_tv = sum(1 for e in ex if e.get("source") == "tradingview")
        rac = diag.get("raccord") or {}
        # LE contrôle : ce qui a été RETENU concorde, ce qui a été écarté est nommé.
        hors = [(c, d["ecart_median_pct"]) for c, d in rac.items()
                if d.get("retenu") and d.get("ecart_median_pct") is not None
                and abs(d["ecart_median_pct"]) > SEUIL]
        ecartees = diag.get("grandeurs_ecartees") or []
        ok = (not hors) and n_tv > 0 and all(e.get("source") for e in ex)
        print("  %-13s %2d exercices (%2d TradingView, devise %s%s) %s"
              % (sym, len(ex), n_tv, devise,
                 ", " + diag["conversion"] if diag.get("conversion") else "",
                 "✓" if ok else "✗"))
        if ecartees:
            print("                écartées faute de raccord : %s" % ", ".join(ecartees))
        if hors:
            échecs.append("D · %s : %s retenu(s) hors seuil" % (sym, hors))
        if n_tv == 0:
            échecs.append("D · %s : aucun exercice TradingView ajouté (%s)"
                          % (sym, diag.get("refus")))
        if not all(e.get("source") for e in ex):
            échecs.append("D · %s : un exercice ne déclare pas sa source" % sym)
    print()


def main():
    argv = sys.argv[1:]
    dossier = str(F.CACHE_DIR)
    if "--paquets" in argv:
        dossier = argv[argv.index("--paquets") + 1]
    controles_mecanique()
    controles_fusion()
    controles_paquets(dossier, maj_temoin="--sans-temoin" not in argv)
    if "--reseau" in argv:
        controles_reseau()
    else:
        print("D. BOUT EN BOUT — non joué (ajouter --reseau)")
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
