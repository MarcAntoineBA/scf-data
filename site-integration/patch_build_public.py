#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch_build_public.py — Prépare le site à lire ses données dans le dépôt de collecte.

CE QUE ÇA CHANGE : aujourd'hui, une donnée fraîche doit traverser quatre maillons
avant d'être publique (collecte → dépôt local → synchronisation → redéploiement),
tous portés par une machine qui peut dormir. Après ce correctif, la donnée est lue
directement depuis le dépôt de collecte : plus aucun redéploiement n'est nécessaire
pour qu'elle devienne publique.

DEUX MODIFICATIONS DANS build_public.sh
  1. autoriser `functions/data/` au déploiement, comme `functions/live/` déjà présent ;
  2. après la copie, DÉPLACER les fichiers de données de la racine vers `public/data/`,
     et écrire les redirections qui conservent les adresses existantes.

POURQUOI DÉPLACER PLUTÔT QUE LAISSER EN PLACE
Une redirection ne l'emporte pas de façon garantie sur un fichier statique de même
chemin — la documentation de la plateforme ne le promet nulle part. Tant qu'un fichier
occupe l'adresse d'origine, on ne peut pas savoir avec certitude lequel des deux
gagne. En déplaçant les fichiers, la question ne se pose plus : il n'y a plus de
concurrent. Les copies déplacées restent le FILET de la fonction (repli hors ligne).

Par défaut : SIMULATION. Affiche le diff, n'écrit rien sans --appliquer.
"""

import difflib
import os
import sys

SITE = os.path.expanduser("~/Desktop/Site_Crypto_Finance")
CIBLE = os.path.join(SITE, "build_public.sh")
MANIFESTE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache_manifest.txt")

ANCRE = '''# ─── REGÉNÈRE .accueil_cache/*.json depuis *.json.bak + DÉCACHE le dossier ──'''

AJOUT = '''# ─── DONNÉES SERVIES DEPUIS LE DÉPÔT DE COLLECTE ────────────────────────────
# La fonction `functions/data/` lit les caches dans le dépôt de collecte, qui tourne
# sur des serveurs et non sur cette machine. Elle n'a AUCUN binding : elle est donc
# déployable, contrairement au module sous functions/api/.
if [ -d "$SRC/functions/data" ]; then
  mkdir -p "$PUB/functions/data"
  cp -f "$SRC"/functions/data/*.js "$PUB/functions/data/" 2>/dev/null || true
  echo "[build_public] functions/data : lecture des données depuis le dépôt de collecte"
fi

# Les fichiers de données quittent la RACINE pour public/data/. Deux raisons :
#   · plus aucun fichier statique n'occupe l'adresse d'origine, donc la redirection
#     vers la fonction s'applique sans dépendre d'une règle de priorité que la
#     documentation ne garantit pas ;
#   · la copie déplacée devient le FILET de la fonction : si le dépôt de collecte est
#     injoignable, elle sert cette version-là — exactement le comportement d'avant.
if [ -f "$SRC/_cache_files_synced.txt" ]; then
  mkdir -p "$PUB/data"
  NDATA=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ -f "$PUB/$f" ]; then
      mv -f "$PUB/$f" "$PUB/data/$f" && NDATA=$((NDATA + 1))
    fi
  done < "$SRC/_cache_files_synced.txt"
  echo "[build_public] données : $NDATA fichier(s) déplacé(s) vers /data/"

  # Redirections : les pages continuent de demander « /truc_cache.js » sans savoir
  # que la donnée a déménagé. Aucune page, aucune balise à modifier.
  {
    echo "# Généré par build_public.sh — les données sont servies par functions/data/."
    while IFS= read -r f; do
      [ -n "$f" ] && echo "/$f  /data/$f  302"
    done < "$SRC/_cache_files_synced.txt"
  } > "$PUB/_redirects"

  # Sans ce fichier, TOUTE requête du site passerait par le runtime des fonctions et
  # consommerait le quota gratuit. En le limitant, le reste du site redevient du
  # statique : gratuit et illimité.
  cat > "$PUB/_routes.json" <<'ROUTES'
{"version": 1, "include": ["/data/*", "/live/*"], "exclude": []}
ROUTES
  echo "[build_public] redirections + routes limitées à /data/* et /live/*"
fi

'''


def main():
    appliquer = "--appliquer" in sys.argv[1:]

    if not os.path.exists(CIBLE):
        sys.exit(f"Introuvable : {CIBLE}")
    avant = open(CIBLE, encoding="utf-8").read()

    if "functions/data" in avant:
        print("Déjà appliqué — rien à faire.")
        return 0
    if ANCRE not in avant:
        sys.exit("Point d'insertion introuvable : le script a changé, à relire à la main.")

    apres = avant.replace(ANCRE, AJOUT + ANCRE, 1)

    diff = difflib.unified_diff(avant.splitlines(True), apres.splitlines(True),
                                fromfile="build_public.sh (actuel)",
                                tofile="build_public.sh (modifié)", n=2)
    print("".join(diff))

    nb = sum(1 for l in open(MANIFESTE) if l.strip()) if os.path.exists(MANIFESTE) else 0
    print(f"\nCe correctif déplacera {nb} fichiers de données vers /data/ au prochain build,")
    print("et écrira autant de redirections pour que les adresses existantes marchent.")

    if not appliquer:
        print("\nSIMULATION — rien n'a été écrit. Relancer avec --appliquer pour installer.")
        return 0

    sauvegarde = CIBLE + ".avant_migration_donnees"
    if not os.path.exists(sauvegarde):
        with open(sauvegarde, "w", encoding="utf-8") as f:
            f.write(avant)
    with open(CIBLE, "w", encoding="utf-8") as f:
        f.write(apres)
    print(f"\nAppliqué. Sauvegarde de la version précédente : {os.path.basename(sauvegarde)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
