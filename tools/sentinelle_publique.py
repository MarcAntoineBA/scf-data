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
import re
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

# ── LE BANDEAU « DONNÉES DATÉES DE Xh » DE LA PAGE DES COMPARAISONS ─────────
# ⚠ CETTE SENTINELLE NE REGARDAIT AUCUN DES BLOCS QUE CE BANDEAU NOMME, sauf les
# indices. Le 17/09/2026 le visiteur lisait « Données datées de 9.2h (bloc
# narratifs) » et elle répondait « Rien à signaler ». Recopier la liste ici en
# ferait une troisième liste écrite à la main (la page, le filet du PC, celle-ci),
# et c'est l'écart entre de telles listes qui a produit chaque bandeau depuis le
# 04/09. On la LIT donc sur la page publiée — libellé, variable, tolérance —, et
# changer une tolérance dans la page suffit à la changer ici.
PAGE_BANDEAU = f"{BASE}/Comparaison_PER_Crypto_TradFi"
# La variable que lit la page → le fichier qu'elle charge pour la remplir.
FICHIER_DU_BLOC = {
    "__PER_DATA__":                "per_data_cache.js",
    "__GLOBAL_MARKETS__":          "global_markets_cache.js",
    "__TRADFI_FUNDAMENTALS__":     "tradfi_fundamentals_cache.js",
    "__NARRATIVES_FUNDAMENTALS__": "narratives_fundamentals_cache.js",
    "__PE_HIST_LIVE__":            "pe_hist_cache.js",
    "__CRYPTO_FICHES__":           "crypto_fiches.js",
    "__CRYPTO_CAPTURE__":          "crypto_capture_cache.js",
    "__CRYPTO_VESTING__":          "crypto_vesting_cache.js",
    "__SECTEURS_MONDIAUX__":       "secteurs_mondiaux.js",
}
# Repli si la page est injoignable ou si sa liste change de forme : une sentinelle
# aveugle parce que le HTML a bougé serait le pire des défauts.
BANDEAU_SECOURS = [
    ("comparateur", "__PER_DATA__", 8.0), ("indices", "__GLOBAL_MARKETS__", 12.0),
    ("fondamentaux", "__TRADFI_FUNDAMENTALS__", 11.0),
    ("narratifs", "__NARRATIVES_FUNDAMENTALS__", 11.0),
    ("historiques", "__PE_HIST_LIVE__", 22.0), ("fiches crypto", "__CRYPTO_FICHES__", 14.0),
    ("capture crypto", "__CRYPTO_CAPTURE__", 11.0), ("calendrier", "__CRYPTO_VESTING__", 28.0),
    ("secteurs", "__SECTEURS_MONDIAUX__", 28.0),
]

# ── LE RATTRAPAGE ───────────────────────────────────────────────────────────
# Constater ne suffit pas : le cron de la plateforme SAUTE des réveils (mesuré :
# le passage quotidien du 27/08 n'a jamais eu lieu), et chaque saut devenait un
# bandeau. Quand un bloc approche de sa tolérance, la sentinelle relance donc
# elle-même le workflow de collecte qui le produit — AVANT que le visiteur ne
# voie quoi que ce soit.
MARGE_RATTRAPAGE_H = 2.0   # on relance quand l'âge dépasse tolérance − marge
DELAI_RATTRAPAGE_H = 3.0   # jamais deux relances du même workflow en moins de ça
# Âge qu'atteint un fichier entre deux passages qui fonctionnent : la cadence,
# plus le temps d'un passage avant publication (1,2 h médian, mesuré sur le seau
# « 6h »), plus une heure de dérive du cron. Une tolérance plus courte déclenche
# le bandeau sur une chaîne saine — c'était « narratifs » à 8 h.
CADENCE_H = {"5min": 5 / 60, "10min": 10 / 60, "1h": 1.0, "6h": 6.0,
             "daily": 24.0, "weekly": 168.0}
PUBLICATION_H = 1.5
DERIVE_CRON_H = 1.0
DEPOT = os.environ.get("GITHUB_REPOSITORY", "MarcAntoineBA/scf-data")


