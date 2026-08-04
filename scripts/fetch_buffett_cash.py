#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_buffett_cash.py — « Niveau de cash de Warren Buffett » (Berkshire Hathaway)
pour l'onglet Indicateur (section Liquidité).

Le trésor de guerre de Buffett = Cash & équivalents + Bons du Trésor US (T-Bills).
Ces T-Bills (~340 Md$) ne sont PAS un fait XBRL propre (tagués dimensionnellement) ->
on parse la ligne du BILAN consolidé (R2.htm "Consolidated Balance Sheets") de chaque
10-Q / 10-K déposé sur EDGAR. Construit aussi un historique (trend).

Sortie : buffett_cash_cache.json/.js  (window.__BUFFETT_CASH__)
"""
import json, re, sys, time, shutil
import html as H
from pathlib import Path
from datetime import datetime, timezone
import urllib.request

CIK = "0001067983"
CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
SITE_DIR  = Path.home() / "Desktop" / "Site_Crypto_Finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_JSON = CACHE_DIR / "buffett_cash_cache.json"
CACHE_JS   = CACHE_DIR / "buffett_cash_cache.js"
RAW = CACHE_DIR / "buffett_cash_raw"; RAW.mkdir(exist_ok=True)

UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")
N_FILINGS = None        # None = TOUT l'historique disponible (limité par l'existence des R-files XBRL ~2010+)
CACHE_MAX_HOURS = 24
NEG_FILE = RAW / "_no_balance_sheet.txt"   # cache négatif : dépôts sans R-file bilan (pré-XBRL)

def log(*a): print(f"[{datetime.now().strftime('%H:%M:%S')}]", *a, flush=True)

def get(url, binary=False, retry=3):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    import gzip; data = gzip.decompress(data)
                return data if binary else data.decode("utf-8", "replace")
        except Exception as e:
            if i == retry - 1: log("GET fail", url[:70], e)
            time.sleep(1.2)
    return None

def list_filings():
    """Tous les 10-Q/10-K : bloc 'recent' + fichiers d'archives (historique complet)."""
    d = json.loads(get(f"https://data.sec.gov/submissions/CIK{CIK}.json") or "{}")
    out = []
    def harvest(r):
        for i, f in enumerate(r["form"]):
            if f in ("10-Q", "10-K"):
                out.append({"form": f, "filed": r["filingDate"][i],
                            "acc": r["accessionNumber"][i].replace("-", ""),
                            "report": r["reportDate"][i]})
    harvest(d["filings"]["recent"])
    for fobj in d["filings"].get("files", []):
        sub = get(f"https://data.sec.gov/submissions/{fobj['name']}")
        if sub:
            try: harvest(json.loads(sub))
            except Exception as e: log("archive parse err", e)
    # dédup par accession, tri décroissant par date de période
    out = list({x["acc"]: x for x in out}.values())
    out.sort(key=lambda x: x["report"], reverse=True)
    return out if N_FILINGS is None else out[:N_FILINGS]

def balance_sheet_rfile(acc):
    fs = get(f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc}/FilingSummary.xml")
    if not fs: return None
    # apparier <Report> contenant ShortName "Balance Sheet" (hors Parenthetical) -> HtmlFileName
    for rep in re.findall(r"<Report[^>]*>(.*?)</Report>", fs, re.S):
        sn = re.search(r"<ShortName>(.*?)</ShortName>", rep, re.S)
        hf = re.search(r"<HtmlFileName>(R\d+\.htm)</HtmlFileName>", rep)
        if sn and hf and re.search(r"balance sheet|financial position", sn.group(1), re.I) \
           and "parenthetical" not in sn.group(1).lower():
            return hf.group(1)
    return None

def _dedup_total(values):
    """Le bilan liste parfois une ligne CONSOLIDÉE + les lignes par SEGMENT (qui en sont
    la somme). Si la plus grande valeur ≈ somme des autres, c'est la consolidée -> on la
    prend seule (évite le double comptage). Sinon (segments seuls) -> somme."""
    values = [v for v in values if v]
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    mx = max(values)
    others = sum(values) - mx
    if others > 0 and abs(mx - others) / mx < 0.02:   # consolidée = somme des segments
        return mx
    return sum(values)

