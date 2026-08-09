#!/usr/bin/env python3
"""Options crypto (Deribit) — onglet Structure de marché, partie crypto.

Deribit concentre l'essentiel du volume d'options crypto et publie tout en accès
libre, sans clé : un seul appel rend les ~830 instruments BTC avec leur open
interest, leur volatilité implicite et le prix du sous-jacent par échéance.

C'est le pendant crypto du gamma des dealers côté actions. Les deux panneaux se
lisent alors de la même façon, avec la même convention et les mêmes réserves.

╔══════════════════════════════════════════════════════════════════════════════╗
║ LES GRECQUES SONT CALCULÉES ICI, PAS FOURNIES                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
`get_book_summary_by_currency` ne renvoie pas gamma. L'obtenir instrument par
instrument via `ticker` coûterait 830 appels par devise — inacceptable. On le
calcule donc en Black-Scholes à partir de la volatilité implicite publiée :

    gamma = φ(d₁) ÷ (S × σ × √T)
    d₁ = [ln(S ÷ K) + σ²T ÷ 2] ÷ (σ√T)

C'est exactement la formule dont Deribit se sert pour afficher les siennes, et
le taux sans risque est nul chez eux (`interest_rate` vaut 0) — la reconstruction
est donc fidèle, pas approchée.

Le sous-jacent retenu est le PRIX FORWARD DE L'ÉCHÉANCE (`underlying_price`), pas
le spot. Les deux diffèrent de plusieurs pour cent sur les échéances lointaines,
et prendre le spot décalerait tout le profil de gamma.

╔══════════════════════════════════════════════════════════════════════════════╗
║ CONVENTIONS ET LIMITES                                                       ║
╚══════════════════════════════════════════════════════════════════════════════╝
Même hypothèse que côté actions : dealers longs des calls, shorts des puts. Ce
n'est pas une observation, et la page doit le dire.

L'open interest de Deribit est libellé EN COINS — un contrat vaut 1 BTC ou 1 ETH.
Le gamma en dollars pour 1 % de mouvement vaut donc gamma × OI × S² × 0,01, sans
le facteur 100 des contrats actions. Confondre les deux gonflerait le résultat
d'un facteur cent.

Le MAX PAIN est le prix qui minimise ce que les vendeurs d'options devraient
verser à l'échéance. C'est une curiosité de marché, pas une prévision : il
suppose que toutes les positions vont à terme sans être couvertes, ce qui est
faux. Publié parce que le marché le regarde, avec sa réserve.

Sorties (~/Library/Caches/site_crypto_finance/) :
  crypto_options_cache.json + .js     window.__CRYPTO_OPTIONS__

Cadence horaire : les options bougent en séance, mais l'open interest ne se
déplace qu'aux règlements. Plus vite ne dirait rien de plus.
"""
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSON = CACHE_DIR / "crypto_options_cache.json"
OUT_JS = CACHE_DIR / "crypto_options_cache.js"

API = "https://www.deribit.com/api/v2/public/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; SiteCryptoFinance/1.0)"}
DEVISES = ["BTC", "ETH"]

# BTC-28AUG26-110000-P  →  échéance, strike, type
RX = re.compile(r"^([A-Z]+)-(\d{1,2}[A-Z]{3}\d{2})-(\d+(?:d\d+)?)-([CP])$")
MOIS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def get(chemin, essais=3):
    dernier = None
    for n in range(1, essais + 1):
        try:
            r = json.loads(urllib.request.urlopen(
                urllib.request.Request(API + chemin, headers=UA), timeout=60).read())
            if "result" not in r:
                raise RuntimeError(r.get("error") or "réponse sans résultat")
            return r["result"]
        except Exception as e:                                    # noqa: BLE001
            dernier = e
            if n < essais:
                time.sleep(4 * n)
    raise RuntimeError(f"{chemin} : {dernier}")


