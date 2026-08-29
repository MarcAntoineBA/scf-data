#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rejouer_resume_intl.py — Rejoue le bloc `resume` des paquets internationaux,
                         sans une seule requête réseau.

POURQUOI CE SCRIPT EXISTE

Un paquet porte l'horodatage de son ÉCRITURE, jamais celui du code qui l'a
produit. Les 512 paquets internationaux dataient du 28/08/2026 à 03:55 ; le
correctif de `_mediane_fenetre` — celui qui refuse d'appeler « médiane sur cinq
ans » une médiane calculée sur UNE variation — a été écrit à 16:16 le même jour.
Douze heures d'écart, et le cache était du mauvais côté.

Mesuré avant rejeu : 14 666 valeurs « 5a » publiées que le code actuel déclare
muettes (fcf 5 584, ocf 3 226, eps 2 829, div 2 659, ca 368). Parmi elles,
8 611 sont des critères notés au barème, portant 2 654,5 points — dont 2 557
PLEINS — sur 2 567 sociétés, soit 13,2 % de l'univers international.

Cas nommé, pour rendre la chose concrète : Galderma n'a que deux dividendes
connus (2024 : 0,166 ; 2025 : 0,441), donc UNE variation. Le résumé publiait
`croissances.div = {"1a": 166.65, "5a": 166.65}` — la « médiane sur cinq ans »
était le même et unique chiffre que le « 1 an », et rapportait un second point
plein.

POURQUOI PAS UNE RECOLLECTE

L'univers international fait 19 495 sociétés et la source ne se parcourt qu'en
plusieurs jours. Or tout ce dont le résumé a besoin est déjà écrit dans les
paquets. Le rejeu prend quelques minutes de processeur et zéro requête.

CE QU'IL RECALCULE AU PASSAGE

`couverture_interets` — le résultat d'exploitation rapporté à la charge
d'intérêts — était renseigné sur 8 sociétés internationales sur 19 495. Les deux
termes sont pourtant stockés dans chaque exercice. Le rejeu le recalcule, sur
l'exercice le plus récent, avant de reconstruire le résumé.

CE QU'IL NE FAIT PAS
  · Il ne touche AUCUN champ d'exercice : les états financiers restent ceux de
    la collecte. Seul le résumé — ce qui se DÉDUIT d'eux — est refait.
  · Il ne réécrit pas `genere_le`. Cet horodatage est déjà trompeur (les 512
    paquets portent la même date à la seconde alors que six septièmes des
    résumés viennent de passages antérieurs) ; l'aggraver serait pire. Le rejeu
    inscrit son propre horodatage sous `resume_rejoue_le`.
  · Il ne répare pas les notes historiques calculées sous une définition
    antérieure du critère dividende.

Lancement :
    python3 scripts/rejouer_resume_intl.py            # rejoue et écrit
    python3 scripts/rejouer_resume_intl.py --essai    # mesure sans rien écrire
