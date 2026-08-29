#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rejouer_calculs_sec.py — Propage un correctif de CALCUL aux paquets SEC,
                         sans réseau et sans reconstruire ce qu'on ne sait pas refaire.

POURQUOI IL EST CIBLÉ, ET NON GÉNÉRAL

Le jumeau international reconstruit le résumé entier : là-bas, `construire_resume`
ne dépend que des exercices. Côté SEC, le même bloc lit une capitalisation, une
devise déduite en cours de route et une variable de module — le sortir en fin de
session serait un refactor risqué pour un gain nul.

Ce script ne touche donc QUE ce que les correctifs de calcul modifient :

  · les taux de croissance, recalculés depuis les séries par action déjà
    stockées — c'est là que vivent la base infinitésimale et la traversée de
    zéro ;
  · les ratios sortis de la bande de plausibilité, écartés ;
  · la note, qui se déduit des deux précédents ;
  · la note historique, calculée sur les mêmes exercices.

Tout le reste du résumé est laissé intact. Un rejeu qui ne reconstruit pas ne
peut pas perdre une clé — c'est le défaut que le jumeau international a failli
commettre, où quatre champs venus de la collecte disparaissaient sur les 19 495
sociétés.

CE QU'IL NE REMPLACE PAS

Les correctifs de VOCABULAIRE — une étiquette XBRL qu'on ne demandait pas — ne
se rejouent pas : la donnée n'est pas dans le paquet, il faut retourner la
chercher. Seule une collecte les applique.

Lancement :
    python3 scripts/rejouer_calculs_sec.py --essai    # mesure sans rien écrire
    python3 scripts/rejouer_calculs_sec.py            # écrit
