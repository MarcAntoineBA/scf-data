#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bootstrap_repo.py — Crée le dépôt public de collecte et y dépose ses secrets.

À lancer UNE fois, depuis la machine d'origine. Idempotent : relancé, il constate
ce qui existe déjà au lieu d'échouer.

Le jeton d'accès est lu dans ~/.ghtoken et n'est jamais affiché. Les valeurs des
secrets sont lues sur la machine (fichiers de clés existants) et chiffrées AVANT
l'envoi, comme l'exige l'API : GitHub ne reçoit jamais une valeur en clair.

Ce que fait le script, dans l'ordre :
  1. vérifie l'identité du jeton ;
  2. crée le dépôt s'il n'existe pas (public, sans fichier initial) ;
  3. dépose les secrets nécessaires aux collecteurs.
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "scf-data"
TOKEN_FILE = os.path.expanduser("~/.ghtoken")


def token():
    if not os.path.exists(TOKEN_FILE):
        sys.exit(f"Jeton absent : {TOKEN_FILE}")
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def call(method, path, body=None, tok=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "Content-Type": "application/json",
                 "User-Agent": "scf-data-bootstrap"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            call.last_headers = dict(r.headers)
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        call.last_headers = dict(e.headers)
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"message": raw[:200].decode("utf-8", "replace")}


call.last_headers = {}


def read_key_file(path):
    p = os.path.expanduser(path)
    if os.path.exists(p):
        with open(p) as f:
            return f.read().strip()
    return ""


def encrypt(public_key_b64, value):
    """Chiffrement scellé (libsodium), imposé par l'API des secrets."""
    from nacl import encoding, public
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()


def main():
    tok = token()

    status, me = call("GET", "/user", tok=tok)
    if status != 200:
        sys.exit(f"Jeton refusé ({status}) : {me.get('message')}")
    owner = me["login"]
    # GitHub répond 404 — et non 403 — quand une portée manque : il refuse de révéler
    # l'existence de ce à quoi on n'a pas droit. Sans cette ligne, on chercherait
    # longtemps une erreur de nom de dépôt qui n'existe pas.
    scopes = call.last_headers.get("X-OAuth-Scopes", "")
    print(f"compte : {owner}")
    print(f"portées du jeton : {scopes or '(aucune — jeton sans droit)'}")

    required = {"repo", "public_repo"}
    granted = {s.strip() for s in scopes.split(",") if s.strip()}
    if not (granted & required):
        sys.exit("\nIl manque la portée « repo » (ou au minimum « public_repo »).\n"
                 "→ github.com/settings/tokens : ouvrir le jeton, cocher repo, "
                 "puis « Update token ».\n"
                 "   Le jeton garde la même valeur : rien à recoller ici.")

    status, _ = call("GET", f"/repos/{owner}/{REPO}", tok=tok)
    if status == 200:
        print(f"dépôt   : {owner}/{REPO} existe déjà")
    else:
        status, r = call("POST", "/user/repos", tok=tok, body=dict(
            name=REPO, private=False, has_issues=False, has_wiki=False,
            has_projects=False, auto_init=False,
            description="Collecte automatisée de données publiques de marché"))
        if status not in (200, 201):
            sys.exit(f"Création refusée ({status}) : {r.get('message')}")
        print(f"dépôt   : {owner}/{REPO} créé (public)")

    # ── Secrets ───────────────────────────────────────────────────────────────
    # Toutes les valeurs existent déjà sur la machine : rien à ressaisir.
    secrets = {
        "FRED_API_KEY": "1410940b18c0dbb6ebcfef7c3c2cba3e",
        "EIA_API_KEY": read_key_file("~/.eia_api_key"),
        "SERPAPI_KEY": read_key_file("~/.serpapi_key"),
        "GIE_API_KEY": read_key_file("~/.gie_api_key"),
        # Contact exigé par la SEC, repris tel quel des collecteurs existants.
        "SCF_CONTACT_UA": "CapitalAntifragile research (marcantoine.bassetti@gmail.com)",
    }

    status, key = call("GET", f"/repos/{owner}/{REPO}/actions/secrets/public-key", tok=tok)
    if status != 200:
        sys.exit(f"Clé publique du dépôt inaccessible ({status}) : {key.get('message')}")

    for name, value in secrets.items():
        if not value:
            print(f"  ! {name:16} valeur introuvable — secret NON déposé")
            continue
        status, r = call("PUT", f"/repos/{owner}/{REPO}/actions/secrets/{name}", tok=tok,
                         body=dict(encrypted_value=encrypt(key["key"], value),
                                   key_id=key["key_id"]))
        ok = status in (201, 204)
        print(f"  {'✓' if ok else '✗'} {name:16} {'déposé' if ok else r.get('message')}")

    print(f"\ngit remote add origin git@github.com:{owner}/{REPO}.git")
    return 0


if __name__ == "__main__":
    sys.exit(main())
