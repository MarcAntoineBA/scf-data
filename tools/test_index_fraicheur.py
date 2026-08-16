#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_index_fraicheur.py — Garde-fou de l'arbitrage par fichier.

CE QU'IL EMPÊCHE DE REVENIR
La panne du 2026-08-07 : deux caches servis avec 6 h de retard pendant qu'une copie
de 40 minutes dormait dans le déploiement, parce que l'arbitrage tranchait sur un
battement de flotte au lieu de l'âge du fichier demandé. Le correctif repose sur trois
promesses, dont aucune ne se voit à l'œil nu dans un diff — donc chacune est vérifiée
ici plutôt que confiée à la mémoire :

  1. les deux côtés extraient la MÊME date du MÊME fichier (sinon on compare des
     grandeurs différentes et l'arbitrage devient un tirage au sort) ;
  2. une date sans fuseau est REFUSÉE (deux heures d'écart en été selon la machine
     qui l'a écrite, pour une marge d'arbitrage de 25 minutes) ;
  3. un fichier qu'aucun collecteur n'a produit ne prend PAS la date du passage
     (sinon il paraît frais pour la seule raison qu'on l'a regardé).

Lancement : python3 tools/test_index_fraicheur.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import index_fraicheur as ix  # noqa: E402

PASSAGE = "2026-01-02T03:04:05Z"
échecs = []


def verifie(nom, obtenu, attendu):
    if obtenu != attendu:
        échecs.append(f"{nom}\n     attendu : {attendu!r}\n     obtenu  : {obtenu!r}")
    print(f"  {'✓' if obtenu == attendu else '✗'} {nom}")


def ecrire(dossier, nom, contenu):
    chemin = os.path.join(dossier, nom)
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)
    return chemin


