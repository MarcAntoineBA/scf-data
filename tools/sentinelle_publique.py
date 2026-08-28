#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sentinelle_publique.py — L'ÂGE de ce que le site public sert vraiment.

POURQUOI CE FICHIER EXISTE (incident du 2026-08-28)
───────────────────────────────────────────────────
Ce soir-là, le site a servi pendant des heures une veille Trésoreries datée de
12:33 UTC et un fil d'actualités de la mi-journée. Personne ne l'a su : c'est
l'utilisateur qui l'a remarqué à l'œil, sur sa propre page.

La raison est structurelle. Toute la surveillance de fraîcheur
(watchdog_freshness.py, kickstart launchd, garde de retard de publication)
tourne SUR LE MAC. Quand le Mac dort ou s'éteint — ce qui arrive tous les
jours — plus rien ne tourne, Y COMPRIS CE QUI A POUR MÉTIER DE CONSTATER QUE
RIEN NE TOURNE. Un gardien qui dort avec la maison ne garde rien.

Cette sentinelle tourne donc AILLEURS : sur un runner GitHub, qui ne dépend ni
du Mac ni du PC. C'est sa seule raison d'être, et c'est ce qui la rend utile.

CE QU'ELLE MESURE
─────────────────
Elle interroge le SITE PUBLIC, pas le dépôt. La distinction est capitale et
l'incident l'a prouvée : ce soir-là, le dépôt contenait un fil d'actualités de
16:00 pendant que le site en ligne servait encore celui de 14:40. La donnée
était collectée, commitée… et jamais déployée. Seul compte ce que reçoit le
visiteur.

Deux verdicts distincts :

  1. LA CHAÎNE EST-ELLE ARRÊTÉE ?  (la panne de ce soir-là)
     Si le PLUS FRAIS de tous les fichiers surveillés dépasse SEUIL_GLOBAL_H,
     ce n'est pas une source qui a lâché : c'est la publication elle-même qui
     s'est tue. C'est l'alarme qui compte, et elle ne dépend d'aucun seuil par
     tuile.

  2. TELLE TUILE MENT-ELLE ?
     Chaque fichier surveillé porte le seuil auquel LA PAGE elle-même se
     déclare périmée. Au-delà, le visiteur voit un bandeau rouge — et le but de
     cette sentinelle est qu'on l'apprenne avant lui.

LE BRUIT EST UN BUG
───────────────────
Une alarme qui se répète toutes les heures pendant une nuit de sommeil du Mac
finit coupée, et on est revenu au point de départ. La sentinelle n'alerte donc
que sur la BASCULE (tout va bien → en panne), puis une fois par RAPPEL_H tant
que la panne dure, et signale le retour à la normale. L'état est gardé dans
sentinelle/etat.json.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = "https://site-crypto-finance.pages.dev"
# Les caches sont servis sous /data/ (la racine y redirige en 302).
PREFIXE = "data"

RACINE = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
ETAT = os.path.join(RACINE, "sentinelle", "etat.json")

# L'horloge du Mac. Les collecteurs écrivent une partie de leurs horodatages
# SANS fuseau ("2026-08-28T14:40:18"), et ce sont des heures de Paris. Les lire
# comme de l'UTC rajeunit la donnée de deux heures en été — c'est-à-dire que la
# sentinelle raterait exactement les pannes qu'elle cherche.
FUSEAU_MAC = ZoneInfo("Europe/Paris")

SEUIL_GLOBAL_H = 2.0   # plus frais fichier au-delà de ça = chaîne de publication arrêtée
RAPPEL_H = 12.0        # panne qui dure : on se rappelle au bon souvenir 1×/12 h

# (fichier, seuil_h, ce que voit le visiteur au-delà)
# Le seuil REPRODUIT celui que la page s'applique à elle-même. S'il change là-bas,
# il doit changer ici : une sentinelle plus laxiste que la page ne sert à rien.
SURVEILLES = [
    ("treasury_alert_live.json",      3.0, "tuile Trésoreries : bandeau « VEILLE PÉRIMÉE »"),
    ("fj_news_cache.json",            3.0, "fil d'actualités : « maj il y a Nh »"),
    ("news_cache.json",               3.0, "dépêches (repli du fil d'actualités)"),
    ("moneyflow_cache.json",          6.0, "podium « Où va l'argent » : pastille périmé"),
    ("macro_calendar_cache.json",     6.0, "calendrier macro"),
    ("prediction_markets_cache.json", 6.0, "Prediction Markets : bandeau « Collecte figée »"),
    ("crypto_etf_cache.json",         8.0, "flux ETF crypto"),
    ("global_markets_cache.json",     8.0, "marchés mondiaux"),
]


def lire(nom):
    """Le JSON servi au visiteur, cache CDN contourné."""
    url = f"{BASE}/{PREFIXE}/{nom}?cb={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={
        # Cloudflare renvoie 403 aux agents par défaut de urllib.
        "User-Agent": "sentinelle-fraicheur (surveillance du site)",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))


