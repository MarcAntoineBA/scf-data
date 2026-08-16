#!/usr/bin/env python3
"""Compléments « Prediction Markets » : historique de volume plateforme (Polymarket
vs Kalshi) et icônes par marché — deux données que le fetcher principal
(fetch_prediction_markets.py, hors dépôt, tourne sur le Mac) ne produit pas.

1. HISTORIQUE DE VOLUME CUMULÉ, PAR PLATEFORME
   Ni Polymarket ni Kalshi n'exposent un total plateforme dans leur API publique
   (Polymarket : volume par marché seulement ; Kalshi : volume en CONTRATS, pas en
   dollars, et pas de total non plus). Source utilisée : DefiLlama, qui republie un
   volume journalier en dollars pour les deux, sans clé :
     https://api.llama.fi/summary/dexs/polymarket?dataType=dailyVolume  (depuis 09/2020)
     https://api.llama.fi/summary/dexs/kalshi?dataType=dailyVolume     (depuis 07/2021)
   RÉSERVE : le total Kalshi de DefiLlama (~62,6 Md$ au 2026-08) est nettement sous
   le chiffre revendiqué par Kalshi lui-même (>100 Md$) — méthodologie de couverture
   probablement différente. Utilisable pour une COURBE DE TENDANCE et une part de
   marché relative, pas comme un total officiel garanti. La page doit le dire.

2. ICÔNES PAR MARCHÉ (Polymarket seulement)
   gamma-api.polymarket.com expose un champ icon/image par event, mais le fetcher
   principal ne le capture pas. Plutôt que de dépendre de la liste exacte des
   marchés actuellement suivis (qui change au gré des seuils de liquidité), on
   balaie les events triés par volume — même endpoint et même tri que le fetcher
   principal (sweep_events) — et on capture id→icône pour tout ce qui passe. Ça
   couvre par construction les marchés qu'on affiche, sans dépendre d'une liste.
   Kalshi n'a AUCUN champ image/icon dans son API (event, market, series) : pas de
   logo par marché possible de ce côté, seulement le logo Kalshi générique.

Sortie : platform_volume_hist.json/.js (window.__PM_VOLUME_HIST__) et
prediction_markets_icons.json/.js (window.__PM_ICONS__), lus par Prediction_Markets.Rmd
directement depuis GitHub (raw.githubusercontent.com/MarcAntoineBA/scf-data/main/cache/…) —
pas de dépendance au Mac ni au PC pour que ces deux fichiers restent à jour.
Lancé par scf.predmarkets_extras (quotidien — DefiLlama et les icônes ne bougent
pas plus vite que ça).
"""
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── timeout global (sécurité runner) ────────────────────────────────
import signal as _signal
def _timeout_handler(signum, frame):
    sys.stderr.write("[fatal] global timeout (20 min) — abort\n")
    sys.exit(2)
try:
    _signal.signal(_signal.SIGALRM, _timeout_handler)
    _signal.alarm(20 * 60)
except Exception:
    pass

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
VOL_JSON = CACHE_DIR / "platform_volume_hist.json"
VOL_JS   = CACHE_DIR / "platform_volume_hist.js"
ICO_JSON = CACHE_DIR / "prediction_markets_icons.json"
ICO_JS   = CACHE_DIR / "prediction_markets_icons.js"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) SiteCryptoFinance-PredMarketsExtras/1.0"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

GAMMA = "https://gamma-api.polymarket.com"


def get_json(url, timeout=25, params=None):
    r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ════════════════════════════════════════════════════════════════════
#  1. Historique de volume — DefiLlama
# ════════════════════════════════════════════════════════════════════

def fetch_platform_daily(protocol, ok, failed):
    try:
        j = get_json(f"https://api.llama.fi/summary/dexs/{protocol}?dataType=dailyVolume", timeout=30)
        chart = j.get("totalDataChart") or []
        pts = [(int(ts), float(v)) for ts, v in chart if v is not None]
        pts.sort()
        if not pts:
            raise RuntimeError("totalDataChart vide")
        ok.append(f"defillama:{protocol}")
        return pts
    except Exception as e:
        sys.stderr.write(f"[defillama {protocol}] {e}\n")
        failed.append(f"defillama:{protocol}")
        return None


def to_cumulative(daily_pts):
    out, total = [], 0.0
    for ts, v in daily_pts:
        total += v
        out.append((ts, total))
    return out


