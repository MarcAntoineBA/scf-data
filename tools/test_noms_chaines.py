#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LE GARDE-FOU DES NOMS DE CHAÎNES.

CE QU'IL EMPÊCHE (mesuré le 05/09/2026)
`fetch_crypto_capture.py` tient une table `CHAINES` qui associe, à chaque
chaîne, son slug d'API et son nom de page WEB. Les deux ne coïncident pas :
`/overview/fees/bitcoin-cash` répond, mais la page publique s'appelle
« Bitcoincash » et `/fees/chain/bitcoin-cash` tombe en 404.

Ce nom web sert à construire les liens d'audit des fiches — ceux qui doivent
prouver le chiffre affiché. **Quatre entrées sur toutes les chaînes suivies
pointaient une page inexistante** : « Ripple » (c'est XRPL), « Cosmos »
(CosmosHub), « Bitcoin Cash » (Bitcoincash) et « zkSync Era » (ZKsync Era).

Un lien d'audit en 404 est pire qu'un lien absent : il donne l'apparence de la
vérifiabilité, et le lecteur qui clique conclut que le chiffre est inventé.

⚠ CE CONTRÔLE DEMANDE LE RÉSEAU, et il le dit quand il ne l'a pas — il ne passe
pas en silence. Une vérification qu'on croit faite et qui ne l'est pas vaut
moins que pas de vérification du tout.

    python3 tools/test_noms_chaines.py
"""

import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLLECTEUR = os.path.join(RACINE, "scripts", "fetch_crypto_capture.py")


def _get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "CapitalAntifragile research",
                      "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as r:
        brut = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            brut = gzip.decompress(brut)
    return json.loads(brut.decode("utf-8"))


def noms_defillama():
    """Les noms de pages que DefiLlama publie, côté TVL et côté frais."""
    noms = set()
    for x in _get("https://api.llama.fi/v2/chains"):
        if x.get("name"):
            noms.add(x["name"])
    d = _get("https://api.llama.fi/overview/fees"
             "?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true")
    noms |= set(d.get("allChains") or [])
    return noms


def table_chaines():
    src = open(COLLECTEUR, encoding="utf-8").read()
    bloc = src[src.index("CHAINES = {"):]
    bloc = bloc[:bloc.index("\n}")]
    return re.findall(r'"([\w\-]+)":\s*\("([\w\-]+)",\s*"([^"]+)"\)', bloc)


def main():
    if not os.path.exists(COLLECTEUR):
        print("collecteur absent :", COLLECTEUR)
        return 1
    table = table_chaines()
    print("══ noms de chaînes ══")
    print("  %d chaînes déclarées" % len(table))

    try:
        noms = noms_defillama()
    except (urllib.error.URLError, OSError, ValueError) as e:
        # ⚠ On ne PASSE PAS en silence : sans réseau, ce contrôle n'a rien
        # vérifié, et le dire est la seule chose honnête à faire.
        print("  ⚠ RÉSEAU INDISPONIBLE (%s) — ce contrôle n'a rien vérifié." % e)
        return 2

    print("  %d noms connus de la source" % len(noms))
    norm = {re.sub(r"[^a-z0-9]", "", n.lower()): n for n in noms}
    faux = []
    for slug, gid, web in table:
        if web in noms:
            continue
        propose = (norm.get(re.sub(r"[^a-z0-9]", "", web.lower()))
                   or norm.get(re.sub(r"[^a-z0-9]", "", slug.lower())))
        faux.append((slug, web, propose))

    if faux:
        print("\n  ✗ %d nom(s) ne correspondent à aucune page :" % len(faux))
        for slug, web, propose in faux:
            print("     %-22s « %s » → %s"
                  % (slug, web, propose or "aucun équivalent trouvé"))
        print("\n%d CONTRÔLE(S) EN ÉCHEC." % len(faux))
        return 1

    print("  ✓ les %d noms correspondent à une page de la source" % len(table))
    print("\nTOUT PASSE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
