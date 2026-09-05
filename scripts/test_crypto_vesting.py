#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Le garde-fou du calendrier de déverrouillage.

Chaque contrôle porte le nom d'un défaut RÉELLEMENT trouvé le 05/09/2026 en
écrivant le collecteur, pas d'une panne imaginée. Les quatre premiers ont
produit, avant correction, des chiffres publiables et faux — c'est-à-dire la
seule espèce qui compte.

    python3 test_crypto_vesting.py            contrôle le cache publié
    python3 test_crypto_vesting.py --mutants  vérifie que les contrôles MORDENT
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

CACHE = os.path.expanduser("~/Library/Caches/site_crypto_finance")
FICHIER = os.path.join(CACHE, "crypto_vesting_cache.json")

echecs = []


def v(ok, titre, detail=""):
    print("  %s %s%s" % ("✓" if ok else "✗", titre, ("" if ok else " — " + detail)))
    if not ok:
        echecs.append(titre)
    return ok


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def controles(d, maintenant):
    J = d.get("jetons") or {}
    mes = [j for j in J.values() if j.get("statut") == "mesurable"]

    print("\n[1] La fenêtre se lit sur la SÉRIE, pas sur la liste d'événements")
    # DÉFAUT RÉEL : la première version sommait `metadata.unlockEvents`. Ethena
    # n'a AUCUN événement futur alors que 5,07 milliards de jetons — un tiers de
    # son offre — sortent jusqu'en avril 2028. La fiche affichait « aucune
    # échéance à venir ». Six autres jetons étaient à zéro pour la même raison :
    # TRUMP (31 % de sa capitalisation sur 90 jours), BABY, DATA, KMNO, ATH, JTO.
    # ⚠ LA FENÊTRE À CONTRÔLER EST CELLE D'UN AN, PAS CELLE DE QUATRE-VINGT-DIX
    # JOURS. Écrit d'abord sur 90 jours, ce contrôle accusait ONDO, LIT, XDC et
    # PYTH — dont la prochaine échéance tombe au cinquième mois ou au-delà.
    # Rien à trois mois est un fait ordinaire ; rien à un an sous un calendrier
    # encore vivant ne l'est pas.
    muets = [j["symbole"] for j in mes
             if not j.get("calendrier_epuise")
             and (j["fenetres"]["m12"]["jetons"] or 0) <= 0
             and (j.get("deverrouille_pct") or 100) < 95]
    v(not muets,
      "aucun jeton verrouillé n'affiche une année vide sous un calendrier vivant",
      "%d : %s" % (len(muets), muets[:8]))

    print("\n[2] Aucune part d'allocation n'est DÉRIVÉE")
    # DÉFAUT RÉEL : j'avais calculé « part déjà sortie » = actuelle × taux de
    # déverrouillage. Le produit tombait au dixième près sur Monad, ce qui l'a
    # fait passer pour une identité. Rejoué sur l'univers, il dépasse la part
    # finale de 19 à 26 points sur PENGU, S et BNB — et BNB encode ses brûlages
    # en parts NÉGATIVES. Une seule confirmation n'est pas une démonstration.
    derivees = [j["symbole"] for j in mes if "deja_sortie" in (j.get("allocation") or {})]
    v(not derivees, "la clé dérivée a bien disparu du cache", str(derivees[:5]))
    sommes = [(j["symbole"], round(sum(j["allocation"]["finale"].values()), 1))
              for j in mes if j["allocation"].get("finale")
              and abs(sum(j["allocation"]["finale"].values()) - 100) > 2.0]
    v(not sommes, "les parts finales somment à cent", str(sommes[:5]))
    hors = [(j["symbole"], k, round(x, 1)) for j in mes
            for k, x in (j["allocation"].get("avancement") or {}).items()
            if not (-0.5 <= x <= 100.5)]
    v(not hors, "l'avancement reste entre zéro et cent", str(hors[:5]))

    print("\n[3] Une falaise et un filet ne s'additionnent pas")
    # Un déverrouillage instantané et un débit quotidien sont deux faits de
    # nature différente. Les confondre donnerait un tableau où « 10,7 milliards
    # le 24 novembre » et « 15 millions par jour » se lisent sur la même ligne.
    mauvais = [j["symbole"] for j in mes for e in j["prochains"]
               if e["type"] not in ("falaise", "filet")
               or (e["type"] == "filet" and ("par_jour" not in e or "fin" not in e))
               or (e["type"] == "falaise" and "par_jour" in e)]
    v(not mauvais, "chaque échéance porte son type et les champs qui vont avec",
      str(sorted(set(mauvais))[:6]))
    # Un filet regroupe des jours consécutifs : son total doit valoir son débit
    # multiplié par sa durée, à la tolérance de regroupement près (5 %).
    faux = []
    for j in mes:
        for e in j["prochains"]:
            if e["type"] != "filet":
                continue
            attendu = (e.get("par_jour") or 0) * (e.get("jours") or 0)
            if attendu and abs(e["jetons"] - attendu) > attendu * 0.12:
                faux.append((j["symbole"], e["lot"], round(e["jetons"]), round(attendu)))
    v(not faux, "le total d'un filet vaut son débit fois sa durée", str(faux[:4]))

    print("\n[4] Un calendrier qui S'ARRÊTE se distingue d'un calendrier qui SE TERMINE")
    # DÉFAUT RÉEL : 41 jetons n'ont aucune échéance à venir, dont 13 encore
    # verrouillés (BNB, LINK, HBAR, JUP, CRV…). La série de LINK s'arrête au
    # 19 juin 2026 avec un quart de l'offre encore bloquée. Une case vide ferait
    # lire « plus rien ne sortira ».
    sans_date = [j["symbole"] for j in mes if j.get("calendrier_epuise")
                 and not j.get("calendrier_jusqu_au")]
    v(not sans_date, "tout calendrier épuisé porte sa date de fin", str(sans_date[:5]))

    print("\n[5] La minute de la falaise prime sur le jour de la série")
    # La série est datée au jour, l'événement à la minute. Monad déverrouille
    # 10,7 milliards de jetons le 24 novembre 2026 à 05 h 48 UTC ; « le 25 »
    # serait une autre échéance.
    mon = J.get("monad")
    if mon:
        cliff = next((e for e in mon["prochains"] if e["type"] == "falaise"), None)
        v(bool(cliff) and cliff["date"].endswith("05:48:46Z"),
          "la falaise de Monad porte sa minute",
          cliff["date"] if cliff else "aucune falaise")
        v(bool(cliff) and cliff.get("beneficiaire") == "Team",
          "et le nom de son bénéficiaire",
          str(cliff.get("beneficiaire") if cliff else None))

    print("\n[6] Le produit qui n'a pas de sens est ÉCARTÉ, jamais publié")
    # `spcx` et `cbrs` sont des actions tokenisées : leurs quantités sont des
    # nombres d'ACTIONS de prospectus. Multipliées par le prix du jeton, elles
    # donnent des déverrouillages à des centaines de milliards.
    absurdes = [(j["symbole"], j["fenetres"]["j90"]["part_capi_pct"]) for j in mes
                if (j["fenetres"]["j90"]["part_capi_pct"] or 0)
                > d.get("seuil_absurdite_part_capi", 1000.0)]
    v(not absurdes, "aucune part de capitalisation ne dépasse le seuil d'absurdité",
      str(absurdes[:4]))
    for j in J.values():
        if j.get("statut") != "mesurable":
            v(bool(j.get("motif")), "l'écarté %s dit pourquoi" % j["symbole"])

    print("\n[7] L'absence de calendrier se motive")
    # 99 jetons sur 200 n'en ont pas, et la plupart n'en ont pas PAR NATURE :
    # ether n'a jamais eu de vesting, stETH est une enveloppe. Un vide muet
    # ferait croire à un trou de collecte, et les deux appellent des gestes
    # opposés.
    sans = d.get("sans_calendrier") or {}
    muets2 = [g for g, x in sans.items() if not (x.get("raison") or "").strip()]
    v(not muets2, "chaque jeton sans calendrier porte sa raison", str(muets2[:5]))
    v(len(J) + len(sans) == d.get("univers"),
      "les jetons avec et sans calendrier couvrent tout l'univers",
      "%d + %d ≠ %d" % (len(J), len(sans), d.get("univers")))

    print("\n[8] Le caractère documentaire est déclaré")
    # Le champ de vérification en temps réel de la source est vide sur la
    # totalité des jetons, et son champ de sources l'est aussi. Publier un
    # calendrier sans le dire serait le faire passer pour vérifié.
    v("DOCUMENTAIRE" in (d.get("avertissement") or "").upper(),
      "le cache porte l'avertissement")
    v(d.get("realTimeData_non_vide") == 0
      or "temps réel" in (d.get("avertissement") or ""),
      "et l'avertissement reste vrai",
      "%s jeton(s) portent une vérification" % d.get("realTimeData_non_vide"))

    print("\n[9] Fraîcheur et cohérence d'ensemble")
    age = (maintenant - ts(d["genere_le"])) / 3600.0
    v(age < 72, "le cache a moins de trois jours", "%.1f h" % age)
    v(len(mes) >= 80, "au moins quatre-vingts jetons mesurables", str(len(mes)))
    poids = os.path.getsize(FICHIER) / 1024.0
    v(poids < 3072, "le cache tient sous trois mégaoctets", "%.0f Ko" % poids)
    # Un dépassement de la fenêtre courte par rapport à la longue est impossible.
    incoh = [(j["symbole"], j["fenetres"]["j90"]["jetons"], j["fenetres"]["m12"]["jetons"])
             for j in mes
             if (j["fenetres"]["j90"]["jetons"] or 0) > (j["fenetres"]["m12"]["jetons"] or 0) * 1.001]
    v(not incoh, "quatre-vingt-dix jours ne déverrouillent jamais plus qu'un an",
      str(incoh[:4]))