def main():
    tmp = tempfile.mkdtemp(prefix="idxfraich-")
    try:
        print("Extraction de la date depuis le contenu")

        # Le cas réel des deux caches de la panne : ISO avec décalage explicite.
        c = ecrire(tmp, "narratives_cache.json",
                   '{"updated": "2026-08-07T08:38:29.452293+00:00", "x": 1}')
        verifie("ISO avec décalage → retenu et normalisé en Z",
                ix.horodatage_contenu(c), "2026-08-07T08:38:29Z")

        # Le format .js : l'objet est enveloppé, la date reste dans l'en-tête.
        c = ecrire(tmp, "enveloppe.js",
                   'window.__X__={"updated":"2026-08-07T08:38:29Z","d":[1,2]}')
        verifie("cache .js enveloppé → date lue quand même",
                ix.horodatage_contenu(c), "2026-08-07T08:38:29Z")

        # Une epoch : la forme la moins ambiguë, donc essayée en premier.
        c = ecrire(tmp, "treasury.json", '{"generated_at": 1786108925.0}')
        verifie("epoch en secondes → converti en UTC",
                ix.horodatage_contenu(c), "2026-08-07T13:22:05Z")

        c = ecrire(tmp, "millis.json", '{"updated_at_unix": 1786108925000}')
        verifie("epoch en millisecondes → détectée, pas prise pour l'an 58 000",
                ix.horodatage_contenu(c), "2026-08-07T13:22:05Z")

        # LA RÈGLE QUI PROTÈGE L'ARBITRAGE : pas de fuseau, pas de verdict.
        c = ecrire(tmp, "nue.json", '{"updated": "2026-08-07T05:58:07"}')
        verifie("date SANS fuseau → refusée (None), pas devinée",
                ix.horodatage_contenu(c), None)

        c = ecrire(tmp, "francaise.json", '{"updated": "07/08/2026 05:58"}')
        verifie("date au format d'affichage → refusée (None)",
                ix.horodatage_contenu(c), None)

        c = ecrire(tmp, "muet.json", '{"valeurs": [1, 2, 3]}')
        verifie("aucun champ de date → None", ix.horodatage_contenu(c), None)

        print("\nRepli sur le mtime, puis sur la date du passage")
        verifie("contenu daté → le contenu gagne sur mtime et passage",
                ix.dater(os.path.join(tmp, "narratives_cache.json"), PASSAGE),
                "2026-08-07T08:38:29Z")

        # RÉCIDIVE DU 2026-08-16 : un contenu non datable ne doit plus retomber
        # directement sur la date du passage. Le passage dit quand on a REGARDÉ ;
        # le mtime dit quand le collecteur a ÉCRIT. Sur 26 caches à date nue, cette
        # nuance valait jusqu'à 224 h d'erreur dans l'index.
        muet = os.path.join(tmp, "muet.json")
        os.utime(muet, (1_754_000_000, 1_754_000_000))   # 2025-07-31T22:13:20Z
        verifie("contenu non daté → mtime du fichier, pas date du passage",
                ix.dater(muet, PASSAGE), ix.horodatage_fichier(muet))
        verifie("fichier illisible → dernier repli sur la date du passage",
                ix.dater(os.path.join(tmp, "absent_du_disque.json"), PASSAGE), PASSAGE)

        print("\nConstruction de l'index")
        idx = ix.construire([tmp], PASSAGE)
        verifie("les battements (préfixe _) restent hors de l'index",
                any(n.startswith("_") for n in idx), False)
        verifie("tout fichier daté ou non a une entrée",
                sorted(idx) == sorted(n for n in os.listdir(tmp)
                                      if n.endswith((".js", ".json"))), True)

        # PROMESSE 3 : ne pas redater ce qu'aucun collecteur n'a produit.
        # Le fichier est plus VIEUX que sa date d'index : il n'a pas été réécrit,
        # donc rien à remesurer, donc il garde sa date — c'est le mensonge « frais
        # parce qu'on l'a regardé » que cette borne existe pour interdire.
        os.utime(muet, (1_577_836_800, 1_577_836_800))   # 2020-01-01T00:00:00Z
        ancien = {"muet.json": "2021-01-01T00:00:00Z"}
        idx = ix.construire([tmp], PASSAGE, ancien, seulement={"narratives_cache.json"})
        verifie("fichier non réécrit → garde sa date d'avant",
                idx["muet.json"], "2021-01-01T00:00:00Z")
        verifie("fichier produit ce passage → redaté",
                idx["narratives_cache.json"], "2026-08-07T08:38:29Z")

        # PROMESSE 4, AJOUTÉE LE 2026-08-16 : la liste `outputs` de jobs.json dérive
        # (67 outputs déclarés n'existent pas). Un cache réellement réécrit mais absent
        # de la liste tombait dans un angle mort et vieillissait sans borne dans
        # l'index — la panne de tradfi_fundamentals_cache.js, daté du 14 pour un
        # contenu du 16. Les faits sur le disque priment désormais sur la déclaration.
        os.utime(muet, (1_786_900_000, 1_786_900_000))
        idx = ix.construire([tmp], PASSAGE, ancien, seulement={"narratives_cache.json"})
        verifie("fichier réécrit mais absent des outputs → remesuré quand même",
                idx["muet.json"], ix.horodatage_fichier(muet))

        # Le fond de carte : ce qui n'est nulle part sur ce runner survit quand même.
        ancien = {"gros_cache_absent.json": "2026-08-01T00:00:00Z"}
        idx = ix.construire([tmp], PASSAGE, ancien)
        verifie("cache absent du clone → sa date publiée est conservée",
                idx.get("gros_cache_absent.json"), "2026-08-01T00:00:00Z")

        print("\nÉcriture sur disque")
        chemin = os.path.join(tmp, "_fichiers.json")
        ix.ecrire(chemin, [tmp], PASSAGE)
        with open(chemin, encoding="utf-8") as f:
            relu = json.load(f)
        verifie("index relu : toutes les valeurs sont des ISO-Z",
                all(isinstance(v, str) and v.endswith("Z") for v in relu.values()), True)

        # PROMESSE 1 : la règle est IMPORTÉE par le déploiement, jamais recopiée.
        # Une copie diverge au premier correctif — c'est arrivé à trois scripts de ce
        # projet. On vérifie que le script de publication importe bien ce module.
        print("\nRègle partagée avec le déploiement")
        deploy = os.path.expanduser(
            "~/Library/Application Support/SiteCryptoFinance/deploy_public_wrangler.sh")
        if os.path.exists(deploy):
            src = open(deploy, encoding="utf-8").read()
            verifie("deploy_public_wrangler.sh importe index_fraicheur (pas de copie)",
                    "import index_fraicheur" in src, True)
            verifie("deploy_public_wrangler.sh publie _deploy_fichiers.json",
                    "_deploy_fichiers.json" in src, True)
        else:
            print("  – script de publication absent de cette machine : contrôle ignoré")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if échecs:
        print(f"✗ {len(échecs)} contrôle(s) en échec :")
        for e in échecs:
            print(f"   · {e}")
        return 1
    print("✓ tous les contrôles passent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
