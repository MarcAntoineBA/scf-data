#!/usr/bin/env python3
"""Fetcher — Treasury Companies (Strategy/MSTR, BitMine/BMNR, Hyperliquid Strategies/PURR).

Alimente Treasury_Companies.html : pour chaque société, prix de l'actif
(Yahoo v8 daily), prix de l'action, historique des achats (seed statique
sourcé SEC 8-K / bitbo, voir ci-dessous) et agrégats (mNAV, % supply, PnL).

SOURCES SEED (re-auditées ligne à ligne sur EDGAR le 2026-07-26)
----------------------------------------------------------------
- MSTR : 118 transactions aug-2020 -> juil-2026, reconstruites depuis les 8-K
  SEC (CIK 1050446). Contrôle : le cumul du seed est comparé aux « Aggregate
  BTC Holdings » déclarés dans chaque 8-K (résidu +11 BTC sur 6 ans, cf.
  coherence dans la sortie). 843,775 BTC, avg $75,476, $63.69B au 2026-07-19.
- BMNR : 56 snapshots hebdo des 8-K SEC EDGAR (CIK 1829311), exhibit 99.
  Achats = delta entre snapshots, coût estimé au prix spot ETH du jour.
  Avg cost $3,517 = 10-Q au 2026-05-31 (ETH seul : $19,052,159k / 5,416,945 —
  ATTENTION le total XBRL us-gaap:CryptoAssetNumberOfUnits agrège BTC+ETH+
  autres, il ne faut PAS l'utiliser comme dénominateur).
- PURR : 10-Q/S-1/S-1-A (CIK 2078856) — roll-forward EXACT du 10-Q au
  2026-03-31 (units ET dollars, cf. PURR_EVENTS), puis soldes déclarés dans les
  prospectus : 20,0 M au 2026-04-29 (8-K du 7 mai), 20,8 M au 2026-05-14 (S-1
  du 22 mai), 29,3 M au 2026-07-15 (S-1/A du 21 juil.). Aucune vente de HYPE à
  ce jour (le tableau du S-1 montre une vente de 3,373,420 *USDC*, colonne
  voisine — piège). Le cumul du seed retombe au dollar près sur le coût de
  revient publié (752 966 k$ pour 18 826 355 HYPE au 2026-03-31) — c'est un
  assert du fetcher, pas un espoir.
  **PIÈGE STRUCTUREL (bug du 2026-07-26, graphe figé au 27 avril)** : PURR ne
  publie PAS de 8-K par achat. Ses achats financés par l'ELOC Chardan
  n'apparaissent que dans les 10-Q et les S-1/A. Un scan limité aux 8-K est
  aveugle sur 9,3 M HYPE (+47%), la plus grosse phase d'accumulation de la
  société. D'où edgar_scan qui lit MAINTENANT 8-K + 10-Q + 10-K + S-1 + S-1/A.

LIVE
----
- **Filings SEC lus à chaque run** (EDGAR submissions API) : tout mouvement
  postérieur au seed est ajouté automatiquement, ACHAT COMME VENTE. C'est la
  source primaire — CoinGecko n'est plus qu'un filet. Chaque filing parsé est
  mémorisé dans treasury_edgar_cache.json (1 seul téléchargement par filing, et
  les événements survivent à une panne SEC).
- **Actions en circulation** : jamais un seed figé (la mNAV en dépend
  linéairement). MSTR = couverture du 10-Q (classes A + B) + les actions
  vendues à l'ATM semaine par semaine dans les 8-K ; BMNR/PURR = couverture du
  dernier 10-Q / S-1-A. Voir SHARES_HIST + edgar_atm_shares().
- Auto-test du parseur : les filings déjà couverts par le seed sont re-parsés
  et comparés au seed ; tout écart est loggé (anti-régression silencieuse).
- Holdings MSTR/BMNR recoupés via CoinGecko /companies/public_treasury/ : si
  l'écart avec le total connu dépasse 0,05%, un mouvement synthétique daté du
  jour est ajouté — **dans les deux sens** (une baisse = vente, elle était
  invisible avant le 2026-07-26).
- Supplies circulantes via CoinGecko /coins/{id}.
- Market caps approximées shares_m (seed dernière publication) x dernier cours.

Sortie : ~/Desktop/Site_Crypto_Finance/treasury_cache.js (window.__TREASURY_LIVE__)
       + treasury_cache.json (soft-refresh 15 min côté page).
Garde-fou : si un fetch critique échoue et qu'un cache existe déjà, on sort
sans rien écraser (pattern anti "DNS flap écrase cache à null").
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from curl_cffi import requests as cr

# launchd n'a pas le droit TCC d'écrire sur ~/Desktop : on écrit dans
# Library/Caches et le repo pointe dessus via symlink (pattern standard du
# site, cf scf.cryptoetf — snapshot_site.sh déréférence au deploy).
CACHE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Caches", "site_crypto_finance")
os.makedirs(CACHE_DIR, exist_ok=True)
OUT_JS = os.path.join(CACHE_DIR, "treasury_cache.js")
OUT_JSON = os.path.join(CACHE_DIR, "treasury_cache.json")
# Wrapper LEGER pour le widget de veille de l'Accueil (~2 Ko contre 125 Ko pour
# le cache complet) : l'Accueil n'a besoin que du dernier mouvement de chaque
# societe + les derniers evenements, pas des 2246 points de prix BTC. Meme
# pattern que mode_crypto_live.js / sentiment_index_live.js.
OUT_ALERT = os.path.join(CACHE_DIR, "treasury_alert_live.js")
# Jumeau JSON du wrapper (2026-08-03). Le .js est chargé par <script defer> avec
# un ?v= gravé au render de index.Rmd : un onglet resté ouvert ne le relit JAMAIS,
# et le navigateur peut même servir l'ancien depuis son cache HTTP. Le batch
# caRefresh de accueil.js, lui, fetch() du JSON avec cache-bust runtime — d'où ce
# second fichier, seul moyen que la tuile 21 se rafraîchisse sans rechargement.
OUT_ALERT_JSON = os.path.join(CACHE_DIR, "treasury_alert_live.json")

UTC = timezone.utc


# ── Seeds achats / holdings (voir docstring pour le sourcing) ──────────────
# MSTR : (date, btc, usd_m, px_moyen_payé). Négatif = vente.
MSTR_PURCHASES = [
    ("2020-08-11", 21454, 250.0, 11652.84), ("2020-09-14", 16796, 175.0, 10419.15),
    ("2020-12-04", 2574, 50.0, 19425.02), ("2020-12-21", 29646, 650.0, 21925.39),
    ("2021-01-22", 314, 10.0, 31847.13), ("2021-02-02", 295, 10.0, 33898.31),
    ("2021-02-24", 19452, 1026.0, 52745.22), ("2021-03-01", 328, 15.0, 45731.71),
    ("2021-03-05", 205, 10.0, 48780.49), ("2021-03-12", 262, 15.0, 57251.91),
    ("2021-04-05", 253, 15.0, 59288.54), ("2021-05-13", 271, 15.0, 55350.55),
    ("2021-05-18", 229, 10.0, 43668.12), ("2021-06-21", 13005, 489.0, 37600.92),
    # 2026-07-26 : le seed portait 8,957 BTC au 13/09/2021 — c'est l'AGRÉGAT de
    # deux 8-K (24 août : 3,907 @ $45,294 ; 13 sept : 5,050 @ $48,099). Total
    # inchangé, mais l'achat d'août n'apparaissait nulle part. Scindé.
    ("2021-08-24", 3907, 177.0, 45294.0), ("2021-09-13", 5050, 242.9, 48099.0),
    ("2021-11-29", 7002, 414.4, 59183.09),
    ("2021-12-09", 1434, 82.4, 57461.65), ("2021-12-30", 1914, 94.2, 49216.30),
    ("2022-01-31", 660, 25.0, 37878.79), ("2022-04-05", 4167, 190.5, 45716.34),
    ("2022-06-28", 480, 10.0, 20833.33), ("2022-09-20", 301, 6.0, 19933.55),
    ("2022-12-21", 2395, 42.8, 17870.56), ("2022-12-22", -704, -11.8, 16761.36),
    ("2022-12-24", 810, 13.65, 16851.85), ("2023-03-27", 6455, 150.0, 23237.80),
    ("2023-04-05", 1045, 29.3, 28038.28), ("2023-06-27", 12333, 347.0, 28135.90),
    ("2023-07-31", 467, 14.4, 30835.12), ("2023-09-24", 5445, 147.3, 27052.34),
    ("2023-11-01", 155, 5.3, 34193.55), ("2023-11-30", 16130, 593.3, 36782.39),
    ("2023-12-27", 14620, 615.7, 42113.54), ("2024-02-06", 850, 37.2, 43764.71),
    ("2024-02-26", 3000, 155.0, 51666.67), ("2024-03-11", 12000, 821.7, 68475.00),
    ("2024-03-19", 9245, 623.0, 67387.78), ("2024-04-30", 164, 7.8, 47560.98),
    ("2024-06-20", 11931, 786.0, 65878.80), ("2024-08-01", 169, 11.4, 67455.62),
    ("2024-09-13", 18300, 1110.0, 60655.74), ("2024-09-20", 7420, 458.2, 61752.02),
    ("2024-11-11", 27200, 2000.0, 73529.41), ("2024-11-18", 51780, 4600.0, 88837.39),
    ("2024-11-25", 55500, 5400.0, 97297.30), ("2024-12-02", 15400, 1500.0, 97402.60),
    ("2024-12-09", 21550, 2100.0, 97447.80), ("2024-12-16", 15350, 1500.0, 97719.87),
    ("2024-12-23", 5262, 561.0, 106613.45), ("2024-12-30", 2138, 209.0, 97754.91),
    ("2025-01-06", 1070, 100.0, 93457.94), ("2025-01-13", 2530, 243.0, 96047.43),
    ("2025-01-21", 11000, 1100.0, 100000.00), ("2025-01-27", 10107, 1100.0, 108835.46),
    ("2025-02-10", 7633, 742.4, 97261.89), ("2025-02-24", 20356, 1990.0, 97759.87),
    ("2025-03-17", 130, 10.7, 82307.69), ("2025-03-24", 6911, 584.1, 84517.44),
    ("2025-03-31", 22048, 1920.0, 87082.73), ("2025-04-14", 3459, 285.8, 82625.04),
    ("2025-04-21", 6556, 555.8, 84777.30), ("2025-04-28", 15355, 1420.0, 92478.02),
    ("2025-05-05", 1895, 180.0, 94986.81), ("2025-05-12", 13390, 1340.0, 100074.68),
    ("2025-05-19", 7390, 764.9, 103504.74), ("2025-05-26", 4020, 427.1, 106243.78),
    ("2025-06-02", 705, 75.0, 106382.98),
    # 2026-07-26 : ACHAT MANQUANT (8-K du 09/06/2025, période 2-8 juin 2025).
    # Son absence expliquait à elle seule l'écart de 1 034 BTC entre le cumul
    # du seed et les holdings déclarés par Strategy.
    ("2025-06-09", 1045, 110.2, 105426.0),
    ("2025-06-16", 10100, 1051.0, 104059.41),
    ("2025-06-23", 245, 26.0, 106122.45), ("2025-06-30", 4980, 532.0, 106827.31),
    ("2025-07-14", 4225, 472.0, 111715.98), ("2025-07-21", 6220, 740.0, 118971.06),
    ("2025-07-29", 21021, 2465.0, 117263.69), ("2025-08-11", 155, 18.0, 116129.03),
    ("2025-08-18", 430, 51.0, 118604.65), ("2025-08-25", 3081, 357.0, 115871.47),
    ("2025-09-02", 4048, 449.0, 110918.97), ("2025-09-08", 1955, 217.0, 110997.44),
    ("2025-09-15", 525, 60.0, 114285.71), ("2025-09-22", 850, 100.0, 117647.06),
    ("2025-09-29", 196, 22.0, 112244.90), ("2025-10-13", 220, 27.0, 122727.27),
    ("2025-10-20", 168, 19.0, 113095.24), ("2025-10-27", 390, 43.0, 110256.41),
    ("2025-11-03", 397, 46.0, 115869.02), ("2025-11-10", 487, 50.0, 102669.40),
    ("2025-11-17", 8178, 836.0, 102225.48), ("2025-12-01", 130, 12.0, 92307.69),
    ("2025-12-08", 10624, 963.0, 90643.83), ("2025-12-15", 10645, 980.0, 92062.00),
    ("2025-12-29", 1229, 109.0, 88689.99), ("2025-12-31", 3, 0.0, None),
    ("2026-01-05", 1283, 116.0, 90413.09), ("2026-01-12", 13627, 1247.0, 91509.50),
    ("2026-01-20", 22305, 2125.0, 95270.12), ("2026-01-26", 2932, 264.0, 90040.93),
    ("2026-02-02", 855, 75.0, 87719.30), ("2026-02-09", 1142, 90.0, 78809.11),
    ("2026-02-17", 2486, 168.0, 67578.44), ("2026-02-23", 592, 40.0, 67567.57),
    ("2026-03-02", 3015, 204.0, 67661.69), ("2026-03-09", 17994, 1277.0, 70968.10),
    ("2026-03-16", 22337, 1568.0, 70197.43), ("2026-03-23", 1031, 77.0, 74684.77),
    ("2026-04-06", 4871, 330.0, 67747.90), ("2026-04-13", 13927, 1001.0, 71874.78),
    ("2026-04-20", 34164, 2540.0, 74347.27), ("2026-04-27", 3273, 255.0, 77910.17),
    ("2026-05-11", 535, 43.0, 80373.83), ("2026-05-18", 24869, 2014.0, 80984.36),
    # Ventes : montant ET usd_m négatifs. px = prix de vente moyen NET de frais
    # tel que publié (le seed calculait usd/amt, d'où un $62 500 faux ci-dessous
    # au lieu des $77 135 déclarés — corrigé le 2026-07-26).
    ("2026-06-01", -32, -2.5, 77135.0, "vente — 8-K du 1er juin 2026 (période 26-31 mai)"),
    ("2026-06-08", 1550, 101.3, 65332.0),
    ("2026-06-15", 1587, 100.0, 63024.0), ("2026-06-22", 520, 34.9, 67068.0),
    # Les deux ventes du programme « BTC Monetization » annoncé le 29/06/2026,
    # publiées dans le MÊME 8-K (6 juil.) : datées à la fin de leur période pour
    # rester distinctes sur le graphe.
    ("2026-06-30", -1363, -80.8, 59256.0, "vente — 8-K du 6 juil. 2026 (période 29-30 juin)"),
    ("2026-07-05", -2225, -135.2, 60773.0, "vente — 8-K du 6 juil. 2026 (période 1-5 juil.)"),
]
# Dernier 8-K « BTC Update » couvert par le seed. Au-delà, c'est le lecteur
# EDGAR (edgar_mstr_events) qui prend le relais tout seul.
MSTR_SEED_UNTIL = "2026-07-20"
MSTR_META = {
    "holdings": 843775, "holdings_asof": "2026-07-19",
    "avg_cost": 75476, "cost_total_usd": 63.69e9,
    # Strategy ne publie pas le tag XBRL dei:EntityCommonStockSharesOutstanding
    # (structure multi-classes) : le compte est reconstruit depuis la COUVERTURE
    # du 10-Q (classes A + B) puis incrémenté des actions vendues à l'ATM,
    # semaine par semaine, dans chaque 8-K. Le seed « 351,6 M est. stockanalysis »
    # sous-estimait de 8% (28 M d'actions émises entre le 10-Q et juillet 2026),
    # donc la mNAV d'autant.
    "shares_m": 350.45, "shares_asof": "2026-04-26",
    "shares_note": "10-Q du 6 mai 2026 (330 807 622 classe A + 19 640 250 classe B)",
    "cost_note": "coût total $63,69 Md (8-K du 20 juil. 2026)",
}

# BMNR : snapshots hebdo "ETH détenus" des 8-K (date as-of, pas date de filing).
BMNR_SNAPSHOTS = [
    ("2025-07-14", 163142), ("2025-07-17", 300657), ("2025-07-23", 566776),
    ("2025-07-28", 625000), ("2025-08-03", 833137), ("2025-08-10", 1150263),
    ("2025-08-17", 1523373), ("2025-08-24", 1713899), ("2025-08-27", 1792690),
    ("2025-08-31", 1866974), ("2025-09-07", 2069443), ("2025-09-14", 2151676),
    ("2025-09-21", 2416054), ("2025-09-28", 2650900), ("2025-10-05", 2830151),
    ("2025-10-12", 3032188), ("2025-10-19", 3236014), ("2025-10-26", 3313069),
    ("2025-11-02", 3395422), ("2025-11-09", 3505723), ("2025-11-16", 3559879),
    ("2025-11-23", 3629701), ("2025-11-30", 3726499), ("2025-12-07", 3864951),
    ("2025-12-14", 3967210), ("2025-12-21", 4066062), ("2025-12-28", 4110525),
    ("2026-01-04", 4143502), ("2026-01-11", 4167768), ("2026-01-19", 4203036),
    ("2026-01-25", 4243338), ("2026-02-01", 4285125), ("2026-02-08", 4325738),
    ("2026-02-16", 4371497), ("2026-02-22", 4422659), ("2026-03-01", 4473587),
    ("2026-03-08", 4534563), ("2026-03-15", 4595562), ("2026-03-22", 4660903),
    ("2026-03-29", 4732082), ("2026-04-05", 4803334), ("2026-04-12", 4874858),
    ("2026-04-19", 4976485), ("2026-04-26", 5078386), ("2026-05-03", 5180131),
    ("2026-05-10", 5206790), ("2026-05-17", 5278462), ("2026-05-25", 5390404),
    ("2026-05-31", 5416901), ("2026-06-07", 5543872),
    # 2026-07-26 : 6 snapshots ajoutés (8-K des 15, 22, 29 juin, 6, 13, 20 juil.).
    # Le 8-K du 6 juil. date son tableau « June 28 » alors que la ligne staking du
    # même communiqué dit July 5 et que 5,700,040 + 42,197 = 5,742,237 : c'est bien
    # le 5 juillet. Le parseur EDGAR applique la même correction automatiquement.
    ("2026-06-14", 5620754), ("2026-06-21", 5672956), ("2026-06-28", 5700040),
    ("2026-07-05", 5742237), ("2026-07-12", 5770038), ("2026-07-19", 5777468),
]
BMNR_SEED_UNTIL = "2026-07-20"
BMNR_META = {
    "holdings": 5777468, "holdings_asof": "2026-07-19",
    # Ancre comptable : 5 416 945 ETH pour 19 052 159 k$ au 2026-05-31 (10-Q du
    # 14 juil., ligne « Ether » — surtout PAS le total qui agrège BTC + autres).
    # Le coût des achats postérieurs est ajouté au spot par recompute_cost() :
    # figer 3 517 $ ferait dériver le coût moyen à chaque snapshot hebdo.
    "avg_cost": 3517, "cost_total_usd": 19.052e9,
    "cost_anchor": {"d": "2026-05-31", "units": 5416945, "usd_m": 19052.159},
    "shares_m": 603.23, "shares_asof": "2026-07-09",  # 10-Q du 14 juil. 2026
    "shares_note": "10-Q du 14 juil. 2026 : 603 226 394 actions au 9 juil.",
    "cost_note": "ancre 10-Q au 31 mai 2026 (ETH seul) + achats suivants au spot",
    "cost_estimated": True,
}

# Historique des actions en circulation, uniquement des chiffres SOURCÉS SEC.
# Sert (a) de base au compte courant, (b) à tracer le ratio tokens/action, la
# seule métrique qui dit si l'accumulation crée ou détruit de la valeur par
# action : PURR est passé de 0,151 HYPE/action (31/03) à 0,144 (14/05) avant de
# remonter à 0,146 (15/07) — toujours SOUS son niveau de mars malgré +56% de
# tokens. Un graphe de holdings seul flatte mécaniquement la société.
SHARES_HIST = {
    "mstr": [("2026-04-26", 350447872, "10-Q du 6 mai 2026 (classes A+B)")],
    "bmnr": [("2026-05-31", 579652432, "10-Q : actions émises au 31 mai 2026"),
             ("2026-07-09", 603226394, "couverture du 10-Q du 14 juil. 2026")],
    "purr": [("2026-03-31", 124220108, "10-Q du 8 mai 2026"),
             ("2026-05-19", 144447571, "couverture du S-1 du 22 mai 2026"),
             ("2026-06-15", 196553055, "S-1/A du 16 juin 2026"),
             ("2026-07-15", 200563691, "S-1/A du 21 juil. 2026")],
}

# PURR : roll-forward EXACT du 10-Q au 2026-03-31 (units + dollars), puis
# soldes déclarés dans les prospectus. `kind` distingue ce qui mesure la QUALITÉ
# D'EXÉCUTION du management (buy/sell) de ce qui n'en dit rien : un apport en
# nature d'investisseurs (contribution) et des récompenses de staking (staking)
# ne sont pas du capital déployé sur le marché — le front les marque autrement
# et les exclut de l'échelle de dimensionnement des bulles.
# est=1 -> montant $ estimé (prix d'exécution non publié) ; `avg_from` = borne
# basse de la fenêtre d'accumulation, le coût est alors estimé au cours MOYEN de
# la période et non au close du jour de publication.
PURR_EVENTS = [
    {"d": "2025-12-02", "amt": 12517592, "usd_m": 580.466, "px": 46.37, "kind": "contribution",
     "note": "apport en nature au closing (Rorschach) — 10-Q : 12 517 592 HYPE inscrits "
             "à 580,5 M$, soit 46,37 $/HYPE (valeur au Commitment Date du 11 juil. 2025)"},
    {"d": "2025-12-31", "amt": 321224, "usd_m": 9.000, "px": 28.02, "kind": "buy",
     "note": "achats du T2 FY26 — 10-Q : 321 224 HYPE pour 9,0 M$"},
    {"d": "2025-12-31", "amt": 18717, "usd_m": 0.500, "kind": "staking",
     "note": "récompenses de staking du T2 FY26 — 10-Q : 0,5 M$ (solde 12 857 533)"},
    {"d": "2026-02-03", "amt": 5000000, "usd_m": 129.700, "px": 25.94, "kind": "buy",
     "note": "5,0 M HYPE au prix moyen DÉCLARÉ de 25,94 $ — deck du 8-K du 11 fév. 2026 "
             "(situation au 3 fév.), soit 20% sous le cours du jour : c'est le prix moyen "
             "de TOUT un trimestre d'achats, pas celui du 3 février."},
    {"d": "2026-03-31", "amt": 884940, "usd_m": 30.673, "px": 34.66, "kind": "buy",
     "note": "solde des achats du T3 FY26 — 10-Q : 5 884 940 HYPE pour 160,4 M$ sur le trimestre"},
    {"d": "2026-03-31", "amt": 83882, "usd_m": 2.626, "kind": "staking",
     "note": "staking + divers du T3 FY26 — 10-Q : 2,6 M$ (solde 18 826 355)"},
    {"d": "2026-04-29", "amt": 1173645, "est": 1, "kind": "buy", "avg_from": "2026-03-31",
     "note": "8-K du 7 mai : trésor porté à 20,0 M HYPE. 216,0 M$ déployés depuis l'origine "
             "pour ~7,3 M HYPE, prix moyen déclaré 29,53 $"},
    {"d": "2026-05-14", "amt": 800000, "est": 1, "kind": "buy", "avg_from": "2026-04-29",
     "note": "S-1 du 22 mai : trésor 20,8 M HYPE au 14 mai 2026"},
    {"d": "2026-07-15", "amt": 8500000, "est": 1, "kind": "buy", "avg_from": "2026-05-14",
     "note": "S-1/A du 21 juil. : trésor 29,3 M HYPE au 15 juil. — financé par l'ELOC Chardan "
             "(76,1 M d'actions émises, 647 M$ nets). Répartition intra-période NON publiée : "
             "coût estimé au cours moyen de la fenêtre, détail attendu dans le 10-Q du T4"},
]
# Contrôle en dur : le roll-forward doit retomber sur le coût de revient publié.
PURR_COST_CHECK = ("2026-03-31", 18826355, 752.966)   # units, M$ (10-Q au 31/03/2026)
PURR_SEED_UNTIL = "2026-07-21"
PURR_META = {
    "holdings": 29300000, "holdings_asof": "2026-07-15",
    "holdings_note": "S-1/A du 21 juil. 2026 — « approximately 29.3 million HYPE tokens » "
                     "au 15 juil. (21,2 M stakés au validateur maison + 8,1 M chez Anchorage)",
    # avg_cost / cost_total sont RECALCULÉS depuis les événements (voir
    # recompute_cost) : un chiffre figé ici redeviendrait faux au prochain achat.
    "avg_cost": None, "cost_total_usd": None,
    "shares_m": 200.56, "shares_asof": "2026-07-15",
    "shares_note": "S-1/A du 21 juil. 2026 : 200 563 691 actions",
    "cost_estimated": True,
}


# ── HTTP helpers ───────────────────────────────────────────────────────────
def yahoo_daily(ticker, start_epoch, retries=3):
    """Yahoo chart v8 daily closes via curl_cffi impersonate chrome120.
    Retourne [(date 'YYYY-MM-DD', close), ...] ou None."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.replace('^', '%5E')}"
    params = f"?interval=1d&period1={start_epoch}&period2={int(time.time())}"
    for attempt in range(retries):
        try:
            r = cr.get(url + params, impersonate="chrome120", timeout=30)
            if r.status_code == 200:
                res = (r.json().get("chart", {}).get("result") or [None])[0]
                if not res:
                    return None
                ts = res.get("timestamp") or []
                closes = res.get("indicators", {}).get("quote", [{}])[0].get("close") or []
                out, seen = [], set()
                for t, c in zip(ts, closes):
                    if c is None:
                        continue
                    d = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d")
                    if d in seen:  # garde le dernier print du jour (close live)
                        out[-1] = (d, round(float(c), 4))
                        continue
                    seen.add(d)
                    out.append((d, round(float(c), 4)))
                return out or None
            if r.status_code in (404, 422):
                return None
        except Exception as e:
            sys.stderr.write(f"[treasury] {ticker}: err {e} (try {attempt + 1})\n")
        time.sleep(1.6 ** attempt)
    return None