"""

import glob
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_intl_fundamentals import construire_resume        # noqa: E402
from fondamentaux_communs import (                            # noqa: E402
    _pct, _wacc, beta_plausible, cours_ancres, cours_a_la_date,
)

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")

# Les clés dont `construire_resume` a la charge : si elle ne les écrit pas, c'est
# qu'elles ne doivent plus être là. Toutes les autres sont reportées.
# `ratios_ecartes` en fait partie — elle est posée quand un ratio sort de la
# bande de plausibilité, et RETIRÉE quand plus aucun n'en sort. La reporter
# aveuglément ressusciterait un avertissement pour un défaut réparé.
GEREES_PAR_LE_REJEU = {"ratios_ecartes"}

# Les clés que le rejeu écrit à None faute de les connaître, et qu'il faut
# reprendre de l'ancien résumé plutôt que d'écraser par un vide.
COLLECTE = {"accn", "depose_le", "devise", "devise_cotation", "devise_deduite",
            "devises_alignees", "cours_natif", "cours_natif_le", "cours_source"}


def defaire_division_fabriquee(exercices):
    """Défait une correction de division que la source n'appelait pas.

    La source internationale RÉTRO-AJUSTE déjà son historique : elle republie les
    exercices anciens en actions d'aujourd'hui. La recouture des divisions n'avait
    donc rien à rattraper — et quand elle trouvait un saut, elle le fabriquait.
    Mesuré le 28/08/2026 : 1 423 corrections sur 1 187 sociétés, toutes fausses.

    La correction est heureusement EXACTEMENT réversible : elle multiplie les
    nombres d'actions par un facteur, divise les grandeurs par action par ce même
    facteur, et inscrit `_facteur_division` sur l'exercice. On applique l'inverse,
    puis on recalcule les trois grandeurs par action qui en dérivent.

    La signature à surveiller : Ritchie Bros portait 167 829 037,5 actions — une
    DEMI-ACTION. Un nombre d'actions non entier ne vient jamais d'un dépôt, il
    vient d'un facteur inventé.

    Rend le nombre d'exercices remis en base.
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

    # Les grandeurs par action DÉRIVENT du nombre d'actions : les laisser telles
    # quelles laisserait la moitié de la série sur l'ancienne base.
    for e in exercices:
        sh = e.get("shares_diluted")
        if isinstance(sh, (int, float)) and sh:
            for cle, source in (("ca_par_action", "revenue"),
                                ("fcf_par_action", "fcf"),
                                ("ocf_par_action", "ocf")):
                v = e.get(source)
                e[cle] = round(v / sh, 4) if isinstance(v, (int, float)) else None
    return n


def couverture(e):
    """Résultat d'exploitation / charge d'intérêts, pour un exercice.

    La charge d'intérêts est déjà stockée en valeur absolue par le collecteur.
    On ne calcule rien quand elle est nulle : une société sans dette n'a pas une
    couverture infinie, elle n'a pas de couverture — la question ne se pose pas.
    """
    ope, interets = e.get("operating_income"), e.get("interest_expense")
    if not isinstance(ope, (int, float)) or not isinstance(interets, (int, float)):
        return None
    i = abs(interets)
    if i <= 0:
        return None
    return round(ope / i, 2)


# Le pas maximal toléré dans une série de nombres d'actions. Voir la garde dans
# `refaire_mcap` : le relevé qui a fixé ce chiffre y est écrit en entier.
SAUT_ACTIONS_MAX = 5.0


