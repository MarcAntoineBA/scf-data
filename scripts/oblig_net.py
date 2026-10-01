#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Accès réseau communs du collecteur obligataire (fetch_obligations.py).

⚠ LE BON IDENTIFIANT DÉPEND DE LA SOURCE (mesuré le 30/09/2026) :
  - FRED, Fed, Yahoo… : un agent HONNÊTE qui nomme le script passe ; celui de
    Python par défaut prend 403 (Fed) ou 429 (Yahoo) ; un faux « Mozilla »
    fait couper la connexion par FRED.
  - FMI (imf.org) : l'INVERSE — un agent personnalisé prend 403, celui de
    Python passe. On ne déguise rien : on laisse celui de la bibliothèque.
  - Yahoo depuis les machines du nuage : 403 sans empreinte TLS de navigateur
    → curl_cffi, comme tous les collecteurs du dépôt qui lisent Yahoo.
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

try:
    from curl_cffi import requests as _cffi
except Exception:  # noqa: BLE001
    _cffi = None

UA_HONNETE = "scf-data/1.0 (collecte de donnees publiques; " + \
    (os.environ.get("SCF_CONTACT_UA") or "https://site-crypto-finance.pages.dev") + ")"
_dernier = [0.0]


def log(*a):
    print(*a, file=sys.stderr)


def get(url, ua="honnete", accept="*/*", essais=3, timeout=90, debit=0.4, navigateur=False, entetes=None):
    """Rend les octets, ou None (404/400, ou échec après `essais`)."""
    for essai in range(essais):
        d = time.time() - _dernier[0]
        if d < debit:
            time.sleep(debit - d)
        _dernier[0] = time.time()
        if navigateur and _cffi is not None:
            try:
                r = _cffi.get(url, headers={"Accept": accept}, impersonate="chrome120", timeout=timeout)
                if r.status_code == 200:
                    return r.content
                if r.status_code in (400, 404):
                    return None
                time.sleep((6 if r.status_code == 429 else 2) * (essai + 1))
                continue
            except Exception as e:  # noqa: BLE001
                if essai == essais - 1:
                    log("[warn] %s → %s" % (url[:100], e))
                    return None
                time.sleep(2 * (essai + 1))
                continue
        h = {"Accept": accept, "Accept-Encoding": "gzip"}
        if ua == "honnete":
            h["User-Agent"] = UA_HONNETE
        elif ua:
            h["User-Agent"] = ua
        if entetes:
            h.update(entetes)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if essai == essais - 1:
                log("[warn] %s → HTTP %s" % (url[:100], e.code))
                return None
            time.sleep((6 if e.code == 429 else 2) * (essai + 1))
        except Exception as e:  # noqa: BLE001
            if essai == essais - 1:
                log("[warn] %s → %s" % (url[:100], e))
                return None
            time.sleep(2 * (essai + 1))
    return None


def get_txt(url, **kw):
    b = get(url, **kw)
    return b.decode("utf-8", "replace") if b is not None else None


def get_json(url, **kw):
    b = get(url, **kw)
    if b is None:
        return None
    try:
        return json.loads(b)
    except Exception:  # noqa: BLE001
        return None


def estnb(v):
    return isinstance(v, (int, float)) and v == v and v not in (float("inf"), float("-inf"))


# ── FRED ────────────────────────────────────────────────────────────────────
def _cle_fred():
    k = os.environ.get("FRED_API_KEY")
    if k:
        return k
    # Sur le Mac : la copie locale du module partagé porte la clé.
    for d in (os.path.dirname(os.path.abspath(__file__)),
              os.path.expanduser("~/Library/Application Support/SiteCryptoFinance")):
        p = os.path.join(d, "_fred_helpers.py")
        if os.path.isfile(p):
            try:
                sys.path.insert(0, d)
                import _fred_helpers as fh  # noqa: E402
                return getattr(fh, "FRED_API_KEY", "") or None
            except Exception:  # noqa: BLE001
                return None
            finally:
                sys.path.pop(0)
    return None


_FRED_CLE = [None, False]