def horodatage(d):
    """(epoch_utc, clé lue) depuis les formes que les collecteurs produisent.

    Il y en a quatre, héritées de collecteurs écrits à des époques différentes :
    epoch entier, ISO avec Z, ISO sans fuseau, et « JJ/MM/AAAA HH:MM ».
    Les deux dernières sont des heures de Paris — voir FUSEAU_MAC.
    """
    if not isinstance(d, dict):
        return None, None
    for cle in ("generated_at", "ts_fetched", "updated", "last_update"):
        if cle not in d:
            continue
        v = d[cle]
        if isinstance(v, (int, float)):
            # Un epoch en secondes ; en millisecondes chez certains collecteurs.
            return (float(v) / 1000.0 if v > 1e11 else float(v)), cle
        if not isinstance(v, str) or not v.strip():
            continue
        s = v.strip()
        for fmt, aware in (
            ("%Y-%m-%dT%H:%M:%S.%fZ", True), ("%Y-%m-%dT%H:%M:%SZ", True),
            ("%Y-%m-%dT%H:%M:%S.%f", False), ("%Y-%m-%dT%H:%M:%S", False),
            ("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False),
            ("%d/%m/%Y %H:%M:%S", False), ("%d/%m/%Y %H:%M", False),
        ):
            try:
                t = datetime.strptime(s, fmt)
            except ValueError:
                continue
            t = t.replace(tzinfo=timezone.utc) if aware else t.replace(tzinfo=FUSEAU_MAC)
            return t.timestamp(), cle
        # Dernier recours : les ISO avec décalage explicite (+00:00).
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=FUSEAU_MAC)).timestamp(), cle
        except ValueError:
            pass
    return None, None


def h(x):
    return f"{x:.1f} h" if x >= 1 else f"{int(x * 60)} min"


def main():
    maintenant = time.time()
    lignes, motifs, ages = [], [], []

    for nom, seuil, effet in SURVEILLES:
        try:
            d = lire(nom)
        except urllib.error.HTTPError as e:
            motifs.append(f"{nom} : le site répond HTTP {e.code}")
            lignes.append(f"  ✗ {nom:32s} HTTP {e.code}")
            continue
        except Exception as e:
            motifs.append(f"{nom} : injoignable ({type(e).__name__})")
            lignes.append(f"  ✗ {nom:32s} injoignable ({type(e).__name__})")
            continue

        ts, cle = horodatage(d)
        if ts is None:
            # Pas un motif d'alarme : plusieurs caches n'exposent aucune date.
            lignes.append(f"  ? {nom:32s} aucun horodatage lisible")
            continue

        age = (maintenant - ts) / 3600.0
        ages.append(age)
        if age > seuil:
            motifs.append(f"{nom} : {h(age)} (seuil {seuil:g} h) — {effet}")
            lignes.append(f"  ✗ {nom:32s} {h(age):>8s}  > {seuil:g} h   [{cle}]")
        else:
            lignes.append(f"  ✓ {nom:32s} {h(age):>8s}  ≤ {seuil:g} h   [{cle}]")

    # ── Verdict 1 : la chaîne de publication est-elle arrêtée ? ────────────
    chaine_morte = False
    if ages:
        plus_frais = min(ages)
        if plus_frais > SEUIL_GLOBAL_H:
            chaine_morte = True
            motifs.insert(0, f"CHAÎNE DE PUBLICATION ARRÊTÉE : même le fichier le plus "
                             f"frais date de {h(plus_frais)} (seuil {SEUIL_GLOBAL_H:g} h). "
                             f"Le Mac ne publie plus.")
    else:
        chaine_morte = True
        motifs.insert(0, "Aucun horodatage exploitable sur tout le site — "
                         "publication cassée, ou site injoignable.")

    print(f"Sentinelle de fraîcheur — {datetime.now(timezone.utc):%F %H:%M UTC}")
    print(f"Site : {BASE}/{PREFIXE}/\n")
    print("\n".join(lignes))
    if ages:
        print(f"\n  fichier le plus frais : {h(min(ages))}   "
              f"le plus vieux : {h(max(ages))}")

    # ── Anti-bruit : n'alerter que sur la bascule, puis 1×/RAPPEL_H ────────
    try:
        with open(ETAT, encoding="utf-8") as f:
            etat = json.load(f)
    except Exception:
        etat = {}

    en_panne = bool(motifs)
    etait = bool(etat.get("en_panne"))
    depuis = etat.get("depuis") if (en_panne and etait) else (maintenant if en_panne else None)
    derniere_alerte = etat.get("derniere_alerte", 0)

    if en_panne:
        bascule = not etait
        rappel = (maintenant - derniere_alerte) / 3600.0 >= RAPPEL_H
        alerter = bascule or rappel
    else:
        alerter = etait   # retour à la normale : on le dit, sans faire échouer
        bascule = False

    nouvel_etat = {
        "en_panne": en_panne,
        "depuis": depuis,
        "derniere_alerte": maintenant if (en_panne and alerter) else derniere_alerte,
        "motifs": motifs,
        "verifie_le": datetime.now(timezone.utc).strftime("%F %H:%M UTC"),
    }
    os.makedirs(os.path.dirname(ETAT), exist_ok=True)
    with open(ETAT, "w", encoding="utf-8") as f:
        json.dump(nouvel_etat, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print()
    if not en_panne:
        if alerter:
            print("✅ RETOUR À LA NORMALE — le site est de nouveau à jour.")
        else:
            print("✅ Rien à signaler.")
        return 0

    print("╔" + "═" * 68)
    print("║ SITE PÉRIMÉ" + ("  (panne déjà signalée)" if not alerter else ""))
    for m in motifs:
        print(f"║  · {m}")
    if depuis:
        print(f"║ Depuis {h((maintenant - depuis) / 3600.0)}.")
    if chaine_morte:
        print("║ À FAIRE : réveiller le Mac. Lui seul collecte et déploie ;")
        print("║ tant qu'il dort, aucune de ces données ne peut bouger.")
    print("╚" + "═" * 68)

    if not alerter:
        # Panne connue, déjà annoncée : on ne renvoie pas un mail de plus.
        print("\n(sortie 0 volontaire : alarme déjà envoyée, prochain rappel dans "
              f"{h(RAPPEL_H - (maintenant - derniere_alerte) / 3600.0)})")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
