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
    if meta["holdings"] != 843775:
        fails.append("holdings")
        print(f"  ECHEC trésor recalculé {meta['holdings']:,} au lieu de 843 775")
    else:
        print(f"  ok    trésor mis à jour tout seul : {meta['holdings']:,} BTC au {meta['holdings_asof']}")

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
    if abs(cum - 29300000) > 1000:
        fails.append("purr_holdings")
        print(f"  ECHEC trésor reconstruit {cum:,.0f} HYPE au lieu de 29 300 000")
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
            if abs(implied / r["px"] - 1) > 0.01:
                fails.append("purr_px")
                print(f"  ECHEC {r['d']} : coût implicite {implied:.2f} vs px {r['px']}")
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


CACHE_JSON = os.path.join(os.path.expanduser("~"), "Library", "Caches",
                          "site_crypto_finance", "treasury_cache.json")


if __name__ == "__main__":
    sys.exit(main())