def merge_cumulative(poly_cum, kal_cum):
    """Grille journalière unique (date civile UTC), chaque plateforme reportant son
    dernier cumul connu — nécessaire car Polymarket démarre ~10 mois avant Kalshi."""
    def day_map(cum):
        m = {}
        for ts, v in cum:
            d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            m[d] = v
        return m
    pm, km = day_map(poly_cum), day_map(kal_cum)
    days = sorted(set(pm) | set(km))
    merged, last_p, last_k = [], 0.0, 0.0
    for d in days:
        if d in pm:
            last_p = pm[d]
        if d in km:
            last_k = km[d]
        merged.append([d, round(last_p, 2), round(last_k, 2)])
    return merged


def build_volume_payload(ok, failed, prev):
    poly = fetch_platform_daily("polymarket", ok, failed)
    kal = fetch_platform_daily("kalshi", ok, failed)
    if poly is None and kal is None:
        return None
    prev_days = (prev or {}).get("days") or []
    prev_map = {}
    if prev_days:
        # repli par plateforme : si une des deux séries échoue aujourd'hui, on
        # reprend sa dernière valeur connue plutôt que de la faire disparaître.
        for d, p, k in prev_days:
            prev_map[d] = (p, k)
    poly_cum = to_cumulative(poly) if poly is not None else None
    kal_cum = to_cumulative(kal) if kal is not None else None
    if poly_cum is None and prev_days:
        poly_cum = [(int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()), p)
                    for d, (p, k) in prev_map.items()]
    if kal_cum is None and prev_days:
        kal_cum = [(int(datetime.fromisoformat(d).replace(tzinfo=timezone.utc).timestamp()), k)
                   for d, (p, k) in prev_map.items()]
    # UNE SOURCE MUETTE SANS REPLI NE DOIT PAS ÊTRE PUBLIÉE COMME UN ZÉRO.
    # Sans ce garde-fou, un premier run (ou un cache perdu) avec Kalshi KO
    # écrivait 0 $ sur les 2143 jours : la page affichait « Kalshi — $0 » et
    # « part Polymarket 100 % » comme une donnée normale, ce qui est faux et
    # indétectable à l'œil. Mieux vaut ne rien publier et garder l'ancien
    # fichier — c'est la règle appliquée partout ailleurs dans ce dépôt.
    if poly_cum is None or kal_cum is None:
        manque = "polymarket" if poly_cum is None else "kalshi"
        sys.stderr.write(f"[volume] {manque} indisponible et aucun repli — publication annulée\n")
        return None
    days = merge_cumulative(poly_cum, kal_cum)
    if not days:
        return None
    last = days[-1]
    return {
        "days": days,
        "latest": {"date": last[0], "polymarket_usd": last[1], "kalshi_usd": last[2]},
        "source": "DefiLlama (api.llama.fi/summary/dexs) — agrégateur tiers, pas les chiffres officiels des plateformes",
        "caveat": "Le total Kalshi est probablement sous-estimé par rapport aux chiffres officiels de Kalshi (méthodologie de couverture DefiLlama). Courbe et part de marché à lire comme une tendance, pas un chiffre garanti.",
    }


# ════════════════════════════════════════════════════════════════════
#  2. Icônes par marché — balayage gamma-api (même tri que le fetcher principal)
# ════════════════════════════════════════════════════════════════════

def fetch_icons(ok, failed):
    """Balaie les events par volume et retient id→icône.

    UN BALAYAGE INTERROMPU NE DOIT JAMAIS ÊTRE PUBLIÉ TEL QUEL. Si la source
    lâche à la page 12 sur 21, on a bien ~1100 icônes valides — mais les
    republier écraserait les 2100 du fichier précédent, et des centaines de
    logos disparaîtraient du site jusqu'au prochain run réussi. On signale
    donc l'interruption à l'appelant (`partial`), qui fusionnera avec le
    cache précédent au lieu de le remplacer.
    """
    out = {}
    partial = False
    offset, page, max_total = 0, 100, 3000
    # La Gamma API plafonne la taille de page à 100, quel que soit le `limit`
    # demandé (observé : limit=500 -> 100 résultats). Avancer `offset` du
    # `limit` demandé au lieu de la taille RÉELLEMENT reçue faisait relire la
    # même première page en boucle puis s'arrêter, croyant avoir tout balayé
    # après 100 events sur les ~2000 du marché.
    try:
        while offset < max_total:
            j = get_json(f"{GAMMA}/events", timeout=25, params={
                "order": "volume", "ascending": "false",
                "limit": page, "offset": offset, "closed": "false",
            })
            if not j:
                break
            for ev in j:
                icon = ev.get("icon") or ev.get("image")
                eid = ev.get("id")
                if eid and icon:
                    out[str(eid)] = icon
            offset += len(j)
            if len(j) < page:
                break
            time.sleep(0.3)
        if not out:
            raise RuntimeError("aucune icône récupérée")
        ok.append("gamma:icons")
    except Exception as e:
        sys.stderr.write(f"[icons] {e}\n")
        failed.append("gamma:icons")
        partial = bool(out)      # on a ramené quelque chose, mais pas tout
    return (out or None), partial


