#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_tv_others_cache.py — publie CRYPTOCAP:OTHERS (bars hebdo) en cache autonome.

POURQUOI CE COLLECTEUR EXISTE
La page Stablecoins_Flows lisait ces bars depuis window.__SD__, c'est-a-dire depuis
le HTML lui-meme, fige au moment du knit. TradingView refuse les appels navigateur
(CORS), donc la valeur avait ete captee au rendu — et n'etait plus jamais rafraichie
ensuite. Mesure le 07/09/2026 : la page en ligne s'arretait au 31/08 alors que
TradingView servait deja la barre du 07/09. Le ratio OTHERS/Stables affiche, et son
« +30,53 % / 30j », portaient donc sur une serie vieille d'une semaine — un ecart qui
grandit tant que personne ne re-knit.

On sort donc ces bars du HTML : ce collecteur les republie tout seul, et la page les
lit comme un fichier, exactement comme le fait deja stablecoin_destinations_cache.js.

CADENCE
Les bars sont HEBDOMADAIRES, mais la barre de la semaine EN COURS bouge tous les
jours : la collecte est donc quotidienne, pas hebdomadaire. Une cadence weekly
laisserait la barre courante figee jusqu'a six jours — le defaut qu'on corrige.

SORTIE
  ~/Library/Caches/site_crypto_finance/tv_others_cache.js    (window.__TV_OTHERS__)
  ~/Library/Caches/site_crypto_finance/tv_others_cache.json  (jumeau : temoin + comparaison)

