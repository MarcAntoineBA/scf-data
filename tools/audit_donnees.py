#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_donnees.py — Le controle qui relit ce que le site PUBLIE et cherche
l'impossible : un chiffre qui ne peut pas exister, deux chiffres de la meme
fiche qui se contredisent, une serie qui ment sur son age.

POURQUOI IL EXISTE
Les garde-fous du depot protegent chacun UN collecteur : la garde de poids,
la garde de fraicheur, la garde de temoins, l'ordre des dependances. Aucun ne
relit le RESULTAT avec les yeux d'un lecteur. Or c'est la que se voient les
defauts qui comptent : une capitalisation superieure a la valeur pleinement
diluee, un prix rapporte a des revenus qui vaut cent vingt-six mille fois, une
fiche qui annonce « ne a moins de quatre ans » sous une serie de cours qui
prouve le contraire, un revenu affiche qui n'est pas le denominateur du rapport
affiche juste a cote.

CE QU'IL FAIT
Il balaie les caches publies et classe chaque anomalie en trois gravites :
  · BLOQUANT — le chiffre est impossible ou se contredit lui-meme ;
  · MAJEUR   — le chiffre est arithmetiquement juste et trompeur ;
  · MINEUR   — le chiffre est defendable mais merite d'etre nomme.
Il ne corrige RIEN. Un auditeur qui repare cache ce qu'il repare.

CE QU'IL N'EST PAS
Ce n'est pas un controle de fraicheur — `watchdog_freshness` et
`index_fraicheur` s'en chargent. Ce n'est pas un controle de non-regression :
chaque famille d'anomalie ci-dessous a ete OBSERVEE en production le
05/09/2026, avec le compte des entites touchees.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

CACHE = os.path.expanduser(os.environ.get(
    "SCF_CACHE", "~/Library/Caches/site_crypto_finance"))
SITE = os.path.expanduser("~/Desktop/Site_Crypto_Finance")

BLOQUANT, MAJEUR, MINEUR = "bloquant", "majeur", "mineur"
_TROUVE = []


def note(gravite, famille, quoi, exemples, combien=None):
    _TROUVE.append({"gravite": gravite, "famille": famille, "quoi": quoi,
                    "exemples": exemples[:4], "combien": combien if combien is not None else len(exemples)})


def _charge(nom, cle_js=None):
    """Un cache, qu'il soit .json ou .js — dans le dossier de cache ou le site."""
    for base in (CACHE, SITE, os.path.join(SITE, "data")):
        p = os.path.join(base, nom)
        if not os.path.exists(p):
            continue
        try:
            texte = open(p, encoding="utf-8").read()
            if nom.endswith(".js"):
                texte = texte[texte.index("{"):].rstrip().rstrip(";")
            return json.loads(texte)
        except Exception as e:
            note(BLOQUANT, "lecture", f"{nom} illisible : {e}", [p], 1)
            return None
    return None


def nb(v):
    return isinstance(v, (int, float)) and v == v and abs(v) != float("inf")


