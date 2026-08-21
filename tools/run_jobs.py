#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_jobs.py — Exécute les collecteurs dus pour une cadence donnée. Remplace launchd.

CE QUI CHANGE PAR RAPPORT À launchd, ET POURQUOI
launchd déclenchait un job par créneau, sur une machine qui pouvait dormir : un créneau
manqué n'était jamais rejoué, et rien ne le signalait. Ici, un workflow par cadence
appelle ce script, qui lance TOUS les collecteurs de la cadence. Un runner ne dort pas ;
s'il échoue, la tentative suivante arrive à l'heure dite.

TROIS PRINCIPES
1. Un collecteur qui échoue n'empêche jamais les autres de publier. Chacun est isolé,
   borné dans le temps, et son échec est une ligne du bilan — pas l'arrêt du lot.
2. On ne publie que ce qui a VRAIMENT changé, comparé par contenu. Un horodatage frais
   sur une donnée identique est un mensonge, et c'est exactement ce que faisait l'ancienne
   chaîne quand elle republiait un dépôt à moitié synchronisé.
3. Le bilan (`cache/_fleet_status.json`) est écrit à CHAQUE passage, succès ou échec.
   Une panne muette est pire qu'une panne visible : c'est ce qui a laissé un collecteur
   mort pendant 109 heures sans que personne ne le voie.
