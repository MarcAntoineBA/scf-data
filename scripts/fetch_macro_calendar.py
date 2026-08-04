#!/usr/bin/env python3
"""Fetch calendar macro 3-étoiles via Investing.com.

Source : POST https://www.investing.com/economic-calendar/Service/getCalendarFilteredData
Filtres : importance[]=3 (High Volatility uniquement) + multi-pays G7+APAC
Couverture : 60 jours glissants (today → today+60).

Injecte un bloc JS `window.__MACRO_CAL_LIVE__` dans News_Crypto.html.
"""
import fcntl
import html as html_lib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHE_DIR / "macro_calendar_cache.json"
JS_CACHE = CACHE_DIR / "macro_cal_live.js"
HTML_FILE = Path.home() / "Desktop/Site_Crypto_Finance/News_Crypto.html"
CACHE_MAX_HOURS = 1

# ── Planification calée sur les releases (mode --auto, lancé par launchd) ──
# En dehors des fenêtres de publication : réveil launchd toutes les 10 min mais
# sortie immédiate SANS réseau. À l'approche d'un event 3★ : rafale (poll 60 s)
# jusqu'à capter la valeur publiée, puis propagation snapshot + deploy.
POLL_SEC           = 60          # intervalle de poll pendant une rafale
ARM_LOOKAHEAD      = 16 * 60     # arme la rafale si une release 3★ est < 16 min devant
BURST_WINDOW       = 25 * 60     # reste en éveil jusqu'à 25 min après la release tant que l'actual est vide
MAX_BURST          = 30 * 60     # plafond dur de durée d'une rafale
BASELINE_REFRESH_H = 6           # refetch réseau au moins toutes les 6h (schedule/prévisions) même sans release
LOCK_FILE          = CACHE_DIR / "macro_calendar.lock"
SNAPSHOT_LABEL     = "scf.snapshot"   # CACHE_DIR → racine repo + origin/main (vue locale)
DEPLOY_LABEL       = "scf.cfdeploy"    # build_public : racine → public/ → pages.dev

API = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# Investing.com country IDs (G7 + APAC majors)
COUNTRY_IDS = {
    5: "USA",
    72: "Zone Euro",
    17: "Allemagne",
    4: "Royaume-Uni",
    35: "Japon",
    37: "Chine",
    12: "Suisse",
    6: "Canada",
    25: "Australie",
}

# data-img_key (flag) → label FR pour l'event
COUNTRY_LABEL = {
    "United_States": "USA",
    "Euro_Zone": "Zone Euro",
    "Europe": "Zone Euro",
    "Germany": "Allemagne",
    "United_Kingdom": "Royaume-Uni",
    "Japan": "Japon",
    "China": "Chine",
    "Switzerland": "Suisse",
    "Canada": "Canada",
    "Australia": "Australie",
}