# ══════════════════════════════════════════════════════════════════════════
# NARRATIFS ET JETONS
# ══════════════════════════════════════════════════════════════════════════
def audit_narratifs():
    d = _charge("narratives_fundamentals_cache.json") or _charge("narratives_fundamentals_cache.js")
    if not d:
        note(MAJEUR, "narratifs", "cache introuvable — rien n'a pu etre verifie", [], 1)
        return
    narr = d.get("narratives") or []

    # ── L'OFFRE : deux impossibilites de definition ──────────────────────
    sur_dilue, sur_cent = {}, {}
    for n in narr:
        for t in n.get("tokens", []):
            m, f = t.get("mcap_b"), t.get("fdv_b")
            if nb(m) and nb(f) and f > 0 and m > f * 1.0005:
                sur_dilue[t.get("symbol")] = f"{m:.3f} > {f:.3f} (x{m/f:.4f})"
            c = t.get("circ_pct")
            if nb(c) and c > 100.05:
                sur_cent[t.get("symbol")] = f"{c} %"
    if sur_dilue:
        note(BLOQUANT, "narratifs / offre",
             "capitalisation SUPERIEURE a la valeur pleinement diluee — impossible : "
             "la seconde compte au moins les jetons deja en circulation",
             [f"{k} : {v}" for k, v in sur_dilue.items()])
    if sur_cent:
        note(BLOQUANT, "narratifs / offre",
             "offre en circulation au-dessus de cent pour cent",
             [f"{k} : {v}" for k, v in sur_cent.items()])

    # ── LE DENOMINATEUR PUBLIE EST-IL CELUI DU RAPPORT PUBLIE ? ──────────
    incoherents = []
    for n in narr:
        ps, rev, mc = n.get("ps_ttm"), n.get("rev_m_1y_total"), n.get("mcap_total_b")
        if not (nb(ps) and nb(mc) and mc > 0):
            continue
        if not nb(rev):
            incoherents.append(f"{n['narrative']} : rapport {ps:g}x publie, revenus « — »")
            continue
        # ⚠ LE NUMERATEUR N'EST PAS LA CAPITALISATION DU PANIER. Il ne compte
        # que les constituants QUI ONT des revenus, et le cache publie cette
        # part sous `ps_ttm_coverage_mcap_pct`. Premiere version de ce controle :
        # elle rejouait mcap_total / revenus et criait a l'incoherence sur six
        # narratifs dont le rapport etait juste — « Immobilisation liquide » a
        # 0,7x contre 28,1x « rejoues », parce que 2,3 % du panier seulement
        # porte des revenus. C'est exactement le faux positif que l'en-tete de ce
        # fichier met en garde contre.
        couv = n.get("ps_ttm_coverage_mcap_pct")
        part = (couv / 100.0) if nb(couv) and couv > 0 else 1.0
        implicite = mc * part * 1000.0 / rev
        if implicite > 0 and (implicite / ps > 1.5 or ps / implicite > 1.5):
            incoherents.append(
                f"{n['narrative']} : {ps:g}x publie, {implicite:.1f}x en rejouant "
                f"(capitalisation x {couv:.1f} % de couverture) / revenus")
    if incoherents:
        note(BLOQUANT, "narratifs / coherence",
             "le revenu affiche n'est pas le denominateur du rapport affiche sur la meme ligne",
             incoherents)

    # ── LES RAPPORTS VIDES DE SENS ──────────────────────────────────────
    absurdes = []
    for n in narr:
        ps, st = n.get("ps_ttm"), n.get("ps_ttm_statut")
        if nb(ps) and ps > 2000 and st not in ("non_mesurable", "sans_objet"):
            absurdes.append(f"{n['narrative']} : prix/frais {ps:,.0f}x sans statut")
        mt, st2 = n.get("mc_tvl"), n.get("mc_tvl_statut")
        if nb(mt) and mt > 200 and st2 not in ("non_mesurable", "sans_objet"):
            absurdes.append(f"{n['narrative']} : capitalisation/immobilise {mt:,.0f}x sans statut")
    if absurdes:
        note(MAJEUR, "narratifs / rapports",
             "un rapport dont le denominateur est effondre est publie comme un chiffre ordinaire",
             absurdes)

    # ── LE DOUBLE COMPTE ENTRE PANIERS ──────────────────────────────────
    vus, partages = {}, {}
    for n in narr:
        for t in n.get("tokens", []):
            vus.setdefault(t.get("id"), []).append(n["narrative"])
    for tid, ns in vus.items():
        if len(ns) > 1:
            partages[tid] = ns
    if partages:
        note(MINEUR, "narratifs / composition",
             "des constituants appartiennent a plusieurs paniers : les capitalisations "
             "des narratifs ne s'additionnent pas",
             [f"{k} dans {', '.join(v)}" for k, v in list(partages.items())],
             len(partages))

    # ── UN PANIER QUI N'EST QU'UN ACTIF DEGUISE ─────────────────────────
    domines = [f"{n['narrative']} : {n.get('dominant_sym')} pese {n.get('dominant_pct')} %"
               for n in narr if nb(n.get("dominant_pct")) and n["dominant_pct"] > 90]
    if domines:
        note(MINEUR, "narratifs / composition",
             "un panier dont le premier constituant pese plus de quatre-vingt-dix pour cent "
             "n'agrege rien : ses moyennes decrivent ce seul actif",
             domines)