def echeance(txt):
    m = re.match(r"^(\d{1,2})([A-Z]{3})(\d{2})$", txt)
    if not m:
        return None
    j, mo, a = m.groups()
    try:
        return datetime(2000 + int(a), MOIS[mo], int(j), 8, 0, tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None


def phi(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def gamma_bs(S, K, sigma, T):
    """Gamma Black-Scholes, taux nul (Deribit publie interest_rate = 0)."""
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + sigma * sigma * T / 2) / (sigma * math.sqrt(T))
    return phi(d1) / (S * sigma * math.sqrt(T))


def max_pain(par_strike_call, par_strike_put, strikes):
    """Prix qui minimise ce que les vendeurs devraient verser à l'échéance.

    Curiosité de marché, pas prévision : suppose que tout va à terme sans
    couverture, ce qui est faux. Publié parce que le marché le regarde.
    """
    meilleur, mini = None, None
    for s in strikes:
        douleur = 0.0
        for k, oi in par_strike_call.items():
            if s > k:
                douleur += (s - k) * oi
        for k, oi in par_strike_put.items():
            if s < k:
                douleur += (k - s) * oi
        if mini is None or douleur < mini:
            mini, meilleur = douleur, s
    return meilleur


def traiter(devise):
    lignes = get(f"get_book_summary_by_currency?currency={devise}&kind=option")
    maintenant = datetime.now(timezone.utc)

    par_strike, oi_call_k, oi_put_k = {}, {}, {}
    par_echeance = {}
    gex_total = oi_c = oi_p = vol_c = vol_p = 0.0
    spot = None
    retenus = 0

    for r in lignes:
        m = RX.match(r.get("instrument_name", ""))
        if not m:
            continue
        _, exp_txt, k_txt, cp = m.groups()
        exp = echeance(exp_txt)
        if not exp:
            continue
        try:
            K = float(k_txt.replace("d", "."))
        except ValueError:
            continue
        S = r.get("underlying_price") or 0.0
        iv = (r.get("mark_iv") or 0.0) / 100.0
        oi = r.get("open_interest") or 0.0
        vol = r.get("volume") or 0.0
        T = max((exp - maintenant).total_seconds(), 0) / (365.25 * 86400)
        if S <= 0:
            continue
        spot = spot or S
        retenus += 1

        if cp == "C":
            oi_c += oi
            vol_c += vol
            oi_call_k[K] = oi_call_k.get(K, 0.0) + oi
        else:
            oi_p += oi
            vol_p += vol
            oi_put_k[K] = oi_put_k.get(K, 0.0) + oi

        # OI en COINS, un contrat = 1 coin : pas de facteur 100 ici, contrairement
        # aux options actions. L'oublier gonflerait le gamma d'un facteur cent.
        g = gamma_bs(S, K, iv, T)
        gex = g * oi * S * S * 0.01 * (1 if cp == "C" else -1)
        par_strike[K] = par_strike.get(K, 0.0) + gex
        gex_total += gex

        e = par_echeance.setdefault(exp_txt, {
            "date": exp.date().isoformat(), "jours": round(T * 365.25, 1),
            "oi": 0.0, "oi_usd": 0.0, "atm_iv": None, "_ecart": None, "S": S})
        e["oi"] += oi
        e["oi_usd"] += oi * S
        # Volatilité « à la monnaie » : celle du strike le plus proche du forward.
        ecart = abs(K - S)
        if iv > 0 and (e["_ecart"] is None or ecart < e["_ecart"]):
            e["_ecart"] = ecart
            e["atm_iv"] = round(iv * 100, 2)

    if not retenus or spot is None:
        raise RuntimeError("aucun instrument exploitable")

    strikes = sorted(set(list(oi_call_k) + list(oi_put_k)))
    mp = max_pain(oi_call_k, oi_put_k, strikes)

    profil = sorted((k, round(v / 1e6, 4)) for k, v in par_strike.items()
                    if abs(k - spot) / spot <= 0.5)
    murs = sorted(((k, v) for k, v in par_strike.items() if abs(k - spot) / spot <= 0.35),
                  key=lambda t: -abs(t[1]))[:10]
    oi_strike = sorted(((k, oi_call_k.get(k, 0.0), oi_put_k.get(k, 0.0))
                        for k in strikes if abs(k - spot) / spot <= 0.5))

    for e in par_echeance.values():
        e.pop("_ecart", None)
        e["oi"] = round(e["oi"], 2)
        e["oi_usd"] = round(e["oi_usd"])
        e.pop("S", None)
    terme = sorted(par_echeance.values(), key=lambda e: e["date"])

    # DVOL : l'indice de volatilité de Deribit, équivalent crypto du VIX.
    dvol = dvol_serie = None
    try:
        fin = int(time.time() * 1000)
        deb = fin - 90 * 86400 * 1000
        d = get(f"get_volatility_index_data?currency={devise}"
                f"&start_timestamp={deb}&end_timestamp={fin}&resolution=43200")
        pts = d.get("data") or []
        if pts:
            dvol = round(pts[-1][4], 2)                 # clôture du dernier seau
            dvol_serie = [[datetime.fromtimestamp(p[0] / 1000, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%MZ"), round(p[4], 2)] for p in pts]
    except Exception:                                             # noqa: BLE001
        pass

    return {
        "spot": round(spot, 2),
        "instruments": retenus,
        "gex_total_m": round(gex_total / 1e6, 3),
        "regime": "long" if gex_total > 0 else "short",
        "max_pain": mp,
        "max_pain_ecart_pct": None if not mp else round((mp / spot - 1) * 100, 2),
        "oi_call": round(oi_c, 2), "oi_put": round(oi_p, 2),
        "pc_oi": round(oi_p / oi_c, 4) if oi_c else None,
        "pc_volume": round(vol_p / vol_c, 4) if vol_c else None,
        "oi_total_usd": round((oi_c + oi_p) * spot),
        "dvol": dvol, "dvol_serie": dvol_serie,
        "profil": profil,
        "oi_par_strike": [[k, round(c, 2), round(p, 2)] for k, c, p in oi_strike],
        "murs": [{"strike": k, "gex_m": round(v / 1e6, 3),
                  "ecart_pct": round((k / spot - 1) * 100, 2),
                  "type": "call" if v > 0 else "put"} for k, v in murs],
        "terme": terme,
        "stale": False,
    }


def ecrire(path, body):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body)
    tmp.replace(path)


def main():
    ancien = {}
    if OUT_JSON.exists():
        try:
            ancien = json.loads(OUT_JSON.read_text()).get("devises", {})
        except Exception:                                         # noqa: BLE001
            pass

    res, rates = {}, []
    for d in DEVISES:
        try:
            r = traiter(d)
            res[d] = r
            print(f"  {d} spot {r['spot']:>10,.2f} · {r['instruments']:>4} instruments"
                  f" · GEX {r['gex_total_m']:>+9.2f} M$ ({r['regime']})"
                  f" · max pain {r['max_pain']:>9,.0f} ({r['max_pain_ecart_pct']:+.1f} %)"
                  f" · P/C {r['pc_oi']} · DVOL {r['dvol']}")
        except Exception as e:                                    # noqa: BLE001
            rates.append(f"{d} : {type(e).__name__} {e}")
            if d in ancien:
                g = dict(ancien[d]); g["stale"] = True; res[d] = g
                print(f"  {d} ÉCHEC — valeur précédente conservée")

    if not any(not v.get("stale") for v in res.values()):
        sys.exit("aucune devise fraîche — fichiers laissés intacts")

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "deribit.com/api/v2/public/get_book_summary_by_currency",
        "convention": {
            "hypothese": "dealers longs des calls, shorts des puts — convention, non observée",
            "grecques": "gamma reconstruit en Black-Scholes depuis la volatilité implicite "
                        "publiée, taux nul : Deribit ne renvoie pas les grecques dans le "
                        "résumé de carnet, et les demander instrument par instrument "
                        "coûterait 830 appels par devise",
            "sous_jacent": "prix FORWARD de chaque échéance, pas le spot — les deux "
                           "diffèrent de plusieurs pour cent au loin",
            "unite": "open interest en COINS (1 contrat = 1 coin), donc pas de facteur 100 "
                     "contrairement aux options actions",
            "max_pain": "prix minimisant ce que les vendeurs verseraient à l'échéance. "
                        "Curiosité de marché, pas prévision : suppose que tout va à terme "
                        "sans couverture, ce qui est faux",
            "dvol": "indice de volatilité de Deribit — l'équivalent crypto du VIX",
        },
        "devises": res,
        "echecs": rates,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    ecrire(OUT_JSON, body)
    ecrire(OUT_JS, "window.__CRYPTO_OPTIONS__=" + body + ";\n")
    print(f"\n  {len(body)//1024} Ko écrits")
    for r in rates:
        print(f"  échec : {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
