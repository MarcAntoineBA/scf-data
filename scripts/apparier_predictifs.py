#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apparier_predictifs.py — RAPPROCHER DEUX PLACES SANS INVENTER DE CHIFFRE.

LE PROBLÈME, POSÉ EXACTEMENT.
Deux marchés dont les titres se ressemblent ne sont pas le même pari. « Récession en 2026 ? »
existe sur les deux places : Kalshi règle sur une définition NBER publiée à date, Polymarket sur
son propre texte. Publier l'écart entre les deux comme s'il s'agissait d'un désaccord d'opinion,
c'est fabriquer un chiffre — le mode de panne exact que le desk interdit.

LA MÉTHODE, EN DEUX TEMPS SÉPARÉS PAR UN HUMAIN.
  1. CANDIDATS (ce module, automatique) — on propose des rapprochements et on publie les INDICES
     qui les motivent. Aucun candidat n'est agrégé, jamais. Un candidat est une question posée.
  2. DÉCLARATION (`prediction_markets_appariements.json`, à la main) — Marc-Antoine signe la
     paire, avec le motif écrit et un degré d'équivalence. Le fichier vit dans git : on sait qui
     a déclaré quoi et quand, et on peut revenir dessus.
  3. AGRÉGATION (ce module aussi) — ne consomme QUE des paires déclarées.
C'est le patron « déclarer puis auditer » du graphe de transmission, appliqué aux places.

CE QUI SERT D'INDICE, ET POURQUOI PAS LE TITRE.
Le titre est ce que le contrat RACONTE ; les indices ci-dessous sont ce qu'il PAIE.
  · l'ÉCHÉANCE — deux contrats qui ne se dénouent pas le même jour ne peuvent pas être le même
    pari, quel que soit leur titre. C'est le filtre le plus dur, et le premier appliqué.
  · les NOMBRES DU TEXTE DE RÉSOLUTION — « 200000 », « 447,000 », « 25 bps ». Un seuil partagé
    est un indice fort ; deux seuils différents sont une réfutation, pas un détail.
  · la SOURCE DE RÈGLEMENT que Kalshi nomme (8 989/8 989 en ont une) — quand Polymarket cite la
    même dans son texte, la probabilité que ce soit le même événement monte beaucoup.
  · le recouvrement de vocabulaire, EN DERNIER et jamais seul : il sert à ne pas rater une paire,
    pas à la valider.

TROIS DEGRÉS D'ÉQUIVALENCE, PARCE QUE « PAREIL » EST TROP GROSSIER.
  · `forte`         — même événement, même date, même source. Agrégation autorisée.
  · `partielle`     — même sujet, règlement différent. JUXTAPOSITION seulement : on montre les
                      deux prix côte à côte, on ne calcule aucun consensus et aucun écart.
  · `juxtaposition` — parenté thématique. Affiché ensemble, explicitement non comparable.
Le degré est déclaré par l'humain. Ce module ne le devine pas.

Usage :
    python3 apparier_predictifs.py --candidats     # propose, n'agrège rien
    python3 apparier_predictifs.py --agreger       # agrège les paires DÉCLARÉES
    python3 apparier_predictifs.py                 # les deux