def audit_fiches_crypto():
    d = _charge("crypto_fiches.js")
    if not d:
        note(MINEUR, "jetons", "crypto_fiches.js introuvable", [], 1)
        return
    jetons = d.get("jetons") or []

    faux_age, notes_hs, capt_hs, cotation = [], [], [], []
    for j in jetons:
        n20 = j.get("note20")
        if nb(n20) and not (0 <= n20 <= 20):
            notes_hs.append(f"{j.get('symbole')} : {n20}")
        for cle, plafond, lib in (("taux_captation_pct", 100.0, "taux de captation"),
                                  ("part_detenteurs_pct", 100.0, "part des detenteurs"),
                                  ("circ_pct", 100.05, "offre en circulation")):
            v = j.get(cle)
            if nb(v) and v > plafond:
                capt_hs.append(f"{j.get('symbole')} : {lib} {v} %")
        # La premiere cotation ne peut pas etre posterieure a la genese, ni
        # tomber pile au bord de la fenetre de collecte pour tout le monde.
        pc = j.get("premiere_cotation")
        gen = j.get("genesis")
        if pc and gen and pc > gen:
            cotation.append(f"{j.get('symbole')} : cotee le {pc}, nee le {gen}")
        if j.get("age_source") and "au moins" in str(j.get("age_source")):
            faux_age.append(f"{j.get('symbole')} : {j.get('age_annees')} ans « {j.get('age_source')} »")
    if notes_hs:
        note(BLOQUANT, "jetons / note", "note hors de l'intervalle 0-20", notes_hs)
    if capt_hs:
        note(MAJEUR, "jetons / captation",
             "une part depasse cent pour cent du total dont elle est une part", capt_hs)
    if cotation:
        note(MAJEUR, "jetons / age",
             "premiere cotation posterieure a la genese declaree — la date n'est pas "
             "une premiere cotation mais le bord de la fenetre de collecte", cotation)
    if len(faux_age) > len(jetons) * 0.5:
        note(MAJEUR, "jetons / age",
             f"l'age de {len(faux_age)} jetons sur {len(jetons)} est une BORNE INFERIEURE "
             "presentee comme une mesure ; la fiche en tire un nombre de cycles traverses",
             faux_age, len(faux_age))

    # ── LES DEUX CACHES DOIVENT DIRE LE MEME PRIX ───────────────────────
    nf = _charge("narratives_fundamentals_cache.json") or {}
    prix_nf = {}
    for n in nf.get("narratives", []):
        for t in n.get("tokens", []):
            if nb(t.get("price")):
                prix_nf.setdefault(t.get("id"), t["price"])
    ecarts = []
    for j in jetons:
        a, b = j.get("prix"), prix_nf.get(j.get("id"))
        if nb(a) and nb(b) and b > 0 and abs(a - b) / b > 0.02:
            ecarts.append(f"{j.get('symbole')} : {a:g} contre {b:g} ({(a/b-1)*100:+.1f} %)")
    if ecarts:
        note(MAJEUR, "jetons / coherence",
             "deux caches de la MEME page donnent deux cours differents pour le meme actif",
             ecarts)


