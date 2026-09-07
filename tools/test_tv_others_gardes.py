#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_gardes.py — verifie que fetch_tv_others_cache refuse de degrader un cache sain.

Chaque cas simule une reponse TradingView abimee et exige que le cache existant
survive INTACT. Un collecteur qui ecrit du vide efface une donnee juste : ces
gardes sont la seule chose qui distingue « pas de collecte » de « collecte perdue ».
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MOD = os.path.join(os.path.expanduser("~"), "scf-data-work", "scripts",
                   "fetch_tv_others_cache.py")

spec = importlib.util.spec_from_file_location("tvo", MOD)
tvo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tvo)

echecs = []


def cache_sain(n=655, last=1788739200):
    """Un cache de reference : n bars hebdo consecutives finissant a `last`."""
    bars = [{"d": last - (n - 1 - i) * 604800, "p": 1e11 + i} for i in range(n)]
    return {"generated_at": 1788784461, "last_bar": last, "count": n,
            "bars": bars, "symbol": "CRYPTOCAP:OTHERS", "interval": "1W"}


def lancer(nom, faux_retour, attendu_intact=True):
    """Installe un cache sain, force fetch_bars a renvoyer faux_retour, verifie."""
    tmp = tempfile.mkdtemp()
    js = os.path.join(tmp, "tv_others_cache.js")
    jsn = js.replace(".js", ".json")
    ref = cache_sain()
    with open(jsn, "w", encoding="utf-8") as f:
        json.dump(ref, f)
    with open(js, "w", encoding="utf-8") as f:
        f.write("/* sain */")

    o_js, o_json, o_fetch = tvo.OUT_JS, tvo.OUT_JSON, tvo.fetch_bars
    tvo.OUT_JS, tvo.OUT_JSON = js, jsn
    tvo.fetch_bars = lambda: faux_retour
    try:
        code = tvo.main()
    finally:
        tvo.OUT_JS, tvo.OUT_JSON, tvo.fetch_bars = o_js, o_json, o_fetch

    apres = json.load(open(jsn, encoding="utf-8"))
    intact = apres == ref

    ok = (intact == attendu_intact) and (code != 0 if attendu_intact else code == 0)
    print(("  OK  " if ok else " ECHEC") + " | " + nom
          + " | code=" + str(code)
          + " bars_apres=" + str(len(apres.get("bars", []))))
    if not ok:
        echecs.append(nom)
    shutil.rmtree(tmp, ignore_errors=True)


print("Gardes de fetch_tv_others_cache.py")
print("-" * 62)

# 1. Reponse vide (TradingView muet, reseau coupe)
lancer("reponse vide", ([], False, "connexion refusee"))

# 2. Serie tronquee sous le plancher
lancer("serie tronquee (50 bars)",
       ([{"d": 1393200000 + i * 604800, "p": 1e11} for i in range(50)], True, None))

# 3. Serie non terminee : TradingView n'a pas dit series_completed
lancer("serie non terminee",
       ([{"d": 1393200000 + i * 604800, "p": 1e11} for i in range(655)], False, None))

# 4. Perte de plus de 10 % des bars
lancer("perte de 15 % des bars",
       ([{"d": 1393200000 + i * 604800, "p": 1e11} for i in range(556)], True, None))

# 5. Regression temporelle : derniere barre plus VIEILLE que celle en cache
vieux = [{"d": 1788739200 - 604800 * (655 - i), "p": 1e11} for i in range(655)]
lancer("derniere barre plus ancienne", (vieux, True, None))

# 6. Cas SAIN : doit passer et ECRIRE
bons = [{"d": 1788739200 + 604800 - (654 - i) * 604800, "p": 1e11 + i}
        for i in range(655)]
lancer("collecte saine (doit ecrire)", (bons, True, None), attendu_intact=False)

print("-" * 62)
if echecs:
    print("ECHECS : " + ", ".join(echecs))
    sys.exit(1)
print("Les 6 gardes tiennent.")
