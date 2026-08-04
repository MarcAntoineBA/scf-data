#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_confirm.py — Confirme (ou dément) deux résultats qui ne tenaient qu'à un essai.

UNE MESURE UNIQUE NE PROUVE RIEN SUR UNE SOURCE PROTÉGÉE. Deux constats de la sonde
précédente sont trop importants pour reposer sur un seul appel :

  1. Farside a renvoyé 403 AVEC usurpation TLS, puis 200 SANS. Si c'est reproductible,
     la correction est d'une ligne (ne pas se déguiser depuis une IP de datacenter).
     Si c'est du hasard, on partirait sur une fausse piste et le collecteur ETF
     tomberait en panne un jour sur deux, silencieusement.
  2. `data-api.binance.vision` a répondu 200 — mais sur un point d'entrée SPOT, alors
     que le besoin est le funding, qui est du futures. On vérifie donc explicitement
     si ce miroir sert, ou non, les points d'entrée futures.

On répète chaque variante plusieurs fois, espacées, et on compte les succès.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_sources import _fetch  # noqa: E402

ESSAIS = 5
PAUSE = 4


def serie(label, url, opts, expect):
    ok = 0
    codes = []
    for i in range(ESSAIS):
        try:
            code, _, body, _ = _fetch(url, opts)
            codes.append(str(code))
            if code == 200 and expect in body:
                ok += 1
        except Exception as e:
            codes.append(type(e).__name__)
        if i < ESSAIS - 1:
            time.sleep(PAUSE)
    verdict = "FIABLE" if ok == ESSAIS else ("INSTABLE" if ok else "TOUJOURS REFUSÉ")
    print(f"   {ok}/{ESSAIS}  {label:44} [{', '.join(codes)}]  {verdict}")
    return ok


def main():
    print("\n1. Farside — le déguisement aide-t-il ou nuit-il ?\n")
    url = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
    nu = serie("sans déguisement (requête honnête)", url, {}, "Total")
    dg = serie("avec usurpation TLS chrome120", url, dict(impersonate=True), "Total")

    print("\n2. Miroir Binance — sert-il les points d'entrée futures ?\n")
    for label, u in [
        ("data-api.binance.vision /fapi/premiumIndex",
         "https://data-api.binance.vision/fapi/v1/premiumIndex?symbol=BTCUSDT"),
        ("data-api.binance.vision /api/v3 (spot, témoin)",
         "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT"),
        ("fapi.binance.com (référence géobloquée)",
         "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
    ]:
        try:
            code, _, body, _ = _fetch(u, {})
            print(f"   {label:44} {code}  {body[:60]!r}")
        except Exception as e:
            print(f"   {label:44} —  {type(e).__name__}")

    print("\n3. OKX — profondeur d'historique du funding (le radar en a besoin)\n")
    for label, u, expect in [
        ("funding actuel", "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP",
         "fundingRate"),
        ("historique funding (100 points)",
         "https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100",
         "fundingRate"),
        ("historique open interest",
         "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H",
         "["),
    ]:
        try:
            code, size, body, _ = _fetch(u, {})
            n = body.count("fundingRate") if "funding" in label else size
            print(f"   {label:44} {code}  {'✓' if code == 200 and expect in body else '✗'}  "
                  f"{n} occurrence(s)/octets")
        except Exception as e:
            print(f"   {label:44} —  {type(e).__name__}")

    print("\nConclusion :")
    if nu == ESSAIS and dg < ESSAIS:
        print("   Farside : ne PAS se déguiser depuis une IP de datacenter.")
    elif nu == ESSAIS and dg == ESSAIS:
        print("   Farside : les deux marchent — le 403 initial était passager.")
    elif nu == 0:
        print("   Farside : inaccessible — il faut une autre source (iShares, CoinGlass).")
    else:
        print("   Farside : INSTABLE dans les deux modes — prévoir une source de secours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