C'est le dossier que run_jobs.py releve apres chaque passage pour publier dans le
depot. Ecrire ailleurs (dans release/, par exemple) donne un collecteur qui reussit
en apparence et dont le fichier n'est jamais publie : run_jobs le range alors parmi
les « fichiers du manifeste jamais produits », sans lever la moindre erreur.
"""

import json
import os
import re
import sys
import time

SYMBOL = "CRYPTOCAP:OTHERS"
INTERVAL = "1W"
BAR_COUNT = 5000
TIMEOUT = 45

# Une serie saine compte ~650 bars hebdo (depuis 2014). En dessous de ce plancher,
# la reponse est tronquee : on refuse plutot que de publier une histoire amputee.
MIN_BARS = 400

CACHE_DIR = os.path.join(os.path.expanduser("~"), "Library", "Caches",
                         "site_crypto_finance")
OUT_JS = os.path.join(CACHE_DIR, "tv_others_cache.js")
OUT_JSON = OUT_JS.replace(".js", ".json")


def log(m):
    print("[tv_others] " + str(m), file=sys.stderr)


def fetch_bars():
    """Renvoie (bars, completed, erreur). bars = [{d,p}] trie et dedoublonne."""
    try:
        import websocket
    except ImportError:
        return [], False, "websocket-client absent"

    bars = []
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&date="
            + str(int(time.time())) + "&type=chart",
            origin="https://www.tradingview.com",
            timeout=15,
            header=["User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"],
        )
    except Exception as e:
        return [], False, "connexion: " + str(e)

    def send(d):
        s = json.dumps(d, separators=(",", ":"))
        ws.send("~m~" + str(len(s)) + "~m~" + s)

    session = "cs_anon_" + str(int(time.time()))[-8:]
    sym_id = "sds_sym_1"
    try:
        send({"m": "set_auth_token", "p": ["unauthorized_user_token"]})
        send({"m": "chart_create_session", "p": [session, ""]})
        send({"m": "resolve_symbol",
              "p": [session, sym_id,
                    '={"symbol":"' + SYMBOL + '","adjustment":"splits"}']})
        send({"m": "create_series",
              "p": [session, "sds_1", "s1", sym_id, INTERVAL, BAR_COUNT, ""]})
    except Exception as e:
        try:
            ws.close()
        except Exception:
            pass
        return [], False, "ouverture: " + str(e)

    t0 = time.time()
    completed = False
    buf = ""
    err = None

    while time.time() - t0 < TIMEOUT and not completed:
        try:
            ws.settimeout(3)
            msg = ws.recv()
        except Exception as e:
            if "timed out" in str(e).lower():
                continue
            break
        if not msg:
            continue
        buf += msg

        while True:
            m = re.match(r"~m~(\d+)~m~", buf)
            if not m:
                break
            ln = int(m.group(1))
            hl = len(m.group(0))
            if len(buf) < hl + ln:
                break
            payload = buf[hl:hl + ln]
            buf = buf[hl + ln:]

            if payload.startswith("~h~"):
                # Battement de coeur : le renvoyer tel quel, sinon TradingView coupe.
                try:
                    ws.send("~m~" + str(len(payload)) + "~m~" + payload)
                except Exception:
                    pass
                continue

            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue

            kind = parsed.get("m", "")
            if kind == "timescale_update":
                pl = parsed.get("p", [])
                if len(pl) >= 2 and isinstance(pl[1], dict):
                    sds = pl[1].get("sds_1", {})
                    if isinstance(sds, dict):
                        for b in sds.get("s", []):
                            v = b.get("v") if isinstance(b, dict) else None
                            if isinstance(v, list) and len(v) >= 5:
                                try:
                                    ts = int(v[0])
                                    close = float(v[4])
                                    if close > 0:
                                        bars.append({"d": ts, "p": close})
                                except (TypeError, ValueError):
                                    pass
            elif kind == "series_completed":
                completed = True
                break
            elif kind in ("symbol_error", "series_error"):
                err = "tv: " + json.dumps(parsed)[:200]
                break

        if err:
            break

    try:
        ws.close()
    except Exception:
        pass

    seen = {}
    for b in bars:
        seen[b["d"]] = b["p"]
    out = [{"d": d, "p": p} for d, p in sorted(seen.items())]
    return out, completed, err


def lire_cache_precedent():
    try:
        with open(OUT_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    t_start = time.time()
    bars, completed, err = fetch_bars()

    if err:
        log("echec de collecte : " + str(err))
    log(str(len(bars)) + " bars, completed=" + str(completed))

    # ── Gardes : on ne remplace un cache existant que par MIEUX ──────────────────
    # Un collecteur qui ecrit du vide est pire qu'un collecteur qui ne tourne pas :
    # il efface une donnee juste et l'ecrase par du rien, en silence.
    ancien = lire_cache_precedent()
    n_ancien = len(ancien.get("bars", [])) if ancien else 0

    if len(bars) < MIN_BARS:
        log("REFUS : " + str(len(bars)) + " bars < plancher " + str(MIN_BARS)
            + " — cache precedent conserve (" + str(n_ancien) + " bars)")
        return 1

    if not completed:
        log("REFUS : serie non terminee par TradingView — cache precedent conserve")
        return 1

    if ancien and len(bars) < n_ancien * 0.9:
        log("REFUS : " + str(n_ancien) + " -> " + str(len(bars))
            + " bars (perte de plus de 10 %) — cache precedent conserve")
        return 1

    derniere = bars[-1]["d"]
    if ancien:
        ancienne_derniere = ancien.get("bars", [{}])[-1].get("d", 0)
        if derniere < ancienne_derniere:
            log("REFUS : derniere barre " + str(derniere) + " anterieure a "
                + str(ancienne_derniere) + " — cache precedent conserve")
            return 1

    now_ts = int(time.time())
    payload = {
        "generated_at": now_ts,
        # Horodatage de la derniere barre : la page s'en sert pour savoir si la
        # barre la plus recente est CELLE DE LA SEMAINE EN COURS (donc partielle)
        # ou une semaine close.
        "last_bar": derniere,
        "count": len(bars),
        "bars": bars,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "unit": "USD absolu (market cap OTHERS, hors top 10)",
        "source": "TradingView WebSocket public — CRYPTOCAP:OTHERS",
    }

    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)

    js = ("/* Genere par fetch_tv_others_cache.py — ne pas editer a la main. */\n"
          "(function(){var d=" + json.dumps(payload, separators=(",", ":"))
          + ";window.__TV_OTHERS__=d;})();\n")

    # Ecriture atomique : jamais un fichier a moitie ecrit servi au navigateur.
    tmp = OUT_JS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(js)
    os.replace(tmp, OUT_JS)

    tmpj = OUT_JSON + ".tmp"
    with open(tmpj, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmpj, OUT_JSON)

    log("OK -> " + OUT_JS + " (" + str(round(os.path.getsize(OUT_JS) / 1024))
        + " Ko) — " + str(len(bars)) + " bars, derniere "
        + time.strftime("%Y-%m-%d", time.gmtime(derniere))
        + ", " + str(round(time.time() - t_start)) + " s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
