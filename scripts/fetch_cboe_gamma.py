#!/usr/bin/env python3
"""Gamma des dealers (GEX) — onglet Structure de marché, partie TradFi.

Le Cboe publie gratuitement ses chaînes d'options avec les grecques, l'open
interest et le volume, contrat par contrat : 31 000 contrats pour le seul SPX.
C'est de quoi reconstituer le POSITIONNEMENT EN GAMMA des teneurs de marché —
l'équivalent actions du funding cross-venue côté crypto. Personne ne le publie
gratuitement.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CE QUE CE CHIFFRE EST, ET CE QU'IL N'EST PAS                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
Le GEX repose sur une CONVENTION, pas sur une observation : on suppose les
teneurs de marché LONGS des calls et SHORTS des puts, parce que le flux public
achète des calls et achète des puts de protection. C'est l'hypothèse retenue par
tout le monde, elle est raisonnable, et elle reste une hypothèse. Les positions
réelles des dealers ne sont publiées nulle part. La page doit le dire.

    GEX(strike) = gamma × OI × 100 × S² × 0,01     signé + pour les calls, − pour les puts

Unité : dollars de delta à couvrir par mouvement de 1 % du sous-jacent.

  GEX > 0  — les dealers achètent la baisse et vendent la hausse pour rester
             neutres : le marché est amorti, épinglé, la volatilité s'écrase.
  GEX < 0  — ils font l'inverse : ils vendent la baisse et achètent la hausse,
             les mouvements s'AMPLIFIENT. C'est le régime des journées violentes.

Le POINT DE BASCULE calculé ici est une APPROXIMATION : on cumule le GEX par
strike et on relève où le cumul change de signe. Le calcul exact demanderait de
repriẍer chaque gamma à plusieurs niveaux de spot — ce que la chaîne, figée à un
instant, ne permet pas. L'ordre de grandeur est juste, le niveau au point près
ne l'est pas. Le libellé de la page doit dire « ≈ ».

╔══════════════════════════════════════════════════════════════════════════════╗
║ LATENCE — à afficher, jamais à masquer                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
Ces chaînes sont DIFFÉRÉES (fin de séance précédente, parfois la veille). Le
champ `observe` porte l'horodatage que le Cboe déclare, et c'est LUI qui doit
s'afficher à côté du chiffre — pas l'heure de collecte. Un visiteur qui découvre
après coup qu'un chiffre « live » datait de la veille cesse de croire au reste
du site, y compris aux modules crypto qui, eux, sont réellement à jour.

Sorties (~/Library/Caches/site_crypto_finance/) :
  tradfi_gamma_cache.json + .js     window.__TRADFI_GAMMA__ — profil + niveaux
  tradfi_gamma_hist.json            série quotidienne du régime, sans limite

Poids : les chaînes pèsent 13,9 Mo (SPX), 6,5 (SPY) et 5,9 (QQQ). On ne
versionne RIEN de tout ça : on calcule et on n'écrit que le dérivé, ~60 Ko.
Verrou de 25 Mo oblige, et de toute façon personne ne relira la chaîne d'hier.

Cadence quotidienne : la donnée étant de fin de séance, la rafraîchir plus vite
ne ferait que republier le même contenu.
"""
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "tradfi_gamma_cache.json"
OUT_JS = CACHE_DIR / "tradfi_gamma_cache.js"
HIST = CACHE_DIR / "tradfi_gamma_hist.json"

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}

# _SPX est l'INDICE (préfixe souligné chez Cboe) ; SPY et QQQ sont les ETF. On
# garde les trois : l'indice porte le gros du gamma institutionnel, les ETF
# portent le flux de détail, et l'écart entre les deux est en soi une lecture.
SYMBOLES = [
    ("SPX", "_SPX.json", "S&P 500 (indice)"),
    ("SPY", "SPY.json", "S&P 500 (ETF)"),
    ("QQQ", "QQQ.json", "Nasdaq 100 (ETF)"),
]

RX = re.compile(r"^([A-Z^_]+)(\d{6})([CP])(\d{8})$")


def get(url, essais=3):
    dernier = None
    for n in range(1, essais + 1):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=180).read()
        except Exception as e:                                    # noqa: BLE001
            dernier = e
            if n < essais:
                time.sleep(10 * n)
    raise RuntimeError(f"{url} injoignable : {dernier}")


def jours(exp, ref):
    """Jours calendaires jusqu'à l'échéance, depuis la date déclarée du snapshot."""
    try:
        d = datetime.strptime("20" + exp, "%Y%m%d").date()
        return (d - ref).days
    except Exception:                                             # noqa: BLE001
        return None


