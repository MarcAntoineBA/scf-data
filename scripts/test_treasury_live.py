#!/usr/bin/env python3
"""Test anti-régression : le site rattrape-t-il tout seul un mouvement non seedé ?

Rejoue la situation exacte du 26/07/2026 : le seed s'arrête au 8 juin 2026 et
quatre mouvements ont été publiés depuis, dont DEUX VENTES. Le pipeline doit
les retrouver seul dans les 8-K, avec les bons montants et le bon signe.

Ne fait aucun appel réseau : il rejoue les 8-K déjà mémorisés dans
treasury_edgar_cache.json. Usage :  python3 test_treasury_live.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_treasury_companies as F   # noqa: E402

# Seed tel qu'il était AVANT le correctif (dernière ligne : 8 juin 2026).
SEED_AU_8_JUIN = [r for r in F.MSTR_PURCHASES if r[0] <= "2026-06-08"]
META_AU_8_JUIN = {"holdings": 845256, "holdings_asof": "2026-06-08",
                  "avg_cost": 75681, "cost_total_usd": 63.97e9, "shares_m": 351.6}

ATTENDU = [   # (date fin de période, montant BTC, source)
    ("2026-06-14", 1587,  "achat"),
    ("2026-06-21", 520,   "achat"),
    ("2026-06-30", -1363, "VENTE"),
    ("2026-07-05", -2225, "VENTE"),
]


def main():
    if not os.path.exists(F.EDGAR_CACHE):
        print("ECHEC: cache EDGAR absent — lancer fetch_treasury_companies.py d'abord")
        return 1
    with open(F.EDGAR_CACHE, encoding="utf-8") as f:
        cache = json.load(f)
    events = F.edgar_items(cache, "mstr")
    if not events:
        print("ECHEC: aucun 8-K mémorisé")
        return 1

    audit = {"added": [], "selftest": []}
    rows, meta = F.merge_mstr(events, audit, seed=SEED_AU_8_JUIN, meta=META_AU_8_JUIN)

    fails = []
    print("=" * 72)
    print("TEST — reprise automatique des 8-K depuis un seed arrêté au 08/06/2026")
    print("=" * 72)
    for d, amt, kind in ATTENDU:
        hit = F.match_event(rows, d, amt)
        ok = hit is not None
        print(f"  {'ok   ' if ok else 'ECHEC'} {kind:5s} {d} {amt:+6d} BTC"
              + (f"  -> retrouvé au {hit['d']} ({hit['amt']:+} BTC, {hit.get('usd_m')} M$)" if ok else "  -> MANQUANT"))
        if not ok:
            fails.append(d)

    # Le signe doit survivre : une vente reste négative de bout en bout.
    for d, amt, kind in ATTENDU:
        if kind != "VENTE":
            continue
        hit = F.match_event(rows, d, amt)
        if hit and (hit["amt"] >= 0 or (hit.get("usd_m") or -1) > 0):
            fails.append(d + " (signe)")
            print(f"  ECHEC vente {d} enregistrée avec un signe positif : {hit}")

    # Le trésor déclaré doit suivre les 8-K, pas rester figé sur le seed.
    # L'attente est le DERNIER trésor publié dans les 8-K mémorisés, jamais une
    # constante : Strategy dépose chaque semaine, donc un nombre écrit en dur
    # devient faux au dépôt suivant et le test échoue sur une donnée SAINE
    # (c'était le cas le 2026-09-01 : 843 775 attendus, 840 447 publiés).
    # Ce qu'on teste, c'est le MÉCANISME — le trésor suit-il le dernier dépôt ?
    declares = sorted((e for e in events if e.get("hold")), key=lambda e: e["d"])
    attendu = round(declares[-1]["hold"]) if declares else None
    if attendu is None:
        fails.append("holdings")
        print("  ECHEC aucun trésor déclaré dans les 8-K mémorisés")
    elif meta["holdings"] != attendu:
        fails.append("holdings")
        print(f"  ECHEC trésor recalculé {meta['holdings']:,} au lieu de {attendu:,} "
              f"(dernier 8-K du {declares[-1]['d']})")
    else:
        print(f"  ok    trésor mis à jour tout seul : {meta['holdings']:,} BTC au {meta['holdings_asof']}")
        if meta["holdings_asof"] == META_AU_8_JUIN["holdings_asof"]:
            fails.append("holdings_fige")
            print("  ECHEC le trésor est resté à la date du seed — rien n'a été repris")

    cum = sum(r["amt"] for r in rows)
    if abs(cum - meta["holdings"]) > 25:
        fails.append("coherence")
        print(f"  ECHEC cumul {cum:,} vs déclaré {meta['holdings']:,}")
    else:
        print(f"  ok    cohérence cumul/déclaré (écart {cum - meta['holdings']:+})")

    # ── Filet CoinGecko : une BAISSE de trésor doit créer une vente ─────
    # (le bug d'origine : seule une hausse était traitée, donc une vente hors
    # 8-K restait invisible.)
    # On se place volontairement APRÈS la fenêtre de retard de CoinGecko : c'est
    # le seul moment où ce filet a le droit de parler (cf. test_cg_lag — dans la
    # fenêtre, l'écart mesure le retard de CoinGecko, pas un mouvement).
    apres_retard = (datetime.strptime(meta["holdings_asof"], "%Y-%m-%d")
                    + timedelta(days=F.CG_LAG_DAYS + 1)).strftime("%Y-%m-%d")
    for live, attendu in ((meta["holdings"] - 4000, "vente"), (meta["holdings"] + 4000, "achat")):
        r2, m2, a2 = list(rows), dict(meta), {"added": []}
        rec = F.cg_net(r2, m2, live, 64000.0, apres_retard, "MSTR", "BTC", a2, with_px=True)
        signe_ok = rec and ((rec["amt"] < 0) == (attendu == "vente")) and (
            (rec.get("usd_m") or 0) < 0) == (attendu == "vente")
        print(f"  {'ok   ' if signe_ok else 'ECHEC'} filet CoinGecko {attendu} : "
              + (f"{rec['amt']:+,} BTC, {rec['usd_m']:+} M$, trésor -> {m2['holdings']:,}" if rec else "AUCUN mouvement créé"))
        if not signe_ok:
            fails.append("cg_" + attendu)
    # Un écart minuscule ne doit PAS fabriquer de faux mouvement.
    r3, m3, a3 = list(rows), dict(meta), {"added": []}
    if F.cg_net(r3, m3, meta["holdings"] + 5, 64000.0, "2026-07-26", "MSTR", "BTC", a3):
        fails.append("cg_bruit")
        print("  ECHEC filet CoinGecko : un écart de 5 BTC a créé un mouvement")
    else:
        print("  ok    filet CoinGecko : écart négligeable ignoré (pas de faux mouvement)")

    fails += test_purr(cache)
    fails += test_cg_lag()
    fails += test_libelles_mstr()
    fails += test_shares_mstr()
    fails += test_shares_hist()

    print("-" * 72)
    print("RESULTAT :", "OK — rien ne peut plus passer inaperçu" if not fails
          else f"{len(fails)} ECHEC(S) : {fails}")
    return 1 if fails else 0


def test_purr(cache):
    """Second bug de la même famille : PURR figé au 27 avril 2026.

    Le graphe s'arrêtait trois mois avant le présent parce que le scan ne lisait
    que les 8-K — or PURR n'annonce PAS ses achats par 8-K, l'ELOC Chardan tourne
    en continu et les soldes ne sortent que dans les 10-Q et les S-1/A. Le test
    repart d'un seed arrêté au 29 avril (20,0 M HYPE) et exige que le pipeline
    retrouve SEUL les 29,3 M du S-1/A du 21 juil., soit +9,3 M (+47%).
    """
    fails = []
    print()
    print("=" * 72)
    print("TEST — PURR : reprise des soldes hors 8-K (10-Q / S-1-A) depuis le 29/04")
    print("=" * 72)
    items = [it for it in F.edgar_items(cache, "purr") if it.get("total")]
    if not items:
        print("  ECHEC aucun solde HYPE mémorisé — le scan ne lit-il que les 8-K ?")
        return ["purr_scan"]
    srcs = sorted({it.get("src") or "?" for it in items})
    print(f"  info  {len(items)} soldes lus, sources : {', '.join(srcs)}")
    if not any("prospectus" in s or "10-Q" in s for s in srcs):
        fails.append("purr_forms")
        print("  ECHEC aucun solde issu d'un 10-Q ou d'un prospectus")

    seed = [dict(e) for e in F.PURR_EVENTS if e["d"] <= "2026-04-29"]
    meta = {"holdings": 20000000, "holdings_asof": "2026-04-29",
            "avg_cost": None, "cost_total_usd": None, "shares_m": 134.62}
    hype = [(d, p) for d, p in
            [(x[0], x[1]) for x in json.load(open(CACHE_JSON, encoding="utf-8"))["assets"]["hype"]["px"]]]
    orig_events, orig_meta, orig_ir = F.PURR_EVENTS, F.PURR_META, F.hypestrat_holdings
    F.PURR_EVENTS, F.PURR_META = seed, meta
    # Le test doit rester hermétique ET prouver que la SEC SEULE suffit : on coupe
    # le site IR. S'il faut le site pour retrouver les 29,3 M, le correctif est
    # incomplet (c'était le cœur du bug : dépendre d'une source qui se tait).
    F.hypestrat_holdings = lambda: None
    try:
        rows, m = F.build_purr(hype, cache, {"added": [], "selftest": [], "cost": {}})
    finally:
        F.PURR_EVENTS, F.PURR_META, F.hypestrat_holdings = orig_events, orig_meta, orig_ir

    cum = sum(r["amt"] for r in rows)
    # Attente = DERNIER solde publié dans les dépôts mémorisés, jamais une
    # constante : PURR a publié 29,4 M le 27 août, ce qui faisait échouer le test
    # sur une donnée saine. Ce qui est testé, c'est que la SEC SEULE (site IR
    # coupé) permette de rattraper le solde le plus récent depuis un seed à 20 M.
    attendu = round(max(items, key=lambda it: it["d"])["total"])
    if abs(cum - attendu) > 1000:
        fails.append("purr_holdings")
        print(f"  ECHEC trésor reconstruit {cum:,.0f} HYPE au lieu de {attendu:,}")
    elif cum <= 20000000:
        fails.append("purr_holdings")
        print(f"  ECHEC trésor resté au niveau du seed ({cum:,.0f}) — rien rattrapé")
    else:
        print(f"  ok    trésor rattrapé tout seul : {cum:,.0f} HYPE au {m['holdings_asof']}")
    tardifs = [r for r in rows if r["d"] > "2026-04-29"]
    if not tardifs:
        fails.append("purr_events")
        print("  ECHEC aucun mouvement postérieur au 29/04 récupéré")
    else:
        print(f"  ok    {len(tardifs)} mouvement(s) postérieur(s) : "
              + ", ".join(f"{r['d']} {r['amt']:+,}" for r in tardifs))
    # Un mouvement estimé doit être payé au cours MOYEN de sa fenêtre, jamais au
    # close du jour de publication (l'écart atteignait 27% en février).
    for r in tardifs:
        if r.get("est") and r.get("px") and r.get("usd_m"):
            implied = r["usd_m"] * 1e6 / r["amt"]
            # `usd_m` est arrondi à 0,1 M$ : sur un PETIT mouvement cet arrondi
            # pèse un pourcentage énorme sans qu'aucun prix ne soit faux (achat
            # du 15/07/2026 : 24 915 HYPE ≈ 1,68 M$, l'arrondi à 1,7 vaut 1,02%).
            # La tolérance doit donc absorber l'arrondi, pas le dénoncer : on
            # compare au demi-pas d'arrondi ramené au montant, plancher à 1%.
            tol = max(0.01, (0.05e6 / (r["amt"] * r["px"])) * 1.05)
            if abs(implied / r["px"] - 1) > tol:
                fails.append("purr_px")
                print(f"  ECHEC {r['d']} : coût implicite {implied:.2f} vs px {r['px']} "
                      f"(écart {abs(implied / r['px'] - 1) * 100:.2f}% > {tol * 100:.2f}%)")
    # Le prix d'exécution DÉCLARÉ ne doit jamais être écrasé par un cours spot.
    fev = next((r for r in F.PURR_EVENTS if r["d"] == "2026-02-03"), None)
    if not fev or abs(fev.get("px", 0) - 25.94) > 0.01:
        fails.append("purr_fev")
        print("  ECHEC l'achat de février a perdu son prix moyen déclaré de 25,94 $")
    else:
        print("  ok    achat de février conservé à son prix d'exécution déclaré (25,94 $)")
    # L'apport en nature ne doit pas être compté comme un achat du management.
    apport = next((r for r in rows if r.get("kind") == "contribution"), None)
    if not apport:
        fails.append("purr_kind")
        print("  ECHEC l'apport en nature de décembre n'est pas marqué 'contribution'")
    else:
        print(f"  ok    apport en nature isolé ({apport['amt']:,} HYPE, kind=contribution)")
    return fails


def test_cg_lag():
    """Vente FANTÔME du 27/07/2026 : CoinGecko en retard pris pour un mouvement.

    Ce jour-là l'Accueil a affiché en rouge « Vente BMNR · hier · −9 946 ETH »
    alors que BitMine venait d'ACHETER exactement ces 9 946 ETH (8-K du 27/07,
    semaine close le 26/07 : 5 777 468 → 5 787 414). CoinGecko, encore à
    5 777 468, a fourni un écart que `cg_net` a transcrit en vente datée du jour.
    Sans garde, ça se reproduit à chaque dépôt hebdomadaire — et une fausse vente
    est bien pire qu'une donnée manquante.
    Le test exige les DEUX comportements : fantôme supprimé, vraie vente gardée.
    """
    fails = []
    print()
    print("=" * 72)
    print("TEST — CoinGecko en retard sur un 8-K frais ≠ mouvement")
    print("=" * 72)

    audit = {"added": []}
    meta = {"holdings": 5787414, "holdings_asof": "2026-07-26"}   # 8-K de la veille
    rec = F.cg_net([{"amt": 5787414}], meta, 5777468.0, 3800.0,
                   "2026-07-27", "BMNR", "ETH", audit)
    if rec is not None:
        fails.append("cg_fantome")
        print(f"  ECHEC vente fantôme recréée : {rec['amt']:+} ETH")
    else:
        print("  ok    aucun mouvement fabriqué (SEC du 26/07 plus récente que CoinGecko)")
    if meta["holdings"] != 5787414:
        fails.append("cg_holdings")
        print(f"  ECHEC le trésor SEC a été écrasé par CoinGecko ({meta['holdings']})")
    else:
        print("  ok    trésor déclaré par la SEC laissé intact")
    if not audit.get("cg_skipped"):
        fails.append("cg_audit")
        print("  ECHEC écart ignoré SANS trace dans l'audit (silence = angle mort)")
    else:
        print("  ok    écart ignoré mais tracé dans l'audit")

    # Non-régression du correctif du 2026-07-26 : quand la SEC se TAIT vraiment,
    # CoinGecko doit continuer à rattraper les ventes (3 588 BTC de Strategy
    # invisibles pendant 3 semaines — c'est ce filet qui les avait rendues).
    audit2 = {"added": []}
    meta2 = {"holdings": 5787414, "holdings_asof": "2026-07-10"}
    rec2 = F.cg_net([{"amt": 5787414}], meta2, 5700000.0, 3800.0,
                    "2026-07-27", "BMNR", "ETH", audit2)
    if not rec2 or rec2["amt"] >= 0:
        fails.append("cg_vente_reelle")
        print("  ECHEC vente réelle non détectée alors que la SEC est muette depuis 17 j")
    else:
        print(f"  ok    vente réelle toujours détectée hors fenêtre de retard "
              f"({rec2['amt']:+,} ETH)")
    if not (0 < F.CG_LAG_DAYS <= 7):
        fails.append("cg_lag_valeur")
        print(f"  ECHEC CG_LAG_DAYS={F.CG_LAG_DAYS} hors du raisonnable (1–7 j)")
    else:
        print(f"  ok    fenêtre de retard = {F.CG_LAG_DAYS} j (< cadence hebdo des dépôts)")
    return fails


def test_libelles_mstr():
    """Le parseur MSTR lit-il TOUS les libellés d'en-tête que Strategy emploie ?

    POURQUOI CE TEST EXISTE (2026-09-01). Strategy a renommé « BTC Acquired » en
    « BTC Purchased » au 8-K du 17 août. Le parseur, ancré sur le seul ancien
    nom, a rendu une liste VIDE pour les trois dépôts suivants — sans erreur.
    Rien ne l'a vu : le reste de la suite rejoue le cache EDGAR *déjà parsé*,
    jamais le TEXTE BRUT d'un filing, donc aucun test ne pouvait attraper une
    régression de libellé (vérifié par mutation le jour du correctif).
    Ce test travaille donc sur des extraits de texte réels, hors réseau.
    """
    fails = []
    print()
    print("=" * 72)
    print("TEST — libellés d'en-tête des 8-K MSTR (achat, vente, renommage)")
    print("=" * 72)
    # Extraits RÉELS, réduits aux colonnes utiles (texte aplati comme le fait le
    # collecteur). Un cas par libellé rencontré dans les dépôts.
    CAS = [
        ("BTC Purchased — 8-K du 31 août 2026", "2026-08-31", 4603, 845050, 75412,
         "During Period August 24, 2026 to August 30, 2026 As of August 30, 2026 "
         "BTC Purchased Aggregate Purchase Price (in millions) Average Purchase Price "
         "Aggregate BTC Holdings Aggregate Purchase Price (in billions) Average Purchase Price "
         "4,603 $ 369.7 $ 80,318 845,050 $ 63.73 $ 75,412"),
        ("BTC Acquired — ancien libellé, doit rester lu", "2026-06-15", 1587, 846842, 75656,
         "During Period June 8, 2026 to June 14, 2026 As of June 14, 2026 "
         "BTC Acquired Aggregate Purchase Price (in millions) Average Purchase Price "
         "Aggregate BTC Holdings Aggregate Purchase Price (in billions) Average Purchase Price "
         "1,587 $100.0 $63,024 846,842 $64.07 $75,656"),
        ("BTC Sold — la vente reste négative", "2026-08-10", -1690, 840447, 75385,
         "During Period August 3, 2026 to August 9, 2026 As of August 9, 2026 "
         "BTC Sold Aggregate Sale Price (in millions) Average Sale Price "
         "Aggregate BTC Holdings Aggregate Purchase Price (in billions) Average Purchase Price "
         "1,690 $108.6 $64,262 840,447 $63.36 $75,385"),
    ]
    for label, filed, amt_att, hold_att, cost_att, texte in CAS:
        evs = F.edgar_mstr_events("BTC Update " + texte, filed)
        if not evs:
            fails.append("libelle:" + filed)
            print(f"  ECHEC {label} : AUCUN événement lu — libellé non reconnu")
            continue
        e = evs[0]
        if e["amt"] != amt_att:
            fails.append("libelle_amt:" + filed)
            print(f"  ECHEC {label} : montant {e['amt']:+} au lieu de {amt_att:+}")
        elif e.get("hold") != hold_att:
            fails.append("libelle_hold:" + filed)
            print(f"  ECHEC {label} : trésor {e.get('hold')} au lieu de {hold_att}")
        elif round(e.get("cost_avg") or 0) != cost_att:
            fails.append("libelle_cost:" + filed)
            print(f"  ECHEC {label} : coût moyen {e.get('cost_avg')} au lieu de {cost_att}")
        else:
            print(f"  ok    {label} : {e['amt']:+} BTC, trésor {e['hold']:,.0f}, "
                  f"coût moyen {e['cost_avg']:,.0f} $")
    # Le format PROSE (semaine sans opération) ne doit pas être pris pour un trou.
    prose = ("BTC Update On May 26, 2026, Strategy announced that, during the period "
             "between May 18, 2026 and May 25, 2026, Strategy did not sell any shares "
             "under its at-the-market offering program and did not purchase any bitcoin.")
    import io as _io
    import contextlib as _ctx
    _err = _io.StringIO()
    with _ctx.redirect_stderr(_err):
        ev_prose = F.edgar_mstr_events(prose, "2026-05-26")
    if ev_prose:
        fails.append("libelle_prose")
        print("  ECHEC un 8-K sans opération a produit un mouvement fantôme")
    else:
        print("  ok    8-K rédigé en prose (semaine sans opération) : aucun mouvement inventé")
    # La sentinelle ne doit PAS crier sur ce cas : une alerte qui se déclenche
    # chaque semaine calme finit par être ignorée, et le prochain vrai
    # renommage passerait alors inaperçu.
    if "renommé" in _err.getvalue():
        fails.append("sentinelle_bruit")
        print("  ECHEC la sentinelle crie sur un 8-K en prose (fausse alerte)")
    else:
        print("  ok    la sentinelle reste muette sur une semaine sans opération")
    # ...mais elle DOIT crier si un vrai libellé inconnu apparaît.
    inconnu = ("BTC Update During Period August 24, 2026 to August 30, 2026 As of August 30, 2026 "
               "BTC Obtained Aggregate Purchase Price (in millions) 4,603 $ 369.7 $ 80,318 845,050")
    _err2 = _io.StringIO()
    with _ctx.redirect_stderr(_err2):
        F.edgar_mstr_events(inconnu, "2026-08-31")
    if "renommé" not in _err2.getvalue():
        fails.append("sentinelle_muette")
        print("  ECHEC la sentinelle n'a pas signalé un libellé inconnu (« BTC Obtained »)")
    else:
        print("  ok    la sentinelle signale un libellé inconnu (« BTC Obtained »)")

    # Le SIGNE d'une vente doit survivre au parseur : « BTC Sold » -> montant
    # négatif. Une inversion ferait passer une vente pour un achat, donc un
    # trésor en hausse alors qu'il baisse.
    ventes = F.edgar_mstr_events("BTC Update " + CAS[2][5], "2026-08-10")
    if not ventes or ventes[0]["amt"] >= 0 or (ventes[0].get("usd_m") or 0) >= 0:
        fails.append("libelle_signe")
        print("  ECHEC la vente du 10 août n'est pas enregistrée négativement : "
              + str(ventes[0] if ventes else "aucun événement"))
    else:
        print("  ok    le signe de la vente survit (montant ET dollars négatifs)")

    # Un tableau de VENTE ne doit jamais être lu comme un achat, même si le
    # libellé d'achat manque : le montant lu serait alors celui des holdings
    # (846 000 BTC « achetés » — le bug attrapé le 26/07/2026), ou le trésor
    # monterait au lieu de baisser.
    for e in (ventes or []):
        if e.get("amt", 0) > 0 or abs(e.get("amt", 0)) > 100000:
            fails.append("vente_lue_achat")
            print(f"  ECHEC la vente est lue comme un achat / montant aberrant : {e}")
            break
    # Et le trésor doit BAISSER dans la méta après une vente.
    seed_v = [r for r in F.MSTR_PURCHASES if r[0] <= "2026-06-08"]
    meta_v = {"holdings": 842137, "holdings_asof": "2026-08-02", "avg_cost": 75681,
              "cost_total_usd": 63.97e9, "cost_asof": "2026-08-02", "shares_m": 351.6}
    _, mv = F.merge_mstr(ventes, {"added": [], "selftest": []},
                         seed=seed_v, meta=dict(meta_v))
    if mv["holdings"] >= meta_v["holdings"]:
        fails.append("vente_tresor")
        print(f"  ECHEC après une vente le trésor n'a pas baissé : "
              f"{meta_v['holdings']:,} -> {mv['holdings']:,}")
    else:
        print(f"  ok    après la vente le trésor baisse : {meta_v['holdings']:,} "
              f"-> {mv['holdings']:,} BTC")

    # GARDE-FOU DU COÛT : deux colonnes incohérentes doivent être REFUSÉES.
    # Sans lui, un tableau mal aligné écrirait un coût moyen absurde en base.
    faux = ("BTC Update During Period August 24, 2026 to August 30, 2026 As of August 30, 2026 "
            "BTC Purchased Aggregate Purchase Price (in millions) Average Purchase Price "
            "Aggregate BTC Holdings Aggregate Purchase Price (in billions) Average Purchase Price "
            "4,603 $ 369.7 $ 80,318 845,050 $ 12.00 $ 75,412")   # 12 Md incohérent avec 845k × 75 412
    ev_faux = F.edgar_mstr_events(faux, "2026-08-31")
    if ev_faux and ev_faux[0].get("cost_total"):
        fails.append("libelle_garde_cout")
        print(f"  ECHEC un coût incohérent (12 Md$) a été accepté : {ev_faux[0]['cost_total']}")
    else:
        print("  ok    coût incohérent refusé par le garde-fou (total ≠ moyen × trésor)")

    # Le coût cumulé doit REMONTER dans la méta via merge_mstr, pas seulement
    # être extrait : c'est ce chaînon qui manquait et qui figeait le coût.
    seed = [r for r in F.MSTR_PURCHASES if r[0] <= "2026-06-08"]
    meta0 = {"holdings": 845256, "holdings_asof": "2026-06-08", "avg_cost": 75681,
             "cost_total_usd": 63.97e9, "cost_asof": "2026-06-08", "shares_m": 351.6}
    ev31 = F.edgar_mstr_events("BTC Update " + CAS[0][5], "2026-08-31")
    _, meta_maj = F.merge_mstr(ev31, {"added": [], "selftest": []},
                               seed=seed, meta=dict(meta0))
    if round(meta_maj.get("avg_cost") or 0) != 75412 or not meta_maj.get("cost_total_usd"):
        fails.append("cout_non_remonte")
        print(f"  ECHEC le coût du 8-K n'a pas remplacé celui de la méta : "
              f"{meta_maj.get('avg_cost')} (attendu 75 412)")
    elif meta_maj["cost_asof"] != "2026-08-30":
        fails.append("cout_asof")
        print(f"  ECHEC cost_asof non mis à jour : {meta_maj.get('cost_asof')}")
    else:
        print("  ok    coût cumulé repris du 8-K : 75 412 $ / "
              f"{meta_maj['cost_total_usd'] / 1e9:.2f} Md$ au {meta_maj['cost_asof']}")

    # Un dépôt PLUS ANCIEN ne doit jamais écraser un coût plus récent.
    ev15 = F.edgar_mstr_events("BTC Update " + CAS[1][5], "2026-06-15")
    _, meta_vieux = F.merge_mstr(ev15, {"added": [], "selftest": []},
                                 seed=seed, meta=dict(meta_maj))
    if round(meta_vieux.get("avg_cost") or 0) != 75412:
        fails.append("cout_regression")
        print(f"  ECHEC un 8-K de juin a écrasé le coût d'août : {meta_vieux.get('avg_cost')}")
    else:
        print("  ok    un dépôt plus ancien n'écrase pas un coût plus récent")
    return fails


def test_shares_mstr():
    """Le compte d'actions MSTR s'ancre-t-il sur le 10-Q le PLUS RÉCENT ?

    Bug du 2026-09-01 : l'ancre était restée au 10-Q de mai alors que celui du
    3 août était disponible, d'où 419,76 M actions au lieu de 425,50 M — une
    capitalisation sous-estimée de 0,76 Md$ et une mNAV faussée d'autant.
    """
    fails = []
    print()
    print("=" * 72)
    print("TEST — actions MSTR : ancre sur le 10-Q le plus récent")
    print("=" * 72)
    hist = F.SHARES_HIST.get("mstr") or []
    if not hist:
        print("  ECHEC aucune ancre d'actions pour MSTR")
        return ["shares_vide"]
    dates = [d for d, _, _ in hist]
    if sorted(dates) != dates:
        fails.append("shares_ordre")
        print(f"  ECHEC les ancres ne sont pas triées par date : {dates}")
    derniere, n, src = hist[-1]
    # La couverture du 10-Q du 3 août 2026 : 364 585 501 A + 19 640 250 B.
    if derniere < "2026-07-24" or n < 384_000_000:
        fails.append("shares_ancre")
        print(f"  ECHEC ancre la plus récente = {n:,} au {derniere} — le 10-Q du "
              "3 août 2026 (384 225 751 au 24 juil.) n'est pas pris en compte")
    else:
        print(f"  ok    ancre la plus récente : {n:,} actions au {derniere} ({src})")
    # Les classes A+B doivent être additionnées, pas la seule classe A.
    if n == 364_585_501:
        fails.append("shares_classeB")
        print("  ECHEC seule la classe A est comptée : la classe B (19 640 250) manque")
    return fails


def test_shares_hist():
    """L'historique des actions est-il fait de COMPTES À DATE, sourcés et cohérents ?

    Refonte du 2026-09-04 : les moyennes pondérées trimestrielles (datées en fin
    de trimestre) sous-estimaient la capitalisation de 20 % sur BitMine au
    30 nov. 2025. Chaque point doit maintenant être un compte exact, dans
    l'échelle d'aujourd'hui, daté et sourcé, et l'historique doit commencer au
    plus tard 45 j après le premier mouvement de trésor — sinon le graphe de
    mNAV démarre en retard, comme pour BitMine et PURR avant la refonte.
    """
    fails = []
    print()
    print("=" * 72)
    print("TEST — historique des actions : comptes à date, sourcés, dès le premier achat")
    print("=" * 72)
    premiers = {"mstr": F.MSTR_PURCHASES[0][0], "bmnr": F.BMNR_SNAPSHOTS[0][0],
                "purr": min(e["d"] for e in F.PURR_EVENTS)}
    for cid, hist in F.SHARES_HIST.items():
        avant = len(fails)
        dates = [d for d, _, _ in hist]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            fails.append(cid + "_ordre")
            print(f"  ECHEC {cid} : dates non triées ou en double")
        if any(n <= 0 for _, n, _ in hist):
            fails.append(cid + "_valeur")
            print(f"  ECHEC {cid} : un compte nul ou négatif")
        if any(not src or "moyenne pondérée" in src for _, _, src in hist):
            fails.append(cid + "_moyenne")
            print(f"  ECHEC {cid} : un point est une moyenne pondérée ou n'a pas de source")
        premier = premiers.get(cid)
        if premier and F.ddays(dates[0], premier) > 45:
            fails.append(cid + "_debut")
            print(f"  ECHEC {cid} : premier compte au {dates[0]}, premier mouvement au {premier} "
                  "— plus de 45 j d'écart, l'historique démarre en retard")
        # Un saut de plus de ×3 entre deux points consécutifs ne peut venir que
        # d'une levée documentée dans la source (BitMine juil. 2025 : placement
        # privé + ATM, de 6,2 M à 112,3 M). Ailleurs c'est une erreur d'échelle.
        for (d0, n0, _), (d1, n1, s1) in zip(hist, hist[1:]):
            if n1 / n0 > 3 and "placement" not in s1 and "prospectus" not in s1:
                fails.append(cid + "_saut")
                print(f"  ECHEC {cid} : saut ×{n1 / n0:.1f} entre {d0} et {d1} sans levée documentée")
        if len(fails) == avant:
            print(f"  ok    {cid} : {len(hist)} comptes exacts du {dates[0]} au {dates[-1]}")
    return fails


CACHE_JSON = os.path.join(os.path.expanduser("~"), "Library", "Caches",
                          "site_crypto_finance", "treasury_cache.json")


if __name__ == "__main__":
    sys.exit(main())