"""
import argparse
import datetime
import json
import os
import re
import sys

CACHE = os.environ.get("SCF_CACHE_DIR") or os.path.expanduser(
    "~/Library/Caches/site_crypto_finance")

# ── LE DÉPÔT SE TROUVE DEPUIS CE FICHIER, PAS DEPUIS UN CHEMIN DE MACHINE ─────────────────────
# ⚠ DÉFAUT MESURÉ EN PRODUCTION, ET IL ÉTAIT SILENCIEUX. La première version codait en dur
# `~/Desktop/scf-data` — le chemin du Mac et du PC. Sur un runner GitHub le dépôt est cloné
# dans `/home/runner/work/scf-data/scf-data` : la table déclarée était donc INTROUVABLE, et
# `agreger()` prenait sa branche « aucune table déclarée » — qui est un état NORMAL et non une
# erreur. Résultat : le collecteur sortait en succès et publiait 0 paire, tous les jours, sans
# que rien ne le signale. Constaté sur la course 1h #144 : 6 paires en local, 0 sur le runner.
# On part donc de l'emplacement de CE fichier (scripts/ → racine), et on ne garde le chemin
# personnel que comme repli, pour les lancements à la main hors du dépôt.
ICI = os.path.dirname(os.path.abspath(__file__))
RACINE = os.path.dirname(ICI)
DEPOT = (os.path.join(RACINE, "cache") if os.path.isdir(os.path.join(RACINE, "cache"))
         else os.path.expanduser("~/Desktop/scf-data/cache"))

# La table DÉCLARÉE vit à la racine du dépôt (versionnée : une déclaration doit avoir un auteur
# et une date), pas dans le cache (réécrit à chaque collecte).
DECLAREES = os.path.join(RACINE, "prediction_markets_appariements.json")
if not os.path.exists(DECLAREES):
    DECLAREES = os.path.expanduser("~/Desktop/scf-data/prediction_markets_appariements.json")

SORTIE_CANDIDATS = os.path.join(CACHE, "pm_appariements_candidats.json")
SORTIE_AGREGE = os.path.join(CACHE, "pm_agrege.json")

# ── SEUILS ────────────────────────────────────────────────────────────────────────────────────
# L'échéance d'abord : deux contrats qui se dénouent à plus de 3 jours d'écart ne sont pas le
# même pari. Trois jours et non zéro, parce que les places bornent différemment (23:59 ET contre
# une heure de règlement) et qu'un écart d'un jour civil est courant sur le même événement.
ECART_ECHEANCE_MAX_J = 3
JACCARD_MINI = 0.18          # rappel, pas décision
CANDIDATS_MAX_PAR_MARCHE = 3  # au-delà, c'est que rien ne distingue : on n'en propose aucun

VIDES = {
    "the", "a", "an", "of", "in", "on", "at", "to", "by", "for", "and", "or", "be", "will",
    "is", "are", "was", "were", "this", "that", "it", "its", "as", "with", "from", "before",
    "after", "than", "then", "any", "all", "more", "less", "market", "resolve", "resolves",
    "yes", "no", "if", "otherwise", "et", "pm", "am", "date", "time", "end", "above", "below",
    "le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "en", "sur", "au", "aux",
}


def mots(txt):
    """Sac de mots utile : minuscules, ponctuation retirée, mots vides écartés.
    Les NOMBRES sont conservés tels quels — ce sont eux qui portent le seuil."""
    if not txt:
        return set()
    bruts = re.findall(r"[a-zA-Zà-ÿ]+|\d[\d.,]*", txt.lower())
    out = set()
    for m in bruts:
        if m[0].isdigit():
            out.add(normalise_nombre(m))
        elif len(m) > 2 and m not in VIDES:
            out.add(m)
    return out


def normalise_nombre(s):
    """« 200,000 » « 200000 » « 200.000 » désignent le même seuil ; les écrire différemment ne
    doit pas empêcher de les reconnaître. On retire les séparateurs de milliers et on laisse
    tomber une décimale nulle."""
    s = s.replace(",", "").rstrip(".")
    try:
        f = float(s)
    except ValueError:
        return s
    return str(int(f)) if f == int(f) else str(f)


def nombres(txt):
    if not txt:
        return set()
    return {normalise_nombre(m) for m in re.findall(r"\d[\d.,]*", txt)}


def quand(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def lire(nom, obligatoire=True):
    """Le dépôt fait foi, le cache local est le repli — même règle que le reste du desk."""
    for base in (DEPOT, CACHE):
        p = os.path.join(base, nom)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    if obligatoire:
        raise SystemExit(f"[apparier] introuvable : {nom} (ni dans le dépôt ni dans le cache)")
    return None


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  CANDIDATS
# ══════════════════════════════════════════════════════════════════════════════════════════════

def charger():
    pm = lire("prediction_markets_cache.json")
    pm_regles = (lire("prediction_markets_rules.json", False) or {}).get("events") or {}
    kal = lire("kalshi_cache.json")
    kal_regles = (lire("kalshi_rules.json", False) or {}).get("regles") or {}

    gauche = []
    for e in pm.get("entries") or []:
        rid = str(e.get("id"))
        texte = (pm_regles.get(rid) or {}).get("t") or ""
        gauche.append({
            "id": rid, "question": e.get("question") or "", "prob": e.get("prob"),
            "issue": e.get("prob_label"), "fin": e.get("end_date"), "volume": e.get("volume"),
            "liquidite": e.get("liquidity"), "tier": e.get("liquidity_tier"),
            "theme": e.get("theme_label"), "url": e.get("url"),
            "regles": texte,
            "mots": mots(e.get("question")) | mots(texte[:600]),
            "nombres": nombres(e.get("question")) | nombres(texte[:600]),
            "t_fin": quand(e.get("end_date")),
        })

    droite = []
    for m in kal.get("marches") or []:
        tk = m.get("ticker")
        r = kal_regles.get(tk) or {}
        texte = r.get("regles") or ""
        titre = (m.get("titre") or "") + " " + (m.get("sous_titre") or "")
        droite.append({
            "ticker": tk, "titre": (m.get("titre") or ""), "sous_titre": m.get("sous_titre") or "",
            "mid": m.get("prob_mid"), "bid": m.get("bid"), "ask": m.get("ask"),
            "ecart_carnet": m.get("ecart_carnet"), "volume": m.get("volume_total"),
            "fin": m.get("echeance"), "categorie": m.get("categorie"), "serie": m.get("serie"),
            "sources": r.get("sources_reglement") or [],
            "regles": texte,
            "mots": mots(titre) | mots(texte[:600]),
            "nombres": nombres(titre) | nombres(texte[:600]),
            "t_fin": quand(m.get("echeance")),
        })
    return pm, gauche, droite


def candidats(gauche, droite):
    """Propose, motive, et ne décide rien.

    Un index inversé sur les mots rares évite les 357 × 8 989 comparaisons complètes : on ne
    compare que des marchés qui partagent au moins un mot peu fréquent."""
    index = {}
    freq = {}
    for i, d in enumerate(droite):
        for w in d["mots"]:
            freq[w] = freq.get(w, 0) + 1
    for i, d in enumerate(droite):
        for w in d["mots"]:
            if freq[w] <= 400:            # un mot présent partout ne rapproche rien
                index.setdefault(w, []).append(i)

    propositions, sans_candidat = [], 0
    for g in gauche:
        vus = {}
        for w in g["mots"]:
            for i in index.get(w, ()):
                vus[i] = vus.get(i, 0) + 1
        lot = []
        for i, communs in vus.items():
            if communs < 2:
                continue
            d = droite[i]

            # ── FILTRE DUR : L'ÉCHÉANCE ───────────────────────────────────────────────────────
            if not g["t_fin"] or not d["t_fin"]:
                continue
            ecart_j = abs((g["t_fin"] - d["t_fin"]).total_seconds()) / 86400.0
            if ecart_j > ECART_ECHEANCE_MAX_J:
                continue

            inter = g["mots"] & d["mots"]
            union = g["mots"] | d["mots"]
            jac = len(inter) / len(union) if union else 0.0
            nb_communs = sorted(g["nombres"] & d["nombres"])
            # Un nombre présent d'un seul côté n'est pas neutre : si les DEUX textes portent des
            # seuils et qu'aucun n'est partagé, ce sont deux paris différents sur le même thème.
            seuils_divergents = bool(g["nombres"] and d["nombres"] and not nb_communs)

            if jac < JACCARD_MINI and not nb_communs:
                continue

            motifs = []
            if ecart_j < 1:
                motifs.append("même date de dénouement")
            else:
                motifs.append(f"dénouement à {round(ecart_j, 1)} j d'écart")
            if nb_communs:
                motifs.append("seuil(s) partagé(s) dans le texte de résolution : "
                              + ", ".join(nb_communs[:4]))
            if seuils_divergents:
                motifs.append("⚠ les deux textes portent des seuils et AUCUN n'est commun — "
                              "probablement deux paris différents")
            if d["sources"]:
                motifs.append("Kalshi règle sur : " + ", ".join(d["sources"][:3]))

            lot.append({
                "kalshi": {"ticker": d["ticker"], "titre": d["titre"][:120],
                           "mid": d["mid"], "ecart_carnet": d["ecart_carnet"],
                           "volume": d["volume"], "echeance": d["fin"],
                           "sources_reglement": d["sources"][:4]},
                "indices": {
                    "ecart_echeance_j": round(ecart_j, 2),
                    "recouvrement_vocabulaire": round(jac, 3),
                    "nombres_communs": nb_communs[:6],
                    "seuils_divergents": seuils_divergents,
                },
                "motifs": motifs,
                "force": round(jac + (0.35 if nb_communs else 0.0)
                               - (0.30 if seuils_divergents else 0.0)
                               - min(ecart_j, 3) * 0.05, 3),
            })

        if not lot:
            sans_candidat += 1
            continue
        lot.sort(key=lambda c: -c["force"])
        # Plus de N candidats crédibles = aucun ne se distingue. En proposer une liste inviterait
        # à valider au hasard ; on préfère n'en proposer aucun et le dire.
        if len(lot) > CANDIDATS_MAX_PAR_MARCHE:
            lot = lot[:CANDIDATS_MAX_PAR_MARCHE]
        propositions.append({
            "polymarket": {"id": g["id"], "question": g["question"][:160], "prob": g["prob"],
                           "issue": g["issue"], "echeance": g["fin"], "volume": g["volume"],
                           "theme": g["theme"], "url": g["url"]},
            "candidats": lot,
            "equivalence": "A_VALIDER",
        })

    propositions.sort(key=lambda p: -(p["candidats"][0]["force"] if p["candidats"] else 0))
    return propositions, sans_candidat


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  AGRÉGATION — uniquement sur des paires DÉCLARÉES
# ══════════════════════════════════════════════════════════════════════════════════════════════

def agreger(gauche, droite):
    if not os.path.exists(DECLAREES):
        # Le chemin cherché sort dans le message : c'est précisément son absence qui a rendu
        # la panne du runner indiagnosticable — « aucune table déclarée » se lit comme un état
        # normal, et ne dit pas qu'on a cherché au mauvais endroit.
        return {"paires": [], "lacunes": [
            f"aucune table d'appariement trouvée à « {DECLAREES} » : rien n'est agrégé. "
            f"C'est l'état normal tant que personne n'a signé de paire — mais si des paires "
            f"ONT été déclarées, c'est que le chemin est faux."]}, 0

    with open(DECLAREES, encoding="utf-8") as f:
        table = json.load(f)

    par_id = {g["id"]: g for g in gauche}
    par_tk = {d["ticker"]: d for d in droite}
    paires, lacunes = [], []

    for p in table.get("paires") or []:
        if not p.get("actif", True):
            continue
        g = par_id.get(str(p.get("polymarket_id")))
        d = par_tk.get(p.get("kalshi_ticker"))
        if not g or not d:
            manquant = "Polymarket" if not g else "Kalshi"
            lacunes.append(f"paire « {p.get('libelle')} » : le marché {manquant} a disparu du "
                           f"cache (expiré, réglé ou renommé) — non agrégée")
            continue

        # ── L'ISSUE COTÉE DOIT ÊTRE CELLE QU'ON A DÉCLARÉE ───────────────────────────────────
        # Sur un marché Polymarket à issues multiples, `prob` est la part de l'issue FAVORITE —
        # pas la probabilité de l'événement déclaré. « How many Senators will vote for the
        # Clarity Act ? » cote aujourd'hui « Above 58 », ce qui répond exactement au contrat
        # Kalshi « above 58 ». Mais la favorite CHANGE : le jour où elle passe à « Above 62 »,
        # la paire compare deux seuils différents et publie un écart qui n'existe pas — sans
        # qu'aucune erreur ne soit levée, puisque les deux marchés existent toujours.
        # On épingle donc l'issue à la déclaration et on la revérifie à chaque agrégation.
        issue_attendue = p.get("polymarket_issue")
        if issue_attendue and (g["issue"] or "") != issue_attendue:
            lacunes.append(
                f"paire « {p.get('libelle')} » : l'issue cotée sur Polymarket est passée de "
                f"« {issue_attendue} » (déclarée) à « {g['issue']} » — les deux contrats ne "
                f"portent plus sur le même seuil, agrégation SUSPENDUE jusqu'à re-déclaration")
            continue

        eq = p.get("equivalence", "juxtaposition")
        bloc = {
            "id": p.get("id"), "libelle": p.get("libelle"), "equivalence": eq,
            "note_reglement": p.get("note_reglement"),
            "declare_le": p.get("declare_le"),
            "polymarket": {"id": g["id"], "question": g["question"][:160], "prob": g["prob"],
                           "volume": g["volume"], "liquidite": g["liquidite"], "url": g["url"]},
            "kalshi": {"ticker": d["ticker"], "titre": d["titre"][:160], "mid": d["mid"],
                       "bid": d["bid"], "ask": d["ask"], "volume": d["volume"]},
        }

        # ── CE QU'ON CALCULE, ET SEULEMENT SI L'ÉQUIVALENCE EST FORTE ─────────────────────────
        # Sur une équivalence `partielle`, un consensus serait un nombre inventé : les deux
        # contrats ne paient pas la même chose. On juxtapose, et on le dit.
        if eq == "forte" and g["prob"] is not None and d["mid"] is not None:
            ecart_pts = round((d["mid"] - g["prob"]) * 100, 2)

            # LE CONSENSUS EST PONDÉRÉ PAR LE VOLUME, pas moyenné. Une place à 3 000 $ et une à
            # 3 M$ ne portent pas la même information ; les moyenner à parts égales donnerait à
            # la petite un poids qu'elle n'a pas.
            vg, vd = (g["volume"] or 0), (d["volume"] or 0)
            consensus = (round((g["prob"] * vg + d["mid"] * vd) / (vg + vd), 4)
                         if (vg + vd) > 0 else None)

            # ── LE CHIFFRE QUI DÉCIDE POUR LE TRADER ──────────────────────────────────────────
            # Un écart entre places ne devient une opportunité qu'au-delà du coût de l'aller-
            # retour. Le carnet Kalshi le donne ; côté Polymarket on n'a pas de carnet, donc on
            # ne peut PAS chiffrer son propre coût — on le déclare au lieu de le supposer nul,
            # ce qui ferait passer du bruit pour un gain.
            cout_kalshi_pts = (round(d["ecart_carnet"] * 100, 2)
                               if d.get("ecart_carnet") is not None else None)
            ecart_net = (round(abs(ecart_pts) - cout_kalshi_pts, 2)
                         if cout_kalshi_pts is not None else None)

            bloc["mesures"] = {
                "consensus_pondere_volume": consensus,
                "ecart_points": ecart_pts,
                "sens": ("Kalshi plus haut" if ecart_pts > 0 else
                         "Polymarket plus haut" if ecart_pts < 0 else "aligné"),
                "cout_execution_kalshi_points": cout_kalshi_pts,
                "ecart_net_de_cout_points": ecart_net,
                "actionnable": (ecart_net is not None and ecart_net > 0),
                "avertissement_cout": ("le coût d'exécution Polymarket n'est PAS mesuré "
                                       "(pas de carnet publié) : l'écart net est un PLAFOND, "
                                       "le gain réel est plus faible"),
            }
        else:
            # ── DEUX RAISONS TRÈS DIFFÉRENTES DE NE PAS AGRÉGER, ET LES CONFONDRE MENT ────────
            # Une équivalence faible est une décision sur le CONTRAT ; un prix manquant est un
            # accident de CARNET, temporaire. La première version servait le message
            # « les deux contrats ne règlent pas à l'identique » dans les deux cas — donc une
            # phrase fausse sur la moitié des paires, et qui décourageait de re-regarder une
            # paire parfaitement valide dont le carnet était vide ce jour-là.
            bloc["mesures"] = None
            if eq == "forte":
                absent = "Polymarket" if g["prob"] is None else "Kalshi"
                bloc["pourquoi_pas_de_consensus"] = (
                    "équivalence FORTE, mais aucun prix exploitable côté %s en ce moment "
                    "(carnet vide ou à un seul côté). Rien n'est calculé cette fois-ci ; la "
                    "paire reste valide et repassera dès que le carnet se reforme." % absent)
            else:
                bloc["pourquoi_pas_de_consensus"] = (
                    "équivalence déclarée « %s » : les deux contrats ne règlent pas à "
                    "l'identique, un consensus serait un chiffre inventé. Les deux prix sont "
                    "montrés côte à côte." % eq)
        paires.append(bloc)

    return {"paires": paires, "lacunes": lacunes}, len(table.get("paires") or [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidats", action="store_true")
    ap.add_argument("--agreger", action="store_true")
    a = ap.parse_args()
    tout = not (a.candidats or a.agreger)

    pm, gauche, droite = charger()
    horod = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if a.candidats or tout:
        props, sans = candidats(gauche, droite)
        avec_seuils_div = sum(1 for p in props for c in p["candidats"]
                              if c["indices"]["seuils_divergents"])
        sortie = {
            "generated_at": horod,
            "avertissement": "AUCUN de ces rapprochements n'est actif. Ce sont des questions "
                             "posées à un humain, à valider dans "
                             "prediction_markets_appariements.json.",
            "exhaustivite": {
                "polymarket_examines": len(gauche),
                "kalshi_examines": len(droite),
                "polymarket_sans_candidat": sans,
                "avec_proposition": len(props),
                "candidats_a_seuils_divergents": avec_seuils_div,
                "ecart_echeance_max_j": ECART_ECHEANCE_MAX_J,
            },
            "propositions": props,
        }
        with open(SORTIE_CANDIDATS, "w", encoding="utf-8") as f:
            json.dump(sortie, f, ensure_ascii=False, indent=1)
        print(f"[apparier] {len(props)} marché(s) Polymarket avec au moins un candidat "
              f"({sans} sans), sur {len(droite)} marchés Kalshi examinés")
        print(f"[apparier] dont {avec_seuils_div} candidat(s) portant des seuils DIVERGENTS — "
              f"signalés comme probablement différents")

    if a.agreger or tout:
        agr, n_declarees = agreger(gauche, droite)
        agr["generated_at"] = horod
        agr["paires_declarees"] = n_declarees
        with open(SORTIE_AGREGE, "w", encoding="utf-8") as f:
            json.dump(agr, f, ensure_ascii=False, indent=1)
        actionnables = sum(1 for p in agr["paires"]
                           if (p.get("mesures") or {}).get("actionnable"))
        print(f"[apparier] {len(agr['paires'])} paire(s) déclarée(s) agrégée(s), "
              f"{actionnables} avec un écart supérieur au coût d'exécution")
        for l in agr["lacunes"]:
            print(f"[apparier] lacune : {l}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