# ── Lecteur EDGAR (source primaire des mouvements) ─────────────────────────
# La SEC exige un User-Agent nominatif ; sans lui elle renvoie 403.
EDGAR_UA = {"User-Agent": os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")}
EDGAR_CACHE = os.path.join(CACHE_DIR, "treasury_edgar_cache.json")
# Bump = cache jeté et refabriqué. À incrémenter DÈS QU'UN PARSEUR CHANGE, sinon
# le correctif ne s'applique qu'aux filings futurs et le bug reste à l'écran.
EDGAR_CACHE_VERSION = 2
# Fenêtre re-parsée à chaque run. Elle DÉBORDE volontairement sur le seed :
# les filings déjà seedés servent d'auto-test au parseur (cf. selftest).
EDGAR_LOOKBACK_DAYS = 80
# Retard typique de CoinGecko sur la SEC (constaté : 1 j pour BMNR le 27/07/2026,
# « quelques jours » selon la doc de l'endpoint). En deçà, un écart CoinGecko/SEC
# n'est PAS un mouvement, c'est ce retard — le prendre pour un mouvement fabrique
# une vente fantôme du montant exact du dernier achat (cf. cg_net).
CG_LAG_DAYS = 4
CIK = {"mstr": "1050446", "bmnr": "1829311", "purr": "2078856"}
# Formes lues par société. PURR n'annonce PAS ses achats par 8-K : se limiter au
# 8-K, c'était rater 9,3 M HYPE (+47% du trésor) entre avril et juillet 2026.
FORMS = {
    "mstr": ("8-K", "10-Q", "10-K"),
    "bmnr": ("8-K", "10-Q", "10-K"),
    "purr": ("8-K", "10-Q", "10-K", "S-1", "S-1/A"),
}


def edgar_get(url, timeout=45, retries=3):
    for attempt in range(retries):
        try:
            r = cr.get(url, headers=EDGAR_UA, impersonate="chrome120", timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except Exception as e:
            sys.stderr.write(f"[treasury] EDGAR {url[-40:]}: {e} (try {attempt + 1})\n")
        time.sleep(1.2 * (attempt + 1))
    return None


def edgar_flat(txt):
    """HTML -> texte plat sur une ligne (les tableaux 8-K deviennent des suites
    de nombres, ce que les parseurs ci-dessous exploitent)."""
    import html as _html
    import re as _re
    t = _re.sub(r"<[^>]+>", " ", txt)
    t = _html.unescape(t)
    return _re.sub(r"\s+", " ", t)


def edgar_recent(cik, since, forms=("8-K",)):
    """[(filed, form, accession, primary_doc)] des filings déposés depuis `since`."""
    txt = edgar_get(f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json")
    if not txt:
        return None
    try:
        rec = json.loads(txt)["filings"]["recent"]
    except Exception:
        return None
    out = []
    for fd, form, acc, doc in zip(rec["filingDate"], rec["form"],
                                  rec["accessionNumber"], rec["primaryDocument"]):
        if form in forms and fd >= since:
            out.append((fd, form, acc, doc))
    out.sort()
    return out


def edgar_doc_text(cik, acc, doc, form="8-K"):
    """Texte du filing : document principal, puis soumission complète si le
    contenu utile est dans un exhibit (cas BitMine : communiqué en EX-99.1 ;
    cas PURR : deck investisseurs en EX-99.2). Le repli ne concerne QUE les 8-K —
    un 10-Q ou un S-1 est autoportant et sa soumission complète pèse plusieurs
    Mo pour rien."""
    a = acc.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{a}/"
    t = edgar_flat(edgar_get(base + doc) or "") if doc else ""
    if form == "8-K" and not _has_payload(t):
        full = edgar_get(base + acc + ".txt", timeout=90)
        if full:
            t = edgar_flat(full)
    return t


def _has_payload(t):
    return bool(t) and ("BTC Update" in t or "BTC Acquired" in t or "BTC Sold" in t
                        or "crypto holdings are comprised of" in t or "HYPE" in t)


def _n(s):
    return float(str(s).replace(",", "").replace("$", "").strip())


def edgar_mstr_events(text, filed):
    """Mouvements BTC d'un 8-K Strategy.

    Format stable depuis 2025 : « During Period <p1> to <p2> ... BTC Acquired|
    BTC Sold ... <montant> $<total M> $<prix moyen> ... <holdings> ». Un même
    8-K peut porter PLUSIEURS périodes (ex. 6 juil. 2026 : deux ventes) — d'où
    le découpage en blocs. Les blocs trimestriels (période > 20 jours) sont
    ignorés : ce sont des agrégats qui feraient double emploi.
    """
    import re as _re
    events = []
    if not text:
        return events
    # Chaque bloc commence à « During Period » et court jusqu'au suivant.
    marks = [m.start() for m in _re.finditer(r"During Period", text)]
    for i, s in enumerate(marks):
        chunk = text[s:(marks[i + 1] if i + 1 < len(marks) else s + 1600)]
        mp = _re.match(r"During Period\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+(?:to|and|through)\s+"
                       r"([A-Z][a-z]+ \d{1,2}, \d{4})", chunk)
        if not mp:
            continue
        try:
            d1 = datetime.strptime(mp.group(1), "%B %d, %Y")
            d2 = datetime.strptime(mp.group(2), "%B %d, %Y")
        except ValueError:
            continue
        if (d2 - d1).days > 20:      # agrégat trimestriel -> déjà couvert semaine par semaine
            continue
        sold = chunk.find("BTC Sold")
        acq = chunk.find("BTC Acquired")
        if sold < 0 and acq < 0:
            continue
        is_sale = sold >= 0 and (acq < 0 or sold < acq)
        head = "BTC Sold" if is_sale else "BTC Acquired"
        tail = chunk[chunk.find(head) + len(head):]
        # Où commence la ligne de données ? Pour un ACHAT, les 6 colonnes
        # (opération + holdings) partagent un seul en-tête : la ligne suit le
        # DERNIER « Average Purchase Price ». Pour une VENTE, le tableau des
        # holdings est un second bloc avec son propre en-tête « Average Purchase
        # Price » — il faut donc s'ancrer sur le PREMIER « Average Sale Price »,
        # sinon on lit les holdings à la place du montant vendu (bug attrapé par
        # l'auto-test le 2026-07-26 : « vente de 846 000 BTC »).
        if is_sale:
            mh = _re.search(r"Average Sale Price", tail)
        else:
            mh = None
            for mh_ in _re.finditer(r"Average Purchase Price", tail):
                mh = mh_
        if not mh:
            continue
        data = _re.sub(r"\(\s*\d\s*\)", " ", tail[mh.end():])   # retire les appels de note (1)(2)
        # Nombre = commence par un chiffre (jamais une virgule seule) ; tiret
        # isolé = zéro (semaine sans achat : « - $- $- 843,775 … »), mais un
        # tiret collé à des lettres (at-the-market) n'est pas un jeton.
        toks = _re.findall(r"\d[\d,]*(?:\.\d+)?|(?<![A-Za-z0-9])[—–-](?![A-Za-z0-9])", data)
        vals = []
        for tk in toks[:8]:
            vals.append(0.0 if tk in ("—", "–", "-") else _n(tk))
        if len(vals) < 3:
            continue
        amt, usd_m, px = vals[0], vals[1], vals[2]
        # « Aggregate Purchase Price (in billions) » plus loin dans la ligne : le
        # montant de l'opération, lui, est en millions. Un achat hebdo à 2,46
        # signifie 2,46 Md$ (colonne en milliards mal alignée) -> on requalifie.
        if amt and usd_m and px and usd_m * 1e6 / max(amt, 1) < px / 3:
            usd_m *= 1000
        d = d2.strftime("%Y-%m-%d")
        # Trésor déclaré : entre l'en-tête et le nombre s'intercalent d'autres
        # libellés de colonnes AVEC des appels de note « (2) ». Il faut donc les
        # retirer d'abord, sinon aucun trésor n'est jamais relu (bug attrapé par
        # test_treasury_live.py le 2026-07-26).
        clean = _re.sub(r"\(\s*\d\s*\)", " ", chunk)
        hold = None
        mhold = _re.search(r"Aggregate BTC Holdings.{0,200}?(?<![\d,])(\d{3},\d{3})(?![\d,])", clean)
        if mhold:
            hold = _n(mhold.group(1))
        if not amt:
            events.append({"d": d, "amt": 0, "hold": hold, "filed": filed})
            continue
        events.append({
            "d": d, "amt": -amt if is_sale else amt,
            "usd_m": round(-usd_m if is_sale else usd_m, 2),
            "px": round(px, 2), "hold": hold, "filed": filed,
            "note": ("vente — 8-K du " + filed if is_sale else None),
        })
    return events


def edgar_bmnr_snapshots(text, filed):
    """Snapshots ETH d'un 8-K BitMine (communiqué hebdomadaire)."""
    import re as _re
    out = []
    if not text:
        return out
    for m in _re.finditer(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})[^.]{0,80}?crypto holdings are "
                          r"comprised of ([\d,]+) ETH", text):
        try:
            d = datetime.strptime(m.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            continue
        out.append({"d": d, "total": _n(m.group(2)), "filed": filed})
    macq = _re.search(r"we acquired ([\d,]+) ETH", text)
    if out and macq:
        out[-1]["acquired"] = _n(macq.group(1))
    return out


def edgar_purr_snapshots(text, filed):
    """Trésor HYPE + actions en circulation de Hyperliquid Strategies.

    Élargi le 2026-07-26 aux 10-Q / S-1 / S-1-A : PURR n'annonce pas ses achats
    par 8-K (l'ELOC Chardan tourne en continu), le trésor n'est chiffré que dans
    les prospectus et les rapports trimestriels. Trois formulations couvertes,
    toutes vues en vrai dans les filings 2025-2026 :
      « Materially increased treasury to 20.0 million HYPE tokens (as of April 29, 2026) »
      « As of July 15, 2026, the Company holds approximately 29.3 million HYPE tokens »
      « Balance, March 31, 2026 18,826,355 » (roll-forward du 10-Q)
    Les actions sont lues au même passage : la mNAV dépend linéairement du compte
    d'actions et PURR en a émis 76 M sur l'ELOC en 7 mois (+61%).
    """
    import re as _re
    out = []
    if not text:
        return out
    seen = set()

    def add(d, total, src):
        key = (d, round(total))
        if key in seen:
            return
        seen.add(key)
        out.append({"d": d, "total": total, "filed": filed, "src": src})

    def dparse(s):
        try:
            return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    # a) « treasury to 20.0 million HYPE tokens (as of April 29, 2026) »
    for m in _re.finditer(r"treasury to ([\d.]+) million HYPE tokens\s*\(?\s*as of "
                          r"([A-Z][a-z]+ \d{1,2}, \d{4})", text):
        d = dparse(m.group(2))
        if d:
            add(d, float(m.group(1)) * 1e6, "communiqué")
    # b) « As of July 15, 2026, the Company holds approximately 29.3 million HYPE »
    #    (S-1/A). Le « approximately » est celui de l'émetteur, pas le nôtre.
    for m in _re.finditer(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}), the Company hold[s]?"
                          r"[^.]{0,60}?([\d.]+) million HYPE tokens", text):
        d = dparse(m.group(1))
        if d:
            add(d, float(m.group(2)) * 1e6, "prospectus")
    # c) « holdings of 29,300,000 HYPE tokens as of ... » (formulation exacte)
    for m in _re.finditer(r"holdings of ([\d,]{9,}) HYPE tokens as of "
                          r"([A-Z][a-z]+ \d{1,2}, \d{4})", text):
        d = dparse(m.group(2))
        if d:
            add(d, _n(m.group(1)), "communiqué")
    # d) roll-forward du 10-Q : « Balance, March 31, 2026 18,826,355 » — c'est le
    #    seul chiffre AUDITÉ, il prime sur les arrondis en millions ci-dessus.
    for m in _re.finditer(r"Balance,\s+([A-Z][a-z]+ \d{1,2}, \d{4})\s+([\d,]{9,})(?![\d,])", text):
        d = dparse(m.group(1))
        if d:
            add(d, _n(m.group(2)), "10-Q (audité)")
    # e) actions en circulation (couverture prospectus / 10-Q)
    for m in _re.finditer(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4}),? there (?:were|are) "
                          r"([\d,]{9,}) shares of (?:the )?Company Common Stock", text):
        d = dparse(m.group(1))
        if d:
            out.append({"d": d, "shares": _n(m.group(2)), "filed": filed})
    return out


