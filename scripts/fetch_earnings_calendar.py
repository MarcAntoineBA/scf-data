#!/usr/bin/env python3
"""Calendrier des résultats d'entreprises AVEC les chiffres publiés (live).

REFONTE 2026-07-28 : avant, ce script ne produisait qu'une DATE de publication
(yfinance, 12 tickers en dur) — aucun chiffre. Désormais il ramène le résultat
lui-même : BPA publié vs consensus, CA publié vs consensus, capi, créneau.

Source : POST https://www.investing.com/earnings-calendar/Service/getCalendarFilteredData
         (même famille d'endpoint que fetch_macro_calendar.py, mêmes en-têtes).
         L'endpoint ignore un intervalle multi-jours : il RENVOIE TOUJOURS le
         premier jour demandé -> on boucle jour par jour (vérifié 2026-07-28,
         dateFrom/dateTo sur 40 jours ne rendait que le 28 juillet).

Univers  : WATCHLIST IA/tech (toujours) + toute société ≥ MCAP_FLOOR (200 Md$).
Sorties  : earnings_calendar_cache.json + earnings_live.js (window.__EARNINGS_LIVE__)
           consommés par l'onglet Actualités ET le widget Calendrier de l'Accueil.

Modes :
  (aucun)  respecte CACHE_MAX_HOURS, refetch sinon
  --force  refetch complet immédiat + propagation si un chiffre vient de tomber
  --auto   mode launchd : no-op si rien d'imminent, sinon rafale poll 60 s calée
           sur l'heure de publication jusqu'à capter le chiffre, puis snapshot
           + deploy (le chiffre est en ligne quelques minutes après sa sortie).
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "earnings_calendar_cache.json"
JS_CACHE = CACHE_DIR / "earnings_live.js"
HTML_FILE = Path.home() / "Desktop/Site_Crypto_Finance/News_Crypto.html"
LOCK_FILE = CACHE_DIR / "earnings_calendar.lock"
CACHE_MAX_HOURS = 6

API = "https://www.investing.com/earnings-calendar/Service/getCalendarFilteredData"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
ET = ZoneInfo("America/New_York")

# ── Univers suivi ─────────────────────────────────────────────────────────
MCAP_FLOOR = 200e9        # toute société ≥ 200 Md$ entre dans le calendrier
WINDOW_DAYS = 45          # profondeur du calendrier (jours calendaires)
KEEP_PUBLISHED_DAYS = 1   # un résultat publié reste affiché 1 jour de plus
REQ_DELAY = 0.35          # politesse entre 2 jours (baseline uniquement)

# Rafale calée sur la publication (mode --auto)
POLL_SEC = 60             # intervalle de poll pendant une rafale
ARM_LOOKAHEAD = 30 * 60   # arme la rafale 30 min avant l'heure estimée
BURST_WINDOW = 150 * 60   # reste en éveil 2h30 après tant que le chiffre manque
MAX_BURST = 30 * 60       # plafond dur d'une rafale (launchd relance ensuite)
BASELINE_REFRESH_H = 6    # refetch complet du calendrier au moins toutes les 6h
SNAPSHOT_LABEL = "scf.snapshot"
DEPLOY_LABEL = "scf.cfdeploy"
# Propagation vers le site public (cf. propagate()) : on attend une PREUVE que le
# repo porte le nouveau cache, et que le verrou de déploiement est libre, plutôt
# qu'une durée fixe. Plafond global pour ne pas bloquer la rafale de publication.
REPO_CACHE = Path.home() / "Desktop/Site_Crypto_Finance/earnings_calendar_cache.json"
DEPLOY_LOCK = "/tmp/cfdeploy.lock.pid"
PROPAGATE_MAX_S = 8 * 60

# Créneaux de publication. Investing.com ne donne JAMAIS l'horodatage exact,
# seulement un créneau via l'infobulle. Quand l'infobulle est absente (souvent
# data-value="2" : AVGO, CRM, CSCO, MRVL, SNOW…), l'heure n'est pas communiquée
# — surtout PAS « en séance ». On l'affiche comme telle plutôt que d'inventer
# une heure, et on élargit la surveillance à toute la journée pour ne pas rater
# la publication (ces sociétés publient en pratique après la clôture).
#   heure nominale (ET) = sert à ordonner le calendrier
#   poll_from / poll_to (ET) = fenêtre de surveillance du chiffre
SESSION_TIME = {"bmo": (7, 0), "amc": (16, 15), "dmh": (12, 0), "tba": (16, 15)}
SESSION_LABEL = {"bmo": "Avant ouverture", "amc": "Après clôture",
                 "dmh": "En séance", "tba": "Heure non précisée"}
SESSION_POLL = {                      # (début, fin) de la fenêtre de poll, ET
    "bmo": ((6, 0), (11, 0)),
    "amc": ((15, 45), (20, 30)),
    "dmh": ((9, 30), (16, 0)),
    "tba": ((6, 0), (21, 0)),
}

# Watchlist IA / semis / cloud / logiciel + bellwethers crypto : toujours
# incluses quelle que soit la capi. Les notes/±BTC ne sont renseignés que là où
# ils ont été établis — pas d'estimation inventée pour le reste.
WATCHLIST = {
    "NVDA":  {"impact": "high", "btc": "±2.5%", "note": "Bellwether IA. Beat datacenter = risk-on tech + crypto."},
    "MSFT":  {"impact": "high", "btc": "±1.0%", "note": "Azure + Copilot, bellwether tech."},
    "AAPL":  {"impact": "high", "btc": "±0.8%", "note": "iPhone cycle + Services."},
    "AMZN":  {"impact": "high", "btc": "±0.9%", "note": "AWS = bellwether cloud + IA."},
    "GOOGL": {"impact": "high", "btc": "±0.8%", "note": "Cloud + IA, baromètre big tech."},
    "GOOG":  {"impact": "high", "btc": "±0.8%", "note": "Cloud + IA, baromètre big tech."},
    "META":  {"impact": "high", "btc": "±0.7%", "note": "CapEx IA, scrutiny Reality Labs."},
    "TSLA":  {"impact": "high", "btc": "±1.0%", "note": "Mention BTC dans le bilan = signal crypto."},
    "AVGO":  {"impact": "high", "btc": "±0.5%", "note": "Semi IA custom (XPU), cycle datacenter."},
    "AMD":   {"impact": "high", "btc": "±0.7%", "note": "Concurrent NVDA datacenter."},
    "TSM":   {"impact": "high", "btc": "", "note": "Fondeur unique des puces IA : capex = amont du cycle."},
    "ASML":  {"impact": "high", "btc": "", "note": "Monopole EUV : bookings = visibilité 2 ans sur les fabs."},
    "MU":    {"impact": "high", "btc": "", "note": "HBM = goulot d'étranglement mémoire de l'IA."},
    "ORCL":  {"impact": "high", "btc": "", "note": "RPO cloud = carnet de commandes IA."},
    "PLTR":  {"impact": "high", "btc": "", "note": "Baromètre de la monétisation IA en entreprise."},
    "COIN":  {"impact": "high", "btc": "±2.0%", "note": "Bellwether crypto pur. Volume + revenus = directionnel BTC."},
    "MSTR":  {"impact": "high", "btc": "±2.5%", "note": "Stratégie BTC treasury. Achats trimestriels."},
    "INTC":  {"impact": "med", "btc": "", "note": "Foundry : exécution 18A."},
    "QCOM":  {"impact": "med", "btc": "", "note": ""},
    "TXN":   {"impact": "med", "btc": "", "note": "Semis analogiques = cycle industriel réel."},
    "ARM":   {"impact": "med", "btc": "", "note": "Royalties = diffusion des architectures IA."},
    "MRVL":  {"impact": "med", "btc": "", "note": "Optique + custom silicon datacenter."},
    "SMCI":  {"impact": "med", "btc": "", "note": "Serveurs IA : marge = intensité de la concurrence."},
    "DELL":  {"impact": "med", "btc": "", "note": "Carnet serveurs IA."},
    "ANET":  {"impact": "med", "btc": "", "note": "Réseau datacenter 800G."},
    "CRM":   {"impact": "med", "btc": "", "note": ""},
    "ADBE":  {"impact": "med", "btc": "", "note": "Test de la thèse « l'IA mange le logiciel »."},
    "NOW":   {"impact": "med", "btc": "", "note": ""},
    "SNOW":  {"impact": "med", "btc": "", "note": "Consommation data = proxy des charges IA."},
    "IBM":   {"impact": "med", "btc": "", "note": ""},
    "CSCO":  {"impact": "med", "btc": "", "note": ""},
    "AMAT":  {"impact": "med", "btc": "", "note": "Équipementier : WFE = capex des fabs."},
    "LRCX":  {"impact": "med", "btc": "", "note": "Équipementier gravure/dépôt."},
    "KLAC":  {"impact": "med", "btc": "", "note": "Métrologie : indicateur avancé des montées en cadence."},
    "NXPI":  {"impact": "med", "btc": "", "note": "Semis auto/industriel."},
    "ON":    {"impact": "med", "btc": "", "note": ""},
    "WDC":   {"impact": "med", "btc": "", "note": "Stockage : demande IA sur le nearline."},
    "STX":   {"impact": "med", "btc": "", "note": "Stockage : demande IA sur le nearline."},
    "VRT":   {"impact": "med", "btc": "", "note": "Refroidissement datacenter = contrainte physique de l'IA."},
    "CRWV":  {"impact": "med", "btc": "", "note": "Neocloud GPU : backlog et dette."},
    # NBIS volontairement absent : Investing.com le rattache encore à l'ancienne
    # paire Yandex NV (/equities/yandex-nv-earnings) et sert des chiffres hors
    # d'échelle — consensus BPA -51,46 et CA 42,85 Md là où Nasdaq donne un BPA
    # de -0,23 à -0,69 sur les 3 derniers trimestres (recoupé le 2026-07-29,
    # facteur ~100). Publier ça serait diffuser une donnée fausse : on l'exclut
    # tant que la paire n'est pas corrigée en amont.
    "APP":   {"impact": "med", "btc": "", "note": ""},
    "NFLX":  {"impact": "med", "btc": "±0.3%", "note": "Indicateur consumer tech."},
    "UBER":  {"impact": "med", "btc": "", "note": ""},
    "HOOD":  {"impact": "med", "btc": "±0.8%", "note": "Volumes retail crypto + actions."},
    "CRCL":  {"impact": "med", "btc": "±1.0%", "note": "Émetteur USDC : revenus = taux + circulation stablecoin."},
}
# Titres dont le résultat déplace l'indice entier, quel que soit le reste.
CORE_HIGH = {"NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "GOOG", "META", "TSLA", "AVGO"}

MCAP_RE = re.compile(r"^([\d.]+)\s*([KMBT])?$")


def to_num(s):
    """'91.79B' -> 9.179e10 ; '-0.76' -> -0.76 ; '--' -> None."""
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace(" ", "")
    if not s or s in ("--", "-", "—", "/"):
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-")
    m = MCAP_RE.match(s)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    v *= {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, None: 1.0}[m.group(2)]
    return -v if neg else v


# Au-delà de ce seuil, l'écart trahit presque toujours un consensus corrompu en
# amont (mauvaise paire, mauvaise devise, mauvaise échelle) plutôt qu'une vraie
# surprise. Dans ce cas on garde les deux chiffres bruts affichés — donc
# auditables via le lien source — mais AUCUN verdict battu/raté n'est affiché,
# faute de pouvoir le calculer honnêtement.
# NB : ce garde-fou ne détecte QUE les paires incohérentes entre elles. Si la
# source se trompe d'échelle sur le publié ET le consensus (cas NBIS/Yandex
# ci-dessous, facteur ~100), l'écart reste plausible et rien ne se déclenche —
# seul un recoupement avec une 2e source le verrait.
SURPRISE_SANITY_PCT = 300.0


def _unit_slip(a, f):
    """La source s'est-elle trompée d'unité entre publié et consensus ?

    Constaté en direct le 2026-07-29 sur ARM : Investing affiche un CA publié de
    « 1.29M » là où son propre consensus est « 991.41M » — c'est 1,29 MILLIARD
    étiqueté M. Le garde-fou ±300 % ne voit rien (l'écart calculé vaut -99,9 %) et
    la tuile affichait donc un « raté -99,9 % » parfaitement faux sur un trimestre
    en réalité battu. Signature : rapport absurde (≥100×) qui redevient plausible
    une fois remis à l'échelle d'un facteur mille. On ne CORRIGE pas la valeur (ce
    serait inventer une donnée) : on retire le verdict, les deux chiffres bruts
    restent affichés et la ligne pointe vers la source.
    """
    if a is None or f is None or a <= 0 or f <= 0:
        return False
    r = a / f
    if 0.01 < r < 100:
        return False                      # écart plausible : verdict conservé
    for k in (1e3, 1e-3, 1e6, 1e-6):
        if 0.4 <= (a * k) / f <= 2.5:     # remis à l'échelle, ça retombe juste
            return True
    return False


def surprise_pct(actual, forecast, kind="eps"):
    """Écart en % entre publié et consensus. None si l'un des deux manque, si
    l'écart dépasse le seuil de vraisemblance (consensus corrompu en amont) ou,
    pour un chiffre d'affaires, si les deux valeurs ne sont pas dans la même
    unité (cf. _unit_slip)."""
    a, f = to_num(actual), to_num(forecast)
    if a is None or f is None or f == 0:
        return None
    if kind == "rev" and _unit_slip(a, f):
        return None
    pct = round((a - f) / abs(f) * 100, 1)
    if abs(pct) > SURPRISE_SANITY_PCT:
        return None
    return pct


def fetch_day(day):
    """HTML des résultats d'un jour (str 'YYYY-MM-DD'), ou None si échec réseau."""
    params = [("country[]", "5"), ("dateFrom", day), ("dateTo", day),
              ("currentTab", "custom"), ("limit_from", "0")]
    req = urllib.request.Request(API, data=urllib.parse.urlencode(params).encode(), headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://www.investing.com",
        "Referer": "https://www.investing.com/earnings-calendar/",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=25).read()
        return json.loads(raw).get("data", "")
    except Exception as e:
        sys.stderr.write(f"[Earnings] fetch {day} err: {e}\n")
        return None


def _txt(html):
    return re.sub(r"<[^>]+>", "", html or "").replace("&nbsp;", " ").replace("/", " ").strip()


def parse_day(html, day):
    """Extrait les lignes retenues (watchlist OU capi ≥ MCAP_FLOOR) d'un jour."""
    out = []
    if not html:
        return out
    for blk in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        inner = blk.group(1)
        if "earnCalCompany" not in inner:
            continue
        comp_m = re.search(r'earnCalCompany"[^>]*title="([^"]*)"', inner)
        tick_m = re.search(r'<a\s+href="([^"]*)"[^>]*target="_blank"[^>]*>([^<]+)</a>', inner)
        if not tick_m:
            continue
        href, ticker = tick_m.group(1), tick_m.group(2).strip()
        company = (comp_m.group(1).strip() if comp_m else ticker)

        # Le consensus est TOUJOURS le <td> qui suit la cellule du publié.
        def pair(kind):
            m = re.search(r'<td[^>]*' + kind + r'[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>',
                          inner, re.DOTALL)
            if not m:
                return "", ""
            a, f = _txt(m.group(1)), _txt(m.group(2))
            clean = lambda v: "" if v in ("--", "-", "—", "") else v
            return clean(a), clean(f)

        eps_a, eps_f = pair("eps_actual")
        rev_a, rev_f = pair("rev_actual")

        mc_m = re.search(r'<td class="right">([^<]*)</td>', inner)
        mcap_lbl = (mc_m.group(1).strip() if mc_m else "")
        mcap = to_num(mcap_lbl) or 0.0

        wl = WATCHLIST.get(ticker)
        if wl is None and mcap < MCAP_FLOOR:
            continue

        tip = re.search(r'data-tooltip="(Before market open|After market close|During market hours)"', inner)
        if tip:
            session = {"Before market open": "bmo", "After market close": "amc",
                       "During market hours": "dmh"}[tip.group(1)]
        else:
            session = "tba"   # pas d'infobulle = heure non communiquée

        y, mo, d = (int(x) for x in day.split("-"))
        hh, mm = SESSION_TIME[session]
        iso = datetime(y, mo, d, hh, mm, tzinfo=ET).isoformat()
        (fh, fm), (th, tm) = SESSION_POLL[session]
        poll_from = datetime(y, mo, d, fh, fm, tzinfo=ET).isoformat()
        poll_to = datetime(y, mo, d, th, tm, tzinfo=ET).isoformat()

        if ticker in CORE_HIGH or mcap >= 1e12:
            impact = "high"
        elif wl:
            impact = wl["impact"]
        elif mcap >= 500e9:
            impact = "high"
        else:
            impact = "med"

        eps_s = surprise_pct(eps_a, eps_f)
        rev_s = surprise_pct(rev_a, rev_f, kind="rev")

        sub = [company, ticker, SESSION_LABEL[session].lower()]
        if eps_a:
            sub.append(f"BPA {eps_a}" + (f" vs {eps_f}" if eps_f else ""))
        elif eps_f:
            sub.append(f"cons. BPA {eps_f}")
        if rev_a:
            sub.append(f"CA {rev_a}" + (f" vs {rev_f}" if rev_f else ""))
        elif rev_f:
            sub.append(f"cons. CA {rev_f}")
        sub.append("source Investing.com")

        url = ("https://www.investing.com" + href) if href.startswith("/") else href

        out.append({
            "date": iso,
            "name": f"{ticker} — Résultats {company}",
            "sub": " · ".join(sub),
            "region": "USA",
            "impact": impact,
            "cat": "earnings",
            "btcHist": (wl or {}).get("btc", ""),
            "btcDir": "neut",
            "note": (wl or {}).get("note", ""),
            "url": url,
            # ── champs structurés (widget Accueil) ──
            "ticker": ticker,
            "company": company,
            "session": session,
            "sessionLabel": SESSION_LABEL[session],
            "timeKnown": session != "tba",
            "pollFrom": poll_from,
            "pollTo": poll_to,
            "mcap": mcap,
            "mcapLabel": mcap_lbl,
            "epsActual": eps_a,
            "epsForecast": eps_f,
            "revActual": rev_a,
            "revForecast": rev_f,
            "epsSurprise": eps_s,
            "revSurprise": rev_s,
            # actual/forecast : alias BPA, réutilisés par la détection de sortie
            "actual": eps_a,
            "forecast": eps_f,
            "previous": "",
        })
    return out


NASDAQ_API = "https://api.nasdaq.com/api/calendar/earnings?date={}"
NASDAQ_SESSION = {"time-pre-market": "bmo", "time-after-hours": "amc",
                  "time-not-supplied": "tba"}


def _mcap_label(v):
    """1_081_910_196_869 → « 1.08T », dans le style des libellés d'origine."""
    for seuil, suffixe in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= seuil:
            return f"{v / seuil:.2f}{suffixe}"
    return f"{v:.0f}"


def fetch_day_nasdaq(day):
    """Repli quand Investing.com refuse l'adresse IP (403 depuis un datacenter).

    CE QUE CE REPLI NE SAIT PAS FAIRE : Nasdaq ne publie pas le CHIFFRE D'AFFAIRES,
    seulement le bénéfice par action et son consensus. Les champs CA restent donc
    vides plutôt que remplis d'une valeur reconstituée — un chiffre d'affaires
    inventé serait indiscernable d'un chiffre publié, et fausserait la surprise.
    La provenance est écrite dans le sous-titre : la page dit ce qu'elle sait.
    """
    try:
        req = urllib.request.Request(NASDAQ_API.format(day), headers={
            "User-Agent": UA, "Accept": "application/json"})
        raw = urllib.request.urlopen(req, timeout=25).read()
        rows = (json.loads(raw).get("data") or {}).get("rows") or []
    except Exception as e:
        sys.stderr.write(f"[Earnings] repli Nasdaq {day} err: {e}\n")
        return None

    out = []
    for r in rows:
        ticker = (r.get("symbol") or "").strip()
        if not ticker:
            continue
        company = (r.get("name") or ticker).strip()

        nettoyer = lambda v: (v or "").replace("$", "").replace(",", "").strip()
        eps_a, eps_f = nettoyer(r.get("eps")), nettoyer(r.get("epsForecast"))
        if eps_a in ("", "-", "--"):
            eps_a = ""

        try:
            mcap = float(nettoyer(r.get("marketCap")) or 0)
        except ValueError:
            mcap = 0.0
        mcap_lbl = _mcap_label(mcap) if mcap else ""

        wl = WATCHLIST.get(ticker)
        if wl is None and mcap < MCAP_FLOOR:
            continue

        session = NASDAQ_SESSION.get(r.get("time"), "tba")
        y, mo, d = (int(x) for x in day.split("-"))
        hh, mm = SESSION_TIME[session]
        iso = datetime(y, mo, d, hh, mm, tzinfo=ET).isoformat()
        (fh, fm), (th, tm) = SESSION_POLL[session]
        poll_from = datetime(y, mo, d, fh, fm, tzinfo=ET).isoformat()
        poll_to = datetime(y, mo, d, th, tm, tzinfo=ET).isoformat()

        if ticker in CORE_HIGH or mcap >= 1e12:
            impact = "high"
        elif wl:
            impact = wl["impact"]
        elif mcap >= 500e9:
            impact = "high"
        else:
            impact = "med"

        eps_s = surprise_pct(eps_a, eps_f)
        sub = [company, ticker, SESSION_LABEL[session].lower()]
        if eps_a:
            sub.append(f"BPA {eps_a}" + (f" vs {eps_f}" if eps_f else ""))
        elif eps_f:
            sub.append(f"cons. BPA {eps_f}")
        sub.append("source Nasdaq")

        out.append({
            "date": iso,
            "name": f"{ticker} — Résultats {company}",
            "sub": " · ".join(sub),
            "region": "USA", "impact": impact, "cat": "earnings",
            "btcHist": (wl or {}).get("btc", ""), "btcDir": "neut",
            "note": (wl or {}).get("note", ""),
            "url": f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/earnings",
            "ticker": ticker, "company": company,
            "session": session, "sessionLabel": SESSION_LABEL[session],
            "timeKnown": session != "tba",
            "pollFrom": poll_from, "pollTo": poll_to,
            "mcap": mcap, "mcapLabel": mcap_lbl,
            "epsActual": eps_a, "epsForecast": eps_f,
            "revActual": "", "revForecast": "",       # non publié par cette source
            "epsSurprise": eps_s, "revSurprise": "",
            "actual": eps_a, "forecast": eps_f, "previous": "",
            "source": "nasdaq",
        })
    return out


def day_range(days_back, days_fwd):
    """Jours ouvrés à interroger. Les sociétés ≥ 200 Md$ ne publient pas le
    week-end -> on saute samedi/dimanche (économise ~13 requêtes sur 45)."""
    today = datetime.now(ET).date()
    out = []
    for i in range(-days_back, days_fwd + 1):
        d = today + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        out.append(d.isoformat())
    return out


def fetch_window(days):
    """Interroge une liste de jours. Retourne (events, n_echecs)."""
    events, fails = [], 0
    for i, day in enumerate(days):
        html = fetch_day(day)
        if html is not None:
            events += parse_day(html, day)
        else:
            # Investing refuse les IP de datacenter (403). On ne compte l'échec que si
            # le repli échoue AUSSI : sinon on signalerait une panne là où la donnée
            # est bien arrivée, et le garde-fou de fraîcheur crierait pour rien.
            secours = fetch_day_nasdaq(day)
            if secours is None:
                fails += 1
            else:
                events += secours
        if i + 1 < len(days):
            time.sleep(REQ_DELAY)
    return events, fails


def merge(base, fresh):
    """Écrase les lignes de `base` par celles de `fresh` sur (ticker, jour).

    Garde-fou : on jette toute entrée sans `ticker`. Le cache d'avant la refonte
    (source yfinance, aucun chiffre) n'a pas ce champ ; sans ce filtre ses lignes
    se retrouvaient toutes sur la clé ("", jour) et survivaient indéfiniment aux
    merges, polluant le widget avec des dates sans résultat.
    """
    key = lambda e: (e.get("ticker", ""), (e.get("date") or "")[:10])
    out = {key(e): e for e in base if e.get("ticker")}
    for e in fresh:
        out[key(e)] = e
    return sorted(out.values(), key=lambda e: (e.get("date") or "", e.get("ticker") or ""))


def in_universe(e):
    """Watchlist, ou capi au-dessus du plancher. Réévalué à chaque merge pour que
    retirer un ticker de la WATCHLIST le fasse VRAIMENT disparaître (sinon il
    survit indéfiniment dans le cache via le merge)."""
    return e.get("ticker") in WATCHLIST or (e.get("mcap") or 0) >= MCAP_FLOOR


def prune(events):
    """Garde le futur + les résultats publiés depuis moins de KEEP_PUBLISHED_DAYS."""
    today = datetime.now(ET).date()
    floor = (today - timedelta(days=KEEP_PUBLISHED_DAYS)).isoformat()
    out = []
    for e in events:
        if not in_universe(e):
            continue
        d = (e.get("date") or "")[:10]
        if d >= today.isoformat():
            out.append(e)
        elif d >= floor and (e.get("epsActual") or e.get("revActual")):
            out.append(e)
    return out


def write_outputs(events):
    payload = {"events": events, "updated": datetime.now().isoformat()}
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, CACHE_FILE)

    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    tmp_js = JS_CACHE.with_suffix(".js.tmp")
    with open(tmp_js, "w") as f:
        f.write(
            "window.__EARNINGS_LIVE__=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
            "window.__EARNINGS_UPDATED__=" + json.dumps(updated_str) + ";\n"
        )
    os.replace(tmp_js, JS_CACHE)
    sys.stderr.write(f"[Earnings] {len(events)} lignes écrites (cache + wrapper JS)\n")
    inject_into_html(events)


