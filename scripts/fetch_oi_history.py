#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Open interest horaire des perpétuels Binance — historique PROFOND.

╔══════════════════════════════════════════════════════════════════════════════╗
║ POURQUOI CE COLLECTEUR EXISTE                                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
Le panneau « Purge de levier » lit l'open interest heure par heure : une purge est
une heure dont la variation d'OI tombe sous le 5e centile de la fenêtre. Sa source
en direct, `fapi/v1 futures/data/openInterestHist`, a DEUX plafonds mesurés le
2026-08-27 :

  · 500 points par requête — contourné en paginant par `endTime` ;
  · **30 jours d'historique, et rien au-delà.** Un `startTime` à J-45 répond
    `{"code":-1130,"msg":"parameter 'startTime' is invalid."}`. Ni `period=4h` ni
    `period=1d` ne remontent plus loin : le mur est dans la donnée, pas dans le pas.

Trente jours ne montrent aucun cycle. Le premier réflexe était d'accumuler nous-mêmes
un point par heure, en attendant des mois que la profondeur vienne. C'était inutile :

╔══════════════════════════════════════════════════════════════════════════════╗
║ BINANCE PUBLIE DÉJÀ CET HISTORIQUE, EN ARCHIVE BRUTE                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
`data.binance.vision` sert un fichier par actif et par jour :

    data/futures/um/daily/metrics/<SYM>/<SYM>-metrics-<AAAA-MM-JJ>.zip

Il contient un CSV au pas de **5 minutes**, dont la colonne `sum_open_interest_value`
est exactement la grandeur que lit le panneau. Mesuré : BTCUSDT remonte au
**2020-09-01**, et les 985 symboles ayant des métriques sont tous servis, jusqu'au
plus petit du carnet. Six ans au lieu de trente jours, disponibles TOUT DE SUITE.