def charger_cours_marche():
    """{symbole: (cours, ancres, devise de cotation)} depuis les fichiers de marché.

    Les ancres sont les cours d'il y a un, trois, cinq et dix ans, déduits du
    cours du jour et de ses variations. La devise est celle de la COTATION : le
    cours devra être ramené dans celle des états avant d'être multiplié par un
    nombre d'actions.

    On retient aussi le jour d'écriture du fichier plutôt qu'« aujourd'hui » : un
    fichier collecté il y a trois jours daterait ses ancres de trois jours de
    trop, et l'erreur se cumulerait avec la tolérance.
    """
    out = {}
    for pth in sorted(glob.glob(os.path.join(CACHE, "marche_[0-9]*.json"))):
        try:
            with open(pth, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        ch = d.get("champs") or []
        if "price" not in ch:
            continue
        i = {n: ch.index(n) for n in
             ("price", "ch1y", "ch3y", "ch5y", "ch10y", "priceCurrency")
             if n in ch}
        # ⚠ `genere_le` ET NON `updated`. Les fichiers de marché datent leur
        # écriture sous `genere_le` ; chercher `updated` rendait None, `jour_ref`
        # était vide, et `refaire_mcap` sortait à sa première ligne — zéro exercice
        # comblé là où la mesure en annonçait 34 544, sans une seule erreur.
        jour = (d.get("genere_le") or d.get("updated") or "")[:10] or None
        for sym, v in (d.get("societes") or {}).items():
            def lire(nom):
                k = i.get(nom)
                return v[k] if (k is not None and k < len(v)) else None
            # ⚠ DES RAPPORTS, PAS DES COURS. `cours_ancres` est appelée avec un
            # cours de 1 : elle rend alors `1 / (1 + chNy/100)`, c'est-à-dire le
            # rapport entre le cours d'alors et celui d'aujourd'hui. Un rapport
            # n'a pas de monnaie — plus de conversion, plus de devise à croire.
            a = cours_ancres(1.0, lire("ch1y"), lire("ch3y"),
                             lire("ch5y"), lire("ch10y"))
            if a:
                out[sym] = (a, jour)
    return out


def refaire_mcap(exercices, resume, variations, jour_ref):
    """Comble la capitalisation manquante en transportant un RAPPORT, pas un montant.

        mcap(exercice) = mcap_du_dernier × cours(exercice)/cours(aujourd'hui)
                                         × actions(exercice)/actions(dernier)

    et le premier rapport vaut exactement `1 / (1 + chNy/100)`.

    ⚠ POURQUOI UN RAPPORT ET NON UN MONTANT. Calculer `cours × actions` obligeait
    à connaître la devise des états (141 sociétés en déclarent une impossible —
    des chinoises cotées à Hong Kong « publiant en yens »), l'unité du nombre
    d'actions, et un taux de change. Trois occasions de se tromper, et un montant
    qui ne se raccordait pas à celui de la collecte : saut de ×3 000 entre le
    dernier exercice et l'avant-dernier sur le graphique.

    Un rapport de deux cours n'a pas de monnaie. Un rapport de deux nombres
    d'actions se moque de l'unité. Et au dernier exercice le rapport vaut 1 :
    la courbe se raccorde exactement.

    ⚠ ON N'EFFACE QUE LE SIEN, mais on l'efface toujours d'abord : une règle qui
    se durcit doit atteindre ce qui est déjà écrit.

    ⚠ ON NE BÂTIT PAS SUR UN NOMBRE D'ACTIONS AUQUEL ON NE CROIT PAS. La source
    rétro-ajuste son historique : un saut de plus de ×5 dans la série d'actions
    est un défaut du paquet (Enel ×153, Sino Green ×111), et une capitalisation
    bâtie dessus l'est aussi. Mesuré : 385 sociétés écartées sur 19 430, pour
    320 reconstructions perdues sur 34 267.

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

    # L'exercice de référence : le plus récent qui porte une capitalisation de
    # la COLLECTE. C'est lui qui donne l'échelle ; tout le reste en dérive.
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
        sh = e.get("shares_diluted")
        fin_e = e.get("fin")
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

    Ces fichiers portent le bêta de 27 013 des 37 574 cotations. Le collecteur
    international les lit désormais, mais les paquets publiés datent d'avant :
    60 sociétés sur 19 455 portent un WACC. Recollecter l'univers demande
    plusieurs jours ; le lire ici demande une seconde.

    ⚠ Passé par `beta_plausible` — la même bande que les collecteurs, pas une
    copie. Sans elle, Elcid Investments entre avec −20 833 et Compass Gas e
    Energia avec 95,39, et `_wacc` en tire des coûts du capital à quatre
    chiffres.
    """
    betas = {}
    hors_bande = set()
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
                # Retenu NOMMÉMENT, pas seulement compté : une société dont le
                # bêta est refusé doit perdre le coût du capital qu'un passage
                # antérieur lui avait donné. Un compteur ne permet pas ça.
                hors_bande.add(sym)
    return betas, hors_bande


def refaire_wacc(exercices, beta, refuse=False):
    """Recalcule le coût du capital sur chaque exercice qui a une capitalisation.

    Les quatre termes de `_wacc` sont déjà dans l'exercice — seul le bêta venait
    d'ailleurs. Rien d'autre n'est touché : les états financiers restent ceux de
    la collecte.

    ⚠ `mcap_estime` est déjà ramené dans la devise des états par le collecteur
    (`_en_devise_etats`). Une société dont la cotation et les états divergent
    sans taux connu n'en a pas — la garde des devises tient donc en amont, et on
    ne la réécrit pas ici. Une garde recopiée est une garde qui dérive.

    Rend le nombre d'exercices qui gagnent un WACC.
    """
    # ⚠ ON N'EFFACE PAS PAR IGNORANCE. Sans bêta ici et sans refus explicite, on
    # ne sait rien : la collecte, elle, en avait peut-être un — le cache
    # sectoriel du tracker en porte 641. Effacer détruirait une valeur juste.
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
            # La règle a dit non — coût des fonds propres négatif, ou bêta hors
            # bande. Une règle qui se durcit doit atteindre ce qui est déjà
            # écrit, sinon le durcissement ne protège que les collectes futures.
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
    return n, efface


def main():
    essai = "--essai" in sys.argv
    t0 = time.time()
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "intl_detail_[0-9][0-9][0-9].json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet intl_detail_NNN.json", file=sys.stderr)
        return 2

    cours_par_sym = charger_cours_marche()
    print("[info] rapports de cours reconstruits pour %d cotation(s)"
          % len(cours_par_sym))

    betas, hors_bande = charger_beta_marche()
    print("[info] bêta lu pour %d cotation(s) ; %d hors bande refusé(s)"
          % (len(betas), len(hors_bande)))

    horodatage = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    mcaps = 0
    soc_mcap = set()
    waccs = effaces = 0
    soc_wacc = set()
    soc_effacee = set()
    total = rejouees = echecs = 0
    couvertures = 0
    defaits = 0
    soc_defaites = set()
    retirees = {"ca": 0, "eps": 0, "fcf": 0, "ocf": 0, "div": 0}
    ratios_ecartes = 0
    montees = descendues = 0
    bascules = []

    for f in paquets:
        try:
            with open(f, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            continue
        change = False
        for sym, v in (doc.get("societes") or {}).items():
            ex = v.get("exercices") or []
            ancien = v.get("resume")
            if not ex or not isinstance(ancien, dict):
                continue
            total += 1

            # Defaire les divisions fabriquees AVANT tout le reste : les
            # grandeurs par action, les croissances et la note en derivent.
            n_def = defaire_division_fabriquee(ex)
            if n_def:
                defaits += n_def
                soc_defaites.add(sym)

            # La couverture d'intérêts, recalculée sur le dernier exercice.
            c = couverture(ex[-1])
            if c is not None:
                ex[-1]["couverture_interets"] = c
                couvertures += 1

            # La capitalisation historique, AVANT le coût du capital : c'est
            # elle qui manquait, et c'est d'elle que le WACC dépend.
            _c = cours_par_sym.get(sym)
            if _c:
                _a, _j = _c
                n_m = refaire_mcap(ex, ancien, _a, _j)
                if n_m:
                    mcaps += n_m
                    soc_mcap.add(sym)

            # Le coût du capital, avec le bêta que la collecte n'avait pas lu.
            # AVANT `construire_resume` : c'est elle qui en tire `wacc_1a`,
            # `wacc_5a`, `wacc_10a` et `roic_moins_wacc`.
            b = betas.get(sym)
            n_w, n_e = refaire_wacc(ex, b, sym in hors_bande)
            if n_w:
                waccs += n_w
                soc_wacc.add(sym)
            if n_e:
                effaces += n_e
                soc_effacee.add(sym)

            try:
                neuf = construire_resume(ex,
                                         ancien.get("divisions_action"),
                                         ancien.get("unites_actions_corrigees"))
            except Exception as e:
                print("[warn] %s : %s" % (sym, e), file=sys.stderr)
                echecs += 1
                continue

            # Ce que le rejeu retire, série par série — le chiffre qui dit si le
            # correctif a mordu là où on l'attendait.
            for cle in retirees:
                a = ((ancien.get("croissances") or {}).get(cle) or {})
                b = ((neuf.get("croissances") or {}).get(cle) or {})
                for fen in ("1a", "5a", "10a"):
                    if a.get(fen) is not None and b.get(fen) is None:
                        retirees[cle] += 1
            ratios_ecartes += len(neuf.get("ratios_ecartes") or {})

            na = (ancien.get("note_q") or {}).get("note_ramenee")
            nb = (neuf.get("note_q") or {}).get("note_ramenee")
            if isinstance(na, (int, float)) and isinstance(nb, (int, float)):
                if nb > na:
                    montees += 1
                elif nb < na:
                    descendues += 1
                if abs(nb - na) >= 2:
                    bascules.append((round(nb - na, 1), sym, na, nb))

            # ── CE QUE LE REJEU NE SAIT PAS REFABRIQUER ──
            # `construire_resume` reconstruit le résumé à partir des seuls
            # exercices. Tout ce que le résumé portait et qui venait d'AILLEURS
            # dans la collecte — la source, son adresse, la date de collecte, la
            # fréquence de publication, la devise, le cours natif — serait perdu
            # en silence, et la fiche afficherait un tiret là où elle affichait
            # un chiffre. Mesuré avant d'écrire : quatre clés disparaissaient sur
            # les 19 495 sociétés (source, source_url, collecte_le,
            # frequence_publication).
            #
            # On reporte donc TOUTE clé absente du résumé neuf, plutôt qu'une
            # liste nommée qui vieillirait mal : perdre une donnée est pire que
            # conserver un champ de trop.
            for cle, val in ancien.items():
                if cle not in neuf and cle not in GEREES_PAR_LE_REJEU:
                    neuf[cle] = val
                elif neuf.get(cle) is None and cle in COLLECTE:
                    neuf[cle] = val
            neuf["resume_rejoue_le"] = horodatage

            # Les paquets d'avant ce jour n'ont pas `montants_marche`, et le
            # rejeu vient d'y poser un coût du capital. Sans ce champ, la fiche
            # afficherait le chiffre SOUS une phrase annonçant qu'il a été laissé
            # vide. La filière internationale convertit — on le dit.
            if neuf.get("devises_alignees") is False and not neuf.get("montants_marche"):
                neuf["montants_marche"] = "convertis"

            v["resume"] = neuf
            rejouees += 1
            change = True

        if change and not essai:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, ensure_ascii=False, separators=(",", ":"))

    print("[ok] %d société(s) lues, %d rejouée(s), %d échec(s) — %.1f s"
          % (total, rejouees, echecs, time.time() - t0))
    print("[ok] divisions FABRIQUEES defaites : %d exercice(s) sur %d societe(s) — "
          "la source internationale retro-ajuste deja son historique"
          % (defaits, len(soc_defaites)))
    print("[ok] couverture d'intérêts calculée sur %d société(s)" % couvertures)
    print("[ok] capitalisation historique reconstruite : %d exercice(s) sur "
          "%d société(s) — quatre ancres, tolérance six mois"
          % (mcaps, len(soc_mcap)))
    print("[ok] coût du capital : %d exercice(s) sur %d société(s) — "
          "le bêta dormait dans les fichiers de marché"
          % (waccs, len(soc_wacc)))
    print("[ok] coût du capital EFFACÉ : %d exercice(s) sur %d société(s) — "
          "coût des fonds propres négatif, ou bêta hors bande"
          % (effaces, len(soc_effacee)))
    print("[ok] taux retirés (calculés sur une base qui n'en était pas une) : %s"
          % ", ".join("%s %d" % (k, n) for k, n in sorted(retirees.items())))
    print("[ok] ratios hors bande écartés : %d" % ratios_ecartes)
    print("[ok] notes ramenées : %d en hausse, %d en baisse" % (montees, descendues))
    if bascules:
        bascules.sort()
        print("[ok] les 6 plus gros mouvements de note :")
        for d, sym, a, b in (bascules[:3] + bascules[-3:]):
            print("      %-11s %.1f → %.1f  (%+.1f)" % (sym, a, b, d))
    if essai:
        print()
        print("[essai] RIEN N'A ÉTÉ ÉCRIT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