def parse_balance_sheet(html):
    """Cash & équivalents + Bons du Trésor US + total actifs (consolidés). (M$ -> $)
    Gère les 2 présentations du bilan Berkshire (consolidé+segments / segments seuls)."""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    clean = lambda c: re.sub(r"<[^>]+>", "", H.unescape(c)).strip()
    cash_v, tbill_v, asset_v = [], [], []
    for r in rows:
        cells = [clean(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if not cells:
            continue
        lab = cells[0]
        nums = [c for c in cells if re.match(r"^[\d,]+$", c)]
        if not nums:
            continue
        val = float(nums[0].replace(",", "")) * 1e6   # 1re colonne = période courante (M$)
        if re.match(r"^Cash and cash equiv", lab, re.I):
            cash_v.append(val)
        elif re.match(r"^Short-term investments in U\.S\. Treasury Bills", lab, re.I):
            tbill_v.append(val)
        elif re.match(r"^Total assets", lab, re.I):
            asset_v.append(val)
    cash = _dedup_total(cash_v)
    tbills = _dedup_total(tbill_v)
    assets = _dedup_total(asset_v)
    if cash == 0 and tbills == 0:
        return None
    total = cash + tbills
    return {"cash": cash, "tbills": tbills, "total": total,
            "pct_assets": round(100 * total / assets, 1) if assets else None}

def build():
    filings = list_filings()
    log(f"{len(filings)} dépôts 10-Q/10-K")
    neg = set(NEG_FILE.read_text().split()) if NEG_FILE.exists() else set()
    series = []
    for f in filings:
        cf = RAW / f"{f['acc']}.json"
        if cf.exists():
            try: series.append(json.loads(cf.read_text())); continue
            except Exception: pass
        if f["acc"] in neg:   # pré-XBRL / pas de bilan parsable -> ne pas re-tenter
            continue
        rfile = balance_sheet_rfile(f["acc"])
        html = get(f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{f['acc']}/{rfile}") if rfile else None
        parsed = parse_balance_sheet(html) if html else None
        if not parsed:
            neg.add(f["acc"]); NEG_FILE.write_text(" ".join(sorted(neg)))
            continue
        rec = {"date": f["report"], "form": f["form"], "filed": f["filed"], **parsed}
        cf.write_text(json.dumps(rec)); series.append(rec)
        log(f"  {f['report']}: cash+T-Bills ${parsed['total']/1e9:.1f} Md ({parsed['pct_assets']}% actifs)")
        time.sleep(0.18)
    # dédup par date, tri croissant
    series = sorted({s["date"]: s for s in series}.values(), key=lambda s: s["date"])
    # Comparabilité : avant ~mi-2017 Berkshire ne séparait pas les Bons du Trésor du
    # reste des placements court terme -> la métrique "cash + T-Bills" n'est cohérente
    # qu'à partir de la 1re apparition de la ligne T-Bills. On démarre là.
    first_tb = next((i for i, s in enumerate(series) if s.get("tbills", 0) > 0), None)
    if first_tb is not None:
        series = series[first_tb:]
    if not series:
        log("aucune donnée — abort"); return None
    cur = series[-1]
    prev = series[-2] if len(series) > 1 else None
    record = cur["total"] >= max(s["total"] for s in series)
    payload = {
        "meta": {"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "source": "SEC EDGAR 10-Q/10-K — bilan consolidé Berkshire Hathaway"},
        "current": {**cur, "is_record": record,
                    "delta_qoq": round((cur["total"] - prev["total"]) / 1e9, 1) if prev else None},
        "series": [{"date": s["date"], "total": round(s["total"]), "cash": round(s["cash"]),
                    "tbills": round(s["tbills"]), "pct_assets": s["pct_assets"]} for s in series],
    }
    return payload

def write(payload):
    CACHE_JSON.write_text(json.dumps(payload, ensure_ascii=False))
    CACHE_JS.write_text(f"/* buffett_cash_cache.js — {payload['meta']['updated_at']} */\n"
                        f"window.__BUFFETT_CASH__={json.dumps(payload, separators=(',',':'), ensure_ascii=False)};\n")
    # Propagation Desktop = best-effort UNIQUEMENT. Sous launchd (TCC) le Desktop est
    # inaccessible → PermissionError ; c'est snapshot_site.sh qui sync Library→repo via
    # _cache_files_synced.txt. On ne crashe donc jamais ici (bug fixé 2026-06-17 : le
    # shutil.copy2 dans le except levait une PermissionError non rattrapée → le job
    # plantait APRÈS avoir écrit le cache Library, sans logguer ni mettre à jour le manifeste).
    try:
        if SITE_DIR.exists():
            for n in ("buffett_cash_cache.json", "buffett_cash_cache.js"):
                link = SITE_DIR / n; tgt = CACHE_DIR / n
                try:
                    if link.is_symlink() or link.exists(): link.unlink()
                    link.symlink_to(tgt)
                except OSError:
                    shutil.copy2(tgt, link)
            mf = SITE_DIR / "_cache_files_synced.txt"
            if mf.exists():
                have = mf.read_text().splitlines()
                for w in ("buffett_cash_cache.js", "buffett_cash_cache.json"):
                    if w not in have: have.append(w)
                mf.write_text("\n".join(have) + "\n")
    except OSError:
        pass  # Desktop inaccessible (launchd TCC) — propagation déléguée à snapshot_site.sh
    log(f"écrit ({CACHE_JS.stat().st_size/1024:.0f} KB)")

def main():
    if CACHE_JSON.exists() and "--force" not in sys.argv:
        age = (time.time() - CACHE_JSON.stat().st_mtime) / 3600
        if age < CACHE_MAX_HOURS:
            log(f"frais ({age:.1f}h) — skip"); return
    p = build()
    if p: write(p); log("Terminé.")

if __name__ == "__main__":
    main()