def inject_into_html(events):
    """Injecte le bloc live JS dans News_Crypto.html (markers JS)."""
    if not HTML_FILE.exists():
        sys.stderr.write(f"[Earnings] {HTML_FILE} not found\n")
        return
    try:
        html = HTML_FILE.read_text()
    except PermissionError as e:
        sys.stderr.write(f"[Earnings] HTML read blocked by TCC (ok from launchd): {e}\n")
        return
    block = (
        "// __EARNINGS_LIVE_START__\n"
        "  window.__EARNINGS_LIVE__ = " +
        json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "  window.__EARNINGS_UPDATED__ = " +
        json.dumps(datetime.now().strftime("%d/%m/%Y %H:%M")) + ";\n"
        "  // __EARNINGS_LIVE_END__"
    )
    pat = re.compile(r"// __EARNINGS_LIVE_START__.*?// __EARNINGS_LIVE_END__", re.DOTALL)
    if pat.search(html):
        html2 = pat.sub(block, html)
    else:
        m = re.search(r"(\s*)var\s+MACRO_EVENTS\s*=\s*\[", html)
        if not m:
            sys.stderr.write("[Earnings] Cannot find MACRO_EVENTS marker, abort\n")
            return
        html2 = html[:m.start()] + "\n  " + block + "\n" + html[m.start():]
    try:
        HTML_FILE.write_text(html2)
    except PermissionError as e:
        sys.stderr.write(f"[Earnings] HTML write blocked by TCC (ok from launchd): {e}\n")