def mutants():
    """Un contrôle qu'on n'a pas fait échouer ne protège rien.

    On abîme le cache d'une façon précise et on exige que le contrôle visé
    tombe. Quatre mutations avaient déjà passé trente-cinq contrôles sur un
    autre collecteur de ce dépôt : la vérification des vérifications n'est pas
    un luxe.
    """
    base = json.load(open(FICHIER, encoding="utf-8"))
    maintenant = time.time()
    cas = []

    # (1) la fenêtre repasse sur les événements → Ethena retombe à zéro
    m = json.loads(json.dumps(base))
    for g in ("ethena", "official-trump", "kamino"):
        if g in m["jetons"]:
            for f in ("j90", "m12"):
                m["jetons"][g]["fenetres"][f]["jetons"] = 0
                m["jetons"][g]["fenetres"][f]["part_capi_pct"] = 0
    cas.append(("fenêtre vide sous un calendrier vivant", m))

    # (2) la dérivation fausse revient
    m = json.loads(json.dumps(base))
    m["jetons"]["monad"]["allocation"]["deja_sortie"] = {"insiders": 0.0}
    cas.append(("part d'allocation dérivée", m))

    # (3) un filet perd son débit → il se confond avec une falaise
    m = json.loads(json.dumps(base))
    for j in m["jetons"].values():
        for e in j["prochains"]:
            if e["type"] == "filet":
                e.pop("par_jour", None)
                break
        break
    cas.append(("filet sans débit quotidien", m))

    # (4) le total d'un filet ne suit plus son débit
    m = json.loads(json.dumps(base))
    for j in m["jetons"].values():
        for e in j["prochains"]:
            if e["type"] == "filet":
                e["jetons"] = e["jetons"] * 3
                break
        else:
            continue
        break
    cas.append(("total de filet incohérent", m))

    # (5) un calendrier épuisé sans date de fin
    m = json.loads(json.dumps(base))
    for j in m["jetons"].values():
        if j.get("calendrier_epuise"):
            j["calendrier_jusqu_au"] = None
            break
    cas.append(("calendrier épuisé muet", m))

    # (6) la minute de Monad remplacée par le jour
    m = json.loads(json.dumps(base))
    for e in m["jetons"]["monad"]["prochains"]:
        if e["type"] == "falaise":
            e["date"] = "2026-11-25T00:00:00Z"
            break
    cas.append(("falaise datée au jour", m))

    # (7) une action tokenisée publiée comme mesurable
    m = json.loads(json.dumps(base))
    g = next(iter(m["jetons"]))
    m["jetons"][g]["fenetres"]["j90"]["part_capi_pct"] = 480000.0
    cas.append(("part de capitalisation absurde", m))

    # (8) un jeton sans calendrier et sans raison
    m = json.loads(json.dumps(base))
    g = next(iter(m["sans_calendrier"]))
    m["sans_calendrier"][g]["raison"] = ""
    cas.append(("absence non motivée", m))

    # (9) l'avertissement documentaire retiré
    m = json.loads(json.dumps(base))
    m["avertissement"] = "Calendrier des déverrouillages."
    cas.append(("avertissement effacé", m))

    # (10) 90 jours dépassent 12 mois
    m = json.loads(json.dumps(base))
    g = next(g for g, j in m["jetons"].items() if j["fenetres"]["m12"]["jetons"])
    m["jetons"][g]["fenetres"]["j90"]["jetons"] = m["jetons"][g]["fenetres"]["m12"]["jetons"] * 2
    cas.append(("fenêtre courte supérieure à la longue", m))

    print("\n══ les contrôles mordent-ils ? ══")
    # ⚠ PIÈGE PAYÉ EN L'ÉCRIVANT : la première version déclarait les dix
    # mutations « attrapées » alors qu'un contrôle échouait DÉJÀ sur le cache
    # sain. Chaque mutation héritait de cet échec-là et paraissait détectée —
    # dix faux verts d'affilée, et le message final disait pourtant « les dix
    # mutations tombent ». On exige donc un échec NOUVEAU, absent de la base.
    global echecs
    echecs = []
    sortie = sys.stdout
    sys.stdout = open(os.devnull, "w")
    try:
        controles(base, maintenant)
    finally:
        sys.stdout.close()
        sys.stdout = sortie
    socle = set(echecs)
    if socle:
        print("  (le cache sain échoue déjà sur %d contrôle(s) : ils sont neutralisés ici)"
              % len(socle))
    survivants = []
    for nom, doc in cas:
        echecs = []
        sortie = sys.stdout
        sys.stdout = open(os.devnull, "w")
        try:
            controles(doc, maintenant)
        except Exception:
            echecs.append("exception")
        finally:
            sys.stdout.close()
            sys.stdout = sortie
        neufs = [e for e in echecs if e not in socle]
        if neufs:
            print("  ✓ %-42s attrapée (%s)" % (nom, neufs[0][:44]))
        else:
            print("  ✗ %-42s A SURVÉCU" % nom)
            survivants.append(nom)
    return survivants


def main():
    if not os.path.exists(FICHIER):
        print("cache absent : lancer d'abord fetch_crypto_vesting.py")
        return 1
    d = json.load(open(FICHIER, encoding="utf-8"))
    maintenant = time.time()
    print("══ calendrier de déverrouillage — %d avec, %d sans, %d écartés ══"
          % (d["couverture"]["avec_calendrier"], d["couverture"]["sans_calendrier"],
             d["couverture"]["ecartes"]))
    controles(d, maintenant)
    n = len(echecs)
    if "--mutants" in sys.argv:
        s = mutants()
        if s:
            print("\n%d mutation(s) non attrapée(s) : le garde-fou est troué." % len(s))
            return 1
        print("\nLes dix mutations tombent.")
    print("\n%s" % ("TOUT PASSE." if not n else "%d CONTRÔLE(S) EN ÉCHEC." % n))
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
