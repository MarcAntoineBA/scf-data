#!/usr/bin/env python3
"""Recalcule l'historique de la note depuis les exercices DÉJÀ en cache.

POURQUOI CE SCRIPT EXISTE
Le collecteur SEC tronquait `note_historique` à douze points (`[-12:]`). Une
société qui dépose depuis vingt et un ans produit dix-sept notes ; cinq étaient
jetées. Or « la note année après année » est exactement ce que le concurrent ne
montre pas — il n'affiche qu'un instantané.

Le plafond est levé dans le collecteur. Mais les notes manquantes se calculent
depuis les exercices, qui sont déjà dans le cache : réinterroger la SEC pour
trois mille sociétés afin de recalculer une valeur qu'on détient déjà serait du
gaspillage, et le collecteur travaille par tranches — l'historique complet
n'apparaîtrait qu'après plusieurs jours.

Ce script fait le travail en local, en une passe, sans une seule requête réseau.

LE CONTRÔLE QUI PASSE AVANT L'ÉCRITURE
On ne réécrit rien tant qu'on n'a pas prouvé que le recalcul REPRODUIT les notes
déjà stockées sur leurs années communes. L'extraction de `entrees_bareme` hors de
`construire` est une refonte : si elle avait changé un comportement — une portée
perdue, un auxiliaire différent — les notes bougeraient, et on l'aurait réécrit
partout sans le voir. Un écart sur une seule année arrête tout.
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_sec_fundamentals as F                              # noqa: E402

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE = os.path.expanduser("~/Site_Crypto_Finance")
TOLERANCE = 0.001          # une note est un nombre à une décimale ; 0,001 est du bruit


def main():
    paquets = sorted(glob.glob(os.path.join(CACHE, "sec_detail_*.json")))
    if not paquets:
        print("[fatal] aucun paquet sec_detail_*.json", file=sys.stderr)
        return 2
    print("%d paquets à traiter" % len(paquets))

    # ── Passe 1 : CONTRÔLE, sans rien écrire ──
    controles = divergences = 0
    exemples = []
    for f in paquets:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for sym, v in (d.get("societes") or {}).items():
            ex = v.get("exercices") or []
            ancien = ((v.get("resume") or {}).get("note_historique")) or []
            if not ex or not ancien:
                continue
            neuf = {x["annee"]: x for x in F.historique_note(ex)}
            for a in ancien:
                n = neuf.get(a["annee"])
                if not n:
                    continue
                controles += 1
                for cle in ("note", "note_ramenee"):
                    va, vn = a.get(cle), n.get(cle)
                    if va is None and vn is None:
                        continue
                    if va is None or vn is None or abs(va - vn) > TOLERANCE:
                        divergences += 1
                        if len(exemples) < 8:
                            exemples.append("%s %s %s : stocké %s → recalculé %s"
                                            % (sym, a["annee"], cle, va, vn))
                        break

    print("\nCONTRÔLE DE NON-RÉGRESSION")
    print("   %d notes confrontées sur leurs années communes" % controles)
    print("   %d divergences" % divergences)
    for e in exemples:
        print("      %s" % e)
    if divergences:
        print("\n[REFUS] le recalcul ne reproduit pas les notes existantes.")
        print("        L'extraction de `entrees_bareme` a donc changé un")
        print("        comportement. On n'écrit rien : une note fausse partout")
        print("        vaut moins qu'une note courte mais juste.")
        return 3
    if controles < 500:
        print("\n[REFUS] trop peu de notes confrontées (%d) : le contrôle ne")
        print("        prouve rien. Un cache vide passerait ce test.")
        return 4
    print("   ✓ identique — l'extraction n'a rien changé, on peut écrire.")

    # ── Passe 2 : ÉCRITURE ──
    total = allonges = points_avant = points_apres = 0
    for f in paquets:
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        change = False
        for sym, v in (d.get("societes") or {}).items():
            ex = v.get("exercices") or []
            if not ex or not isinstance(v.get("resume"), dict):
                continue
            avant = len(v["resume"].get("note_historique") or [])
            neuf = F.historique_note(ex)
            if not neuf:
                continue
            total += 1
            points_avant += avant
            points_apres += len(neuf)
            if len(neuf) != avant or avant == 0:
                allonges += 1
            v["resume"]["note_historique"] = neuf
            change = True
        if change:
            with open(f, "w", encoding="utf-8") as h:
                json.dump(d, h, ensure_ascii=False, separators=(",", ":"))
            # Le jumeau publié, s'il existe.
            jumeau = os.path.join(SITE, os.path.basename(f))
            if os.path.exists(jumeau):
                with open(jumeau, "w", encoding="utf-8") as h:
                    json.dump(d, h, ensure_ascii=False, separators=(",", ":"))

    print("\nÉCRITURE")
    print("   %d sociétés traitées · %d dont l'historique change" % (total, allonges))
    print("   points de note : %d → %d  (+%d)"
          % (points_avant, points_apres, points_apres - points_avant))
    if total:
        print("   moyenne par société : %.1f → %.1f"
              % (points_avant / total, points_apres / total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