def load_cached():
    if CACHE_FILE.exists():
        try:
            return json.load(open(CACHE_FILE)).get("events", [])
        except Exception:
            return []
    return []


def actual_map(events):
    return {(e.get("ticker", ""), (e.get("date") or "")[:10]):
            ((e.get("epsActual") or "").strip(), (e.get("revActual") or "").strip())
            for e in events}


def newly_filled(old_map, events):
    """Tickers dont le BPA ou le CA vient de passer de vide → publié."""
    out = []
    for e in events:
        k = (e.get("ticker", ""), (e.get("date") or "")[:10])
        new = ((e.get("epsActual") or "").strip(), (e.get("revActual") or "").strip())
        old = old_map.get(k, ("", ""))
        if (new[0] and not old[0]) or (new[1] and not old[1]):
            out.append(k)
    return out


def pending_releases(events, now):
    """Publications attendues maintenant et dont le chiffre manque encore.

    La fenêtre vient de pollFrom/pollTo (calculés depuis le créneau annoncé), pas
    d'un delta symétrique : un « heure non communiquée » doit être surveillé toute
    la journée, un « avant ouverture » seulement le matin.
    """
    out = []
    for e in events:
        if (e.get("epsActual") or "").strip() or (e.get("revActual") or "").strip():
            continue
        try:
            if e.get("pollFrom") and e.get("pollTo"):
                start = datetime.fromisoformat(e["pollFrom"])
                end = datetime.fromisoformat(e["pollTo"])
            else:   # entrée d'un ancien cache : repli sur la fenêtre par défaut
                dt = datetime.fromisoformat(e["date"])
                start, end = dt - timedelta(seconds=ARM_LOOKAHEAD), dt + timedelta(seconds=BURST_WINDOW)
        except Exception:
            continue
        if start <= now <= end:
            out.append(e)
    return out