"""

import argparse
import concurrent.futures as cf
import email.utils
import filecmp
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import index_fraicheur

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
CACHE_OUT = os.path.join(ROOT, "cache")

# Seuil de séparation entre les deux régimes de publication. 12 fichiers pèsent
# 93 des 114 Mo du parc : versionner ceux-là à chaque passage ferait grossir le dépôt
# sans fin, pour une donnée dont personne ne relira jamais la version d'avant-hier.
# En dessous, l'historique git est au contraire précieux — on voit quelle valeur a
# changé, et quand.
GIT_SIZE_LIMIT = 1_000_000
RELEASE_OUT = os.path.join(ROOT, "release")
MANIFEST = os.path.join(ROOT, "cache_manifest.txt")

# ── QUAND CHAQUE CACHE A ÉTÉ ÉCRIT POUR LA DERNIÈRE FOIS ─────────────────────
# POURQUOI CE REGISTRE EXISTE — LA PANNE DU 2026-08-04, VUE LE 2026-08-20
# Un clone est neuf : tous ses fichiers portent la date du clone. Or la moitié des
# collecteurs décident de travailler en regardant l'âge de leur cache précédent
# (« moins de 4 h : rien à faire »). Depuis qu'on leur restitue ce cache — ce qui
# leur rendait leur base de fusion, et c'était juste — ils le reçoivent daté de
# MAINTENANT : la garde se referme toujours, le collecteur sort en SUCCÈS sans
# avoir rien écrit, et le fichier publié se fige. Mesuré : 17 caches gelés pendant
# seize jours, dont la valorisation L1 et les news, avec un bilan à 26/26 OK.
#
# Une date d'écriture ne peut venir ni du clone (superficiel : `git log` n'a qu'un
# commit), ni du contenu (la moitié du parc n'y met pas de date lisible). On la
# TRANSPORTE donc : chaque passage note ce qu'il a réellement écrit, le registre
# voyage avec les données, et le passage suivant rend aux caches restitués leur âge
# véritable. Un fichier inconnu du registre garde la date de sa copie — un nouveau
# cache est réellement neuf.
#
# Un fichier par cadence, pour la même raison que les bilans : sept cadences qui
# réécriraient le même fichier s'annuleraient l'une l'autre au moment de publier.
ECRITS_PREFIXE = "_ecrits_"

# Durée nominale de chaque cadence, en secondes. Sert à juger si un collecteur qui
# n'a rien écrit est en panne ou simplement en avance : dans une cadence de 5 min
# rejouée trois fois dans la même exécution, un collecteur qui a écrit il y a
# quatre minutes et dont la source n'a rien publié depuis est parfaitement sain.
# Ne signaler que ce qui dépasse DEUX tours : une alerte permanente n'est plus une
# alerte, c'est un décor — et c'est exactement ce qui a laissé passer seize jours.
PERIODES = {"5min": 300, "10min": 600, "1h": 3600,
            "6h": 21600, "daily": 86400, "weekly": 604800}
JOBS = os.path.join(ROOT, "jobs.json")

# Les pièces jointes de la release, telles que le site les lit lui aussi. `release/`
# est ignoré par git : sur un runner fraîchement cloné, ce dossier est TOUJOURS vide.
# Sans cette adresse, prepare_env() ne pouvait restaurer que les petits fichiers
# versionnés, et tout collecteur dont la base de fusion pèse plus d'un mégaoctet
# repartait de zéro à chaque exécution — en perdant l'historique qu'il accumule.
RELEASE_URL = "https://github.com/{}/releases/download/data/".format(
    os.environ.get("GITHUB_REPOSITORY") or "MarcAntoineBA/scf-data")

# Là où les collecteurs écrivent, tel qu'ils l'ont toujours fait. Sous Linux, ces
# chemins n'ont rien de spécial : ce sont de simples dossiers qu'on crée.
CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE_DIR = os.path.expanduser("~/Desktop/Site_Crypto_Finance")

# Cadences. Un job tombe dans le PREMIER seau dont le seuil est atteint.
# Le plafond est dimensionné sur le collecteur le PLUS LENT du seau, avec une marge.
# Mesuré : tradfi met ~50 min, et le plafond valait exactement 50 — il l'aurait tué
# pile à la limite, un jour sur deux, en laissant croire à une panne de source.
# C'est la même erreur que celle qui avait fait perdre des heures de réparation au
# gardien de fraîcheur : un plafond sans marge ne protège pas, il sabote.
#
# NB : ce plafond REMPLACE l'enveloppe anti-zombie qui entourait tradfi sur la machine
# d'origine. Elle apportait deux choses — empêcher la veille (sans objet ici) et borner
# la durée (assuré ici). Rien n'est perdu.
BUCKETS = [
    ("5min", 288, 4),      # cotations en séance + guetteurs de publication (rafale)
    ("10min", 96, 8),      # publications guettées (résultats, macro, news)
    ("1h", 12, 45),        # marchés et dérivés
    ("6h", 2, 75),         # lourd, ou source qui ne publie pas plus vite
    ("daily", 0.9, 70),
    ("weekly", 0.0, 70),
]
PARALLEL = 6

# Un collecteur peut lire la SORTIE d'un autre. Lancés en parallèle dans la même
# cadence, l'ordre n'est pas garanti : mesuré, l'historique tradfi mourait sur
# « fichier introuvable » parce qu'il démarrait avant le collecteur qui le produit.
# Deux vagues suffisent — ce qui est attendu par quelqu'un d'abord, le reste ensuite.
# Une file de dépendances complète serait disproportionnée pour un seul cas connu, et
# masquerait le vrai sujet : ces deux-là ne devraient pas partager une cadence.
DEPENDANCES = {
    "tradfihist": ["tradfifund"],       # lit tradfi_fundamentals_cache.json
    "appariepredictifs": ["kalshi"],    # lit kalshi_cache.json et ses règles
}

# ── RAFALE INTERNE ────────────────────────────────────────────────────────────
# Le planning de la plateforme ne descend pas sous 5 minutes. Or pour un chiffre macro
# ou une dépêche, l'intérêt est à l'instant de la publication : cinq minutes de retard
# sur un CPI, c'est le mouvement déjà passé. La machine d'origine le savait — son
# calendrier macro interrogeait la source toutes les 60 s autour d'une publication.
#
# On reproduit ce comportement là où il compte : ces collecteurs sont rejoués plusieurs
# fois DANS la même exécution, espacés, jusqu'à une échéance qui reste sous la cadence.
# Le délai réel tombe ainsi à ~80 s au lieu de 5 min, sans monopoliser un serveur.
#
# Volontairement limité à quatre collecteurs : appliquer ça partout occuperait un
# runner en continu pour des sources qui ne publient pas plus vite, et transformerait
# un usage légitime en gaspillage.
REPETITIONS = {
    "macrocal": 80,      # secondes entre deux passages
    "fjnews": 80,
    "earningscal": 80,
}
# POURQUOI « news » N'EST PLUS DANS CETTE LISTE (21/08/2026)
# La rafale n'a jamais rien apporte a ce collecteur : sa garde de fraicheur se
# refermait derriere le premier passage, les deux suivants sortaient aussitot — mais
# la boucle, elle, dormait quand meme 80 s entre chacun. Deux minutes et demie de
# serveur immobilise a chaque execution, toutes les cinq minutes, pour zero depeche
# supplementaire. Le besoin reel est de quelques rafraichissements par heure, pas
# d'un delai de 80 s : c'est la garde du collecteur qui le regle desormais, et un
# seul passage par execution suffit a l'ouvrir.
# Échéance volontairement basse : au-delà, le temps de mise en place du serveur
# (~60 s) ferait déborder la fenêtre de 5 min et deux exécutions se mettraient en
# file d'attente au lieu de se suivre.
RAFALE_ECHEANCE = 175


def bucket_of(job):
    """Cadence DÉCLARÉE dans l'inventaire, classée selon la vitesse de la source.

    L'ancienne version déduisait la cadence du planning de la machine d'origine — donc
    d'un arbitrage batterie/veille/processeur qui n'a plus lieu d'être. Un job non
    classé retombe sur cette déduction, mais il est signalé : le silence produirait
    exactement le défaut qu'on corrige ici, une donnée rafraîchie trop lentement sans
    que personne ne sache pourquoi.
    """
    declaree = job.get("cadence")
    if declaree:
        return declaree
    per_day = job.get("per_day", 0)
    for name, threshold, _ in BUCKETS:
        if per_day >= threshold:
            return name
    return "weekly"


def timeout_of(bucket):
    return next(t for n, _, t in BUCKETS if n == bucket) * 60


def rapatrier_pieces_jointes(noms):
    """Redescend dans `release/` les gros caches précédents que git ne versionne pas.

    POURQUOI CETTE FONCTION EXISTE
    `release/` est ignoré par git. Sur un runner, le clone ne le contient donc jamais,
    et la restauration d'en dessous n'avait plus rien à restaurer pour les fichiers
    lourds. Un collecteur qui relit son cache précédent pour préserver ce qu'il n'a pas
    pu récupérer cette fois-ci repartait alors d'une page blanche, à chaque exécution.
    Constaté sur l'historique fondamental TradFi : 138 valeurs publiées au lieu de 781,
    zéro ligne préservée, 162 échecs perdus — et aucune reconstitution possible d'un
    passage à l'autre, puisque chacun recommençait de zéro. Les graphes du site
    perdaient les quatre cinquièmes de leur profondeur sans qu'aucune erreur ne le dise.

    On ne rapatrie QUE ce que les collecteurs de cette cadence vont réécrire : tirer les
    93 Mo du parc à chaque passage de cinq minutes coûterait plus que ça ne rapporte.
    Un fichier absent de la release (jamais produit) répond 404 : on passe, sans bruit.
    """
    os.makedirs(RELEASE_OUT, exist_ok=True)
    repris = 0
    for name in sorted(noms):
        if os.path.exists(os.path.join(CACHE_DIR, name)):
            continue          # déjà là (machine d'origine, ou petit fichier versionné)
        try:
            with urllib.request.urlopen(RELEASE_URL + name, timeout=120) as r:
                contenu = r.read()
                # La date d'envoi de la pièce jointe est la seule trace de son âge
                # réel : sans elle, le fichier redescend daté de maintenant et fait
                # croire à son collecteur qu'il vient de travailler.
                envoi = r.headers.get("Last-Modified")
        except Exception:
            continue          # 404 ou réseau muet : le collecteur repartira de zéro
        if not contenu:
            continue          # un fichier vide serait pire qu'absent : il ferait base
        destination = os.path.join(RELEASE_OUT, name)
        with open(destination, "wb") as f:
            f.write(contenu)
        if envoi:
            try:
                quand = email.utils.parsedate_to_datetime(envoi).timestamp()
                os.utime(destination, (quand, quand))
            except (TypeError, ValueError, OverflowError, OSError):
                pass          # en-tête illisible : on garde la date de téléchargement
        repris += 1
    return repris


def lire_ecrits():
    """Fusionne les registres de toutes les cadences : { nom : epoch }.

    On prend la date la PLUS RÉCENTE quand deux cadences ont écrit le même fichier :
    c'est l'âge que le collecteur aurait constaté sur la machine d'origine, où les
    deux écritures tombent dans le même dossier.
    """
    fusion = {}
    for chemin in sorted(glob.glob(os.path.join(CACHE_OUT, ECRITS_PREFIXE + "*.json"))):
        try:
            with open(chemin, encoding="utf-8") as f:
                lot = json.load(f)
        except (OSError, ValueError):
            continue          # registre illisible : on s'en passe, jamais on ne casse
        if not isinstance(lot, dict):
            continue
        for nom, quand in lot.items():
            if isinstance(quand, (int, float)) and quand > 0:
                fusion[nom] = max(fusion.get(nom, 0), float(quand))
    return fusion


def ecrire_ecrits(bucket, noms, quand):
    """Note ce que CE passage a écrit, sans effacer ce qu'il n'a pas mesuré."""
    chemin = os.path.join(CACHE_OUT, f"{ECRITS_PREFIXE}{bucket}.json")
    registre = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            charge = json.load(f)
        if isinstance(charge, dict):
            registre = {k: v for k, v in charge.items()
                        if isinstance(v, (int, float))}
    except (OSError, ValueError):
        registre = {}
    for nom in noms:
        registre[nom] = round(quand)
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(registre, f, indent=0, sort_keys=True)
    return os.path.basename(chemin)


def age_des_sorties(noms):
    """Âge, en secondes, de la sortie la PLUS RÉCENTE. None si aucune n'existe."""
    ages = []
    for nom in noms:
        try:
            ages.append(time.time() - os.path.getmtime(os.path.join(CACHE_DIR, nom)))
        except OSError:
            continue
    return min(ages) if ages else None


