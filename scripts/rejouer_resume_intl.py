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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_intl_fundamentals import construire_resume        # noqa: E402
from fondamentaux_communs import _pct                        # noqa: E402

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


def main():
    essai = "--essai" in sys.argv
    t0 = time.time()
    paquets = [f for f in sorted(glob.glob(os.path.join(CACHE, "intl_detail_[0-9][0-9][0-9].json")))
               if "sync-conflict" not in f]
    if not paquets:
        print("[fatal] aucun paquet intl_detail_NNN.json", file=sys.stderr)
        return 2

    horodatage = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
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