def _kick(label):
    try:
        r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
                           capture_output=True, text=True, timeout=20)
        sys.stderr.write(f"[Earnings] kick {label}: rc={r.returncode} {r.stderr.strip()}\n")
    except Exception as e:
        sys.stderr.write(f"[Earnings] kick {label} err: {e}\n")


_prop_thread = None


def propagate_async(newly):
    """Propage en tâche de fond. False si une propagation est déjà en vol (les
    chiffres suivants partiront avec le lot de fin de run) — inutile d'empiler
    trois déploiements pour trois tickers tombés à la même minute."""
    global _prop_thread
    if _prop_thread is not None and _prop_thread.is_alive():
        return False
    _prop_thread = threading.Thread(target=propagate, args=(list(newly),), daemon=True)
    _prop_thread.start()
    return True


def _join_propagation():
    """Attend la propagation en vol : un thread daemon mourrait avec le process."""
    if _prop_thread is not None and _prop_thread.is_alive():
        sys.stderr.write("[Earnings] attente de la propagation en cours…\n")
        _prop_thread.join(timeout=PROPAGATE_MAX_S + 60)


def _repo_synced():
    """Le repo porte-t-il DÉJÀ le cache qu'on vient d'écrire ?

    True / False / None (None = illisible, typiquement TCC : le job launchd n'a
    pas forcément accès à ~/Desktop). On compare le champ `updated`, pas le mtime :
    c'est la seule preuve que snapshot_site.sh a bien recopié CE contenu-là.
    """
    try:
        a = json.load(open(CACHE_FILE)).get("updated")
        b = json.load(open(REPO_CACHE)).get("updated")
    except (PermissionError, OSError):
        return None
    except Exception:
        return False
    return bool(a) and a == b