def collecteurs_muets(jobs, aboutis, ecrits, seuil_gel):
    """Les collecteurs sortis en succès dont le cache se fige, avec son âge.

    Trois cas ne sont PAS des pannes et ne doivent pas encombrer le bilan :
    un collecteur qui a écrit ; un collecteur en échec (il est déjà nommé
    ailleurs) ; un collecteur dont la sortie est récente — sa garde de fraîcheur a
    joué son rôle, la source n'a rien publié depuis. Reste ce qui compte : un
    succès, aucune écriture, et une donnée plus vieille que deux tours de cadence.
    """
    muets = []
    for j in jobs:
        if j["id"] not in aboutis or not (j.get("outputs") or []):
            continue
        if set(j["outputs"]) & ecrits:
            continue
        age = age_des_sorties(j["outputs"])
        if age is None:
            continue                      # rien sur le disque : c'est « absents »
        if age > seuil_gel:
            muets.append(f"{j['id']} ({age / 3600:.1f} h)")
    return muets


def ecrits_depuis(instant, noms):
    """Les fichiers de `noms` que quelqu'un a RÉELLEMENT écrits depuis `instant`.

    C'est la seule question qui distingue un collecteur qui a travaillé d'un
    collecteur qui a répondu. Un fichier réécrit à l'identique compte comme écrit :
    la donnée est vérifiée fraîche, elle n'a simplement pas changé.
    """
    faits = set()
    for nom in noms:
        chemin = os.path.join(CACHE_DIR, nom)
        try:
            if os.path.getmtime(chemin) >= instant - 1:
                faits.add(nom)
        except OSError:
            continue
    return faits