def fred(sid, debut=None):
    """[(date ISO, valeur)] ; API officielle avec clé, repli CSV public."""
    if not _FRED_CLE[1]:
        _FRED_CLE[0], _FRED_CLE[1] = _cle_fred(), True
    cle = _FRED_CLE[0]
    if cle:
        q = {"series_id": sid, "api_key": cle, "file_type": "json"}
        if debut:
            q["observation_start"] = debut
        j = get_json("https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(q), accept="application/json")
        if j and "observations" in j:
            out = []
            for o in j["observations"]:
                try:
                    out.append((o["date"], float(o["value"])))
                except (ValueError, TypeError):
                    pass
            return out
    t = get_txt("https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + sid + ("&cosd=" + debut if debut else ""))
    if not t:
        return []
    out = []
    for r in csv.reader(io.StringIO(t)):
        if len(r) < 2 or not r[0][:1].isdigit():
            continue
        try:
            out.append((r[0], float(r[1])))
        except ValueError:
            pass
    return out


# ── Yahoo ───────────────────────────────────────────────────────────────────
def yahoo_jours(sym, ajuste=True, periode="max"):
    """[(date de séance ISO, clôture)] — ajustée des dividendes si `ajuste`.
    ⚠ `range=max` rend des barres MENSUELLES : bornes explicites."""
    plage = ("period1=0&period2=%d" % int(time.time())) if periode == "max" else ("range=" + periode)
    u = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?%s&interval=1d&events=div%%2Csplit&includeAdjustedClose=true"
         % (urllib.parse.quote(sym, safe=""), plage))
    raw = get(u, accept="application/json", timeout=60, navigateur=True, debit=0.3)
    if not raw:
        return []
    try:
        r = json.loads(raw)["chart"]["result"][0]
        off = int((r.get("meta") or {}).get("gmtoffset") or 0)
        ts = r["timestamp"]
        cl = r["indicators"]["adjclose"][0]["adjclose"] if ajuste else r["indicators"]["quote"][0]["close"]
    except Exception:  # noqa: BLE001
        return []
    jours = {}
    for t, c in zip(ts, cl):
        if estnb(c) and c > 0:
            jours[datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()] = float(c)
    out = sorted(jours.items())
    per = ((r.get("meta") or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    if out and per.get("start") and per.get("end") and time.time() < per["end"]:
        seance = datetime.fromtimestamp(per["start"] + off, timezone.utc).date().isoformat()
        if out[-1][0] == seance:
            out.pop()   # la séance en cours n'est pas une clôture
    return out


# ── Séries : réduire, mesurer ──────────────────────────────────────────────
def hebdo(pts):
    """Dernier point de chaque semaine (vendredi ou avant)."""
    out, cle_prec = [], None
    for d, v in pts:
        y, w, _ = date.fromisoformat(d).isocalendar()
        if (y, w) == cle_prec:
            out[-1] = (d, v)
        else:
            out.append((d, v))
            cle_prec = (y, w)
    return out


def mensuel(pts):
    out, cle_prec = [], None
    for d, v in pts:
        if d[:7] == cle_prec:
            out[-1] = (d, v)
        else:
            out.append((d, v))
            cle_prec = d[:7]
    return out


def compacter(pts, jours_recents=1830):
    """Quotidien sur les `jours_recents` derniers jours, hebdomadaire avant :
    le détail là où l'œil le cherche, un fichier léger pour 60 ans d'histoire."""
    if not pts:
        return []
    fin = date.fromisoformat(pts[-1][0])
    lim = date.fromordinal(fin.toordinal() - jours_recents).isoformat()
    vieux = [p for p in pts if p[0] < lim]
    recents = [p for p in pts if p[0] >= lim]
    return hebdo(vieux) + recents


def en_colonnes(pts, dec=3):
    """{"d": [...], "v": [...]} — plus léger que des paires."""
    return {"d": [p[0] for p in pts], "v": [round(p[1], dec) for p in pts]}


def valeur_au(pts, iso):
    """Dernière valeur AU PLUS TARD à la date (règle du Comparateur)."""
    r = None
    for d, v in pts:
        if d <= iso:
            r = v
        else:
            break
    return r


def il_y_a(iso, jours):
    return date.fromordinal(date.fromisoformat(iso).toordinal() - jours).isoformat()


def point_au(pts, iso):
    """(date, valeur) du dernier point AU PLUS TARD à la date, ou None."""
    r = None
    for d, v in pts:
        if d <= iso:
            r = (d, v)
        else:
            break
    return r


# ⚠ Un point de référence trop ANCIEN fausse la variation sans bruit : la France
#   n'a qu'un trou de trois mois dans son quotidien, et « sur 1 mois » reprenait
#   le 30 juin. Au-delà de la tolérance, pas de variation.
TOL = {"1s": 5, "1m": 7, "3m": 12, "1a": 20, "3a": 40, "5a": 40, "ytd": 12}


def variations(pts, mult=100.0, mensuel=False):
    """Écarts (en pb si mult=100 et série en %) sur 1 sem., 1 mois, 3 mois, 1 an, 3 ans, 5 ans, depuis janvier."""
    if not pts:
        return {}
    d, v = pts[-1]
    out = {}
    cibles = [("1s", il_y_a(d, 7)), ("1m", il_y_a(d, 30)), ("3m", il_y_a(d, 91)), ("1a", il_y_a(d, 365)),
              ("3a", il_y_a(d, 1095)), ("5a", il_y_a(d, 1826)), ("ytd", "%d-12-31" % (int(d[:4]) - 1))]
    for cle, iso in cibles:
        if mensuel and cle == "1s":
            continue
        p = point_au(pts, iso)
        if not p:
            continue
        tol = 40 if mensuel else TOL[cle]
        if (date.fromisoformat(iso) - date.fromisoformat(p[0])).days > tol:
            continue
        out[cle] = round((v - p[1]) * mult, 1)
    return out


def centile(pts, jours=None):
    """Place de la dernière valeur dans l'historique (0 = plus bas, 100 = plus haut)."""
    if not pts:
        return None
    if jours:
        lim = il_y_a(pts[-1][0], jours)
        pts = [p for p in pts if p[0] >= lim]
    vals = sorted(p[1] for p in pts)
    if len(vals) < 20:
        return None
    x = pts[-1][1]
    inf = sum(1 for v in vals if v < x)
    ega = sum(1 for v in vals if v == x)
    return round(100 * (inf + 0.5 * ega) / len(vals), 1)


def extremes(pts, jours=None):
    if not pts:
        return None
    if jours:
        lim = il_y_a(pts[-1][0], jours)
        pts = [p for p in pts if p[0] >= lim]
    hi = max(pts, key=lambda p: p[1])
    lo = min(pts, key=lambda p: p[1])
    return {"haut": round(hi[1], 3), "haut_d": hi[0], "bas": round(lo[1], 3), "bas_d": lo[0]}


def perf(pts, jours):
    """Performance (en %) d'une série de PRIX sur `jours`."""
    if not pts:
        return None
    d, v = pts[-1]
    a = valeur_au(pts, il_y_a(d, jours))
    if not a or date.fromisoformat(pts[0][0]) > date.fromisoformat(il_y_a(d, jours)):
        return None
    return round(100 * (v / a - 1), 2)


def perf_ytd(pts):
    if not pts:
        return None
    a = valeur_au(pts, "%d-12-31" % (int(pts[-1][0][:4]) - 1))
    return round(100 * (pts[-1][1] / a - 1), 2) if a else None


def repli_max(pts, jours=None):
    """Pire baisse d'un PRIX entre un sommet et un creux ultérieur."""
    if jours and pts:
        lim = il_y_a(pts[-1][0], jours)
        pts = [p for p in pts if p[0] >= lim]
    hi, pire, d_hi, pire_d = None, 0.0, None, None
    for d, v in pts:
        if hi is None or v > hi:
            hi, d_hi = v, d
        dd = 100 * (v / hi - 1)
        if dd < pire:
            pire, pire_d = dd, (d_hi, d)
    return {"pct": round(pire, 1), "sommet": pire_d[0], "creux": pire_d[1]} if pire_d else None