# ════════════════════════════════════════════════════════════════════
#  Écriture
# ════════════════════════════════════════════════════════════════════

def _read_prev(path):
    import json as _json
    try:
        return _json.loads(path.read_text())
    except Exception:
        return None


def write_pair(json_path, js_path, varname, payload):
    import json as _json
    json_path.write_text(_json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    js_path.write_text(
        f"/* {json_path.name} — generated {payload.get('meta', {}).get('updated_at', '')} */\n"
        f"window.{varname} = {_json.dumps(payload, separators=(',', ':'), ensure_ascii=False)};\n"
    )


def main():
    t0 = time.time()
    ok, failed = [], []
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── 1. Historique de volume ────────────────────────────────────────
    prev_vol = _read_prev(VOL_JSON)
    vol = build_volume_payload(ok, failed, prev_vol)
    if vol is None and prev_vol:
        sys.stderr.write("[GUARD volume] tout a échoué — conserve le cache précédent\n")
        vol = {k: v for k, v in prev_vol.items() if k != "meta"}
    # `ok`/`failed` sont partagés par les deux collectes de ce script. Figer
    # ici la part qui concerne le volume : sinon le fichier de volume, écrit
    # AVANT le balayage d'icônes, ne pouvait jamais mentionner un échec
    # d'icônes — et le fichier d'icônes s'attribuait les sources DefiLlama.
    vol_ok = list(ok)
    vol_failed = list(failed)
    if vol is not None:
        vol["meta"] = {"updated_at": now_iso, "sources_ok": vol_ok, "sources_failed": vol_failed}
        write_pair(VOL_JSON, VOL_JS, "__PM_VOLUME_HIST__", vol)

    # ── 2. Icônes par marché ───────────────────────────────────────────
    icons, partial = fetch_icons(ok, failed)
    prev_icons = _read_prev(ICO_JSON)
    prev_map = {k: v for k, v in (prev_icons or {}).items() if k != "meta"}
    if icons is None and prev_map:
        sys.stderr.write("[GUARD icons] fetch KO — conserve le cache précédent\n")
        icons = prev_map
    elif icons is not None and partial and prev_map:
        # Balayage interrompu : on FUSIONNE au lieu de remplacer. Les icônes
        # fraîches gagnent, celles que le balayage n'a pas atteintes cette
        # fois survivent. Sans ça, la couverture ne pouvait que régresser.
        fusion = dict(prev_map)
        fusion.update(icons)
        sys.stderr.write(f"[GUARD icons] balayage partiel ({len(icons)}) fusionné "
                         f"avec le cache ({len(prev_map)}) → {len(fusion)}\n")
        icons = fusion
    if icons is not None:
        payload = dict(icons)
        ico_ok = [s for s in ok if s not in vol_ok]
        ico_failed = [s for s in failed if s not in vol_failed]
        payload["meta"] = {"updated_at": now_iso, "sources_ok": ico_ok,
                           "sources_failed": ico_failed, "partial": partial}
        write_pair(ICO_JSON, ICO_JS, "__PM_ICONS__", payload)

    dt = time.time() - t0
    n_ok, n_fail = len(ok), len(failed)
    if vol is None and icons is None:
        sys.stderr.write(f"[predmarkets_extras] ÉCHEC TOTAL · {n_fail} échecs · {dt:.1f}s\n")
        sys.exit(2)
    sys.stdout.write(f"[predmarkets_extras] OK · {n_ok} sources OK, {n_fail} échec · {dt:.1f}s\n")


if __name__ == "__main__":
    main()