def prepare_env(attendus=()):
    """Recrée l'arborescence que les collecteurs attendent."""
    for d in (CACHE_DIR, SITE_DIR, CACHE_OUT):
        os.makedirs(d, exist_ok=True)

    # Les enveloppes shell se placent d'abord dans le dossier où elles vivent sur la
    # machine d'origine. Ce chemin n'existe pas ici : quatre collecteurs mouraient sur
    # « cd: … No such file or directory » avant d'avoir rien fait. On y pointe le
    # dossier des scripts, et leur `cd` retombe sur leurs voisins comme prévu.
    maison = os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")
    if not os.path.exists(maison):
        os.makedirs(os.path.dirname(maison), exist_ok=True)
        try:
            os.symlink(SCRIPTS, maison)
        except OSError:
            shutil.copytree(SCRIPTS, maison)   # si les liens sont refusés
    # Les gros caches d'abord : ils ne sont pas dans le clone, il faut aller les chercher
    # à la source. Sinon la boucle suivante ne trouve dans `release/` que du vide.
    repris = rapatrier_pieces_jointes(attendus)

    # Les collecteurs relisent souvent leur propre cache précédent (fusion, historique,
    # préservation en cas d'échec partiel). Sans cette copie, chaque exécution repartirait
    # de zéro et perdrait l'historique accumulé — et un collecteur dont la source est
    # momentanément muette écraserait ses données au lieu de les conserver.
    # ── RENDRE LEUR ÂGE AUX FICHIERS DU DÉPÔT, AVANT TOUTE COPIE ─────────────
    # Un clone donne à tout le monde la date du clone. Deux mécanismes s'y trompent,
    # pas un seul : la garde de fraîcheur des collecteurs (via la copie qu'on leur
    # rend) ET l'index qui dit au site quelle origine est la plus fraîche — celui-ci
    # lit les fichiers du DÉPÔT et remesure tout fichier dont la date de fichier
    # dépasse celle qu'il avait notée, ce qui est vrai de tous après un clone. C'est
    # pourquoi le premier correctif n'avait pas suffi : l1_valuation_cache.js restait
    # daté « il y a 4 minutes » avec un contenu du 4 août.
    # On date donc la SOURCE ; les copies suivantes héritent (copy2 conserve la date).
    ecrits = lire_ecrits()
    redates = 0
    for dossier in (CACHE_OUT, RELEASE_OUT):
        if not os.path.isdir(dossier):
            continue
        for name in os.listdir(dossier):
            quand = ecrits.get(name)
            chemin = os.path.join(dossier, name)
            if not quand or not os.path.isfile(chemin):
                continue
            try:
                if abs(os.path.getmtime(chemin) - quand) > 2:
                    os.utime(chemin, (quand, quand))
                    redates += 1
            except OSError:
                pass

    restored = 0
    for source in (CACHE_OUT, RELEASE_OUT):
        if not os.path.isdir(source):
            continue
        for name in os.listdir(source):
            src = os.path.join(source, name)
            if not os.path.isfile(src):
                continue
            if not os.path.exists(os.path.join(CACHE_DIR, name)):
                shutil.copy2(src, os.path.join(CACHE_DIR, name))
                restored += 1
            # Sur la machine d'origine, plusieurs collecteurs relisent leur cache
            # précédent À CÔTÉ D'EUX, pas dans le dossier des caches (les deux copies
            # y cohabitent depuis toujours). On reproduit cette disposition, sinon ces
            # collecteurs repartiraient de zéro à chaque exécution — en perdant
            # l'historique qu'ils accumulent, sans que rien ne le signale.
            jumeau = os.path.join(SCRIPTS, name)
            if not os.path.exists(jumeau):
                shutil.copy2(src, jumeau)
    return restored, repris, redates