# Mots-clés event → impact crypto historique + biais directionnel
# (parcouru dans l'ordre, premier match gagne)
EVENT_RULES = [
    # FOMC / Fed
    (r"federal funds rate|fomc statement|fomc economic projections|fomc press conference",
     "±4.2%", "neut", "Surveiller le dot plot et le ton du discours."),
    (r"fomc minutes",                  "±2.0%", "neut", "Détails des dissents et bilan des risques."),
    (r"\bfed (chair|chairman) [a-z]+ speaks", "±2.5%", "neut",
     "Tout commentaire sur l'inflation ou le rythme de baisse des taux est market-moving."),
    # ECB / BCE
    (r"ecb (interest|main refinancing) rate|ecb monetary policy", "±1.5%", "neut",
     "Impact indirect sur BTC via EUR/USD et appétit pour le risque."),
    (r"ecb press conference", "±1.3%", "neut", ""),
    # BoE / BoJ
    (r"\bboe (interest )?rate|official bank rate", "±0.8%", "neut", ""),
    (r"\bboj (interest )?rate|monetary policy statement.*jpy|boj policy rate", "±1.0%", "neut",
     "Impact yen / carry trade global."),
    # CPI
    (r"core cpi.*y/y|core cpi.*yoy",   "±3.5%", "bull", "Core CPI = mesure préférée Fed pour l'inflation tendancielle."),
    (r"\bcpi\b.*y/y|\bcpi\b.*yoy",     "±3.8%", "bull",
     "CPI < consensus = favorable crypto. CPI > consensus = pression baissière."),
    (r"\bcpi\b.*m/m|\bcpi\b.*mom",     "±3.0%", "bull", ""),
    (r"\bcpi\b",                       "±3.5%", "bull", ""),
    # PCE
    (r"core pce",                      "±2.9%", "bull", "Indicateur d'inflation préféré de la Fed."),
    (r"\bpce\b",                       "±2.5%", "bull", ""),
    # NFP / Employment
    (r"nonfarm payrolls",              "±1.8%", "neut",
     "Bon NFP = risque hawkishness Fed = légèrement négatif crypto."),
    (r"adp nonfarm",                   "±0.9%", "neut", "Indicateur avancé du NFP officiel."),
    (r"unemployment rate",             "±1.2%", "neut",
     "Hausse du chômage = Fed plus dovish = haussier risk-on."),
    (r"average hourly earnings",       "±1.0%", "neut",
     "Pression salariale = inflation collante."),
    (r"initial jobless claims",        "±0.6%", "neut", "Indicateur hebdomadaire."),
    # GDP
    (r"\bgdp\b.*q/q|\bgdp\b.*qoq",     "±2.1%", "neut", "PIB < 2% = Fed plus dovish = haussier crypto."),
    (r"\bgdp\b.*y/y|\bgdp\b.*yoy",     "±1.8%", "neut", ""),
    (r"\bgdp\b.*m/m|\bgdp\b.*mom",     "±1.0%", "neut", ""),
    (r"\bgdp\b",                       "±1.6%", "neut", ""),
    # PPI
    (r"core ppi",                      "±1.5%", "neut", "Précurseur du Core CPI."),
    (r"\bppi\b",                       "±1.5%", "neut", "Précurseur du CPI."),
    # ISM / PMI
    (r"ism manufacturing pmi|ism manufacturing prices", "±1.3%", "neut", "PMI < 50 = contraction industrielle."),
    (r"ism non.manufacturing|ism services", "±1.2%", "neut", ""),
    (r"s&p global (manufacturing|services|composite) pmi", "±0.8%", "neut", ""),
    # Retail
    (r"retail sales|core retail sales", "±1.4%", "neut", "Ventes faibles = consommateur affaibli = Fed dovish."),
    # Confidence
    (r"cb consumer confidence|consumer confidence", "±0.8%", "neut", ""),
    (r"\bum consumer sentiment|michigan", "±0.7%", "neut", ""),
    # Real estate
    (r"existing home sales|new home sales|building permits|housing starts", "±0.4%", "neut", ""),
    # JOLTS
    (r"jolts job openings",            "±0.7%", "neut", "Job openings = mesure tension marché du travail."),
    # Crude oil inventories
    (r"crude oil inventories",         "±0.3%", "neut", ""),
    # RBA / BoC
    (r"rba (interest )?rate|reserve bank of australia", "±0.6%", "neut", ""),
    (r"boc (interest )?rate|bank of canada", "±0.6%", "neut", ""),
]