def analyser(sym, fichier):
    brut = get(BASE + fichier)
    d = json.loads(brut)
    data = d["data"]
    spot = data.get("current_price")
    if not spot:
        raise RuntimeError(f"{sym} : pas de prix courant")
    observe = d.get("timestamp")
    ref = datetime.strptime(observe[:10], "%Y-%m-%d").date()

    par_strike, par_strike_court = {}, {}
    tot = court = 0.0
    oi_c = oi_p = vol_c = vol_p = 0.0
    vol_0dte = 0.0
    k_mult = 100.0 * spot * spot * 0.01          # $ par 1 % de mouvement

    for o in data.get("options", []):
        m = RX.match(o.get("option", ""))
        if not m:
            continue
        _, exp, cp, k = m.groups()
        strike = int(k) / 1000.0
        g = o.get("gamma") or 0.0
        oi = o.get("open_interest") or 0.0
        vol = o.get("volume") or 0.0
        dte = jours(exp, ref)

        if cp == "C":
            oi_c += oi
            vol_c += vol
        else:
            oi_p += oi
            vol_p += vol
        if dte is not None and dte <= 0:
            vol_0dte += vol

        # Convention : dealers longs des calls, shorts des puts. Documentée en
        # tête de fichier — ce n'est PAS une mesure, c'est une hypothèse.
        gex = g * oi * k_mult * (1 if cp == "C" else -1)
        par_strike[strike] = par_strike.get(strike, 0.0) + gex
        tot += gex
        # Le gamma court terme est celui qui bouge vraiment le marché du jour :
        # au-delà d'un mois, il est trop étalé pour contraindre une séance.
        if dte is not None and dte <= 30:
            par_strike_court[strike] = par_strike_court.get(strike, 0.0) + gex
            court += gex

    # Le profil publié est borné à ±15 % : au-delà, les strikes ne pèsent rien et
    # ne feraient qu'alourdir le fichier et écraser l'échelle du graphe.
    profil = sorted((k, round(v / 1e9, 4)) for k, v in par_strike.items()
                    if abs(k - spot) / spot <= 0.15)

    murs = sorted(((k, v) for k, v in par_strike.items()
                   if abs(k - spot) / spot <= 0.12),
                  key=lambda t: -abs(t[1]))[:10]

    # Point de bascule : première traversée du zéro par le CUMUL. Approximation
    # assumée (cf. en-tête) — d'où le « ≈ » obligatoire côté page.
    cum, bascule = 0.0, None
    for k in sorted(par_strike):
        avant = cum
        cum += par_strike[k]
        if (avant < 0 <= cum) or (avant > 0 >= cum):
            bascule = k

    return {
        "spot": round(spot, 2),
        "observe": observe,
        "iv30": data.get("iv30"),
        "contrats": len(data.get("options", [])),
        "gex_total_md": round(tot / 1e9, 3),
        "gex_30j_md": round(court / 1e9, 3),
        "regime": "long" if tot > 0 else "short",
        "bascule": None if bascule is None else round(bascule, 1),
        "bascule_ecart_pct": None if bascule is None else round((bascule / spot - 1) * 100, 2),
        "oi_call": int(oi_c), "oi_put": int(oi_p),
        "pc_oi": round(oi_p / oi_c, 4) if oi_c else None,
        "pc_volume": round(vol_p / vol_c, 4) if vol_c else None,
        "part_0dte": round(vol_0dte / (vol_c + vol_p), 4) if (vol_c + vol_p) else None,
        "profil": profil,
        "murs": [{"strike": round(k, 1), "gex_md": round(v / 1e9, 3),
                  "ecart_pct": round((k / spot - 1) * 100, 2),
                  "type": "call" if v > 0 else "put"} for k, v in murs],
    }


def ecrire(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def main():
    ancien = {}
    if OUT_JSON.exists():
        try:
            ancien = json.loads(OUT_JSON.read_text()).get("symboles", {})
        except Exception:                                         # noqa: BLE001
            pass

    res, rates = {}, []
    for sym, fichier, libelle in SYMBOLES:
        try:
            r = analyser(sym, fichier)
            r["libelle"] = libelle
            r["stale"] = False
            res[sym] = r
            print(f"  {sym:<4} {r['contrats']:>6} contrats · spot {r['spot']:>9,.2f}"
                  f" · GEX {r['gex_total_md']:>+8.2f} Md$ · régime {r['regime']}"
                  f" · bascule ≈ {r['bascule']}")
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{sym} : {type(e).__name__} {e}")
            # Une chaîne muette ne doit pas effacer la précédente : le module
            # afficherait un trou là où une donnée de la veille reste utile.
            if sym in ancien:
                garde = dict(ancien[sym])
                garde["stale"] = True
                res[sym] = garde
                print(f"  {sym:<4} ÉCHEC — valeur précédente conservée")

    if not any(not v.get("stale") for v in res.values()):
        sys.exit("aucune chaîne fraîche — fichiers laissés intacts")

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "cdn.cboe.com/api/global/delayed_quotes/options",
        "convention": {
            "hypothese": "dealers longs des calls, shorts des puts — convention, non observée",
            "formule": "GEX(strike) = gamma × OI × 100 × S² × 0,01, signé + call / − put",
            "unite": "milliards de dollars de delta à couvrir par mouvement de 1 %",
            "bascule": "approximation par cumul du GEX par strike ; le calcul exact "
                       "demanderait de repricer les gammas à plusieurs niveaux de spot",
            "latence": "chaînes différées — afficher le champ `observe`, pas `updated`",
        },
        "symboles": res,
        "echecs": rates,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ecrire(OUT_JSON, body)
    ecrire(OUT_JS, "window.__TRADFI_GAMMA__=" + body + ";\n")

    # Série quotidienne du régime : c'est elle qui dira, dans six mois, si le
    # marché a passé l'année en gamma positif ou s'il a basculé.
    h = {"rows": []}
    if HIST.exists():
        try:
            h = json.loads(HIST.read_text())
        except Exception:                                         # noqa: BLE001
            pass
    for sym, r in res.items():
        if r.get("stale"):
            continue
        jour = r["observe"][:10]
        h["rows"] = [x for x in h["rows"] if not (x[0] == jour and x[1] == sym)]
        h["rows"].append([jour, sym, r["gex_total_md"], r["gex_30j_md"],
                          r["spot"], r["bascule"], r["pc_oi"]])
    h["rows"].sort(key=lambda x: (x[0], x[1]))
    h["colonnes"] = ["jour", "symbole", "gex_total_md", "gex_30j_md",
                     "spot", "bascule", "pc_oi"]
    ecrire(HIST, json.dumps(h, separators=(",", ":")))

    print(f"\n  écrit {len(body)//1024} Ko · historique {len(h['rows'])} lignes")
    for r in rates:
        print(f"  échec : {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