def _sans_chemin_perso(msg):
    """Retire le dossier personnel des messages d'erreur avant publication.

    Le bilan est publié dans un dépôt PUBLIC, et un message d'échec cite volontiers
    le chemin complet du fichier fautif — donc le nom du compte de la machine. C'est
    arrivé au premier essai réel : « univers introuvable (/Users/<compte>/…) ».
    Le message reste lisible, il perd juste ce qu'il n'avait pas à dire.
    """
    return (msg or "").replace(os.path.expanduser("~"), "~")


def _executer(cmd, timeout):
    """Un passage. Renvoie le même dictionnaire que run_one, sans l'identifiant."""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=SCRIPTS, capture_output=True, text=True,
                           timeout=timeout, env=os.environ.copy())
        ok = p.returncode == 0
        why = "" if ok else (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
        return dict(job="", ok=ok, secs=round(time.time() - t0, 1), code=p.returncode,
                    why="" if ok else _sans_chemin_perso(str(why[0]))[:200])
    except subprocess.TimeoutExpired:
        return dict(job="", ok=False, secs=round(time.time() - t0, 1), code=None,
                    why=f"dépassement du plafond ({timeout//60} min)")
    except Exception as e:
        return dict(job="", ok=False, secs=round(time.time() - t0, 1), code=None,
                    why=_sans_chemin_perso(f"{type(e).__name__}: {e}")[:200])


def run_one(job, timeout):
    script = os.path.join(SCRIPTS, job["script"])
    if not os.path.exists(script):
        return dict(job=job["id"], ok=False, secs=0, code=None, why="script absent")

    # Les arguments de lancement font partie de la commande, au même titre que le nom
    # du script : `--force` traverse une garde de fraîcheur qui, sans lui, fait sortir
    # le collecteur sans rien collecter ; `--resume` lui fait relire son cache précédent
    # au lieu de repartir d'une page blanche. Les ignorer revenait à lancer une AUTRE
    # commande que celle qui tourne sur la machine d'origine, sans que rien ne le dise.
    cmd = (["bash", script] if script.endswith(".sh")
           else [sys.executable, script]) + list(job.get("args") or [])
    t0 = time.time()

    # Guetteurs de publication : on repasse plusieurs fois dans la même exécution.
    # Le dernier passage fait foi ; les précédents servent à attraper un chiffre qui
    # tombe entre deux cadences. On s'arrête à l'échéance, jamais au milieu d'un passage.
    espacement = REPETITIONS.get(job["id"])
    if espacement:
        # Chaque passage est borné à l'espacement : un passage qui traîne mangerait
        # la rafale entière et on n'aurait gagné qu'un appel sur trois.
        plafond_passage = min(timeout, espacement + 40)
        dernier, passages = None, 0
        while True:
            dernier = _executer(cmd, plafond_passage)
            passages += 1
            reste = RAFALE_ECHEANCE - (time.time() - t0)
            if reste < espacement + 20:
                break
            time.sleep(espacement)
        dernier["secs"] = round(time.time() - t0, 1)
        dernier["job"] = job["id"]
        dernier["passages"] = passages
        return dernier

    try:
        p = subprocess.run(cmd, cwd=SCRIPTS, capture_output=True, text=True,
                           timeout=timeout, env=os.environ.copy())
        secs = round(time.time() - t0, 1)
        ok = p.returncode == 0
        why = "" if ok else (p.stderr or p.stdout or "").strip().splitlines()[-1:] or [""]
        return dict(job=job["id"], ok=ok, secs=secs, code=p.returncode,
                    why="" if ok else _sans_chemin_perso(str(why[0]))[:200])
    except subprocess.TimeoutExpired:
        return dict(job=job["id"], ok=False, secs=round(time.time() - t0, 1),
                    code=None, why=f"dépassement du plafond ({timeout//60} min)")
    except Exception as e:
        return dict(job=job["id"], ok=False, secs=round(time.time() - t0, 1),
                    code=None, why=_sans_chemin_perso(f"{type(e).__name__}: {e}")[:200])


def collect(manifest):
    """Range les fichiers du manifeste qui ont RÉELLEMENT changé, selon leur poids.

    La comparaison se fait par CONTENU, jamais par date : un collecteur réécrit
    souvent un fichier identique (données inchangées depuis la veille), et se fier
    à la date de modification produirait une publication à chaque passage — du bruit
    qui noie les vrais changements dans l'historique.

    Petits fichiers → `cache/`, versionnés par git.
    Gros fichiers   → `release/`, publiés en pièces jointes remplacées sur place.
    Le comparant reste le même dans les deux cas : la copie précédente, où qu'elle soit.
    """
    os.makedirs(RELEASE_OUT, exist_ok=True)
    small, big, absent, retires, fondus = [], [], [], [], []
    for name in manifest:
        src = os.path.join(CACHE_DIR, name)
        if not os.path.exists(src):
            absent.append(name)
            continue
        taille = os.path.getsize(src)
        heavy = taille >= GIT_SIZE_LIMIT
        dst = os.path.join(RELEASE_OUT if heavy else CACHE_OUT, name)

        # Un fichier qui FOND est le signe qu'un collecteur a perdu sa base de fusion :
        # il republie ce qu'il a réussi à récupérer aujourd'hui, sans ce qu'il avait
        # accumulé avant. C'est passé inaperçu pendant des semaines sur l'historique
        # fondamental TradFi — 2,17 Mo devenus 741 Ko, 781 valeurs devenues 138, et
        # rien pour le dire : la publication réussissait, le collecteur sortait en
        # succès, seule la profondeur des graphes du site avait disparu. Un tiers de
        # perte n'arrive pas par le jeu normal de la donnée ; on le nomme.
        ancien = next((p for p in (os.path.join(CACHE_OUT, name),
                                   os.path.join(RELEASE_OUT, name)) if os.path.exists(p)), None)
        if ancien:
            avant = os.path.getsize(ancien)
            if avant > 50_000 and taille < avant * 0.66:
                fondus.append(f"{name} ({avant//1024} Ko → {taille//1024} Ko)")

        # Un fichier peut changer de camp (il grossit avec l'historique qu'il accumule) :
        # on nettoie l'ancienne place, sinon le site continuerait de lire une copie
        # figée pendant que la nouvelle est publiée ailleurs.
        stale = os.path.join(CACHE_OUT if heavy else RELEASE_OUT, name)
        if os.path.exists(stale):
            os.remove(stale)
            if heavy:
                retires.append(name)     # la copie versionnée doit disparaître aussi

        if os.path.exists(dst) and filecmp.cmp(src, dst, shallow=False):
            continue
        shutil.copy2(src, dst)
        (big if heavy else small).append(name)
    return small, big, absent, retires, fondus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, choices=[b[0] for b in BUCKETS])
    ap.add_argument("--only", help="un seul job, pour tester (étiquette exacte)")
    ap.add_argument("--list", action="store_true", help="affiche la répartition et sort")
    args = ap.parse_args()

    jobs = [j for j in json.load(open(JOBS))["jobs"] if j["category"] == "public"]

    if args.list:
        for name, _, mins in BUCKETS:
            sel = [j for j in jobs if bucket_of(j) == name]
            print(f"{name:7} {len(sel):3} collecteurs  (plafond {mins} min)")
            for j in sorted(sel, key=lambda j: -j["per_day"]):
                print(f"          {j['per_day']:6.1f}/j  {j['id']:26} {j['script']}")
        return 0

    due = [j for j in jobs if bucket_of(j) == args.bucket]
    orphelins = [j["id"] for j in jobs if not j.get("cadence")]
    if orphelins:
        print(f"  ! {len(orphelins)} collecteur(s) sans cadence déclarée : "
              + ", ".join(orphelins[:6]))
    if args.only:
        due = [j for j in jobs if j["id"] == args.only]
        if not due:
            print(f"Aucun job « {args.only} »", file=sys.stderr)
            return 1

    manifest = [l.strip() for l in open(MANIFEST) if l.strip()] if os.path.exists(MANIFEST) else []
    # Ce que les collecteurs de cette cadence vont réécrire : ce sont exactement les
    # fichiers dont ils relisent la version précédente pour la fusionner.
    attendus = {n for j in due for n in j.get("outputs", [])} & set(manifest)
    restored, repris, redates = prepare_env(attendus)
    timeout = timeout_of(args.bucket)

    print(f"cadence « {args.bucket} » · {len(due)} collecteurs · plafond {timeout//60} min "
          f"· {restored} cache(s) restauré(s) dont {repris} pièce(s) jointe(s), "
          f"{redates} rendu(s) à leur âge réel\n")

    # Deux vagues : d'abord ce dont un autre collecteur dépend, ensuite le reste.
    attendus = {d for deps in DEPENDANCES.values() for d in deps}
    vague1 = [j for j in due if j["id"] in attendus]
    vague2 = [j for j in due if j["id"] not in attendus]

    t0 = time.time()
    results = []
    for vague in (vague1, vague2):
        if not vague:
            continue
        with cf.ThreadPoolExecutor(max_workers=PARALLEL) as ex:
            results += list(ex.map(lambda j: run_one(j, timeout), vague))
    elapsed = round(time.time() - t0, 1)

    small, big, absent, retires, fondus = collect(manifest)
    changed = small + big

    # ── QUI A TRAVAILLÉ, QUI S'EST CONTENTÉ DE RÉPONDRE ───────────────────────
    # « Succès » veut dire « sorti sans erreur », pas « a collecté ». Un collecteur
    # dont la garde de fraîcheur se referme sort en succès sans rien écrire — et
    # c'est ainsi que 17 caches sont restés figés seize jours derrière un bilan à
    # 26/26 OK. On mesure donc l'écriture elle-même, et on NOMME les muets.
    surveilles = {n for j in due for n in j.get("outputs", [])} | set(manifest)
    ecrits = ecrits_depuis(t0, surveilles)
    aboutis = {r["job"] for r in results if r["ok"]}
    # Deux tours de cadence, avec un plancher de deux heures : plusieurs collecteurs
    # portent une garde interne PLUS LONGUE que la cadence qui les appelle (les news
    # sont réveillées toutes les 5 min mais ne se réécrivent qu'à l'heure). Sans ce
    # plancher, la sonde les nommerait 55 minutes sur 60 — et une alerte permanente
    # n'alerte plus personne. Un gel de seize jours, lui, reste vu au premier passage.
    muets = collecteurs_muets(due, aboutis, ecrits,
                              max(2 * PERIODES.get(args.bucket, 3600), 7200))

    ko = [r for r in results if not r["ok"]]
    for r in sorted(results, key=lambda r: (r["ok"], -r["secs"])):
        mark = "✓" if r["ok"] else "✗"
        print(f"{mark} {r['secs']:6.1f}s  {r['job']:28} {r['why']}")

    print(f"\n{len(results)-len(ko)}/{len(results)} collecteurs OK en {elapsed}s "
          f"· {len(changed)} fichier(s) modifié(s) : {len(small)} versionné(s), "
          f"{len(big)} en pièce jointe")
    if changed:
        print("  " + ", ".join(changed[:12]) + (" …" if len(changed) > 12 else ""))
    if absent:
        # Un fichier attendu par le site que personne ne produit : ni erreur bruyante
        # ni silence — la page servirait une donnée figée sans que rien ne l'indique.
        print(f"  {len(absent)} fichier(s) du manifeste jamais produit(s) : "
              + ", ".join(absent[:8]) + (" …" if len(absent) > 8 else ""))
    if fondus:
        print(f"  ! {len(fondus)} fichier(s) ont FONDU d'un tiers ou plus — base de "
              f"fusion probablement perdue : " + ", ".join(fondus[:6])
              + (" …" if len(fondus) > 6 else ""))
    if muets:
        # Sorti en succès, n'a rien écrit. Ni erreur ni collecte : exactement la
        # forme que prend une panne quand personne ne la regarde.
        print(f"  ! {len(muets)} collecteur(s) MUETS — succès sans écriture depuis "
              f"plus de deux tours, leur cache publié se fige : " + ", ".join(muets[:8])
              + (" …" if len(muets) > 8 else ""))

    # Bilan cumulatif : on garde l'état des cadences qui n'ont pas tourné cette fois-ci,
    # sinon chaque passage effacerait la vue d'ensemble du parc.
    # Un fichier par cadence : c'était le SEUL fichier écrit par les sept, donc le
    # seul point de conflit quand plusieurs publient en même temps. Séparer supprime
    # le conflit à la racine, au lieu de le rattraper au rebase.
    status_path = os.path.join(CACHE_OUT, f"_fleet_status_{args.bucket}.json")
    status = dict(bucket=args.bucket)
    status["run"] = dict(
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        total=len(results), ok=len(results) - len(ko), secs=elapsed,
        changed=len(changed), versionnes=len(small), pieces_jointes=len(big),
        absents=len(absent), muets=muets,
        failed=[dict(job=r["job"], why=r["why"]) for r in ko])
    status["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(status_path, "w") as f:
        json.dump(status, f, indent=1, ensure_ascii=False)

    # ── ÂGE DE CHAQUE FICHIER, UN PAR UN (ajouté 2026-08-07) ──────────────────
    # Le bilan ci-dessus dit si la CADENCE a tourné ; il ne dit rien de l'âge d'un
    # fichier donné. C'est cette confusion qui a laissé le Narrative Tracker et le
    # TradFi Tracker servir 6 h de retard pendant que le battement affichait 21 min :
    # un seul chiffre répondait pour 139 fichiers. On publie donc la date de chacun,
    # que la fonction /data/ compare à celle de la copie déployée — fichier par
    # fichier. Voir tools/index_fraicheur.py pour la règle d'extraction, qui doit
    # rester identique des deux côtés.
    # On ne redate QUE les sorties des collecteurs qui ont abouti : un collecteur en
    # échec laisse son cache précédent en place, et le redater le ferait passer pour
    # frais — la panne muette que cet index existe pour rendre visible.
    # Un collecteur qui aboutit sans écrire ne date rien : c'est le mensonge que
    # l'index existe pour supprimer, et il se rejouait ici sous une autre forme —
    # « frais parce que le collecteur est sorti en succès ». On croise donc la
    # réussite avec l'écriture réelle, mesurée sur le disque.
    produits = {n for j in due for n in j.get("outputs", [])
                if any(r["job"] == j["id"] and r["ok"] for r in results)
                and n in ecrits}
    index_path = os.path.join(CACHE_OUT, "_fichiers.json")
    datés = index_fraicheur.ecrire(index_path, [CACHE_OUT, RELEASE_OUT],
                                   status["updated"], produits)
    print(f"  index de fraîcheur : {datés} fichier(s) datés "
          f"({len(produits)} redaté(s) par ce passage)")

    # Le registre voyage avec les données : c'est lui qui rendra leur âge véritable
    # aux caches restitués au passage suivant, sur une machine qui n'a aucun moyen
    # de le deviner autrement.
    registre_nom = ecrire_ecrits(args.bucket, ecrits, time.time())
    print(f"  registre d'écriture : {len(ecrits)} fichier(s) écrits ce passage")

    # Liste EXACTE des fichiers à publier. Sans elle, la publication stockait tout
    # le dossier : les fichiers qu'une autre cadence venait d'ajouter étaient absents
    # de CE poste de travail, donc enregistrés comme des suppressions. Résultat mesuré :
    # sur sept bilans publiés, un seul survivait — chaque cadence annulait les autres.
    with open(os.path.join(ROOT, ".publish_list"), "w") as f:
        for nom in small + [os.path.basename(status_path),
                            os.path.basename(index_path), registre_nom]:
            f.write(f"cache/{nom}\n")
        for nom in retires:
            f.write(f"cache/{nom}\n")

    # Même raisonnement pour les pièces jointes : la liste EXACTE de celles qui ont
    # changé. La publication renvoyait tout le dossier `release/`, ce qui était sans
    # conséquence à un envoi par heure. Depuis que la cadence fine se joue en dix
    # passages dans une même exécution, renvoyer l'inchangé coûterait dix fois le
    # poids du parc. Et on ne peut pas vider `release/` après envoi : c'est la base
    # de comparaison du passage suivant.
    with open(os.path.join(ROOT, ".upload_list"), "w") as f:
        for nom in big:
            f.write(f"{nom}\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"### {args.bucket} — {len(results)-len(ko)}/{len(results)} OK · "
                    f"{len(changed)} fichier(s) modifié(s) · {elapsed}s\n\n")
            if ko:
                f.write("| Collecteur en échec | Raison |\n|---|---|\n")
                for r in ko:
                    f.write(f"| {r["job"]} | {r['why'][:120]} |\n")

    # Toujours 0 : l'échec d'un collecteur ne doit pas empêcher la publication des
    # autres. Les échecs se lisent dans le bilan, qui est fait pour ça.
    return 0


if __name__ == "__main__":
    sys.exit(main())

# migration : declencheur temporaire (rejoue une collecte reelle apres correction)
# relance apres correction du verrou
# verification de la migration
