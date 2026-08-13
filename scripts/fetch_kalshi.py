#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_kalshi.py — LA SECONDE PLACE DE MARCHÉS PRÉDICTIFS.

POURQUOI UNE DEUXIÈME PLACE, ET PAS JUSTE PLUS DE MARCHÉS.
Polymarket seul donne UNE opinion. Deux places donnent une opinion ET un désaccord — et le
désaccord est la seule chose ici qui ne soit pas déjà dans le prix. Kalshi est réglementé CFTC,
en dollars, avec des participants majoritairement américains ; Polymarket est mondial et en
stablecoins. Quand les deux divergent durablement sur le MÊME événement, c'est l'un des trois :
    1. une information que l'un des deux publics a et pas l'autre ;
    2. une différence de RÈGLEMENT (les contrats ne disent pas la même chose) ;
    3. une friction structurelle (accès, capital, frais) qui n'est pas une opinion du tout.
Publier un écart sans savoir lequel des trois, c'est fabriquer un chiffre. Tout ce module existe
pour rendre ce tri POSSIBLE, et pour refuser d'agréger quand il ne l'est pas.

CE QUE KALSHI DONNE ET QUE POLYMARKET NE DONNE PAS — et ça décide de la méthode.
    · `yes_bid_dollars` / `yes_ask_dollars` : un VRAI carnet. On peut donc calculer un mid, et
      surtout un COÛT D'EXÉCUTION. Polymarket expose un prix ; Kalshi expose ce qu'il en coûte
      de le toucher.
    · `rules_primary` + `settlement_sources` (au niveau de l'événement) : les CONDITIONS DE
      RÈGLEMENT, dans l'API. C'est ce qui permet d'apparier sur ce que le contrat PAIE, et non
      sur ce que son titre RACONTE. Deux titres jumeaux qui règlent sur deux sources
      différentes ne sont pas le même pari.

LA RÈGLE D'OR DE L'APPARIEMENT — et elle est restrictive exprès.
On n'apparie JAMAIS par ressemblance de titre. L'appariement est DÉCLARÉ dans
`prediction_markets_appariements.json`, à la main, avec le motif écrit — puis AUDITÉ par la
donnée (les deux séries bougent-elles ensemble ?). C'est le patron « déclarer puis auditer » du
graphe de transmission, appliqué ici. Ce module ne crée aucun appariement : il RÉCOLTE, il
mesure, et il signale des CANDIDATS à valider par un humain. Un candidat n'est jamais agrégé.

CE QU'ON NE FAIT PAS : la moyenne. Moyenner deux places, c'est effacer la seule information
neuve. On publie le consensus pondéré par le volume ET l'écart, jamais l'un sans l'autre.

Sortie : cache/kalshi_cache.json (+ .js) et cache/kalshi_history.jsonl (append seul).
Aucune clé d'API : la lecture publique de Kalshi est ouverte (vérifié depuis la France,
HTTP 200, 2026-08-13).
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import urllib.error

API = "https://api.elections.kalshi.com/trade-api/v2"
ICI = os.path.dirname(os.path.abspath(__file__))
# ⚠ LE DOSSIER DE SORTIE EST CELUI DE TOUS LES COLLECTEURS, PAS `cache/` DU DÉPÔT.
# `tools/run_jobs.py` lit `CACHE_DIR = ~/Library/Caches/site_crypto_finance`, compare au contenu
# précédent, PUIS range vers `cache/` (petits fichiers, versionnés) ou `release/` (≥ 1 Mo, pièces
# jointes). Écrire directement dans `cache/` court-circuiterait ce tri : le fichier ne serait
# jamais comparé, jamais publié, et le collecteur passerait pour muet en sortant en succès.
CACHE_DIR = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")
SORTIE_JSON = os.path.join(CACHE_DIR, "kalshi_cache.json")
SORTIE_JS = os.path.join(CACHE_DIR, "kalshi_cache.js")
SORTIE_LEGER = os.path.join(CACHE_DIR, "kalshi_cache_light.json")
SORTIE_REGLES = os.path.join(CACHE_DIR, "kalshi_rules.json")
SORTIE_SERIES = os.path.join(CACHE_DIR, "kalshi_series.json")