# Actions vendues à l'ATM, semaine par semaine (tableau « ATM Update » des 8-K
# Strategy). Sans ça le compte d'actions reste celui du dernier 10-Q et la mNAV
# se dégrade de plusieurs % par trimestre : 28 M d'actions émises entre le 10-Q
# du 6 mai et le 8-K du 20 juil. 2026, soit +8%.
def edgar_atm_shares(text):
    """(shares_sold, as_of) du tableau ATM d'un 8-K Strategy, ou (None, None)."""
    import re as _re
    if not text or "ATM Update" not in text:
        return None, None
    seg = text[text.find("ATM Update"):]
    md = _re.search(r"As of ([A-Z][a-z]+ \d{1,2}, \d{4})", seg[:600])
    asof = None
    if md:
        try:
            asof = datetime.strptime(md.group(1), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            asof = None
    # « MSTR Stock 2,732,318 $ - $ 263.5 … » — un tiret = aucune action vendue.
    m = _re.search(r"MSTR Stock\s+([\d,]+|[—–-])\s*\$", seg)
    if not m:
        return None, asof
    tk = m.group(1)
    return (0.0 if tk in ("—", "–", "-") else _n(tk)), asof


PARSERS = {"mstr": edgar_mstr_events, "bmnr": edgar_bmnr_snapshots, "purr": edgar_purr_snapshots}


def edgar_scan(since_by_company):
    """Parcourt les 8-K récents des 3 sociétés et renvoie le cache enrichi.

    Robustesse : un filing déjà parsé n'est jamais retéléchargé (cache disque
    par numéro d'accession), et si la SEC est injoignable on renvoie le cache
    tel quel — on ne perd jamais un mouvement déjà connu.
    """
    cache = {}
    if os.path.exists(EDGAR_CACHE):
        try:
            with open(EDGAR_CACHE, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    if (cache.get("_meta") or {}).get("version") != EDGAR_CACHE_VERSION:
        # Parseur modifié : on repart de zéro, sinon un correctif ne s'applique
        # jamais aux filings déjà en cache (et le bug reste visible à l'écran).
        sys.stderr.write("[treasury] cache EDGAR d'une version antérieure — reconstruction\n")
        cache = {}
    meta = cache.get("_meta") or {}
    fetched = errors = 0
    for cid, since in since_by_company.items():
        cache.setdefault(cid, {})
        filings = edgar_recent(CIK[cid], since, FORMS.get(cid, ("8-K",)))
        if filings is None:
            errors += 1
            sys.stderr.write(f"[treasury] EDGAR {cid}: liste des filings inaccessible — cache conservé\n")
            continue
        meta[cid + "_last_filing"] = filings[-1][0] if filings else None
        for fd, form, acc, doc in filings:
            if acc in cache[cid]:
                continue
            txt = edgar_doc_text(CIK[cid], acc, doc, form)
            if txt is None:
                errors += 1
                continue
            try:
                items = PARSERS[cid](txt, fd)
            except Exception as e:
                sys.stderr.write(f"[treasury] EDGAR parse {cid} {acc}: {e}\n")
                errors += 1
                continue
            blob = {"filed": fd, "form": form, "items": items}
            if cid == "mstr":
                atm, asof = edgar_atm_shares(txt)
                if atm is not None:
                    blob["atm"] = {"shares": atm, "asof": asof}
            cache[cid][acc] = blob
            fetched += 1
            time.sleep(0.25)   # < 10 req/s, limite SEC
    meta["last_scan"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta["errors"] = errors
    meta["version"] = EDGAR_CACHE_VERSION
    cache["_meta"] = meta
    try:
        tmp = EDGAR_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, EDGAR_CACHE)   # écriture atomique
    except Exception as e:
        sys.stderr.write(f"[treasury] EDGAR cache non écrit : {e}\n")
    sys.stderr.write(f"[treasury] EDGAR scan : {fetched} nouveaux 8-K, {errors} erreur(s)\n")
    return cache


def sec_shares(cik):
    """Nombre d'actions en circulation déclaré en couverture du dernier 10-Q/10-K.

    La mNAV en dépend directement : figée sur un seed, elle dérive de plusieurs
    % par trimestre (BMNR était compté 537,6 M actions contre 603,2 M réelles au
    2026-07-09, soit une mNAV sous-évaluée de 12%). Retourne (millions, date) ou
    None si la société ne publie pas ce tag (cas de Strategy, multi-classes).
    """
    txt = edgar_get(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik.zfill(10)}"
                    "/dei/EntityCommonStockSharesOutstanding.json")
    if not txt:
        return None
    try:
        units = json.loads(txt).get("units", {})
        vals = []
        for arr in units.values():
            vals.extend(arr)
        vals = [v for v in vals if v.get("val") and v.get("end")]
        if not vals:
            return None
        best = max(vals, key=lambda v: (v.get("end"), v.get("filed") or ""))
        return round(float(best["val"]) / 1e6, 2), best["end"]
    except Exception:
        return None


def ddays(a, b):
    return (datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days


def match_event(rows, d, amt, tol_days=4):
    """Retrouve un mouvement déjà connu malgré la double convention de date.

    Le seed historique date les opérations au JOUR DE L'ANNONCE (le 8-K du
    lundi), le parseur EDGAR à la FIN DE LA PÉRIODE couverte (le dimanche) :
    sans cette tolérance, chaque achat serait compté deux fois.
    """
    for r in rows:
        if abs(ddays(r["d"], d)) <= tol_days and abs(r["amt"] - amt) <= max(1, abs(amt) * 0.001):
            return r
    return None


def edgar_items(cache, cid):
    """Tous les items parsés d'une société, triés par date."""
    out = []
    for acc, blob in (cache.get(cid) or {}).items():
        for it in blob.get("items") or []:
            out.append(it)
    out.sort(key=lambda x: (x.get("d") or "", x.get("filed") or ""))
    return out


def coingecko(path, retries=2):
    url = "https://api.coingecko.com/api/v3" + path
    for attempt in range(retries):
        try:
            r = cr.get(url, impersonate="chrome120", timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(15)
        except Exception as e:
            sys.stderr.write(f"[treasury] CG {path}: err {e}\n")
            time.sleep(3)
    return None


def hype_from_coingecko():
    """Fallback prix HYPE si Yahoo HYPE32196-USD échoue (CG free = 365j max)."""
    j = coingecko("/coins/hyperliquid/market_chart?vs_currency=usd&days=365&interval=daily")
    if not j or not j.get("prices"):
        return None
    out, seen = [], set()
    for ms, px in j["prices"]:
        d = datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")
        if d in seen:
            out[-1] = (d, round(float(px), 4))
            continue
        seen.add(d)
        out.append((d, round(float(px), 4)))
    return out or None


def px_at(series, date_str):
    """Dernier close <= date_str (series triée)."""
    best = None
    for d, px in series:
        if d <= date_str:
            best = px
        else:
            break
    return best


def mean_px(series, d0, d1):
    """Cours moyen sur ]d0, d1]. Utilisé quand une accumulation est connue par
    son SOLDE de fin de période et non achat par achat : la poser au close du
    jour de publication invente un prix d'exécution (l'écart atteignait 27% sur
    l'achat PURR de février), alors que la moyenne de la fenêtre est un estimé
    honnête, signalé comme tel."""
    vals = [p for d, p in series if d0 < d <= d1]
    if not vals:
        return px_at(series, d1)
    return sum(vals) / len(vals)


def epoch(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())


def days_before(date_str, n):
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d")


def merge_mstr(events, audit, seed=None, meta=None):
    """Seed MSTR + mouvements lus dans les 8-K -> série complète + méta à jour.

    Isolée de main() pour être testable sans réseau (cf. test_treasury_live.py).
    Tout mouvement publié et absent du seed est AJOUTÉ, achat comme vente — le
    seed n'est qu'un point de départ, la SEC fait foi.
    """
    rows = []
    for row in (seed if seed is not None else MSTR_PURCHASES):
        d, a, u, p = row[:4]
        rec = {"d": d, "amt": a, "usd_m": u, "px": p, "kind": "sell" if a < 0 else "buy"}
        if len(row) > 4 and row[4]:
            rec["note"] = row[4]
        rows.append(rec)
    m = dict(meta if meta is not None else MSTR_META)
    for ev in events:
        # Le trésor déclaré se relit même les semaines SANS opération : c'est lui
        # qui fait autorité (et qui garde « trésor X BTC » à jour sur l'Accueil).
        if ev.get("hold") and ev["d"] >= m["holdings_asof"]:
            m["holdings"] = round(ev["hold"])
            m["holdings_asof"] = ev["d"]
        if not ev.get("amt"):
            continue
        if match_event(rows, ev["d"], ev["amt"]):
            continue
        rows.append({"d": ev["d"], "amt": ev["amt"], "usd_m": ev.get("usd_m"),
                     "px": ev.get("px"), "kind": "sell" if ev["amt"] < 0 else "buy",
                     "note": ev.get("note") or ("8-K du " + ev["filed"])})
        audit["added"].append(f"MSTR {ev['d']} {ev['amt']:+} BTC (8-K {ev['filed']})")
        if ev["d"] <= MSTR_SEED_UNTIL:
            msg = f"MSTR {ev['d']} {ev['amt']:+} BTC absent du seed (8-K du {ev['filed']}) — ajouté"
            audit["selftest"].append(msg)
            sys.stderr.write("[treasury] TROU DANS LE SEED — " + msg + "\n")
    rows.sort(key=lambda r: r["d"])
    return rows, m


def recompute_cost(rows, meta, cid, audit):
    """Coût de revient total + coût moyen, recalculés depuis les mouvements.

    Pourquoi ne pas garder le chiffre publié tel quel : il vieillit. BMNR publie
    son coût au 31 mai, achète 360 000 ETH depuis ; PURR n'a plus rien publié
    depuis avril. Un `avg_cost` figé fait dériver la ligne dorée du graphe ET le
    PnL latent. On garde donc l'ancre comptable auditée et on n'ajoute au spot
    QUE les mouvements postérieurs.
    - MSTR : Strategy publie « Average Purchase Price » à chaque 8-K, net des
      ventes — c'est plus juste que tout recalcul, on n'y touche pas.
    - Retourne aussi avg_cost_buys : coût moyen des seuls ACHATS (hors apport en
      nature, hors staking), le chiffre qui juge l'exécution du management.
    """
    anchor = meta.get("cost_anchor")
    if anchor:
        # Ancre auditée + achats postérieurs uniquement.
        usd = anchor["usd_m"]
        units = anchor["units"]
        for r in rows:
            if r["d"] > anchor["d"] and r.get("usd_m"):
                usd += r["usd_m"]
                units += r["amt"]
    else:
        usd = sum(r["usd_m"] for r in rows if r.get("usd_m"))
        units = sum(r["amt"] for r in rows if r.get("usd_m"))
    if units > 0 and meta.get("avg_cost") is None:
        meta["avg_cost"] = round(usd * 1e6 / units, 2)
        meta["cost_total_usd"] = round(usd * 1e6)
    elif units > 0 and anchor:
        meta["avg_cost"] = round(usd * 1e6 / units, 2)
        meta["cost_total_usd"] = round(usd * 1e6)
    # Achats seuls : ce que le management a réellement décidé de payer.
    bu = [r for r in rows if r.get("usd_m") and r.get("kind", "buy") in ("buy", "sell")
          and r["amt"] > 0]
    if bu:
        meta["avg_cost_buys"] = round(sum(r["usd_m"] for r in bu) * 1e6
                                      / sum(r["amt"] for r in bu), 2)
        meta["buys_total_usd"] = round(sum(r["usd_m"] for r in bu) * 1e6)
        meta["buys_units"] = round(sum(r["amt"] for r in bu))
    # Part de l'estimé dans le coût total : l'utilisateur doit savoir si la
    # ligne dorée est un fait publié ou une reconstitution. Avec une ancre
    # comptable, seul le POST-ancre est estimé (sinon on afficherait « 100%
    # estimé » pour BitMine alors que 97% du coût sort du 10-Q).
    if anchor:
        est_usd = sum(abs(r.get("usd_m") or 0) for r in rows if r["d"] > anchor["d"])
        tot_usd = usd
    else:
        est_usd = sum(abs(r.get("usd_m") or 0) for r in rows if r.get("est"))
        tot_usd = sum(abs(r.get("usd_m") or 0) for r in rows)
    meta["cost_est_share_pct"] = round(est_usd / tot_usd * 100, 1) if tot_usd else None
    audit["cost"][cid] = {"avg": meta.get("avg_cost"), "avg_buys": meta.get("avg_cost_buys"),
                          "est_pct": meta["cost_est_share_pct"]}
    return meta


def shares_series(cid, edgar, meta, audit):
    """Actions en circulation : historique sourcé SEC + ATM courant.

    La mNAV est proportionnelle à ce nombre : le laisser sur un seed, c'est
    publier une mNAV fausse. Trois apports :
      1. SHARES_HIST — couvertures 10-Q / prospectus (chiffres exacts).
      2. le tag XBRL dei:EntityCommonStockSharesOutstanding s'il est plus récent.
      3. MSTR seulement : + les actions vendues à l'ATM chaque semaine (8-K),
         car Strategy ne publie pas le tag et émet en continu.
    Retourne (hist, shares_m, asof, note).
    """
    hist = [{"d": d, "shares": n, "src": src} for d, n, src in SHARES_HIST.get(cid, [])]
    # 2. XBRL (BMNR/PURR le publient ; MSTR non — multi-classes)
    sh = sec_shares(CIK[cid])
    if sh and hist and 0.5 < sh[0] * 1e6 / hist[-1]["shares"] < 2.0 and sh[1] > hist[-1]["d"]:
        hist.append({"d": sh[1], "shares": round(sh[0] * 1e6), "src": "XBRL dei (10-Q/10-K)"})
    # 1b. actions lues dans les prospectus par le parseur (PURR)
    for it in edgar_items(edgar, cid):
        if it.get("shares") and not any(h["d"] == it["d"] for h in hist):
            hist.append({"d": it["d"], "shares": round(it["shares"]),
                         "src": "prospectus (" + it["filed"] + ")"})
    hist.sort(key=lambda h: h["d"])
    base = hist[-1] if hist else None
    if not base:
        return [], meta["shares_m"], meta.get("shares_asof"), meta.get("shares_note")
    shares, asof, note = base["shares"], base["d"], base["src"]
    # 3. ATM MSTR : cumul des actions vendues APRÈS la date de référence.
    if cid == "mstr":
        added, last = 0.0, None
        for acc, blob in (edgar.get(cid) or {}).items():
            atm = blob.get("atm")
            if not atm or not atm.get("shares") or not atm.get("asof"):
                continue
            if atm["asof"] > asof:
                added += atm["shares"]
                last = max(last or "", atm["asof"])
        if added:
            shares += added
            audit["added"].append(
                f"MSTR actions {base['shares'] / 1e6:.2f} -> {shares / 1e6:.2f} M "
                f"(+{added / 1e6:.2f} M vendues à l'ATM jusqu'au {last})")
            note = f"{base['src']} + {added / 1e6:.1f} M d'actions vendues à l'ATM (8-K)"
            hist.append({"d": last, "shares": round(shares), "src": "ATM 8-K cumulé"})
            asof = last
    if abs(shares / 1e6 / meta["shares_m"] - 1) > 0.02:
        audit["added"].append(f"{cid.upper()} actions seed {meta['shares_m']} M -> "
                              f"{shares / 1e6:.2f} M ({asof})")
    return hist, round(shares / 1e6, 2), asof, note


def hypestrat_holdings():
    """Trésor HYPE publié par Hyperliquid Strategies sur son site IR.

    Le S-1/A du 21 juil. 2026 désigne explicitement cette source : « The
    Company's HYPE balance is published on its website (hypestrat.xyz), updated
    weekly with a one-week data delay. » C'est la SEULE source infra-
    trimestrielle de PURR — sans elle, le site affichait 20,0 M HYPE pendant
    trois mois alors que la société en détenait 26 puis 29 M.
    Garde-fous : on n'accepte le chiffre que s'il est daté, cohérent en ordre de
    grandeur, et il ne prime JAMAIS sur un dépôt SEC plus récent.
    Retourne (date, tokens) ou None.
    """
    import re as _re
    try:
        r = cr.get("https://hypestrat.xyz/data/dashboard.json", impersonate="chrome120", timeout=25)
        if r.status_code != 200:
            return None
        j = r.json()
    except Exception as e:
        sys.stderr.write(f"[treasury] hypestrat.xyz: {e}\n")
        return None
    held = j.get("hypeHeld")
    if not held or not (1 < float(held) < 200):     # exprimé en MILLIONS
        return None
    # « HSI admin-published inputs (as of 06/04/2026) … » -> date de validité ;
    # à défaut _generated (date de publication).
    d = None
    m = _re.search(r"as of (\d{2})/(\d{2})/(\d{4})", str(j.get("_source") or ""))
    if m:
        d = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
    elif j.get("_generated"):
        d = str(j["_generated"])[:10]
    if not d or not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return None
    return d, round(float(held) * 1e6)


def _price_window(rec, hype, d0):
    """Chiffre un mouvement dont le prix d'exécution n'est pas publié, au cours
    MOYEN de la fenêtre ]d0, rec.d]. Au close du jour de dépôt on inventerait un
    prix : l'écart atteignait 27% sur l'achat PURR de février 2026."""
    spot = mean_px(hype, d0, rec["d"]) if d0 else px_at(hype, rec["d"])
    rec["usd_m"] = round(rec["amt"] * spot / 1e6, 1) if spot else None
    rec["px"] = round(spot, 2) if spot else None
    # La fenêtre part au FRONT : un prix moyen de période posé sur la seule date
    # de publication tombe à côté de la courbe sans que rien ne l'explique. Le
    # graphe centre la bulle sur ]w0, d] et trace la fenêtre — encore faut-il
    # qu'il la connaisse, la redeviner côté JS serait une reconstitution.
    if d0 and d0 < rec["d"]:
        rec["w0"] = d0
    else:
        rec.pop("w0", None)
    return rec


def insert_disclosure(rows, d, total, note, hype):
    """Insère un SOLDE publié à la date d en respectant les soldes déjà connus.

    Cas concret : le solde du 15 juil. (29,3 M, S-1/A) est connu comme un bloc de
    +8,5 M depuis le 14 mai. Quand le site IR révèle 26,15 M au 4 juin, il ne faut
    surtout pas ajouter +5,35 M en double : on SCINDE le bloc estimé — 5,35 M
    daté du 4 juin, 3,15 M restant au 15 juil., chacun rechiffré sur sa fenêtre.
    Retourne l'événement créé ou None si le solde est incompatible.
    """
    cum = sum(r["amt"] for r in rows if r["d"] <= d)
    delta = round(total - cum)
    if not delta:
        return None
    if delta > 0:
        lump = next((r for r in sorted(rows, key=lambda x: x["d"])
                     if r["d"] > d and r.get("est") and r["amt"] > delta), None)
        if lump is not None:
            lump["amt"] -= delta
            _price_window(lump, hype, d)
        elif any(r["d"] > d for r in rows):
            return None      # incompatible avec un solde postérieur déjà publié
    prev = max([r["d"] for r in rows if r["d"] <= d] or [d])
    rec = {"d": d, "amt": delta, "kind": "buy" if delta > 0 else "sell",
           "est": 1, "note": note}
    _price_window(rec, hype, prev)
    rows.append(rec)
    return rec


def build_purr(hype, edgar, audit):
    """Événements PURR : seed 10-Q/prospectus + tout solde plus récent.

    Le seed n'est qu'un point de départ. Deux sources l'enrichissent :
    les dépôts SEC (8-K, 10-Q, S-1/A — PURR n'annonce PAS ses achats par 8-K) et
    le site IR désigné par le S-1/A, seule source infra-trimestrielle.
    """
    rows = []
    for ev in PURR_EVENTS:
        r = dict(ev)
        d0 = r.pop("avg_from", None)
        if r.get("usd_m") is None:
            _price_window(r, hype, d0)
        rows.append(r)
    meta = dict(PURR_META)

    # Contrôle en dur du roll-forward contre le coût de revient publié.
    d, units, usd_m = PURR_COST_CHECK
    cum_u = sum(r["amt"] for r in rows if r["d"] <= d)
    cum_d = sum(r["usd_m"] for r in rows if r["d"] <= d and r.get("usd_m"))
    if abs(cum_u - units) > 1 or abs(cum_d - usd_m) > 0.5:
        msg = (f"PURR roll-forward {d} : {cum_u:,.0f} HYPE / {cum_d:.3f} M$ vs 10-Q "
               f"{units:,} / {usd_m} M$")
        audit["selftest"].append(msg)
        sys.stderr.write("[treasury] " + msg + "\n")

    # a) soldes déclarés dans les dépôts SEC (8-K, 10-Q, S-1, S-1/A).
    for ev in edgar_items(edgar, "purr"):
        if not ev.get("total"):
            continue
        if ev["d"] <= meta["holdings_asof"] or not (0.5 < ev["total"] / meta["holdings"] < 3.0):
            continue
        rec = insert_disclosure(
            rows, ev["d"], ev["total"],
            ev.get("src", "filing") + " du " + ev["filed"] + " : trésor "
            + f"{ev['total']:,.0f}".replace(",", " ") + " HYPE"
            + " — prix d'exécution non publié, estimé au cours moyen de la période", hype)
        if rec:
            meta["holdings"] = round(ev["total"])
            meta["holdings_asof"] = ev["d"]
            meta["holdings_note"] = ev.get("src", "filing") + " du " + ev["filed"]
            audit["added"].append(f"PURR {ev['d']} {rec['amt']:+} HYPE ({ev.get('src')} {ev['filed']})")

    # b) site IR (hypestrat.xyz), source citée par le S-1/A. Ne prime jamais sur
    #    la SEC : elle ne sert qu'à combler les trous entre deux dépôts.
    ir = hypestrat_holdings()
    if ir:
        ird, irtot = ir
        known = {r["d"] for r in rows}
        if ird not in known and 0.5 < irtot / meta["holdings"] < 3.0:
            rec = insert_disclosure(
                rows, ird, irtot,
                "site IR hypestrat.xyz (source désignée par le S-1/A) : trésor "
                + f"{irtot:,.0f}".replace(",", " ") + " HYPE — prix d'exécution non publié, "
                "estimé au cours moyen de la période", hype)
            if rec:
                audit["added"].append(f"PURR {ird} {rec['amt']:+} HYPE (site IR)")
                if ird > meta["holdings_asof"]:
                    meta["holdings"] = irtot
                    meta["holdings_asof"] = ird
                    meta["holdings_note"] = "site IR hypestrat.xyz au " + ird
        meta["ir_asof"] = ird
    rows.sort(key=lambda r: (r["d"], r.get("kind") != "contribution"))
    return rows, meta


def cg_net(rows, meta, live, spot, today, ticker, unit, audit, with_px=False):
    """Filet CoinGecko : rattrape un mouvement que la SEC n'a pas (encore) publié.

    SYMÉTRIQUE — c'est le cœur du bug du 2026-07-26 : l'ancienne version ne
    réagissait qu'à une HAUSSE du trésor (`live > holdings`), donc une vente
    laissait le site figé sur le dernier achat, sans le moindre signal. Garde
    0,75–1,25 pour ignorer un chiffre CoinGecko aberrant, seuil 0,05% pour ne
    pas fabriquer de mouvement sur un arrondi.

    VENTE FANTÔME CORRIGÉE 2026-07-28 : CoinGecko a QUELQUES JOURS DE RETARD sur
    la SEC. Quand un 8-K vient d'être lu, l'écart entre le chiffre frais (SEC) et
    le chiffre en retard (CoinGecko) n'est pas un mouvement : c'est le retard de
    CoinGecko. Le 27/07 le site a donc affiché « VENTE BMNR −9 946 ETH, hier »,
    en rouge et en alerte… alors que BitMine venait au contraire d'ACHETER
    exactement ces 9 946 ETH (8-K du 27/07, semaine close le 26/07 : 5 777 468 →
    5 787 414). CoinGecko, resté à 5 777 468, a fait naître une vente du montant
    exact de l'achat. Vérifié le lendemain : CoinGecko affiche 5 787 414, le
    fantôme s'est retourné en achat tout seul. Sans garde, le scénario se répète
    à CHAQUE dépôt hebdomadaire de BitMine et de Strategy.
    Règle : CoinGecko n'a droit à la parole que si la SEC se TAIT depuis plus de
    CG_LAG_DAYS jours — c'est-à-dire dans le seul cas où il apporte vraiment une
    information (mouvement non encore publié), jamais pour contredire un filing
    plus récent que lui.
    """
    if not live or not (0.75 < live / meta["holdings"] < 1.25):
        return None
    if abs(live / meta["holdings"] - 1) <= 0.0005:
        return None
    asof = meta.get("holdings_asof")
    if asof:
        try:
            lag = (datetime.strptime(today, "%Y-%m-%d")
                   - datetime.strptime(asof, "%Y-%m-%d")).days
        except ValueError:
            lag = None
        if lag is not None and lag < CG_LAG_DAYS:
            audit.setdefault("cg_skipped", []).append(
                f"{ticker}: CoinGecko {live:.0f} ignoré ({live - meta['holdings']:+.0f} "
                f"{unit}) — publication SEC du {asof} vieille de {lag} j seulement, "
                f"CoinGecko est en retard sur elle")
            return None
    delta = round(live - sum(p["amt"] for p in rows))
    if not delta:
        return None
    rec = {"d": today, "amt": delta, "usd_m": round(delta * spot / 1e6, 1),
           "px": round(spot, 2) if with_px else None, "est": 1,
           "note": ("vente détectée via CoinGecko — 8-K pas encore publié/parsé"
                    if delta < 0 else "delta CoinGecko (8-K pas encore parsé)")}
    rows.append(rec)
    meta["holdings"] = round(live)
    meta["holdings_asof"] = today
    audit["added"].append(f"{ticker} {today} {delta:+} {unit} (CoinGecko)")
    return rec


def build_alert(out):
    """Extrait du cache complet le strict necessaire au widget de veille Accueil.

    Pour chaque societe : dernier mouvement (achat OU vente : amt negatif) +
    holdings courants. Plus une liste des 12 derniers evenements toutes societes
    confondues, triee du plus recent au plus ancien — c'est elle qui declenche le
    badge « NOUVEAU » cote client (comparaison a la derniere cle vue en
    localStorage). `est=1` = montant $ estime au spot du jour (8-K sans prix).
    """
    comps, events = [], []
    for c in out["companies"]:
        # Un apport en nature d'investisseurs ou une récompense de staking n'est
        # PAS un mouvement décidé par le management : la veille de l'Accueil ne
        # doit pas l'annoncer comme un achat.
        ps = sorted([p for p in c.get("purchases") or [] if p.get("amt")
                     and p.get("kind", "buy") in ("buy", "sell")], key=lambda p: p["d"])
        last = ps[-1] if ps else None
        comps.append({
            "id": c["id"], "ticker": c["ticker"], "name": c["name"],
            "asset": c["asset_label"], "holdings": c["holdings"],
            "holdings_asof": c.get("holdings_asof"),
            "holdings_stale_days": c.get("holdings_stale_days"),
            "last": ({"d": last["d"], "amt": last["amt"], "usd_m": last.get("usd_m"),
                      "est": int(last.get("est") or 0), "kind": last.get("kind", "buy"),
                      "note": last.get("note")}
                     if last else None),
        })
        for p in ps[-6:]:
            events.append({"id": c["id"], "ticker": c["ticker"], "asset": c["asset_label"],
                           "d": p["d"], "amt": p["amt"], "usd_m": p.get("usd_m"),
                           "est": int(p.get("est") or 0), "kind": p.get("kind", "buy"),
                           "note": p.get("note")})
    events.sort(key=lambda e: e["d"], reverse=True)
    return {
        "ts_fetched": out["ts_fetched"],
        "generated_at": out["generated_at"],
        "companies": comps,
        "events": events[:12],
    }


def write_alert(out):
    alert = build_alert(out)
    js = ("/* Auto-generated by fetch_treasury_companies.py — do not edit.\n"
          "   Wrapper leger consomme par le widget de veille de l'Accueil. */\n"
          "window.__TREASURY_ALERT__=" + json.dumps(alert, ensure_ascii=False, separators=(",", ":")) + ";\n")
    with open(OUT_ALERT, "w", encoding="utf-8") as f:
        f.write(js)
    # Écriture ATOMIQUE du jumeau JSON : il est lu en boucle par les onglets
    # ouverts (caRefresh toutes les 15 min). Un fetch tombant pile pendant une
    # réécriture non atomique lirait un JSON tronqué → tuile vidée.
    tmp = OUT_ALERT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(alert, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_ALERT_JSON)
    return alert


def main():
    had_cache = os.path.exists(OUT_JS)

    def bail(msg):
        sys.stderr.write(f"[treasury] FATAL: {msg}" + (" — cache existant conservé\n" if had_cache else "\n"))
        sys.exit(1 if had_cache else 2)

    # ── Prix des actifs ────────────────────────────────────────────────
    btc = yahoo_daily("BTC-USD", epoch("2020-06-01"))
    eth = yahoo_daily("ETH-USD", epoch("2025-05-01"))
    hype = yahoo_daily("HYPE32196-USD", epoch("2025-08-01")) or hype_from_coingecko()
    if not btc or len(btc) < 500:
        bail("BTC-USD vide/trop court")
    if not eth or len(eth) < 100:
        bail("ETH-USD vide/trop court")
    if not hype or len(hype) < 50:
        bail("HYPE vide/trop court (Yahoo + CoinGecko KO)")
    if hype:
        hype = [(d, p) for (d, p) in hype if d >= "2025-08-01"]

    # ── Prix des actions ───────────────────────────────────────────────
    mstr_px = yahoo_daily("MSTR", epoch("2020-06-01"))
    bmnr_px = yahoo_daily("BMNR", epoch("2025-05-01"))
    purr_px = yahoo_daily("PURR", epoch("2025-11-25"))
    if not mstr_px:
        bail("MSTR vide")
    if not bmnr_px:
        sys.stderr.write("[treasury] WARN: BMNR sans historique\n")
        bmnr_px = []
    if not purr_px:
        sys.stderr.write("[treasury] WARN: PURR sans historique\n")
        purr_px = []

    # ── Supplies circulantes (CoinGecko) ───────────────────────────────
    # PIÈGE CORRIGÉ le 2026-07-26 : en cas de 429 CoinGecko, le repli tombait sur
    # un seed écrit à la main (343 M pour HYPE contre 222 M réels), et le « % du
    # supply » affiché basculait de 8,5% à 13% d'un run à l'autre — deux valeurs
    # pour la même donnée selon la météo réseau. Le repli est maintenant le
    # DERNIER CHIFFRE CONNU du cache, et la provenance est publiée.
    supplies = {"btc": 19.9e6, "eth": 120.7e6, "hype": 222.4e6}
    supply_src = {}
    prev = {}
    if os.path.exists(OUT_JSON):
        try:
            with open(OUT_JSON, encoding="utf-8") as f:
                for k, v in (json.load(f).get("assets") or {}).items():
                    if v.get("supply"):
                        prev[k] = float(v["supply"])
        except Exception:
            prev = {}
    for key, cid in (("btc", "bitcoin"), ("eth", "ethereum"), ("hype", "hyperliquid")):
        j = coingecko(f"/coins/{cid}?localization=false&tickers=false&market_data=true"
                      "&community_data=false&developer_data=false&sparkline=false")
        s = None
        try:
            s = j["market_data"]["circulating_supply"]
        except Exception:
            s = None
        if s:
            supplies[key] = float(s)
            supply_src[key] = "CoinGecko (offre en circulation, ce jour)"
        elif key in prev:
            supplies[key] = prev[key]
            supply_src[key] = "CoinGecko — dernier chiffre connu (API muette ce run)"
        else:
            supply_src[key] = "valeur de repli codée en dur (CoinGecko injoignable)"
        time.sleep(1.5)

    # ── Holdings live (CoinGecko public treasuries) ────────────────────
    live_holdings = {}
    for key, cid, want_sym in (("mstr", "bitcoin", "MSTR"), ("bmnr", "ethereum", "BMNR")):
        j = coingecko(f"/companies/public_treasury/{cid}")
        try:
            for comp in (j or {}).get("companies", []):
                sym = (comp.get("symbol") or "").upper()
                if want_sym in sym:
                    live_holdings[key] = float(comp["total_holdings"])
                    break
        except Exception:
            pass
        time.sleep(1.5)

    today = datetime.now(UTC).strftime("%Y-%m-%d")

    # ── Lecture des 8-K (source primaire des mouvements) ───────────────
    edgar = edgar_scan({
        "mstr": days_before(MSTR_SEED_UNTIL, EDGAR_LOOKBACK_DAYS),
        "bmnr": days_before(BMNR_SEED_UNTIL, EDGAR_LOOKBACK_DAYS),
        "purr": days_before(PURR_SEED_UNTIL, EDGAR_LOOKBACK_DAYS),
    })
    audit = {"selftest": [], "added": [], "coherence": {}, "cost": {}}

    # ── MSTR ───────────────────────────────────────────────────────────
    mstr_purchases, mstr = merge_mstr(edgar_items(edgar, "mstr"), audit)
    cg_net(mstr_purchases, mstr, live_holdings.get("mstr"),
           px_at(btc, today) or 0, today, "MSTR", "BTC", audit, with_px=True)

    # ── BMNR : snapshots -> achats (delta), coût estimé au spot ────────
    snaps = list(BMNR_SNAPSHOTS)
    known = {d for d, _ in snaps}
    for ev in edgar_items(edgar, "bmnr"):
        d, total = ev["d"], ev["total"]
        # Garde-fou daterie : BitMine a déjà daté un tableau d'une semaine
        # antérieure (8-K du 6 juil. 2026 titré « June 28 »). Si la date lue est
        # <= au dernier snapshot connu, on retient la veille du dépôt.
        if snaps and d <= snaps[-1][0]:
            d = days_before(ev["filed"], 1)
        if d in known or (snaps and d <= snaps[-1][0]):
            # Auto-test sur la date RETENUE (pas la date brute) : sinon le
            # décalage connu du communiqué BitMine crierait à chaque run.
            ref = dict(snaps).get(d)
            if total and ref and abs(total - ref) > 2:
                audit["selftest"].append(f"BMNR {d} : seed {ref:.0f} vs 8-K {total:.0f}")
            continue
        if not (snaps and 0.5 < total / snaps[-1][1] < 2.0):
            continue
        snaps.append((d, total))
        known.add(d)
        audit["added"].append(f"BMNR {d} trésor {total:,.0f} ETH (8-K {ev['filed']})")
    snaps.sort()
    bmnr_purchases = []
    prev = 0
    for d, total in snaps:
        delta = total - prev
        prev = total
        spot = px_at(eth, d)
        bmnr_purchases.append({
            "d": d, "amt": delta,
            "usd_m": round(delta * spot / 1e6, 1) if spot else None,
            "px": None, "est": 1,
        })
    bmnr = dict(BMNR_META)
    if snaps and snaps[-1][0] > bmnr["holdings_asof"]:
        bmnr["holdings"] = round(snaps[-1][1])
        bmnr["holdings_asof"] = snaps[-1][0]
    cg_net(bmnr_purchases, bmnr, live_holdings.get("bmnr"),
           px_at(eth, today) or 0, today, "BMNR", "ETH", audit)

    # ── PURR (aucune source live hors SEC : ni CoinGecko ni Yahoo) ──────
    purr_purchases, purr = build_purr(hype, edgar, audit)

    # ── Derniers cours + var 1j ────────────────────────────────────────
    def last_and_chg(series):
        if not series:
            return None, None
        last = series[-1][1]
        chg = (last / series[-2][1] - 1) * 100 if len(series) > 1 and series[-2][1] else None
        return round(last, 2), (round(chg, 2) if chg is not None else None)

    out = {
        "ts_fetched": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at": int(time.time()),
        "assets": {
            "btc": {"label": "BTC", "supply": supplies["btc"], "supply_src": supply_src.get("btc"),
                    "last": btc[-1][1], "px": [[d, round(p, 2)] for d, p in btc]},
            "eth": {"label": "ETH", "supply": supplies["eth"], "supply_src": supply_src.get("eth"),
                    "last": eth[-1][1], "px": [[d, round(p, 2)] for d, p in eth]},
            "hype": {"label": "HYPE", "supply": supplies["hype"], "supply_src": supply_src.get("hype"),
                     "last": hype[-1][1], "px": [[d, round(p, 3)] for d, p in hype]},
        },
        "companies": [],
    }

    # cadence = rythme normal de publication du trésor. Au-delà, le front dit
    # explicitement « données publiées jusqu'au … » au lieu de laisser croire
    # que la dernière bulle est le dernier achat.
    for meta, cfg in (
        (mstr, {"id": "mstr", "ticker": "MSTR", "name": "Strategy", "asset": "btc", "asset_label": "BTC",
                "stock": mstr_px, "purchases": mstr_purchases, "cadence": 7}),
        (bmnr, {"id": "bmnr", "ticker": "BMNR", "name": "BitMine Immersion", "asset": "eth", "asset_label": "ETH",
                "stock": bmnr_px, "purchases": bmnr_purchases, "cadence": 7}),
        (purr, {"id": "purr", "ticker": "PURR", "name": "Hyperliquid Strategies", "asset": "hype", "asset_label": "HYPE",
                "stock": purr_px, "purchases": purr_purchases, "cadence": 45}),
    ):
        # Actions en circulation : historique SEC + ATM (jamais un seed figé, la
        # mNAV en dépend linéairement).
        sh_hist, sh_m, sh_asof, sh_note = shares_series(cfg["id"], edgar, meta, audit)
        meta["shares_m"], meta["shares_asof"], meta["shares_note"] = sh_m, sh_asof, sh_note
        recompute_cost(cfg["purchases"], meta, cfg["id"], audit)
        last_px, chg = last_and_chg(cfg["stock"])
        # Tokens par action : la seule métrique qui dit si accumuler CRÉE de la
        # valeur. Émettre des actions pour acheter des tokens peut parfaitement
        # la détruire (PURR : +56% de tokens, tokens/action toujours sous mars).
        # PIÈGE : ne calculer le ratio QUE là où le trésor est publié à la même
        # date que le compte d'actions (±7 j). Entre le 14 mai et le 15 juil.
        # 2026 PURR n'a rien publié : rapporter les actions du 15 juin à un
        # trésor de mai donnait 0,106 HYPE/action, un effondrement de 26% qui
        # n'a jamais eu lieu.
        # Dates où le TRÉSOR est publié = mouvements + la dernière date de
        # trésor déclaré (Strategy publie son solde chaque semaine, même sans
        # opération : sans elle le ratio le plus récent serait perdu).
        ev_dates = sorted({p["d"] for p in cfg["purchases"] if p.get("amt")}
                          | {meta["holdings_asof"]})
        tps, seen_tps = [], set()
        for h in sh_hist:
            if not ev_dates or not h["shares"]:
                continue
            near = min(ev_dates, key=lambda d: abs(ddays(d, h["d"])))
            if abs(ddays(near, h["d"])) > 7:
                continue
            cum = sum(p["amt"] for p in cfg["purchases"] if p["d"] <= near)
            d = max(near, h["d"])
            if cum > 0 and d not in seen_tps:
                seen_tps.add(d)
                tps.append([d, round(cum / h["shares"], 6)])
        stale = ddays(today, meta["holdings_asof"])
        c = {
            "id": cfg["id"], "ticker": cfg["ticker"], "name": cfg["name"],
            "asset": cfg["asset"], "asset_label": cfg["asset_label"],
            "holdings": meta["holdings"], "holdings_asof": meta["holdings_asof"],
            "holdings_note": meta.get("holdings_note"),
            # Un graphe qui s'arrête trois mois avant le présent SANS LE DIRE est
            # pire qu'un graphe absent : le front affiche l'âge de la dernière
            # publication dès qu'il dépasse le rythme normal de la société.
            "holdings_stale_days": stale,
            "disclosure_cadence_days": cfg["cadence"],
            "avg_cost": meta["avg_cost"], "cost_total_usd": meta["cost_total_usd"],
            "avg_cost_buys": meta.get("avg_cost_buys"),
            "buys_total_usd": meta.get("buys_total_usd"),
            "buys_units": meta.get("buys_units"),
            "cost_est_share_pct": meta.get("cost_est_share_pct"),
            "cost_note": meta.get("cost_note"),
            "cost_estimated": bool(meta.get("cost_estimated")),
            "shares_m": meta["shares_m"],
            "shares_asof": meta.get("shares_asof"),
            "shares_note": meta.get("shares_note"),
            "shares_hist": [[h["d"], round(h["shares"] / 1e6, 3), h["src"]] for h in sh_hist],
            "tokens_per_share": tps,
            "mcap_usd": round(last_px * meta["shares_m"] * 1e6) if last_px else None,
            "last_px": last_px, "chg_1d_pct": chg,
            "purchases": cfg["purchases"],
            "stock": [[d, round(p, 2)] for d, p in cfg["stock"]],
        }
        # ── Garde anti-régression : le cumul des mouvements doit retomber sur
        # les holdings déclarés. C'est CE contrôle qui aurait crié en juillet
        # (cumul 844 222 vs 843 775 déclarés) au lieu d'afficher un trésor figé.
        cum = sum(p["amt"] for p in cfg["purchases"])
        gap = cum - meta["holdings"]
        tol = max(25, meta["holdings"] * 0.0005)
        c["coherence"] = {"cum": round(cum), "declared": meta["holdings"],
                          "gap": round(gap), "ok": abs(gap) <= tol}
        audit["coherence"][cfg["id"]] = c["coherence"]
        if abs(gap) > tol:
            sys.stderr.write(f"[treasury] INCOHERENCE {cfg['ticker']} : cumul {cum:,.0f} vs "
                             f"déclaré {meta['holdings']:,.0f} (écart {gap:+,.0f})\n")
        out["companies"].append(c)

    out["audit"] = {
        "edgar_last_scan": (edgar.get("_meta") or {}).get("last_scan"),
        "edgar_errors": (edgar.get("_meta") or {}).get("errors"),
        "edgar_last_filing": {k: (edgar.get("_meta") or {}).get(k + "_last_filing")
                              for k in ("mstr", "bmnr", "purr")},
        "added": audit["added"][-12:],
        "selftest": audit["selftest"][:12],
        "coherence": audit["coherence"],
        "cost": audit["cost"],
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    js = "/* Auto-generated by fetch_treasury_companies.py — do not edit. */\n"
    js += "window.__TREASURY_LIVE__=" + json.dumps(out, ensure_ascii=False, separators=(",", ":")) + ";\n"
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(js)
    alert = write_alert(out)
    last_ev = alert["events"][0]["d"] if alert["events"] else "—"
    nsell = sum(1 for p in mstr_purchases + bmnr_purchases + purr_purchases if p["amt"] < 0)
    sys.stderr.write(
        f"[treasury] OK -> {OUT_JS} ({os.path.getsize(OUT_JS) / 1024:.0f} KB) · "
        f"BTC {len(btc)}d ETH {len(eth)}d HYPE {len(hype)}d · "
        f"MSTR {len(mstr_purchases)} mvts / BMNR {len(bmnr_purchases)} / PURR {len(purr_purchases)} "
        f"({nsell} ventes)\n"
        f"[treasury] veille -> {OUT_ALERT} ({os.path.getsize(OUT_ALERT) / 1024:.1f} KB) · "
        f"dernier mouvement {last_ev}"
        + (" · AJOUTS: " + " | ".join(audit["added"][-4:]) if audit["added"] else "")
        # Un écart CoinGecko ignoré doit rester VISIBLE : c'est la trace qui
        # aurait permis de voir tout de suite que la « vente » du 27/07 n'était
        # que le retard de CoinGecko sur le 8-K de la veille.
        + (" · IGNORÉ: " + " | ".join(audit.get("cg_skipped", [])[-3:])
           if audit.get("cg_skipped") else "")
        + "\n")


if __name__ == "__main__":
    main()