def lire(nom):
    """(le JSON servi au visiteur, l'origine annoncée par le site), cache CDN contourné.

    Un `.js` de la forme `window.__X__={…};` est lu comme la page le lit : on
    saute jusqu'au premier objet. ⚠ Pas de repli sur le jumeau `.json` : certains
    sont des fossiles (17 jours de retard mesurés sur les fondamentaux narratifs),
    et la page ne les charge pas.
    """
    url = f"{BASE}/{PREFIXE}/{nom}?cb={int(time.time() * 1000)}"
    req = urllib.request.Request(url, headers={
        # Cloudflare renvoie 403 aux agents par défaut de urllib.
        "User-Agent": "sentinelle-fraicheur (surveillance du site)",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        texte = r.read().decode("utf-8")
        origine = (r.headers.get("x-scf-origin") or "").strip().lower()
    if nom.endswith(".js"):
        debut = min((i for i in (texte.find("{"), texte.find("[")) if i >= 0), default=-1)
        if debut < 0:
            raise ValueError("aucun objet dans le script")
        return json.JSONDecoder().raw_decode(texte[debut:])[0], origine
    return json.loads(texte), origine


def horodatage(d, origine=""):
    """(epoch_utc, clé lue) depuis les formes que les collecteurs produisent.

    Il y en a quatre, héritées de collecteurs écrits à des époques différentes :
    epoch entier, ISO avec Z, ISO sans fuseau, et « JJ/MM/AAAA HH:MM ».

    ⚠ UNE HEURE SANS FUSEAU N'EST PAS TOUJOURS UNE HEURE DE PARIS. C'est vrai
    d'un fichier écrit par le Mac, faux d'un fichier écrit par la collecte en
    ligne, dont `datetime.now()` rend l'heure UTC. Le site dit d'où vient ce
    qu'il sert (`x-scf-origin`) : « branche » et « piece-jointe » sortent d'un
    runner, « deploiement » du Mac. Les clés qui portent leur fuseau
    (`donnees_du`, `genere_le`) passent en tête et rendent la question caduque.
    """
    if not isinstance(d, dict):
        return None, None
    fuseau_naif = timezone.utc if origine in ("branche", "piece-jointe") else FUSEAU_MAC
    for cle in ("donnees_du", "genere_le", "generated_at", "ts_fetched", "updated", "last_update"):
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
            t = t.replace(tzinfo=timezone.utc) if aware else t.replace(tzinfo=fuseau_naif)
            return t.timestamp(), cle
        # Dernier recours : les ISO avec décalage explicite (+00:00).
        try:
            t = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=fuseau_naif)).timestamp(), cle
        except ValueError:
            pass
    return None, None