# ── LA MÉMOIRE DES PRIX, ET POURQUOI ELLE EST BORNÉE ──────────────────────────────────────────
# Toute la méthode du desk est le PERCENTILE DANS SA PROPRE HISTOIRE (« un seuil absolu
# vieillit »). Une cotation ponctuelle ne se classe pas : il faut une mémoire.
# MAIS le collecteur tourne sur un runner ÉPHÉMÈRE : ce qu'il n'a pas publié est perdu, et ce
# qu'il publie est retéléversé à chaque passage. Un journal en ajout seul aurait ajouté ~1,1 Mo
# par heure — 26 Mo par jour, retéléversés en entier à chaque fois, pour toujours.
# On garde donc une série BORNÉE, et seulement pour les marchés qui peuvent alerter : les autres
# n'ont aucun usage d'un rang.
# 72 points à la cadence horaire = trois jours. Mesuré : 1 352 séries suivies pèsent 37 octets
# par point, soit ~3,6 Mo à pleine fenêtre — retéléversés à chaque passage. Trois jours suffisent
# à tout écart de 24 ou 48 h ; au-delà, on paierait de la bande passante horaire pour une
# profondeur dont aucun calcul actuel ne se sert.
SERIE_MAX_POINTS = 72
SERIE_VOLUME_MINI = 25000     # le palier « Moyen » de la page : sous ce volume, pas de rang

UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")

# ── CE QU'ON GARDE, ET POURQUOI CE N'EST PAS UNE LISTE DE MOTS-CLÉS ────────────────────────────
# Kalshi est massivement un marché de PARIS SPORTIFS : les combinés MLB à eux seuls remplissent
# les premières pages de l'API. Ils n'ont aucun rapport avec un book d'investissement et les
# récolter coûterait des minutes de collecte pour du bruit.
# On filtre par CATÉGORIE déclarée par Kalshi, pas par mots du titre : un filtre sur les mots
# raterait « KXFED… » et garderait « Fed Cup » (tennis). Les catégories réellement rencontrées
# sont COMPTÉES et publiées — si Kalshi en renomme une, on le verra dans les lacunes au lieu de
# perdre silencieusement un pan du catalogue.
# ⚠ CETTE LISTE A ÉTÉ ÉCRITE DE TRAVERS AU PREMIER JET, ET C'EST L'ÉTAGE D QUI L'A DIT.
# Le premier passage a écarté « Elections » (11 429 marchés), « Crypto » (4 037) et
# « Commodities » (1 160) — c'est-à-dire précisément les trois catégories qui touchent le book —
# parce que j'avais DEVINÉ les noms de catégorie au lieu de les lire. Rien n'aurait signalé la
# perte si le compte des écartés n'avait pas été publié : la collecte aurait tourné, produit un
# cache d'apparence saine, et le desk aurait conclu « pas de marché sur le bitcoin ».
# C'est l'argument entier du principe « l'exhaustivité se PROUVE », en une seule collecte.
CATEGORIES_GARDEES = {
    "Economics", "Financials", "Politics", "World", "Climate and Weather",
    "Science and Technology", "Companies", "Health",
    "Elections", "Crypto", "Commodities",
}
# Écartées SCIEMMENT. « Mentions » = « X prononcera-t-il le mot Y » ; « Social » = audiences et
# abonnés. Ni l'un ni l'autre ne porte de conséquence sur un book, et les nommer ici les
# distingue d'un oubli — une catégorie absente des deux listes lève une lacune.
CATEGORIES_ECARTEES_CONNUES = {"Sports", "Entertainment", "Transportation", "Mentions",
                               "Social", "(sans catégorie)"}

# Sous ce volume, un marché ne dit rien : son prix est celui du dernier passant. Le seuil est
# repris de `fetch_prediction_markets.py` (MIN_INCLUDE_VOL) pour que les deux places soient
# jugées à la même aune — un seuil différent de chaque côté produirait un écart qui ne serait
# qu'un artefact de filtrage.
MIN_VOLUME = 1000
# Au-dessous, on garde le marché mais on interdit qu'il ALERTE (même logique que le rang de
# liquidité côté Polymarket : un carnet minuscule ne déclenche pas une alerte de déplacement).
MIN_VOLUME_ALERTE = 5000


def http_json(url, essais=3):
    """GET JSON avec reprise. Kalshi limite le débit ; on ralentit au lieu d'abandonner."""
    for n in range(essais):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 * (n + 1))
                continue
            if n == essais - 1:
                raise
            time.sleep(1 + n)
        except Exception:
            if n == essais - 1:
                raise
            time.sleep(1 + n)
    return None