"""

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_sec_fundamentals import historique_note            # noqa: E402
from fondamentaux_communs import (                            # noqa: E402
    _corriger_divisions, _croissances, ecarter_ratios_degeneres, note_quantitative,
    _mediane_fenetre, _wacc, beta_plausible, cours_ancres, cours_a_la_date,
)
from datetime import datetime                                 # noqa: E402


def facteurs_des_millesimes(evenements):
    """Reconstruit `facteurs_lus` depuis les événements déjà stockés.

    ⚠ SANS ÇA, LE REJEU DÉGRADE. `_corriger_divisions` appelé sans `facteurs_lus`
    retombe sur l'inférence, alors que sa propre règle dit « quand les millésimes
    parlent, on ne devine plus ». Les facteurs lus viennent du XBRL brut, absent
    du paquet — mais l'ÉVÉNEMENT, lui, est là, avec sa provenance et sa date.

    Mesuré sur les paquets réels : sans cette reconstruction, Hub Group perd sa
    division ×2 de 2021 (son chiffre d'affaires par action chutait alors de 40 %
    une année où le chiffre d'affaires montait, et sa note de 5,3 à 3,3), Macy's
    perd son ×2,5 de 2024, et Chord Energy voyait son facteur lu remplacé par un
    ×0,0667 inféré — qui n'est pas un regroupement d'actions mais l'annulation
    des actions d'Oasis Petroleum à sa sortie de faillite.

    `_corriger_divisions` regroupe ensuite par facteur et retient la date la plus
    ancienne de chaque grappe : une entrée par événement suffit à le reproduire.
    Deux divisions du même facteur restent deux événements dès qu'elles sont
    séparées de plus de quatre ans — et c'est bien pour ça qu'elles l'étaient.
    """
    lus = {}
    for ev in (evenements or []):
        if ev.get("source") != "millésimes":
            continue
        f, depuis = ev.get("facteur"), ev.get("depuis")
        if isinstance(f, (int, float)) and f and depuis:
            lus[depuis] = f
    return lus or None


def refaire_divisions(exercices, facteurs_lus=None):
    """Remet les séries sur leur base brute, puis relance la recouture.

    Les paquets portent des corrections décidées sous des règles qui ont changé :
    1,5 n'est plus un facteur usuel (447 événements faux sur 389 sociétés), et la
    garde de confirmation ne s'éteint plus sur les sociétés en perte (680
    événements passaient sans contrôle).

    On ne cherche pas à deviner lesquelles restent valides. On DÉFAIT tout —
    l'opération est exactement réversible, le facteur étant inscrit sur chaque
    exercice — puis on REFAIT avec les règles d'aujourd'hui.

    ⚠ Contrairement au jeu international, les divisions sont ici LÉGITIMES : la
    SEC publie les faits tels qu'ils ont été déposés à l'époque, sans
    rétro-ajustement. On refait donc, on ne se contente pas de défaire.

    Rend (nombre d'exercices remis en base, événements retenus après recouture).
    """
    n = 0
    for e in exercices:
        f = e.get("_facteur_division")
        if not isinstance(f, (int, float)) or f in (0, 1.0):
            continue
        for cle in ("shares_diluted", "shares_basic"):
            if isinstance(e.get(cle), (int, float)):
                e[cle] = e[cle] / f
        for cle in ("eps_diluted", "eps_basic", "dps"):
            if isinstance(e.get(cle), (int, float)):
                e[cle] = e[cle] * f
        e.pop("_facteur_division", None)
        n += 1

    # ⚠ `facteurs_lus` REPASSÉ. Sans lui, cette ligne retombe sur l'inférence et
    # écrase des corrections lues dans les dépôts par des corrections devinées.
    evenements = _corriger_divisions(exercices, facteurs_lus)

    # Les grandeurs par action DÉRIVENT du nombre d'actions : sans ce recalcul,
    # la moitié de la série resterait sur l'ancienne base.
    for e in exercices:
        sh = e.get("shares_diluted")
        if isinstance(sh, (int, float)) and sh:
            for cle, source in (("ca_par_action", "revenue"),
                                ("fcf_par_action", "fcf"),
                                ("ocf_par_action", "ocf")):
                v = e.get(source)
                e[cle] = round(v / sh, 4) if isinstance(v, (int, float)) else None
    return n, evenements

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")

# (clé de la croissance, champ par action de l'exercice)
SERIES = (("ca", "ca_par_action"), ("eps", "eps_diluted"),
          ("fcf", "fcf_par_action"), ("ocf", "ocf_par_action"), ("div", "dps"))


# Le pas maximal toléré dans une série de nombres d'actions APRÈS recouture.
# Un saut qui subsiste est un défaut du paquet ou une entrée en bourse ; dans les
# deux cas, transporter une capitalisation à travers lui donnerait un faux.
SAUT_ACTIONS_MAX = 5.0


def grandeurs_de_substitution(exercices, resume):
    """Pose `fonds_propres_sur_actif` et `capex_sur_ca` au résumé, si absentes.

    Ce sont les deux remplaçants que `_SUBSTITUTS` demande et que les paquets
    déjà publiés n'ont pas : les collecteurs viennent de les ajouter, mais la
    donnée en ligne date d'avant. Sans elles, le barème de substitution perd son
    remplaçant le plus fécond — les fonds propres rapportés au bilan, qui
    répondent pour 830 sociétés américaines et 2 339 internationales.

    ⚠ NULLES PLUTÔT QUE ZÉRO : un ratio sans dénominateur n'est pas nul, il est
    inconnu, et la note fait la différence entre les deux.

    Rend le nombre de grandeurs posées.
    """
    if not exercices or not isinstance(resume, dict):
        return 0
    d = exercices[-1]
    n = 0
    if resume.get("fonds_propres_sur_actif") is None:
        cp, act = d.get("equity"), d.get("assets")
        if (isinstance(cp, (int, float)) and isinstance(act, (int, float))
                and act > 0):
            resume["fonds_propres_sur_actif"] = round(100.0 * cp / act, 2)
            n += 1
    if resume.get("capex_sur_ca") is None:
        cx, ca = d.get("capex"), d.get("revenue")
        if (isinstance(cx, (int, float)) and isinstance(ca, (int, float))
                and ca > 0):
            resume["capex_sur_ca"] = round(100.0 * abs(cx) / ca, 2)
            n += 1
    return n


def charger_rapports_cours():
    """{symbole: ({jours: rapport de cours}, jour du fichier)}.

    ⚠ DES RAPPORTS, PAS DES COURS. `cours_ancres` appelée avec un cours de 1 rend
    `1 / (1 + chNy/100)`, c'est-à-dire le rapport entre le cours d'alors et celui
    d'aujourd'hui. Un rapport n'a pas de monnaie : plus de conversion, plus de
    devise à croire, et une erreur d'échelle constante se simplifie.

    Le jour retenu est celui d'écriture du fichier (`genere_le`), pas
    « aujourd'hui » : un fichier de trois jours daterait ses ancres de trois
    jours de trop, et l'erreur se cumulerait avec la tolérance de six mois.
    """
    out = {}
    for pth in sorted(glob.glob(os.path.join(CACHE, "marche_[0-9]*.json"))):
        try:
            with open(pth, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        ch = d.get("champs") or []
        i = {n: ch.index(n) for n in ("ch1y", "ch3y", "ch5y", "ch10y") if n in ch}
        if not i:
            continue
        jour = (d.get("genere_le") or d.get("updated") or "")[:10] or None
        for sym, v in (d.get("societes") or {}).items():
            def lire(nom):
                k = i.get(nom)
                return v[k] if (k is not None and k < len(v)) else None
            a = cours_ancres(1.0, lire("ch1y"), lire("ch3y"),
                             lire("ch5y"), lire("ch10y"))
            if a:
                out[sym] = (a, jour)
    return out


def refaire_mcap(exercices, variations, jour_ref):
    """Comble la capitalisation manquante en transportant un RAPPORT.

        mcap(exercice) = mcap_du_dernier × cours(exercice)/cours(aujourd'hui)
                                         × actions(exercice)/actions(dernier)

    ⚠ ON EFFACE D'ABORD LES SIENNES. Une règle qui se durcit doit atteindre ce
    qui est déjà écrit ; sans ce nettoyage, une reconstruction d'un passage
    antérieur survivrait à la garde qui la refuse.

    ⚠ LA GARDE DES DEVISES TIENT EN AMONT : `ecarter_marche_devise.py` a effacé
    `mcap_estime` sur les 170 sociétés à états non dollars. Sans capitalisation
    de référence, il n'y a rien à transporter.

    Rend le nombre d'exercices comblés.
    """
    for e in exercices:
        if e.get("mcap_source") == "ancre":
            e["mcap_estime"] = None
            e.pop("mcap_source", None)
            e.pop("mcap_ecart_jours", None)

    if not variations or not jour_ref:
        return 0
    try:
        ref_date = datetime.fromisoformat(jour_ref)
    except Exception:
        return 0

    serie = [e.get("shares_diluted") for e in exercices]
    serie = [x for x in serie if isinstance(x, (int, float)) and x > 0]
    for k in range(1, len(serie)):
        if max(serie[k] / serie[k - 1], serie[k - 1] / serie[k]) > SAUT_ACTIONS_MAX:
            return 0

    ref = None
    for e in reversed(exercices):
        if (e.get("mcap_estime") or 0) > 0 and e.get("shares_diluted"):
            ref = e
            break
    if ref is None:
        return 0
    sh_ref = ref["shares_diluted"]

    n = 0
    for e in exercices:
        if e is ref or (e.get("mcap_estime") or 0) > 0:
            continue
        sh, fin_e = e.get("shares_diluted"), e.get("fin")
        if not sh or sh <= 0 or not fin_e:
            continue
        try:
            jours = (ref_date - datetime.fromisoformat(fin_e)).days
        except Exception:
            continue
        rapport, ecart = cours_a_la_date(variations, jours)
        if rapport is None or rapport <= 0:
            continue
        e["mcap_estime"] = round(ref["mcap_estime"] * rapport * sh / sh_ref)
        e["mcap_source"] = "ancre"
        e["mcap_ecart_jours"] = ecart
        n += 1
    return n


def charger_beta_marche():
    """Le bêta de chaque cotation, depuis les fichiers de marché locaux.

    Le cache sectoriel `tradfi_fundamentals_cache.json` ne porte que 641 titres.
    Les fichiers `marche_NN.json` en portent 27 013, et le collecteur les ouvre
    déjà pour bâtir la liste des cotations principales. Mesuré sur le parc SEC :
    2 828 sociétés sans coût du capital ont un bêta qui les attend là.

    ⚠ Passé par `beta_plausible` — la même bande que les collecteurs, pas une
    copie. Sans elle, un bêta de 95 donnerait un coût des fonds propres à trois
    chiffres, et la fiche afficherait une destruction de valeur inventée.

    Rend (les bêtas retenus, les symboles explicitement REFUSÉS). Les seconds
    comptent autant : une société dont le bêta est refusé doit perdre le coût du
    capital qu'un passage antérieur lui avait donné.
    """
    betas, refuses = {}, set()
    for pth in sorted(glob.glob(os.path.join(CACHE, "marche_[0-9]*.json"))):
        try:
            with open(pth, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        ch = d.get("champs") or []
        if "beta" not in ch:
            continue
        i_b = ch.index("beta")
        for sym, v in (d.get("societes") or {}).items():
            brut = v[i_b] if i_b < len(v) else None
            b = beta_plausible(brut)
            if b is not None:
                betas[sym] = b
            elif brut not in (None, 0):
                refuses.add(sym)
    return betas, refuses


def refaire_wacc(exercices, resume, beta, refuse=False):
    """Recalcule le coût du capital, puis les trois fenêtres du résumé.

    Les quatre termes de `_wacc` sont déjà dans chaque exercice — capitalisation,
    dette, charge d'intérêts, taux d'impôt. Seul le bêta venait d'ailleurs.

    ⚠ Ce rejeu ne reconstruit PAS le résumé (c'est tout son intérêt : il ne peut
    pas perdre une clé). Il faut donc réécrire à la main les quatre champs qui
    dérivent du WACC, avec la même fenêtre médiane que le collecteur.

    ⚠ La garde des devises n'est pas recopiée : `ecarter_marche_devise.py` a déjà
    effacé `mcap_estime` sur les sociétés dont les états ne sont pas en dollars,
    et sans capitalisation `_wacc` rend None. Elle tient en amont.

    ⚠ On n'efface pas par ignorance : sans bêta ici et sans refus explicite, la
    collecte en avait peut-être un — le cache sectoriel en porte 641.

    Rend (exercices gagnés, exercices effacés).
    """
    if beta is None and not refuse:
        return 0, 0
    n = efface = 0
    for e in exercices:
        mc = e.get("mcap_estime")
        w = None
        if beta is not None and isinstance(mc, (int, float)) and mc > 0:
            w = _wacc(mc, e.get("dette_totale"), e.get("interest_expense"),
                      e.get("_taux_nopat"), beta)
        avant = e.get("wacc")
        if w is None:
            if avant is not None:
                e["wacc"] = None
                e["roic_moins_wacc"] = None
                efface += 1
            continue
        e["wacc"] = w
        r = e.get("roic")
        e["roic_moins_wacc"] = (round(r - w, 2)
                                if isinstance(r, (int, float)) else None)
        if avant is None:
            n += 1

    if not exercices:
        return n, efface
    dernier = exercices[-1]
    resume["wacc_1a"] = dernier.get("wacc")
    for fen in (5, 10):
        resume["wacc_%da" % fen] = _mediane_fenetre(
            [e.get("wacc") for e in exercices[-fen:]], fen)
    resume["roic_moins_wacc"] = dernier.get("roic_moins_wacc")
    return n, efface


def main():
    essai = "--essai" in sys.argv
    t0 = time.time()
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "sec_detail_[0-9][0-9][0-9].json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet sec_detail_NNN.json", file=sys.stderr)
        return 2

    rapports = charger_rapports_cours()
    print("[info] rapports de cours reconstruits pour %d cotation(s)" % len(rapports))
    mcaps = 0
    soc_mcap = set()

    betas, refuses = charger_beta_marche()
    print("[info] bêta lu pour %d cotation(s) ; %d hors bande refusé(s)"
          % (len(betas), len(refuses)))

    horodatage = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    waccs = effaces = 0
    soc_wacc = set()
    soc_effacee = set()
    total = touchees = 0
    retirees = {c: 0 for c, _ in SERIES}
    ecartes = 0
    rebasees = 0
    div_changees = 0
    decouvertes = []
    soc_rebasees = set()
    montees = descendues = 0
    mouvements = []

    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        change = False
        for sym, v in (doc.get("societes") or {}).items():
            ex = v.get("exercices") or []
            r = v.get("resume")
            if not ex or not isinstance(r, dict):
                continue
            total += 1
            avant = (r.get("note_q") or {}).get("note_ramenee")

            # ── Les divisions AVANT tout : le reste en dérive ──
            #
            # ⚠ ON RE-JUGE, ON NE DÉCOUVRE PAS. Les règles n'ont fait que se
            # durcir (1,5 retiré des facteurs usuels, garde de confirmation
            # rallumée sur les sociétés en perte) : elles ne peuvent que RETIRER
            # des événements. Un événement AJOUTÉ vient donc d'une inférence
            # faite sans les millésimes — que le paquet ne contient pas, et qui
            # sont justement la preuve qu'une division a eu lieu ou non.
            #
            # Mesuré : le rejeu inventait pour VNET Group une division ×2 entre
            # 2023 et 2024. Demandé à la SEC : l'exercice 2023 vaut 901 143 138
            # actions dans les dépôts de 2024, 2025 ET 2026 — jamais retraité, et
            # une division retraite toujours. VNET a émis, pas divisé : ses
            # actions de base passent de 901 M à 1 594 M sur le seul 2024. Treize
            # ans de bénéfice par action allaient être divisés par deux.
            avant_ex = json.loads(json.dumps(ex))
            avant_div = list(r.get("divisions_action") or [])
            n_def, evts = refaire_divisions(
                ex, facteurs_des_millesimes(avant_div))
            connus = {(e.get("facteur"), e.get("entre"), e.get("depuis"))
                      for e in avant_div}
            neufs = [e for e in evts
                     if (e.get("facteur"), e.get("entre"), e.get("depuis"))
                     not in connus]
            if neufs:
                # Rien n'est touché : ni les séries, ni la liste d'événements.
                # La prochaine collecte aura les millésimes et tranchera.
                ex[:] = avant_ex
                decouvertes.append((sym, neufs))
            else:
                if n_def:
                    rebasees += n_def
                    soc_rebasees.add(sym)
                if len(evts) != len(avant_div):
                    div_changees += 1
                r["divisions_action"] = evts

            # ── Les croissances, depuis les séries déjà stockées ──
            neuves = {}
            for cle, champ in SERIES:
                serie = [(e.get("annee"), e.get(champ)) for e in ex]
                neuves[cle] = _croissances(serie)
                anc = ((r.get("croissances") or {}).get(cle) or {})
                for fen in ("1a", "5a", "10a"):
                    if anc.get(fen) is not None and neuves[cle].get(fen) is None:
                        retirees[cle] += 1
            r["croissances"] = neuves

            # ── La capitalisation historique, APRÈS la recouture des
            # divisions (elle en dépend : le nombre d'actions vient d'être remis
            # sur sa base) et AVANT le coût du capital (qui en dépend). ──
            _r = rapports.get(sym)
            if _r:
                n_m = refaire_mcap(ex, _r[0], _r[1])
                if n_m:
                    mcaps += n_m
                    soc_mcap.add(sym)

            # ── Le coût du capital, avec le bêta que la collecte n'a pas lu ──
            # Avant la note : elle ne lit pas le WACC aujourd'hui, mais l'ordre
            # du collecteur doit être respecté pour que ça reste vrai demain.
            n_w, n_e = refaire_wacc(ex, r, betas.get(sym), sym in refuses)
            if n_w:
                waccs += n_w
                soc_wacc.add(sym)
            if n_e:
                effaces += n_e
                soc_effacee.add(sym)

            # ── Les ratios hors bande ──
            ecartes += ecarter_ratios_degeneres(r)

            # ── Ce qui s'en déduit ──
            # Les deux grandeurs du barème de substitution AVANT la note : c'est
            # elle qui les consomme, et un paquet publié avant ce jour ne les a
            # pas.
            grandeurs_de_substitution(ex, r)
            r["note_q"] = note_quantitative(r)
            try:
                r["note_historique"] = historique_note(ex)
            except Exception as e:
                print("[warn] %s : note historique : %s" % (sym, e), file=sys.stderr)
            r["calculs_rejoues_le"] = horodatage

            apres = (r.get("note_q") or {}).get("note_ramenee")
            if isinstance(avant, (int, float)) and isinstance(apres, (int, float)):
                if apres > avant:
                    montees += 1
                elif apres < avant:
                    descendues += 1
                if abs(apres - avant) >= 2:
                    mouvements.append((round(apres - avant, 1), sym, avant, apres))
            touchees += 1
            change = True

        if change and not essai:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))

    print("[ok] %d société(s) lues, %d rejouée(s) — %.1f s"
          % (total, touchees, time.time() - t0))
    print("[ok] taux retirés (base infinitésimale ou traversée de zéro) : %s"
          % ", ".join("%s %d" % (k, n) for k, n in sorted(retirees.items())))
    print("[ok] divisions REFAITES : %d exercice(s) remis en base sur %d société(s), "
          "%d société(s) changent d'événements" % (rebasees, len(soc_rebasees), div_changees))
    print("[ok] capitalisation historique reconstruite : %d exercice(s) sur "
          "%d société(s) — quatre ancres, tolérance six mois"
          % (mcaps, len(soc_mcap)))
    print("[ok] coût du capital : %d exercice(s) sur %d société(s) — le bêta "
          "dormait dans les fichiers de marché" % (waccs, len(soc_wacc)))
    print("[ok] coût du capital EFFACÉ : %d exercice(s) sur %d société(s) — coût "
          "des fonds propres négatif, ou bêta hors bande"
          % (effaces, len(soc_effacee)))
    if decouvertes:
        # Ce n'est pas un avertissement décoratif : c'est la liste des sociétés
        # dont une division reste à trancher, et seule une collecte le peut.
        print("[!!] %d société(s) où le rejeu aurait INVENTÉ une division — "
              "laissées intactes, à trancher par une collecte :" % len(decouvertes))
        for sym, evs in decouvertes[:10]:
            print("      %-8s %s" % (sym, ", ".join(
                "×%s entre %s et %s" % (e.get("facteur"), e.get("entre"),
                                        e.get("et")) for e in evs)))
    print("[ok] ratios hors bande écartés : %d" % ecartes)
    print("[ok] notes ramenées : %d en hausse, %d en baisse" % (montees, descendues))
    if mouvements:
        mouvements.sort()
        print("[ok] les plus gros mouvements :")
        for d, sym, a, b in (mouvements[:3] + mouvements[-3:]):
            print("      %-8s %.1f → %.1f  (%+.1f)" % (sym, a, b, d))
    if essai:
        print()
        print("[essai] RIEN N'A ÉTÉ ÉCRIT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