def blocs_du_bandeau():
    """[(libellé, variable, tolérance_h)] lus sur la page publiée, et d'où ils viennent."""
    try:
        req = urllib.request.Request(f"{PAGE_BANDEAU}?cb={int(time.time() * 1000)}", headers={
            "User-Agent": "sentinelle-fraicheur (surveillance du site)",
            "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", errors="replace")
        m = re.search(r"var sources = \[(.*?)\];", html, re.S)
        blocs = [(l, v, float(t)) for l, v, t in re.findall(
            r"\[\s*'([^']+)'\s*,\s*window\.(__[A-Z_]+__)[^\]]*?,\s*(\d+(?:\.\d+)?)\s*\]",
            m.group(1) if m else "")]
        if len(blocs) >= 5:
            return blocs, "page publiée"
    except Exception as e:
        print(f"  (liste du bandeau illisible sur la page : {type(e).__name__})")
    return BANDEAU_SECOURS, "copie de secours"


def workflows_par_fichier():
    """{fichier publié : {cadence}} d'après l'inventaire des collecteurs en ligne."""
    try:
        sys.path.insert(0, os.path.join(RACINE, "tools"))
        from run_jobs import bucket_of
        with open(os.path.join(RACINE, "jobs.json"), encoding="utf-8") as f:
            jobs = json.load(f)["jobs"]
    except Exception as e:
        print(f"  (inventaire des collecteurs illisible : {type(e).__name__})")
        return {}
    carte = {}
    for j in jobs:
        if j.get("category") == "locale":
            continue          # ne tourne que sur le Mac : rien à relancer d'ici
        for sortie in j.get("outputs") or []:
            carte.setdefault(sortie, set()).add(bucket_of(j))
    return carte


def github(methode, chemin, corps=None):
    """Appel à l'API GitHub avec le jeton du workflow ; None sans jeton."""
    jeton = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not jeton:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{DEPOT}{chemin}", method=methode,
        data=None if corps is None else json.dumps(corps).encode("utf-8"),
        headers={"Authorization": f"Bearer {jeton}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "sentinelle-fraicheur"})
    with urllib.request.urlopen(req, timeout=30) as r:
        brut = r.read()
        return json.loads(brut) if brut else {}


def rattraper(cadences, etat, maintenant, a_blanc=False):
    """Relance `collect-<cadence>.yml` pour chaque cadence en retard. → lignes du rapport.

    Trois gardes, chacune pour un emballement possible :
      · un passage de ce workflow déjà en file ou en cours → on attend, il rafraîchira ;
      · une relance de moins de DELAI_RATTRAPAGE_H → la source est peut-être en
        panne, la marteler ne la réparera pas (l'alarme, elle, continue) ;
      · pas de jeton → on le dit, on ne prétend pas avoir relancé.
    """
    lignes = []
    faits = etat.setdefault("rattrapages", {})
    for cadence in sorted(cadences):
        wf = f"collect-{cadence}.yml"
        if not os.path.exists(os.path.join(RACINE, ".github", "workflows", wf)):
            lignes.append(f"  ↻ {wf} : workflow absent, rien à relancer")
            continue
        depuis = (maintenant - faits.get(wf, 0)) / 3600.0
        if depuis < DELAI_RATTRAPAGE_H:
            lignes.append(f"  ↻ {wf} : déjà relancé il y a {h(depuis)}, on laisse travailler")
            continue
        if a_blanc:
            lignes.append(f"  ↻ {wf} : SERAIT relancé (mode à blanc)")
            continue
        try:
            runs = github("GET", f"/actions/workflows/{wf}/runs?per_page=10")
            if runs is None:
                lignes.append(f"  ↻ {wf} : pas de jeton GitHub, relance impossible d'ici")
                continue
            actifs = [r for r in runs.get("workflow_runs", [])
                      if r.get("status") in ("queued", "in_progress", "waiting", "requested", "pending")]
            if actifs:
                lignes.append(f"  ↻ {wf} : un passage est déjà {actifs[0].get('status')}, il rafraîchira")
                continue
            github("POST", f"/actions/workflows/{wf}/dispatches", {"ref": "main"})
            faits[wf] = maintenant
            lignes.append(f"  ↻ {wf} : RELANCÉ")
        except Exception as e:
            lignes.append(f"  ↻ {wf} : relance refusée ({type(e).__name__}: {str(e)[:80]})")
    return lignes


def h(x):
    return f"{x:.1f} h" if x >= 1 else f"{int(x * 60)} min"


def main():
    maintenant = time.time()
    a_blanc = "--a-blanc" in sys.argv
    lignes, motifs, ages = [], [], []
    # L'état est lu D'ABORD : le rattrapage a besoin de savoir quand il a relancé.
    try:
        with open(ETAT, encoding="utf-8") as f:
            etat = json.load(f)
    except Exception:
        etat = {}

    for nom, seuil, effet in SURVEILLES:
        try:
            d, origine = lire(nom)
        except urllib.error.HTTPError as e:
            motifs.append(f"{nom} : le site répond HTTP {e.code}")
            lignes.append(f"  ✗ {nom:32s} HTTP {e.code}")
            continue
        except Exception as e:
            motifs.append(f"{nom} : injoignable ({type(e).__name__})")
            lignes.append(f"  ✗ {nom:32s} injoignable ({type(e).__name__})")
            continue

        ts, cle = horodatage(d, origine)
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

    # ── Le bandeau de la page des comparaisons, et son rattrapage ───────────
    blocs, provenance = blocs_du_bandeau()
    cadences_de = workflows_par_fichier()
    lignes.append(f"\n  Bandeau « Données datées de Xh » ({len(blocs)} blocs, liste lue sur la {provenance}) :")
    a_relancer, incoherences = set(), []
    for libelle, variable, tolerance in blocs:
        nom = FICHIER_DU_BLOC.get(variable)
        if not nom:
            lignes.append(f"  ? {libelle:32s} variable {variable} sans fichier connu")
            continue
        try:
            d, origine = lire(nom)
        except Exception as e:
            motifs.append(f"bloc « {libelle} » ({nom}) : illisible ({type(e).__name__})")
            lignes.append(f"  ✗ {libelle:32s} {nom} illisible ({type(e).__name__})")
            continue
        ts, cle = horodatage(d, origine)
        if ts is None:
            lignes.append(f"  ? {libelle:32s} {nom} : aucun horodatage lisible")
            continue
        age = (maintenant - ts) / 3600.0
        ages.append(age)
        cadences = cadences_de.get(nom, set())
        for c in cadences:
            attendu = CADENCE_H.get(c, 0) + PUBLICATION_H + DERIVE_CRON_H
            if tolerance < attendu:
                incoherences.append(f"  ⚠ « {libelle} » tolère {tolerance:g} h pour une collecte « {c} » : "
                                    f"une chaîne saine atteint {attendu:g} h — le bandeau criera à tort")
        marque = "✓"
        if age > tolerance:
            marque = "✗"
            motifs.append(f"bloc « {libelle} » ({nom}) : {h(age)} (tolérance {tolerance:g} h) — "
                          f"bandeau « Données datées de {age:.1f}h » sur la page")
        if age > tolerance - MARGE_RATTRAPAGE_H and cadences:
            a_relancer |= cadences
            if marque == "✓":
                marque = "↻"
        lignes.append(f"  {marque} {libelle:32s} {h(age):>8s}  / {tolerance:g} h   "
                      f"[{cle}, {origine or 'origine ?'}]{' ← ' + ','.join(sorted(cadences)) if cadences else ''}")
    lignes.extend(incoherences)
    if a_relancer:
        lignes.append("  Rattrapage :")
        lignes.extend(rattraper(a_relancer, etat, maintenant, a_blanc))

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
        "rattrapages": etat.get("rattrapages", {}),
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