def fetch_window(date_from, date_to):
    """POST vers Investing.com filtré 3-étoiles, date range custom."""
    body = []
    for cid in COUNTRY_IDS.keys():
        body.append(("country[]", str(cid)))
    body.append(("importance[]", "3"))
    body.append(("timeZone", "8"))  # 8 = ET (US Eastern)
    body.append(("timeFilter", "timeOnly"))
    body.append(("dateFrom", date_from))
    body.append(("dateTo", date_to))
    body.append(("submitFilters", "1"))
    body.append(("limit_from", "0"))
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(API, data=data, headers={
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://www.investing.com",
        "Referer": "https://www.investing.com/economic-calendar/",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=20).read()
        return json.loads(raw)
    except Exception as e:
        sys.stderr.write(f"[Macro] fetch err: {e}\n")
        return None


NASDAQ_CAL = "https://api.nasdaq.com/api/calendar/economicevents?date={}"

# Nasdaq nomme les pays en anglais ; on retrouve les libellés déjà utilisés par le site.
# Un pays absent de cette table est ignoré : le calendrier ne suit que ces économies,
# et laisser passer le reste noierait les publications qui comptent.
NASDAQ_PAYS = {
    "United States": "USA", "Euro Zone": "Zone Euro", "Germany": "Allemagne",
    "United Kingdom": "Royaume-Uni", "Japan": "Japon", "China": "Chine",
    "Switzerland": "Suisse", "Canada": "Canada", "France": "France",
    "Italy": "Italie", "Spain": "Espagne", "Australia": "Australie",
}


def fetch_window_nasdaq(date_from, date_to):
    """Repli quand Investing.com refuse l'adresse IP (403 depuis un datacenter).

    DIFFÉRENCE ASSUMÉE : Investing était filtré sur les publications « 3 étoiles ».
    Nasdaq ne note pas l'importance — on ne peut donc pas reproduire ce filtre, et
    inventer une note d'importance reviendrait à décider soi-même de ce qui compte.
    On retient à la place les événements que le site sait déjà interpréter (ceux qui
    correspondent à une règle de `classify`), ce qui est un critère explicite et
    vérifiable. Les autres sont écartés.
    """
    events, jour = [], datetime.strptime(date_from, "%Y-%m-%d")
    fin = datetime.strptime(date_to, "%Y-%m-%d")
    while jour <= fin:
        j = jour.strftime("%Y-%m-%d")
        try:
            req = urllib.request.Request(NASDAQ_CAL.format(j), headers={
                "User-Agent": UA, "Accept": "application/json"})
            rows = (json.loads(urllib.request.urlopen(req, timeout=25).read())
                    .get("data") or {}).get("rows") or []
        except Exception as e:
            sys.stderr.write(f"[Macro] repli Nasdaq {j} err: {e}\n")
            jour += timedelta(days=1)
            continue

        for r in rows:
            pays = NASDAQ_PAYS.get((r.get("country") or "").strip())
            if not pays:
                continue
            nom = (r.get("eventName") or "").strip()
            btc, direction, note = classify(nom)
            if not note and not btc:
                continue                      # publication que le site ne sait pas lire

            def propre(v):
                """Nasdaq remplit les valeurs absentes par une espace insécable HTML.
                Sans décodage, « &nbsp; » s'afficherait tel quel dans le calendrier —
                et pire, passerait pour une valeur PUBLIÉE, donc pour un événement déjà
                sorti alors qu'il est à venir."""
                v = html_lib.unescape((v or "").strip()).replace("\xa0", " ").strip()
                return "" if v in ("", "-", "--", "—") else v

            actual, cons, prev = (propre(r.get("actual")), propre(r.get("consensus")),
                                  propre(r.get("previous")))

            hh, mm = 12, 0
            gmt = (r.get("gmt") or "").strip()
            if ":" in gmt:
                try:
                    hh, mm = (int(x) for x in gmt.split(":")[:2])
                except ValueError:
                    hh, mm = 12, 0
            iso = datetime(jour.year, jour.month, jour.day, hh, mm,
                           tzinfo=timezone.utc).isoformat()

            sub = [pays] + ([f"prév. {cons}"] if cons else []) + \
                  ([f"préc. {prev}"] if prev else []) + ["source Nasdaq"]
            events.append({
                "date": iso, "name": nom, "sub": " · ".join(sub), "region": pays,
                "actual": actual, "forecast": cons, "previous": prev,
                "impact": "high", "cat": "macro",
                "btcHist": btc, "btcDir": direction, "note": note,
                "url": "https://www.nasdaq.com/market-activity/economic-calendar",
                "source": "nasdaq",
            })
        jour += timedelta(days=1)

    events.sort(key=lambda x: x["date"])
    return events


def classify(name):
    """Mappe le nom de l'event vers (btcHist, btcDir, note)."""
    n = name.lower()
    for rgx, btc, direction, note in EVENT_RULES:
        if re.search(rgx, n, re.IGNORECASE):
            return btc, direction, note
    return "", "neut", ""


def parse_rows(html):
    """Extrait events 3-étoiles du HTML inline."""
    out = []
    seen = set()
    for m in re.finditer(
        r'<tr id="eventRowId_(\d+)"[^>]+data-event-datetime="([^"]+)"[^>]*>(.*?)</tr>',
        html, re.DOTALL,
    ):
        rid, dt_str, inner = m.groups()
        # Vérifie 3 étoiles (3x grayFullBullishIcon)
        bulls = inner.count("grayFullBullishIcon")
        if bulls < 3:
            continue
        # Pays / devise
        flag_m = re.search(r'class="ceFlags ([A-Za-z_]+)"', inner)
        country = flag_m.group(1) if flag_m else ""
        country_lbl = COUNTRY_LABEL.get(country, country.replace("_", " ") or "?")
        cur_m = re.search(r'class="left flagCur[^"]*"[^>]*>.*?\s+([A-Z]{3})', inner, re.DOTALL)
        currency = cur_m.group(1) if cur_m else ""
        # Nom de l'event + href réel (slug-id) vers la page Investing.com
        name_m = re.search(r'<a\s+([^>]*)>([^<]+)</a>', inner)
        href = ""
        name = ""
        if name_m:
            attrs, name = name_m.group(1), name_m.group(2).strip()
            href_m = re.search(r'href="([^"]+)"', attrs)
            if href_m:
                href = href_m.group(1)
        if not name:
            continue
        # URL de l'event : utilise le href réel (slug-id), fallback sur event-id
        if href.startswith("/"):
            event_url = "https://www.investing.com" + href
        elif href.startswith("http"):
            event_url = href
        else:
            event_url = f"https://www.investing.com/economic-calendar/event-{rid}"
        # Actuel / Prévision / Précédent. Les cellules Investing.com portent un id
        # (eventActual_/eventForecast_/eventPrevious_) et parfois un <span> de
        # couleur (redFont/greenFont) à l'intérieur -> on nettoie le contenu.
        def cell(pattern):
            m = re.search(pattern, inner, re.DOTALL)
            if not m:
                return ""
            raw = re.sub(r"<[^>]+>", "", m.group(1))
            return raw.replace("&nbsp;", "").strip()
        actual = cell(r'<td[^>]*id="eventActual_\d+"[^>]*>(.*?)</td>')
        forecast = cell(r'<td[^>]*id="eventForecast_\d+"[^>]*>(.*?)</td>')
        previous = cell(r'<td[^>]*id="eventPrevious_\d+"[^>]*>(.*?)</td>')
        # Fallback sur les classes si les id ne matchent pas (variantes de markup)
        if not forecast:
            forecast = cell(r'<td class="[^"]*forecast[^"]*"[^>]*>(.*?)</td>')
        if not previous:
            previous = cell(r'<td class="[^"]*prev[^"]*"[^>]*>(.*?)</td>')
        # Date (format : YYYY/MM/DD HH:MM:SS, timezone ET = -04:00 in May)
        try:
            dt = datetime.strptime(dt_str, "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        iso = dt.strftime("%Y-%m-%dT%H:%M:00-04:00")
        key = (name, dt.strftime("%Y-%m-%d"))
        if key in seen:
            continue
        seen.add(key)

        btc, direction, note = classify(name)
        sub_parts = [country_lbl]
        if forecast:
            sub_parts.append(f"prév. {forecast}")
        if previous:
            sub_parts.append(f"préc. {previous}")
        sub = " · ".join(sub_parts)

        out.append({
            "date": iso,
            "name": name,
            "sub": sub,
            "region": country_lbl,       # label pays FR (widget: colonne région)
            "actual": actual,            # valeur publiée (vide tant que non sortie)
            "forecast": forecast,        # consensus / prévision
            "previous": previous,        # publication précédente
            "impact": "high",  # toujours high (filtré 3 étoiles)
            "cat": "macro",
            "btcHist": btc,
            "btcDir": direction,
            "note": note,
            "url": event_url,
        })
    out.sort(key=lambda x: x["date"])
    return out


def inject_into_html(events):
    if not HTML_FILE.exists():
        sys.stderr.write(f"[Macro] {HTML_FILE} not found\n")
        return
    try:
        html = HTML_FILE.read_text()
    except PermissionError as e:
        sys.stderr.write(f"[Macro] HTML read blocked by TCC (ok from launchd): {e}\n")
        return
    block = (
        "// __MACRO_CAL_LIVE_START__\n"
        "  window.__MACRO_CAL_LIVE__ = " +
        json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
        "  window.__MACRO_CAL_UPDATED__ = " +
        json.dumps(datetime.now().strftime("%d/%m/%Y %H:%M")) + ";\n"
        "  // __MACRO_CAL_LIVE_END__"
    )
    bad_html = re.compile(
        r"\s*<!-- __MACRO_CAL_LIVE_START__ -->.*?<!-- __MACRO_CAL_LIVE_END__ -->",
        re.DOTALL,
    )
    if bad_html.search(html):
        html = bad_html.sub("", html)
    js_pattern = re.compile(
        r"// __MACRO_CAL_LIVE_START__.*?// __MACRO_CAL_LIVE_END__",
        re.DOTALL,
    )
    if js_pattern.search(html):
        html2 = js_pattern.sub(block, html)
        sys.stderr.write("[Macro] Replacing JS block\n")
    else:
        marker_match = re.search(r"(\s*)var\s+MACRO_EVENTS\s*=\s*", html)
        if not marker_match:
            sys.stderr.write("[Macro] Cannot find MACRO_EVENTS, abort\n")
            return
        idx = marker_match.start()
        html2 = html[:idx] + "\n  " + block + "\n" + html[idx:]
        sys.stderr.write("[Macro] First-time JS injection before MACRO_EVENTS\n")
    try:
        HTML_FILE.write_text(html2)
        sys.stderr.write(f"[Macro] Injected {len(events)} events\n")
    except PermissionError as e:
        sys.stderr.write(f"[Macro] HTML write blocked by TCC (ok from launchd): {e}\n")


def fetch_and_save():
    """Fetch Investing.com, écrit cache JSON + wrapper JS + inject HTML. Retourne events (ou None)."""
    today = datetime.now()
    date_from = today.strftime("%Y-%m-%d")
    date_to = (today + timedelta(days=60)).strftime("%Y-%m-%d")
    sys.stderr.write(f"[Macro] Investing.com 3-stars {date_from} → {date_to}\n")
    raw = fetch_window(date_from, date_to)
    if raw:
        events = parse_rows(raw.get("data", ""))
        sys.stderr.write(f"[Macro] {len(events)} events 3-stars extracted\n")
    else:
        # Investing refuse les IP de datacenter (403). Le repli couvre une fenêtre plus
        # courte à dessein : il interroge un jour à la fois, et 60 appels à chaque
        # exécution feraient de nous exactement le robot que ces sources filtrent.
        # Deux semaines suffisent au calendrier affiché ; le reste se remplira aux
        # exécutions suivantes.
        court = (today + timedelta(days=14)).strftime("%Y-%m-%d")
        sys.stderr.write(f"[Macro] Investing indisponible → repli Nasdaq {date_from} → {court}\n")
        events = fetch_window_nasdaq(date_from, court)
        if not events:
            return None
        sys.stderr.write(f"[Macro] {len(events)} events via Nasdaq\n")

    payload = {"events": events, "updated": datetime.now().isoformat()}
    with open(CACHE_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False)
    # Wrapper JS loadable via <script src> (resilient au blocage TCC sur News_Crypto.html)
    updated_str = datetime.now().strftime("%d/%m/%Y %H:%M")
    with open(JS_CACHE, "w") as f:
        f.write(
            "window.__MACRO_CAL_LIVE__=" + json.dumps(events, ensure_ascii=False, separators=(",", ":")) + ";\n"
            "window.__MACRO_CAL_UPDATED__=" + json.dumps(updated_str) + ";\n"
        )
    inject_into_html(events)
    return events


def load_cached_events():
    if CACHE_FILE.exists():
        try:
            return json.load(open(CACHE_FILE)).get("events", [])
        except Exception:
            return []
    return []


def actual_map(events):
    """(nom, jour) → valeur actuelle nettoyée."""
    return {(e["name"], e["date"][:10]): (e.get("actual") or "").strip() for e in events}


def newly_filled(old_map, events):
    """Clés dont l'actual vient de passer de vide → non-vide."""
    out = []
    for e in events:
        key = (e["name"], e["date"][:10])
        if (e.get("actual") or "").strip() and not old_map.get(key):
            out.append(key)
    return out


def pending_releases(events, now):
    """Events 3★ dans [now-BURST_WINDOW, now+ARM_LOOKAHEAD] dont l'actual est encore vide."""
    out = []
    for e in events:
        try:
            dt = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        delta = (dt - now).total_seconds()
        if -BURST_WINDOW <= delta <= ARM_LOOKAHEAD and not (e.get("actual") or "").strip():
            out.append(e)
    return out


def _kick(label):
    """Déclenche un job launchd maintenant (réutilise le job existant et ses garde-fous)."""
    try:
        r = subprocess.run(
            ["launchctl", "kickstart", f"gui/{os.getuid()}/{label}"],
            capture_output=True, text=True, timeout=20,
        )
        sys.stderr.write(f"[Macro] kick {label}: rc={r.returncode} {r.stderr.strip()}\n")
    except Exception as e:
        sys.stderr.write(f"[Macro] kick {label} err: {e}\n")


def propagate(newly):
    names = ", ".join(sorted({n for n, _ in newly}))
    sys.stderr.write(f"[Macro] NOUVEL ACTUAL → propagation: {names}\n")
    _kick(SNAPSHOT_LABEL)   # CACHE_DIR → racine repo + origin/main (vue locale fraîche)
    time.sleep(90)          # laisse snapshot copier la donnée fraîche vers la racine
    _kick(DEPLOY_LABEL)     # build_public : racine → public/ → pages.dev (vue en ligne)


def main():
    force = "--force" in sys.argv
    auto = "--auto" in sys.argv

    # Verrou anti-chevauchement : une rafale peut durer plusieurs minutes, le
    # réveil launchd suivant ne doit pas lancer une 2e instance en parallèle.
    lockf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.stderr.write("[Macro] déjà en cours (lock) — skip\n")
        return

    old_map = actual_map(load_cached_events())
    cache_age_h = ((datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600) if CACHE_FILE.exists() else 999.0

    # Appel manuel simple (ni --force ni --auto) : comportement historique.
    if not (force or auto):
        if cache_age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f"[Macro] cache frais ({cache_age_h:.1f}h)\n")
            return
        fetch_and_save()
        return

    if force:
        events = fetch_and_save()
        if events is not None:
            nf = newly_filled(old_map, events)
            if nf:
                propagate(nf)
        return

    # ── mode --auto (launchd, calé sur les releases) ──
    now = datetime.now(timezone.utc)
    hot = bool(pending_releases(load_cached_events(), now))
    stale = cache_age_h >= BASELINE_REFRESH_H
    if not (hot or stale):
        sys.stderr.write(f"[Macro] idle (aucune release imminente, cache {cache_age_h:.1f}h) — no-op\n")
        return

    any_new = []
    events = fetch_and_save()
    if events is not None:
        any_new += newly_filled(old_map, events)
        old_map = actual_map(events)

    # Rafale calée sur la release tant qu'un actual reste vide dans la fenêtre.
    if events is not None and pending_releases(events, datetime.now(timezone.utc)):
        sys.stderr.write("[Macro] rafale calée release: poll 60s\n")
        t0 = time.time()
        while time.time() - t0 < MAX_BURST:
            time.sleep(POLL_SEC)
            events = fetch_and_save()
            if events is None:
                continue
            any_new += newly_filled(old_map, events)
            old_map = actual_map(events)
            if not pending_releases(events, datetime.now(timezone.utc)):
                break

    if any_new:
        propagate(any_new)
    else:
        sys.stderr.write("[Macro] run terminé, aucun nouvel actual\n")


if __name__ == "__main__":
    main()