def _deploy_busy():
    """Un déploiement tourne-t-il ? Même verrou que deploy_public_wrangler.sh."""
    try:
        pid = int(Path(DEPLOY_LOCK).read_text().strip())
    except Exception:
        return False          # pas de verrou
    try:
        os.kill(pid, 0)       # signal 0 = test d'existence
    except Exception:
        return False          # verrou orphelin (le script le retire à son EXIT)
    return True


def propagate(newly):
    """Pousse le chiffre fraîchement publié jusqu'au site public.

    Réécrit le 2026-07-29 (latence mesurée : 10-15 min, parfois 2 h). Deux
    défauts corrigés :
      1. `sleep(90)` en aveugle. Le déploiement attend LUI-MÊME la fin du
         snapshot (SNAP_LOCK), mais rien ne garantissait que le repo portait
         déjà le nouveau cache au moment du kick. On attend maintenant une
         PREUVE (`updated` identique des deux côtés) au lieu d'une durée.
      2. Kick avalé. `launchctl kickstart` renvoie rc=0 même quand le script
         sort aussitôt sur « SKIP: déploiement déjà en cours » — le chiffre
         attendait alors le déploiement planifié suivant (trou de 2 h 25 observé
         le 29/07 entre 17:35 et 20:00). On attend donc que le verrou se libère.
    Plafond global PROPAGATE_MAX_S : ce process tient le lock du fetcher, on ne
    bloque pas la rafale de publication plus longtemps que nécessaire.
    """
    names = ", ".join(sorted({t for t, _ in newly}))
    sys.stderr.write(f"[Earnings] RÉSULTAT PUBLIÉ → propagation: {names}\n")
    deadline = time.time() + PROPAGATE_MAX_S
    _kick(SNAPSHOT_LABEL)

    synced = _repo_synced()
    if synced is None:
        sys.stderr.write("[Earnings] repo illisible (TCC) — attente forfaitaire 90 s\n")
        time.sleep(90)
    else:
        time.sleep(10)                       # laisse le snapshot poser son verrou
        while not _repo_synced() and time.time() < deadline:
            time.sleep(15)
        if _repo_synced():
            sys.stderr.write("[Earnings] repo synchronisé (cache identique) → deploy\n")
        else:
            sys.stderr.write("[Earnings] WARN: repo toujours pas synchronisé — kick quand même\n")

    while _deploy_busy() and time.time() < deadline:
        time.sleep(20)
    if _deploy_busy():
        sys.stderr.write("[Earnings] WARN: déploiement encore occupé au plafond — kick (risque SKIP)\n")
    _kick(DEPLOY_LABEL)