# ══════════════════════════════════════════════════════════════════════════
# ETATS FINANCIERS DES SOCIETES
# ══════════════════════════════════════════════════════════════════════════
def audit_vesting():
    """Le calendrier de deverrouillage, relu comme le site le publie.

    Ce que l'auditeur cherche ici n'est pas ce que cherche le garde-fou du
    collecteur : celui-la verifie que le cache est bien FORME, celui-ci qu'il
    n'affirme rien d'impossible une fois pose a cote des autres caches.
    """
    d = _charge("crypto_vesting_cache.json") or _charge("crypto_vesting_cache.js")
    if not d:
        return
    J = d.get("jetons") or {}
    F = _charge("crypto_fiches.js") or {}
    par_id = {j.get("id"): j for j in (F.get("jetons") or [])}

    # ── Le rapport dont les deux membres viennent d'instants differents ──
    # La part de capitalisation se calcule avec le cours ET la capitalisation
    # des fiches. Si le calendrier est plus vieux que les fiches, il rapporte
    # des jetons d'aujourd'hui a une capitalisation d'hier, sans que rien ne le
    # dise. C'est le defaut que la dependance crypto.vesting <- crypto.fiches
    # previent ; on verifie qu'elle a tenu.
    desync = []
    for g, j in J.items():
        f = par_id.get(g)
        if not f or not nb(f.get("mcap_b")) or not nb(j.get("fenetres", {}).get("j90", {}).get("usd")):
            continue
        part = j["fenetres"]["j90"].get("part_capi_pct")
        if not nb(part) or not part:
            continue
        attendu = j["fenetres"]["j90"]["usd"] / (f["mcap_b"] * 1e9) * 100.0
        if abs(attendu - part) > max(0.5, abs(part) * 0.05):
            desync.append(f"{j.get('symbole')} : {part:.1f} % publie contre "
                          f"{attendu:.1f} % recalcule sur la capitalisation des fiches")
    if desync:
        note(BLOQUANT, "vesting / coherence",
             "la part de capitalisation ne se retrouve pas a partir des fiches : "
             "les deux caches ne decrivent pas le meme instant", desync)

    # ── Un deverrouillage superieur a ce qui reste a deverrouiller ──
    # Ce qui sort sur douze mois ne peut pas depasser ce que le calendrier n'a
    # pas encore libere. Un depassement dit que le taux de deverrouillage et la
    # serie quotidienne ne parlent pas de la meme offre.
    trop = []
    for j in J.values():
        mx, dev = j.get("offre_max"), j.get("deverrouille_pct")
        v12 = (j.get("fenetres") or {}).get("m12", {}).get("jetons")
        if not (nb(mx) and nb(dev) and nb(v12)) or mx <= 0:
            continue
        restant = mx * (100.0 - dev) / 100.0
        if v12 > restant * 1.02 and v12 - restant > mx * 0.001:
            trop.append(f"{j.get('symbole')} : {v12:.4g} jetons sur douze mois "
                        f"pour {restant:.4g} restant a liberer")
    if trop:
        note(BLOQUANT, "vesting / offre",
             "il sort plus de jetons que le calendrier n'en a encore a liberer", trop)

    # ── Une echeance dans le passe ──
    maintenant = time.time()
    passees = []
    for j in J.values():
        for e in (j.get("prochains") or [])[:3]:
            try:
                t = datetime.strptime(e["date"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if t < maintenant - 86400 * 2:
                passees.append(f"{j.get('symbole')} : {e['date']} deja passee")
                break
    if passees:
        note(MAJEUR, "vesting / fraicheur",
             "des echeances annoncees comme a venir sont deja passees : le cache "
             "n'a pas ete refait depuis", passees)

    # ── Un jeton note sans que son calendrier soit dit ──
    # Un jeton dont un tiers de l'offre reste a sortir et dont la fiche n'en dit
    # rien porte une note qui ignore sa dilution.
    muets = []
    for g, j in J.items():
        if j.get("statut") != "mesurable" or j.get("nature") != "vesting":
            continue
        dev = j.get("deverrouille_pct")
        f12 = (j.get("fenetres") or {}).get("m12", {}).get("part_capi_pct")
        # ⚠ UN CALENDRIER EPUISE N'EST PAS UNE OMISSION. Cinq jetons — BNB, MNT,
        # JUP, GEOD, SKY — sont a moitie deverrouilles sans fenetre a douze mois
        # parce que leur serie publiee S'ARRETE : celle de BNB au 05/09/2026,
        # celle de JUP avant. La fiche le dit deja en toutes lettres (« le
        # calendrier publie s'arrete au X, il reste pourtant Y % a liberer »).
        # Les signaler ici reprocherait au cache de ne pas inventer une date que
        # personne ne publie — et un auditeur qui crie au loup se fait desarmer.
        if nb(dev) and dev < 70 and not nb(f12) and not j.get("calendrier_epuise"):
            muets.append(f"{j.get('symbole')} : {dev:.0f} % deverrouille, "
                         "aucune fenetre a douze mois chiffree")
    if muets:
        note(MAJEUR, "vesting / couverture",
             "un jeton largement verrouille ne chiffre pas sa dilution a venir", muets)

    # ── La contradiction avec le champ « offre en circulation » de la fiche ──
    ecarts = []
    for g, j in J.items():
        f = par_id.get(g)
        if not f or j.get("nature") == "emission":
            continue
        a, b = j.get("circulant_pct"), f.get("circ_pct")
        if nb(a) and nb(b) and abs(a - b) > 0.5:
            ecarts.append(f"{j.get('symbole')} : {a:.1f} % dans le calendrier "
                          f"contre {b:.1f} % dans la fiche")
    if ecarts:
        note(MAJEUR, "vesting / coherence",
             "l'offre en circulation differe entre le calendrier et la fiche", ecarts)


def audit_societes(max_paquets=None):
    import glob
    paquets = sorted(glob.glob(os.path.join(CACHE, "sec_detail_*.json"))) + \
              sorted(glob.glob(os.path.join(CACHE, "intl_detail_*.json")))
    if not paquets:
        note(MINEUR, "societes", "aucun paquet d'etats financiers a auditer", [], 1)
        return
    if max_paquets:
        paquets = paquets[:max_paquets]

    bilan, marges, actions, futur, notes_hs, altman = [], [], [], [], [], []
    n_soc = n_ex = 0
    annee_max = datetime.now(timezone.utc).year + 1
    for chemin in paquets:
        try:
            soc = (json.load(open(chemin, encoding="utf-8")) or {}).get("societes") or {}
        except Exception:
            continue
        for sym, s in soc.items():
            n_soc += 1
            r = s.get("resume") or {}
            nq = r.get("note_q") or {}
            n = nq.get("note")
            if nb(n) and not (0 <= n <= 20):
                notes_hs.append(f"{sym} : {n}")
            nr = nq.get("note_ramenee")
            if nb(nr) and not (0 <= nr <= 20):
                notes_hs.append(f"{sym} : ramenee {nr}")
            az = r.get("altman_z")
            if nb(az) and abs(az) > 100:
                altman.append(f"{sym} : Z = {az:,.0f}")
            for e in s.get("exercices") or []:
                n_ex += 1
                an = e.get("annee")
                if nb(an) and an > annee_max:
                    futur.append(f"{sym} : exercice {an}")
                # ⚠ LE BILAN NE SE FERME PAS SUR TROIS LIGNES, MAIS SUR CINQ.
                # Premiere version de ce controle : 775 exercices signales, et
                # le premier examine — Apollo 2022 — se refermait EXACTEMENT
                # des qu'on ajoutait les interets minoritaires (7,726 Md$) et
                # les capitaux mezzanine (1,032 Md$). `equity` porte la part du
                # GROUPE ; l'actif est finance par le passif, la part du groupe,
                # celle des minoritaires et le mezzanine. Un auditeur qui crie
                # au loup sur sept cent soixante-quinze cas justes se fait
                # desarmer au premier coup d'oeil — et c'est alors le vrai
                # defaut qui passe.
                a, p, cp = e.get("assets"), e.get("liabilities"), e.get("equity")
                mi = e.get("interets_minoritaires_bilan") or 0
                mz = e.get("capitaux_mezzanine") or 0
                if nb(a) and nb(p) and nb(cp) and a > 0:
                    total = p + cp + (mi if nb(mi) else 0) + (mz if nb(mz) else 0)
                    ecart = abs(total - a) / a
                    if ecart > 0.02:
                        bilan.append(f"{sym} {an} : actif {a:,.0f} contre passif+capitaux "
                                     f"{total:,.0f} ({ecart*100:.1f} %)")
                mb = e.get("marge_brute")
                if nb(mb) and (mb > 100.5 or mb < -1000):
                    marges.append(f"{sym} {an} : marge brute {mb:,.1f} %")
                sd, ca = e.get("shares_diluted"), e.get("ca_par_action")
                if nb(sd) and 0 < sd < 100000 and nb(e.get("revenue")) and e["revenue"] > 1e8:
                    actions.append(f"{sym} {an} : {sd:,.0f} actions pour "
                                   f"{e['revenue']/1e6:,.0f} M de chiffre d'affaires"
                                   + (f", soit {ca:,.0f} par action" if nb(ca) else ""))
    if notes_hs:
        note(BLOQUANT, "societes / note", "note hors de l'intervalle 0-20", notes_hs)
    if actions:
        note(BLOQUANT, "societes / unites",
             "un nombre d'actions incompatible avec le chiffre d'affaires : l'unite du "
             "depot (milliers, millions) n'a pas ete redressee, et tout montant PAR ACTION "
             "de cet exercice est faux du meme facteur", actions)
    if bilan:
        note(MAJEUR, "societes / bilan",
             "l'actif ne se referme pas sur passif + capitaux propres a plus de deux pour cent",
             bilan)
    if marges:
        note(MAJEUR, "societes / marges",
             "marge brute impossible — au-dessus de cent pour cent ou effondree sous "
             "un chiffre d'affaires quasi nul", marges)
    if altman:
        note(MAJEUR, "societes / scores",
             "score d'Altman hors de toute echelle lisible (1,8 = detresse, 3 = securite)",
             altman)
    if futur:
        note(MAJEUR, "societes / dates", "exercice posterieur a l'annee prochaine", futur)
    return n_soc, n_ex


# ══════════════════════════════════════════════════════════════════════════
def main():
    t0 = time.time()
    borne = None
    for a in sys.argv[1:]:
        if a.startswith("--paquets="):
            borne = int(a.split("=", 1)[1])
    print("AUDIT DES DONNEES PUBLIEES")
    print(f"  cache : {CACHE}\n")

    audit_narratifs()
    audit_fiches_crypto()
    audit_vesting()
    compte = audit_societes(borne) or (0, 0)

    par_gravite = {BLOQUANT: [], MAJEUR: [], MINEUR: []}
    for x in _TROUVE:
        par_gravite[x["gravite"]].append(x)

    for g, titre in ((BLOQUANT, "BLOQUANT — le chiffre est impossible ou se contredit"),
                     (MAJEUR, "MAJEUR — juste et trompeur"),
                     (MINEUR, "MINEUR — defendable, a nommer")):
        lot = par_gravite[g]
        if not lot:
            continue
        print(f"\n{'=' * 74}\n{titre}\n{'=' * 74}")
        for x in lot:
            print(f"\n  [{x['famille']}] {x['quoi']}")
            print(f"      {x['combien']} entite(s) touchee(s)")
            for e in x["exemples"]:
                print(f"        · {e}")

    print(f"\n{'-' * 74}")
    print(f"{compte[0]} societe(s) et {compte[1]} exercice(s) relus · "
          f"{len(par_gravite[BLOQUANT])} bloquant(s), {len(par_gravite[MAJEUR])} majeur(s), "
          f"{len(par_gravite[MINEUR])} mineur(s) · {round(time.time() - t0, 1)} s")
    return 1 if par_gravite[BLOQUANT] else 0


if __name__ == "__main__":
    sys.exit(main())