Et — ce qui décide de tout — ce domaine n'est **pas géobloqué**. `fapi.binance.com`
renvoie 451 aux runners GitHub (cf. `_cloud_sources.py`, et la sonde qui l'a mesuré
depuis l'IP 52.157.33.44) ; l'archive, elle, est servie par un CDN qui répond. Ce
collecteur peut donc tourner là où l'API ne le pourrait pas.

╔══════════════════════════════════════════════════════════════════════════════╗
║ LE RACCORD ARCHIVE ↔ DIRECT — LA MESURE QUI REND LA COUTURE INVISIBLE        ║
╚══════════════════════════════════════════════════════════════════════════════╝
Naïvement, on prend la ligne à `HH:00` pour l'heure `HH`. C'est FAUX, et faux de
manière sournoise : écart moyen 0,26 %, maximum 0,97 % contre l'API. Sur une série
dont on mesure des variations de 1 à 4 %, ce bruit-là fabrique des purges.

Le point horaire `T` de l'API vaut le relevé 5 minutes de **T − 5 min** : Binance
étiquette le seau horaire par sa FIN, et le dernier relevé du seau est à `HH:55`.
Vérifié sur 24 heures et trois actifs (BTC, ETH, 1000PEPE) :

    décalage  −5 min  →  écart moyen 0,000000 %   maximum 0,000000 %
    décalage    0 min  →  écart moyen 0,259785 %   maximum 0,965258 %

L'archive et le direct sont donc le MÊME nombre, et le site peut les coudre sans
créer une seule variation artificielle. C'est cette égalité qui autorise tout le
reste ; si elle cassait un jour, il faudrait le voir — d'où `--verifier`.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUI EST STOCKÉ, ET POURQUOI PAS PLUS                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝
Le pas retenu est l'HEURE, pas les 5 minutes de la source : c'est la granularité de
la méthode du panneau, et garder les 5 minutes multiplierait le parc par douze pour
une précision dont rien ne se sert.

Un fichier par actif, `oi_hist_<SYM>.json`, parce que le site n'affiche qu'un actif
à la fois : il ne doit télécharger que celui-là. Le tableau `oi` est DENSE — indexé
par le rang de l'heure depuis `t0`, `null` pour une heure manquante. Pas
d'horodatage par point : il se recalcule (`t0 + i × 3600`) et le stocker doublerait
le poids pour zéro information.

Les valeurs sont divisées par `k` (une puissance de dix, écrite dans le fichier) de
sorte que la plus grande garde sept chiffres significatifs — soit une précision
relative de 1e-7, mille fois sous la plus petite variation qu'on mesure. `k` est
FIXÉ à la création et n'est jamais recalculé : le changer obligerait à réécrire tout
l'historique, et une erreur d'échelle y serait invisible.

╔══════════════════════════════════════════════════════════════════════════════╗
║ RAPATRIEMENT PROGRESSIF                                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝
L'historique complet représente ~630 000 fichiers journaliers. À 28 fichiers/s
(débit mesuré), c'est six heures : impossible en une exécution, et grossier envers
le CDN. Le rapatriement va donc DU PLUS RÉCENT AU PLUS ANCIEN, borné par un budget
de téléchargements par passage. L'archive est utile dès le premier passage et
s'approfondit toute seule.

Chaque fichier note `av` — le plus ancien jour déjà tenté — et `complet` quand la
date de première cotation est atteinte. Un passage ne refait donc jamais le travail
du précédent.

Sorties (~/Library/Caches/site_crypto_finance/) :
  oi_hist_<SYM>.json    un par actif  (~200 Ko pour un actif de deux ans)
  oi_hist_index.json    ce qui est archivé, et jusqu'où — lu par le site

Usage :
  python fetch_oi_history.py                    # passage normal (cadence quotidienne)
  python fetch_oi_history.py --budget 200000    # gros rapatriement, depuis le PC
  python fetch_oi_history.py --actifs BTCUSDT,ETHUSDT
  python fetch_oi_history.py --verifier         # recontrôle le raccord archive/direct
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
INDEX = CACHE_DIR / "oi_hist_index.json"

ARCHIVE = "https://data.binance.vision/data/futures/um/daily/metrics"
LISTAGE = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
PREFIXE = "data/futures/um/daily/metrics/"
API = "https://fapi.binance.com"

HEURE = 3600
UA = "scf-data/oi-history (+https://github.com/MarcAntoineBA/scf-data)"
TIMEOUT = 45
FILS = 48                       # 28 fichiers/s mesurés à 32 fils depuis une box FR ; le
                                # CDN tient bien au-delà, et 48 divise le rapatriement par deux.
BUDGET_DEFAUT = 25_000          # ~15 min de téléchargement par passage quotidien.
DEPART = date(2020, 9, 1)       # premier jour publié, tous actifs confondus (BTCUSDT).


def _lire(url, essais=3):
    for n in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                     # jour non publié : ce n'est pas une panne
            if n == essais - 1:
                raise
        except Exception:                                             # noqa: BLE001
            if n == essais - 1:
                raise
        time.sleep(1.5 * (n + 1))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Découverte : quels actifs, et quels jours pour chacun
#
# `exchangeInfo` donnerait la liste, mais il vit sur fapi.binance.com — donc 451
# depuis un runner. Le listage S3 de l'archive, lui, répond partout ET dit la vérité
# la plus utile : quels symboles ont RÉELLEMENT des métriques publiées.
# ─────────────────────────────────────────────────────────────────────────────
def symboles_archives():
    out, marqueur = [], ""
    while True:
        url = f"{LISTAGE}?delimiter=/&prefix={PREFIXE}"
        if marqueur:
            url += "&marker=" + urllib.parse.quote(marqueur, safe="")
        xml = _lire(url).decode("utf-8", "replace")
        lot = re.findall(r"<Prefix>" + re.escape(PREFIXE) + r"([^<]+)/</Prefix>", xml)
        out += lot
        if re.search(r"<IsTruncated>true", xml) and lot:
            marqueur = PREFIXE + lot[-1] + "/"
            continue
        return sorted(set(out))


def actifs_cotes(archives):
    """Les perpétuels USDT ENCORE COTÉS, croisés avec ce que l'archive contient.

    L'archive garde les contrats délistés — ils y sont pour toujours. Les rapatrier
    coûterait des dizaines de milliers de fichiers pour des séries que le panneau ne
    proposera jamais, puisqu'il n'affiche que ce qui se traite aujourd'hui.

    `exchangeInfo` vit sur fapi.binance.com, donc répond depuis une machine
    européenne et PAS depuis un runner (451). En cas de refus, on retombe sur ce que
    l'index connaît déjà : un runner continue d'entretenir ce qui a été rapatrié, et
    n'ouvre pas de chantier tout seul sur des contrats morts.
    """
    dispo = set(archives)
    try:
        d = json.loads(_lire(f"{API}/fapi/v1/exchangeInfo"))
        cotes = [s["symbol"] for s in d.get("symbols", [])
                 if s.get("status") == "TRADING"
                 and s.get("contractType") == "PERPETUAL"
                 and s.get("quoteAsset") == "USDT"]
        gardes = [s for s in cotes if s in dispo]
        if gardes:
            print(f"  {len(gardes)} perpétuels USDT cotés (sur {len(dispo)} archivés)")
            return sorted(gardes)
    except Exception as e:                                            # noqa: BLE001
        print(f"  liste des cotations indisponible ({type(e).__name__})")

    # REPLI : `cache_manifest.txt`, pas l'index de l'archive.
    # Le manifeste est la liste des fichiers que ce dépôt publie — donc la liste
    # des actifs qu'on VEUT archiver, indépendamment de ceux déjà rapatriés. Se
    # replier sur l'index serait circulaire : un runner n'entretiendrait que ce
    # qui existe déjà et n'ouvrirait jamais un actif de plus, si bien que les
    # quatre cents actifs restants n'arriveraient jamais. Le défaut serait muet —
    # le collecteur sortirait en succès à chaque passage.
    manifeste = Path(__file__).resolve().parent.parent / "cache_manifest.txt"
    voulus = []
    try:
        for l in manifeste.read_text().splitlines():
            l = l.strip()
            if l.startswith("oi_hist_") and l.endswith(".json") and l != "oi_hist_index.json":
                voulus.append(l[len("oi_hist_"):-len(".json")])
    except Exception:                                                 # noqa: BLE001
        pass
    gardes = sorted(set(voulus) & dispo)
    if gardes:
        print(f"  repli sur le manifeste : {len(gardes)} actifs")
        return gardes

    # Dernier recours : ce qui est déjà archivé. On n'ouvre pas de chantier sur
    # les huit cent cinquante symboles de l'archive — dont trois cents délistés —
    # sur la foi d'un manifeste illisible.
    try:
        connus = [a["s"] for a in json.loads(INDEX.read_text()).get("actifs", [])]
        if connus:
            print(f"  repli sur l'index : {len(connus)} actifs déjà archivés")
            return sorted(set(connus) & dispo)
    except Exception:                                                 # noqa: BLE001
        pass
    return sorted(dispo)


def jours_publies(sym):
    """Toutes les dates disponibles pour un actif, lues au listage.

    Sonder les 404 un jour après l'autre coûterait deux mille requêtes par actif ;
    le listage en coûte deux ou trois et donne la réponse exacte, y compris les
    trous éventuels au milieu de la série.
    """
    prefixe, out, marqueur = f"{PREFIXE}{sym}/", [], ""
    while True:
        url = f"{LISTAGE}?prefix={urllib.parse.quote(prefixe, safe='/')}"
        if marqueur:
            url += "&marker=" + urllib.parse.quote(marqueur, safe="")
        xml = _lire(url).decode("utf-8", "replace")
        cles = re.findall(r"<Key>([^<]+)</Key>", xml)
        out += [m.group(1) for k in cles
                for m in [re.search(r"-metrics-(\d{4}-\d{2}-\d{2})\.zip$", k)] if m]
        if re.search(r"<IsTruncated>true", xml) and cles:
            marqueur = cles[-1]
            continue
        return sorted(set(out))


# ─────────────────────────────────────────────────────────────────────────────
# Un jour d'archive → des points HORAIRES
# ─────────────────────────────────────────────────────────────────────────────
def heures_du_jour(sym, jour):
    """{epoch_seconde_de_l_heure: open_interest_usd} pour un fichier journalier.

    LA RÈGLE DES −5 MINUTES est ici, et nulle part ailleurs : seul le relevé de
    `HH:55` produit un point, étiqueté `HH+1:00`. Un fichier du jour J rend donc les
    heures J 01:00 → J+1 00:00, et l'heure J 00:00 vient du fichier de la veille.
    """
    brut = _lire(f"{ARCHIVE}/{sym}/{sym}-metrics-{jour}.zip")
    if not brut:
        return None
    try:
        z = zipfile.ZipFile(io.BytesIO(brut))
        txt = z.read(z.namelist()[0]).decode("utf-8", "replace")
    except Exception:                                                 # noqa: BLE001
        return None
    out = {}
    for r in csv.DictReader(io.StringIO(txt)):
        try:
            t = datetime.strptime(r["create_time"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc)
            if t.minute != 55:
                continue
            v = float(r["sum_open_interest_value"])
        except (KeyError, ValueError, TypeError):
            continue
        if v > 0:
            out[int(t.timestamp()) + 300] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Le fichier d'un actif
# ─────────────────────────────────────────────────────────────────────────────
def chemin(sym):
    return CACHE_DIR / f"oi_hist_{sym}.json"


def charger(sym):
    try:
        d = json.loads(chemin(sym).read_text())
        if isinstance(d, dict) and isinstance(d.get("oi"), list):
            return d
    except Exception:                                                 # noqa: BLE001
        pass
    return None


def ecrire_atomique(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def echelle(vmax):
    """k = puissance de dix laissant SEPT chiffres significatifs à la plus grande
    valeur. Fixé une fois pour toutes : le recalculer obligerait à réécrire tout
    l'historique à chaque nouveau sommet, et une erreur d'échelle y serait muette."""
    chiffres = len(str(int(vmax))) if vmax >= 1 else 1
    return 10 ** max(0, chiffres - 7)


def fusionner(sym, base, points):
    """Range de nouveaux points horaires dans le tableau dense, sans jamais perdre
    ce qui y était. Un point déjà connu n'est PAS réécrit : l'archive est figée, et
    une valeur qui changerait sous nos pieds serait un signal, pas une mise à jour."""
    connus = {}
    if base:
        t0, k = base["t0"], base["k"]
        for i, v in enumerate(base["oi"]):
            if v is not None:
                connus[t0 + i * HEURE] = v * k
    connus.update({t: v for t, v in points.items() if t not in connus})
    if not connus:
        return None

    t0 = min(connus)
    t1 = max(connus)
    k = base["k"] if base else echelle(max(connus.values()))
    n = (t1 - t0) // HEURE + 1
    oi = [None] * n
    for t, v in connus.items():
        oi[(t - t0) // HEURE] = round(v / k)
    return {"t0": t0, "k": k, "oi": oi, "n": n,
            "vus": sum(1 for x in oi if x is not None)}


def jour_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# Un actif, un passage
# ─────────────────────────────────────────────────────────────────────────────
def traiter(sym, budget_recul, hier, pool):
    """Complète l'actif : d'abord vers l'AVANT (les jours récents manquants), puis
    vers l'ARRIÈRE tant qu'il reste du budget. L'avant n'est JAMAIS borné par le
    budget — une archive à jour et courte sert le lecteur, une archive profonde et
    périmée ne sert personne. Seul le recul se rationne."""
    base = charger(sym)
    jours_a_prendre = []

    if base:
        dernier = date.fromisoformat(base.get("fin") or jour_utc(base["t0"]))
        j = dernier
        while j < hier:
            j += timedelta(days=1)
            jours_a_prendre.append(j.isoformat())
        avant = base.get("av")
        complet = bool(base.get("complet"))
    else:
        avant, complet = None, False

    # Recul : on ne connaît la date de première cotation qu'au listage, et le listage
    # ne coûte que deux ou trois requêtes contre deux mille sondages à l'aveugle.
    dispo = None
    if not complet and budget_recul > 0:
        dispo = jours_publies(sym)
        if not dispo:
            return sym, 0, base, "aucun jour publié"
        borne = avant or (base and base.get("debut")) or hier.isoformat()
        recul = [d for d in dispo if d < borne]
        jours_a_prendre += list(reversed(recul))[:budget_recul]   # du récent vers l'ancien
        if len(recul) <= budget_recul:
            complet = True      # la date de première cotation est atteinte

    if not jours_a_prendre:
        return sym, 0, base, "à jour"

    lots = list(pool.map(lambda d: (d, heures_du_jour(sym, d)), jours_a_prendre))
    points = {}
    for _, h in lots:
        if h:
            points.update(h)
    if not points and not base:
        return sym, 0, None, "vide"

    f = fusionner(sym, base, points)
    if not f:
        return sym, 0, None, "vide"

    # `av` = le plus ancien jour DÉJÀ TENTÉ. C'est lui qui évite qu'un passage
    # refasse le travail du précédent, y compris sur les jours revenus vides.
    candidats = [d for d, _ in lots] + ([avant] if avant else [])
    av = min(candidats) if candidats else None

    obj = {
        "s": sym,
        "b": re.sub(r"(USDT|USDC)$", "", sym),
        "pas": HEURE,
        "k": f["k"],
        "t0": f["t0"],
        "n": f["n"],
        "vus": f["vus"],
        "oi": f["oi"],
        "debut": jour_utc(f["t0"]),
        "fin": jour_utc(f["t0"] + (f["n"] - 1) * HEURE),
        "av": av,
        "complet": complet,
        "unite": "open interest notionnel en dollars = oi[i] × k",
        "src": "data.binance.vision futures/um/daily/metrics (pas 5 min, "
               "heure T = relevé de T−5 min)",
        # `updated` et non un nom maison : c est le vocabulaire que lit
        # index_fraicheur, et lui seul evite que ce fichier soit date de sa
        # COPIE plutot que de sa collecte.
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    ecrire_atomique(chemin(sym), obj)
    return sym, len(jours_a_prendre), obj, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Contrôle du raccord : l'archive DOIT rendre le même nombre que l'API
# ─────────────────────────────────────────────────────────────────────────────
def verifier(syms=("BTCUSDT", "ETHUSDT", "AAVEUSDT")):
    """Recompare archive et direct heure par heure. Ce contrôle n'est pas décoratif :
    tout l'intérêt du montage tient à l'égalité exacte des deux séries, et une
    dérive silencieuse ferait apparaître des purges qui n'ont pas eu lieu."""
    pire = 0.0
    for sym in syms:
        try:
            live = json.loads(_lire(
                f"{API}/futures/data/openInterestHist?symbol={sym}&period=1h&limit=500"))
        except Exception as e:                                        # noqa: BLE001
            print(f"  {sym} : API injoignable ({e}) — contrôle impossible ici")
            continue
        d = charger(sym)
        if not d:
            print(f"  {sym} : pas d'archive locale")
            continue
        t0, k = d["t0"], d["k"]
        ecarts = []
        for o in live:
            t = int(o["timestamp"]) // 1000
            i = (t - t0) // HEURE
            if 0 <= i < d["n"] and d["oi"][i] is not None:
                a, l = d["oi"][i] * k, float(o["sumOpenInterestValue"])
                if l:
                    ecarts.append(abs(a - l) / l * 100)
        if ecarts:
            m = max(ecarts)
            pire = max(pire, m)
            print(f"  {sym} : {len(ecarts)} heures comparées, écart max {m:.6f} %"
                  + ("  ← DÉRIVE" if m > 0.05 else ""))
        else:
            print(f"  {sym} : aucune heure commune")
    return pire


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=BUDGET_DEFAUT,
                    help="téléchargements maximum pour ce passage")
    ap.add_argument("--actifs", default="", help="liste explicite, séparée par des virgules")
    ap.add_argument("--verifier", action="store_true", help="contrôle du raccord seul")
    a = ap.parse_args()

    if a.verifier:
        pire = verifier()
        print(f"écart maximal : {pire:.6f} %")
        return 0 if pire <= 0.05 else 1

    t0 = time.time()
    if a.actifs:
        syms = [s.strip().upper() for s in a.actifs.split(",") if s.strip()]
    else:
        syms = actifs_cotes([s for s in symboles_archives() if s.endswith("USDT")])
    if not syms:
        print("aucun actif : l'archive Binance n'a rien renvoyé", file=sys.stderr)
        return 1
    print(f"{len(syms)} actifs · budget {a.budget} téléchargements")

    hier = datetime.now(timezone.utc).date() - timedelta(days=1)
    reste = a.budget
    index, faits, telecharges = [], 0, 0

    with ThreadPoolExecutor(max_workers=FILS) as pool:
        # Les actifs déjà à jour ne consomment rien : ils sont vus, mesurés, passés.
        # Le budget se dépense donc sur ce qui manque, jamais sur ce qui est acquis.
        for sym in syms:
            # Plafond par actif : sans lui, le premier de la liste (BTCUSDT, six ans
            # d'historique) mangerait le budget entier et les cinq cents autres
            # n'auraient jamais un seul jour. Le rapatriement avance de front.
            part = min(max(reste, 0), 400)
            try:
                _, pris, obj, etat = traiter(sym, part, hier, pool)
            except Exception as e:                                    # noqa: BLE001
                print(f"  {sym} : {type(e).__name__} {e}")
                obj, pris, etat = charger(sym), 0, "erreur"
            reste -= pris
            telecharges += pris
            if obj:
                faits += 1
                index.append({"s": obj["s"], "b": obj["b"], "t0": obj["t0"],
                              "n": obj["n"], "vus": obj["vus"], "k": obj["k"],
                              "debut": obj["debut"], "fin": obj["fin"],
                              "complet": bool(obj.get("complet"))})
            if faits % 50 == 0 and pris:
                print(f"  … {faits}/{len(syms)} actifs · {telecharges} fichiers · "
                      f"{int(time.time()-t0)} s")

    index.sort(key=lambda x: -x["n"])
    profond = max((x["n"] for x in index), default=0)
    ecrire_atomique(INDEX, {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pas": HEURE,
        "n_actifs": len(index),
        "n_complets": sum(1 for x in index if x["complet"]),
        "profondeur_max_h": profond,
        "src": "data.binance.vision futures/um/daily/metrics",
        "note": "oi[i] × k = open interest notionnel en dollars à t0 + i × 3600 s (UTC). "
                "Le point horaire T vaut le relevé 5 min de T−5 min, ce qui le rend "
                "identique au point de futures/data/openInterestHist.",
        "actifs": index,
    })

    print(f"{faits} actifs archivés · {telecharges} fichiers repris · "
          f"{sum(1 for x in index if x['complet'])} complets · "
          f"profondeur maximale {profond // 24} jours · {int(time.time()-t0)} s")
    return 0 if faits else 1


if __name__ == "__main__":
    sys.exit(main())