def refresh(full):
    """full=True : tout le calendrier. full=False : hier+aujourd'hui seulement
    (1-2 requêtes, utilisé pendant une rafale)."""
    if full:
        days = day_range(KEEP_PUBLISHED_DAYS, WINDOW_DAYS)
        sys.stderr.write(f"[Earnings] calendrier complet : {len(days)} jours ouvrés\n")
        fresh, fails = fetch_window(days)
        if fails and fails >= len(days) / 2:
            sys.stderr.write(f"[Earnings] {fails}/{len(days)} jours en échec — cache conservé\n")
            return None
        events = prune(merge(load_cached(), fresh))
    else:
        days = day_range(KEEP_PUBLISHED_DAYS, 0)
        fresh, fails = fetch_window(days)
        if fails == len(days):
            return None
        events = prune(merge(load_cached(), fresh))
    write_outputs(events)
    return events


def main():
    force = "--force" in sys.argv
    auto = "--auto" in sys.argv

    lockf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write("[Earnings] déjà en cours (lock) — skip\n")
        return

    old_map = actual_map(load_cached())
    cache_age_h = ((datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
                   if CACHE_FILE.exists() else 999.0)

    if not (force or auto):
        if cache_age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[Earnings] cache frais ({cache_age_h:.1f}h)\n")
            return
        refresh(full=True)
        return

    if force:
        events = refresh(full=True)
        if events is not None:
            nf = newly_filled(old_map, events)
            if nf:
                propagate(nf)
        return

    # ── mode --auto (launchd toutes les 10 min) ──
    now = datetime.now(timezone.utc)
    hot = bool(pending_releases(load_cached(), now))
    stale = cache_age_h >= BASELINE_REFRESH_H
    if not (hot or stale):
        sys.stderr.write(f"[Earnings] idle (aucune publication imminente, cache {cache_age_h:.1f}h) — no-op\n")
        return

    any_new, sent = [], []
    events = refresh(full=stale)
    if events is not None:
        nf = newly_filled(old_map, events)
        any_new += nf
        old_map = actual_map(events)
        if nf and propagate_async(nf):
            sent += nf

    # Rafale : tant qu'un chiffre attendu manque, on repoll le jour courant.
    if events is not None and pending_releases(events, datetime.now(timezone.utc)):
        sys.stderr.write("[Earnings] rafale calée publication : poll 60 s\n")
        t0 = time.time()
        while time.time() - t0 < MAX_BURST:
            time.sleep(POLL_SEC)
            events = refresh(full=False)
            if events is None:
                continue
            nf = newly_filled(old_map, events)
            any_new += nf
            old_map = actual_map(events)
            # Propagation IMMÉDIATE et en tâche de fond (2026-07-29). AVANT :
            # propagate() n'était appelé qu'APRÈS la rafale. Le 29/07, MSFT et
            # META sont tombés à 16:06 ET mais QCOM/HOOD manquaient encore : la
            # rafale a tourné ses 30 min pleines et les chiffres déjà publiés
            # sont restés dans le cache local ~20 min avant même de PARTIR vers
            # le site. Un thread, sinon le poll 60 s s'arrêterait pendant que la
            # propagation attend le déploiement.
            if nf and propagate_async(nf):
                sent += nf
            if not pending_releases(events, datetime.now(timezone.utc)):
                break

    _join_propagation()
    rest = [k for k in any_new if k not in sent]
    if rest:
        propagate(rest)          # dernier lot du run : bloquant, on a le temps
    elif not any_new:
        sys.stderr.write("[Earnings] run terminé, aucun nouveau chiffre\n")


if __name__ == "__main__":
    main()