def dollars(v):
    """Kalshi rend ses prix en chaînes (« 0.3400 »). Une chaîne vide ou absente n'est PAS zéro :
    c'est une absence, et la confondre avec 0 fabriquerait une probabilité nulle là où il n'y a
    simplement pas de carnet."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nombre(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def recolte_evenements():
    """Tous les événements ouverts, avec leurs marchés imbriqués.

    On passe par `/events` et non `/markets` pour une raison de fond : `settlement_sources` et
    la catégorie vivent au niveau de l'ÉVÉNEMENT. Récolter les marchés seuls donnerait des prix
    sans savoir sur quoi ils règlent — donc des prix inapariables."""
    evenements, curseur, pages = [], None, 0
    while True:
        url = f"{API}/events?limit=200&status=open&with_nested_markets=true"
        if curseur:
            url += f"&cursor={curseur}"
        d = http_json(url)
        if not d:
            break
        lot = d.get("events") or []
        evenements.extend(lot)
        curseur = d.get("cursor")
        pages += 1
        if not curseur or not lot or pages > 60:   # borne dure : le catalogue ne doit pas
            break                                   # pouvoir faire tourner la collecte sans fin
        time.sleep(0.15)
    return evenements, pages


def mesure_marche(m, ev):
    """Un marché Kalshi réduit à ce qui DÉCIDE, pour chacun des quatre usages.

    Chaque champ existe parce qu'un profil précis en a besoin — et l'ordre des rubriques suit
    ces profils, pour qu'on voie tout de suite ce qui manque à l'un d'eux."""
    bid = dollars(m.get("yes_bid_dollars"))
    ask = dollars(m.get("yes_ask_dollars"))
    dernier = dollars(m.get("last_price_dollars"))
    vol = nombre(m.get("volume_fp")) or 0.0
    vol24 = nombre(m.get("volume_24h_fp"))
    oi = nombre(m.get("open_interest_fp"))
    taille_bid = nombre(m.get("yes_bid_size_fp"))
    taille_ask = nombre(m.get("yes_ask_size_fp"))

    # ── LE MID, ET POURQUOI PAS LE DERNIER ÉCHANGE ─────────────────────────────────────────────
    # Le dernier échange peut dater d'hier et avoir eu lieu au bid ou à l'ask. Le comparer au mid
    # de Polymarket, c'est comparer deux grandeurs différentes et appeler la différence un écart.
    # Un carnet à sens unique (un seul côté coté) N'A PAS de mid : on le dit au lieu d'en
    # fabriquer un.
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        mid = round((bid + ask) / 2, 4)
        ecart_carnet = round(ask - bid, 4)
        carnet = "deux_cotes"
    elif bid is not None and ask is not None and (bid > 0 or ask > 0):
        mid, ecart_carnet, carnet = None, None, "un_seul_cote"
    else:
        mid, ecart_carnet, carnet = None, None, "aucun"

    fin = m.get("close_time") or m.get("expiration_time")
    jours = None
    if fin:
        try:
            t = datetime.datetime.fromisoformat(fin.replace("Z", "+00:00"))
            jours = round((t - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400, 2)
        except ValueError:
            pass

    return {
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "titre": (m.get("title") or "").strip(),
        "sous_titre": (m.get("yes_sub_title") or "").strip(),
        "type": m.get("market_type"),
        "statut": m.get("status"),

        # ANALYSTE — ce que le marché croit, et sur quoi il règle.
        "prob_mid": mid,
        "prob_dernier_echange": dernier,
        "carnet": carnet,
        "regles": (m.get("rules_primary") or "").strip()[:1200],
        "sources_reglement": [s.get("name") for s in (ev.get("settlement_sources") or []) if s.get("name")],
        "categorie": ev.get("category"),
        "serie": ev.get("series_ticker"),
        "exclusifs": bool(ev.get("mutually_exclusive")),

        # TRADER — ce qu'il en coûte de toucher ce prix, et combien de temps il reste.
        "bid": bid,
        "ask": ask,
        "ecart_carnet": ecart_carnet,
        "profondeur_bid": taille_bid,
        "profondeur_ask": taille_ask,
        "jours_avant_echeance": jours,

        # INVESTISSEUR — l'horizon et l'assise. Un marché à trois jours ne cadre pas un book.
        "volume_total": vol,
        "volume_24h": vol24,
        "interet_ouvert": oi,
        "echeance": fin,

        # SPÉCULATEUR — le rapport de paiement. Calculé, jamais commenté : à 4 % le contrat paie
        # 25 fois la mise s'il touche. C'est une division, pas une opinion.
        "paye_x_fois": (round(1.0 / mid, 2) if (mid and mid > 0.005) else None),

        # ── LE GARDE-FOU QUI ÉVITE LA FAUSSE ALERTE ────────────────────────────────────────────
        # Un marché qui expire dans les heures qui viennent converge vers 0 ou 100 par MÉCANIQUE,
        # pas par information. L'extracteur Polymarket a mesuré le dégât : 5 alertes sur 48
        # n'étaient que cette convergence. On marque le marché ici, à la source.
        "convergence_mecanique": (jours is not None and jours < 1.0),
        "peut_alerter": (vol >= MIN_VOLUME_ALERTE and carnet == "deux_cotes"
                         and not (jours is not None and jours < 1.0)),
    }


def main():
    t0 = time.time()
    os.makedirs(CACHE_DIR, exist_ok=True)

    evenements, pages = recolte_evenements()

    # ── L'ÉTAGE D, DÈS LA COLLECTE : on compte TOUT, y compris ce qu'on jette ──────────────────
    # « L'exhaustivité se PROUVE » : un extracteur en aval doit pouvoir dire que les marchés non
    # montrés ont été VUS. Un filtre silencieux rendrait ce dernier étage impossible à écrire.
    par_categorie, retenus, lacunes = {}, [], []
    n_marches_vus = 0
    n_hors_categorie = 0
    n_sous_volume = 0
    n_expires = 0
    categories_inconnues = {}

    for ev in evenements:
        cat = ev.get("category") or "(sans catégorie)"
        marches = ev.get("markets") or []
        n_marches_vus += len(marches)
        par_categorie[cat] = par_categorie.get(cat, 0) + len(marches)

        if cat not in CATEGORIES_GARDEES:
            n_hors_categorie += len(marches)
            if cat not in CATEGORIES_ECARTEES_CONNUES:
                categories_inconnues[cat] = categories_inconnues.get(cat, 0) + len(marches)
            continue

        for m in marches:
            if (nombre(m.get("volume_fp")) or 0.0) < MIN_VOLUME:
                n_sous_volume += 1
                continue
            mes = mesure_marche(m, ev)
            # ── LE MARCHÉ ZOMBIE, DÉFAUT MESURÉ AU PREMIER PASSAGE ─────────────────────────────
            # `status=open` ne suffit PAS : l'API rend des marchés dont l'échéance est passée de
            # 9 à 43 JOURS, encore marqués actifs, avec de gros volumes historiques et un carnet
            # vide. Quatre d'entre eux figuraient dans les douze plus gros « marchés retenus » du
            # premier jet. Les garder aurait donné au desk des paris expirés présentés comme
            # ouverts — et un « écart entre places » contre un contrat qui ne cote plus.
            if mes["jours_avant_echeance"] is not None and mes["jours_avant_echeance"] < 0:
                n_expires += 1
                continue
            retenus.append(mes)

    # Une catégorie inconnue n'est PAS écartée en silence : soit Kalshi en a créé une, soit il a
    # renommé une des nôtres, et dans les deux cas on vient peut-être de perdre un pan entier du
    # catalogue. Le dire coûte une ligne ; ne pas le dire coûte un angle mort.
    for cat, n in sorted(categories_inconnues.items(), key=lambda kv: -kv[1]):
        lacunes.append(f"catégorie inconnue « {cat} » écartée : {n} marché(s) jamais examinés — "
                       f"vérifier si elle doit rejoindre CATEGORIES_GARDEES")

    sans_mid = [r for r in retenus if r["prob_mid"] is None]
    if sans_mid:
        lacunes.append(f"{len(sans_mid)} marché(s) sur {len(retenus)} sans mid exploitable "
                       f"(carnet à un seul côté ou vide) : ils ont un volume mais aucun prix "
                       f"comparable — ils ne peuvent pas entrer dans un écart entre places")

    convergents = [r for r in retenus if r["convergence_mecanique"]]
    if convergents:
        lacunes.append(f"{len(convergents)} marché(s) à moins de 24 h de l'échéance : leur "
                       f"déplacement est MÉCANIQUE et ne peut pas alerter")

    if n_expires:
        lacunes.append(f"{n_expires} marché(s) écartés car leur échéance est PASSÉE alors que "
                       f"l'API les rend encore « open » — vus, comptés, jamais publiés")

    retenus.sort(key=lambda r: -(r["volume_total"] or 0))

    # ── LES RÈGLES PARTENT DANS LEUR PROPRE FICHIER ───────────────────────────────────────────
    # Même traitement que `prediction_markets_rules.json` côté Polymarket, et pour la même raison,
    # écrite dans le Rmd : « ~700 Ko de contrats n'ont rien à faire dans le HTML alors qu'ils ne
    # s'affichent qu'à l'ouverture d'un tiroir ». Ici les règles pèsent plus de la moitié du
    # cache. Elles restent INDISPENSABLES — c'est sur elles que se juge un appariement — mais
    # elles se chargent en différé.
    regles = {r["ticker"]: {"regles": r.pop("regles"),
                            "sources_reglement": r.get("sources_reglement") or []}
              for r in retenus}

    entete = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": "Kalshi trade-api v2 (lecture publique, sans authentification)",
        "duree_s": round(time.time() - t0, 1),
        "exhaustivite": {
            "evenements_lus": len(evenements),
            "pages_api": pages,
            "marches_vus": n_marches_vus,
            "marches_hors_categorie": n_hors_categorie,
            "marches_sous_volume": n_sous_volume,
            "marches_expires": n_expires,
            "marches_retenus": len(retenus),
            "seuil_volume": MIN_VOLUME,
            "categories_gardees": sorted(CATEGORIES_GARDEES),
            "repartition_par_categorie": dict(sorted(par_categorie.items(), key=lambda kv: -kv[1])),
        },
        "lacunes": lacunes,
    }

    def ecrire(chemin, obj, prefixe_js=None):
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if prefixe_js:
                f.write(prefixe_js)
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            if prefixe_js:
                f.write(";")
        os.replace(tmp, chemin)

    ecrire(SORTIE_JSON, dict(entete, marches=retenus))
    ecrire(SORTIE_JS, dict(entete, marches=retenus), "window.__KALSHI__=")
    ecrire(SORTIE_REGLES, {"generated_at": entete["generated_at"], "regles": regles})

    # ── LA VERSION ALLÉGÉE, POUR LE PREMIER AFFICHAGE ─────────────────────────────────────────
    # Le Rmd inline un instantané au rendu puis écrase avec le `.js` complet. Inliner le cache
    # entier ferait un HTML de sa taille pour une donnée qui ne sert qu'à la première seconde.
    # On ne garde ici que ce qui s'affiche AVANT toute interaction.
    leger = [{k: m[k] for k in ("ticker", "titre", "categorie", "prob_mid", "ecart_carnet",
                                "volume_total", "jours_avant_echeance", "paye_x_fois",
                                "peut_alerter")}
             for m in retenus]
    ecrire(SORTIE_LEGER, dict(entete, marches=leger))

    # ── LA SÉRIE BORNÉE : ON RELIT, ON AJOUTE UN POINT, ON ÉLAGUE ─────────────────────────────
    # Le fichier précédent est restauré par `run_jobs.prepare_env()` (pièce jointe) avant le
    # lancement. S'il manque — premier passage, ou release absente — on repart d'un dictionnaire
    # vide : c'est une mémoire courte, pas une erreur.
    # ÉLAGAGE EN DEUX TEMPS, et les deux comptent : on coupe les points trop vieux de chaque
    # série, ET on supprime les séries dont le marché a disparu (expiré, réglé). Sans le second,
    # le fichier ne ferait que grossir de marchés morts — le défaut exact qu'on cherche à éviter.
    try:
        series = json.load(open(SORTIE_SERIES, encoding="utf-8")).get("series") or {}
    except Exception:
        series = {}

    vivants = set()
    for r in retenus:
        if r["prob_mid"] is None or (r["volume_total"] or 0) < SERIE_VOLUME_MINI:
            continue
        t = r["ticker"]
        vivants.add(t)
        pts = series.get(t) or []
        pts.append([entete["generated_at"][:16], r["prob_mid"]])
        series[t] = pts[-SERIE_MAX_POINTS:]

    disparus = [t for t in series if t not in vivants]
    for t in disparus:
        del series[t]

    ecrire(SORTIE_SERIES, {"generated_at": entete["generated_at"],
                           "points_max": SERIE_MAX_POINTS,
                           "volume_mini": SERIE_VOLUME_MINI,
                           "series_suivies": len(series),
                           "series_retirees_ce_passage": len(disparus),
                           "series": series})

    e = entete["exhaustivite"]
    print(f"[kalshi] {e['marches_retenus']} retenus sur {e['marches_vus']} vus "
          f"({e['evenements_lus']} événements, {pages} pages) — "
          f"{n_hors_categorie} hors catégorie, {n_sous_volume} sous {MIN_VOLUME} $, "
          f"{n_expires} expirés — {entete['duree_s']} s")
    for l in lacunes:
        print(f"[kalshi] lacune : {l}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
