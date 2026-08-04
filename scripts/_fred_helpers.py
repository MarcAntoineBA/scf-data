"""Helper FRED partagé — fetch via API officielle st louis fed.

Pourquoi : fred.stlouisfed.org/graph/fredgraph.csv est devenu inaccessible
depuis ce réseau (timeout total observé 2026-05-19). L'API officielle
api.stlouisfed.org marche normalement avec une clé gratuite.

Usage :
    from _fred_helpers import fetch_fred
    obs = fetch_fred("DCOILWTICO", start="1986-01-01")
    # obs == {"dates": [...], "values": [...], "fred_id": "DCOILWTICO",
    #         "source_url": "https://fred.stlouisfed.org/series/DCOILWTICO"}
    # ou None en cas d'échec.
"""
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Clé FRED API (gratuite, enregistrée par l'auteur 2026-05-19).
# Override possible via env var FRED_API_KEY.
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"


def fetch_fred(series_id, start=None, end=None, units=None,
               timeout=20, max_retries=4, ua="Mozilla/5.0 SiteCryptoFinance"):
    """Récupère une série FRED via l'API officielle.

    series_id : ID FRED (ex 'DCOILWTICO', 'M2SL', etc.)
    start, end : 'YYYY-MM-DD' (None => toute la série)
    units : 'lin' (par défaut), 'chg', 'pch', 'log', 'pc1', etc.

    Retourne {'dates': [...], 'values': [...], 'fred_id': ..., 'source_url': ...}
    ou None si la série est inexistante / l'API échoue.
    """
    params = {
        "series_id": series_id,
        "api_key":   FRED_API_KEY,
        "file_type": "json",
    }
    if start: params["observation_start"] = start
    if end:   params["observation_end"]   = end
    if units: params["units"]             = units

    url = FRED_API_URL + "?" + urlencode(params)
    req = Request(url, headers={"User-Agent": ua, "Accept": "application/json"})

    last_err = None
    for attempt in range(max_retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
            d = json.loads(body)
            obs = d.get("observations") or []
            dates, values = [], []
            for r in obs:
                v = r.get("value", "")
                if v in ("", ".", "NA", None):
                    continue
                try:
                    values.append(float(v))
                    dates.append(r["date"])
                except (ValueError, KeyError):
                    continue
            if not dates:
                sys.stderr.write(f"[FRED {series_id}] empty series\n")
                return None
            return {
                "dates": dates, "values": values, "fred_id": series_id,
                "source_url": f"https://fred.stlouisfed.org/series/{series_id}",
            }
        except HTTPError as e:
            # 400 = série inexistante/discontinuée → ne pas retry
            if e.code == 400:
                try:
                    err_body = e.read().decode("utf-8", errors="ignore")
                    err_msg = json.loads(err_body).get("error_message", "")
                except Exception:
                    err_msg = str(e)
                sys.stderr.write(f"[FRED {series_id}] {err_msg}\n")
                return None
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))
        except (URLError, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = e
            if attempt < max_retries - 1:
                time.sleep(3 * (2 ** attempt))
    sys.stderr.write(f"[FRED {series_id}] giving up after {max_retries} retries: {last_err}\n")
    return None


def fetch_fred_csv(series_id, start=None):
    """Alias compatible avec l'ancienne signature fetch_fred_csv(series_id, start)
    pour drop-in replacement dans les fetchers existants."""
    return fetch_fred(series_id, start=start)
