# -*- coding: utf-8 -*-
"""Fetcher « Or vs bons du Trésor dans les réserves mondiales » — onglet Indicateur.

Reproduit, avec des sources publiques auditables et sans aucune clé payante, le
graphique de référence de Tavi Costa / Crescat Capital : la part de l'OR et la
part des BONS DU TRÉSOR AMÉRICAIN dans les réserves internationales des banques
centrales, de 1968 à aujourd'hui.

  DÉFINITION RETENUE
     dénominateur D = or officiel mondial valorisé au marché
                    + réserves de change mondiales (hors or)
     part de l'or   = or / D
     part du Trésor = bons du Trésor US détenus par les institutions
                      officielles étrangères / D

Cette définition a été retenue parce qu'elle est la seule qui reproduise les
DEUX repères chiffrés imprimés sur la capture de référence (27/08/2025) :
or 24 %, Trésor 23 %, et le croisement des deux courbes au 3e trimestre 2025.
Un dénominateur incluant les DTS et les positions de réserve au FMI donne 2 à 6
points de moins sur la part de l'or ; un dénominateur restreint aux seules
banques centrales ÉTRANGÈRES (hors États-Unis) donne 19 % au lieu de 24 %.

QUATRE SOURCES, TOUTES GRATUITES ET SANS CLÉ (sauf FRED, clé libre déjà en place)

 1. LBMA - fixing quotidien de l'once d'or en dollars, depuis le 01/04/1968.
    https://prices.lbma.org.uk/json/gold_pm.json (+ gold_am.json en secours)
    ~14 700 points. C'est LE prix auquel les banques centrales valorisent.

 2. FMI / COFER - réserves de change mondiales trimestrielles (hors or),
    série G001.TFXRA.CI_T.NV_USD.Q, 1999-Q1 -> aujourd'hui.
    C'est exactement la bonne mesure : change SEUL, sans DTS ni position FMI.

 3. Banque mondiale - avant 1999, socle annuel reconstruit en sommant les pays
    déclarants : FI.RES.TOTL.CD (réserves totales, or au fixing de fin d'année)
    et FI.RES.XGLD.CD (réserves hors or). Leur DIFFÉRENCE donne la valeur de
    l'or officiel mondial, dont on déduit le TONNAGE en divisant par le fixing
    du 31 décembre - un tonnage que l'on peut alors revaloriser à n'importe
    quelle date. Contrôle : 32 700 t en 1970, 27 400 t en 1995 (ventes des
    banques centrales européennes), 31 850 t en 2024 - conforme au World Gold
    Council à quelques centaines de tonnes près (l'or du FMI et de la BRI n'est
    pas dans les déclarations pays).

 4. FRED - bons du Trésor détenus par les institutions officielles étrangères :
    - BOGZ1FL263061130Q ... comptes financiers (Z.1) de la Fed, TRIMESTRIEL
                            depuis 1945-Q4. C'est la série de fond.
    - FORTREASPOS99990 .... enquête TIC du Trésor US, MENSUEL depuis 1984-12,
                            publiée ~2 mois plus tôt que la Z.1 : elle sert
                            uniquement à prolonger la série au-delà du dernier
                            trimestre publié par la Z.1.

DEUX RACCORDS, TOUS DEUX MESURÉS ET PUBLIÉS DANS LE CACHE (clé `meta`)

 - Change avant 1999 : la somme Banque mondiale « hors or » contient aussi les
   DTS et les positions de réserve au FMI (~20 % du total en 1970, ~3 % en
   1999), et manque les pays non déclarants. On mesure le rapport COFER/BM sur
   les cinq années de recouvrement (1999-2003) et on l'applique en amont, de
   sorte que les deux moitiés se rejoignent sans marche d'escalier. Le facteur
   mesuré (~1,02) est écrit dans `meta.raccord_change`.
   ATTENTION, conséquence assumée : dans les années 1970 la part de l'or
   ressort ~5 points SOUS la capture de référence (41 % vs 48 % en 1970), parce
   que le correctif est constant alors que le poids des DTS, lui, décroît.
   Toute la période 1999 -> aujourd'hui, elle, est exacte au dixième de point.

 - Tonnage d'or : la Banque mondiale publie une dernière année incomplète (124
   pays déclarants en 2025 contre 164 en 2024), ce qui ferait FONDRE le tonnage
   de 2 500 t d'un coup. Une garde de couverture rejette toute année dont le
   nombre de déclarants tombe sous 90 % de l'année précédente, et le tonnage de
   la dernière année valide est ALORS reporté. Il bouge de ~1 000 t/an : le
   report coûte moins de 1 point de part au bout d'un an, contre 2 points
   d'erreur si on gobait la collecte partielle.

REJOUABLE HORS LIGNE : `collecter()` ne fait que du réseau et dépose son butin
brut dans reserves_or_ust_raw.json ; `construire(brut)` ne fait que du calcul.
`python3 fetch_reserves_or_ust.py --rejouer` recalcule tout depuis le fichier
brut, sans une seule requête (cf. project_rejouer_hors_ligne).

Sorties :
  ~/Library/Caches/site_crypto_finance/reserves_or_ust_cache.js   (+ .json)
  ~/Desktop/Site_Crypto_Finance/reserves_or_ust_cache.js
"""
import bisect
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

CACHE_DIR = os.path.expanduser("~/Library/Caches/site_crypto_finance")
SITE_DIR = os.path.expanduser("~/Desktop/Site_Crypto_Finance")
RAW_PATH = os.path.join(CACHE_DIR, "reserves_or_ust_raw.json")

UA = {"User-Agent": "SiteCryptoFinance/1.0 (dashboard perso)"}
OZ_PAR_TONNE = 32150.7466          # onces troy fines dans une tonne
DEBUT = 1968                       # premier fixing LBMA : 01/04/1968

# Cle FRED : libre et publique (https://fred.stlouisfed.org/docs/api/api_key.html).
FRED_KEY = os.environ.get("FRED_API_KEY", "1410940b18c0dbb6ebcfef7c3c2cba3e")


def _get(url, timeout=120, accept=None, essais=3):
    """GET avec retry/backoff. Les quatre sources sont des services publics
    sans SLA : un 5xx passager ne doit pas faire echouer le run entier."""
    h = dict(UA)
    if accept:
        h["Accept"] = accept
    attente = 4
    for i in range(essais):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            if i == essais - 1:
                raise
            print("[reserves] " + type(e).__name__ + " sur " + url[:70]
                  + " ... retry dans " + str(attente) + "s", file=sys.stderr)
            time.sleep(attente)
            attente *= 2.5


# ---------------------- 1. COLLECTE (reseau seul) ---------------------------

def _lbma():
    """Fixings LBMA en dollars. PM (reference de valorisation) prioritaire, AM
    en bouche-trou : le PM n'existe pas sur quelques centaines de journees."""
    px = {}
    for u in ("https://prices.lbma.org.uk/json/gold_am.json",
              "https://prices.lbma.org.uk/json/gold_pm.json"):
        try:
            for r in json.loads(_get(u, 90)):
                v = (r.get("v") or [None])[0]
                if v and float(v) > 0:
                    px[r["d"]] = round(float(v), 4)
        except Exception as e:
            print("[reserves] LBMA " + u.rsplit("/", 1)[-1] + " : " + str(e),
                  file=sys.stderr)
    return dict(sorted(px.items()))


def _cofer():
    """Reserves de change mondiales, trimestrielles (FMI/COFER, monde = G001)."""
    xml = _get("https://api.imf.org/external/sdmx/2.1/data/COFER/"
               "G001.TFXRA.CI_T.NV_USD.Q", 180, accept="application/xml")
    out = {}
    for p, v in re.findall(r'TIME_PERIOD="([^"]+)"[^>]*OBS_VALUE="([^"]+)"', xml):
        m = re.match(r"^(\d{4})-Q([1-4])$", p)
        if m:
            out[m.group(1) + "-Q" + m.group(2)] = float(v)
    return out


def _banque_mondiale():
    """Reserves annuelles, sommees sur les PAYS declarants (les agregats de la
    Banque mondiale - « Monde », « Zone euro » - sont vides sur ces deux
    indicateurs, et les melanger aux pays double-compterait)."""
    c = json.loads(_get("https://api.worldbank.org/v2/country?format=json&per_page=400"))
    pays = set(x["id"] for x in c[1] if x["region"]["id"] != "NA")
    out = {}
    for ind, cle in (("FI.RES.TOTL.CD", "tot"), ("FI.RES.XGLD.CD", "xg")):
        page = 1
        while True:
            u = ("https://api.worldbank.org/v2/country/all/indicator/" + ind
                 + "?format=json&per_page=12000&date=" + str(DEBUT) + ":"
                 + str(date.today().year) + "&page=" + str(page))
            d = json.loads(_get(u))
            for r in (d[1] or []):
                if r["value"] is None or r["countryiso3code"] not in pays:
                    continue
                a = out.setdefault(r["date"], {})
                a[cle] = a.get(cle, 0.0) + r["value"]
                a[cle + "_n"] = a.get(cle + "_n", 0) + 1
            if page >= d[0]["pages"]:
                break
            page += 1
    return out


def _fred(sid):
    u = ("https://api.stlouisfed.org/fred/series/observations?"
         + urllib.parse.urlencode({"series_id": sid, "api_key": FRED_KEY,
                                   "file_type": "json"}))
    d = json.loads(_get(u, 120))
    return dict((o["date"], float(o["value"]))
                for o in d["observations"] if o["value"] != ".")


def collecter(brut_precedent=None):
    """Reseau seul. Chaque source qui tombe reprend sa valeur du run precedent
    plutot que de publier du vide (cf. project_scf_data, garde anti-cache-vide)."""
    brut = dict(brut_precedent or {})
    plan = [("or_lbma", _lbma),
            ("change_cofer", _cofer),
            ("banque_mondiale", _banque_mondiale),
            ("ust_z1", lambda: _fred("BOGZ1FL263061130Q")),
            ("ust_tic", lambda: _fred("FORTREASPOS99990"))]
    for cle, fn in plan:
        try:
            v = fn()
            if not v:
                raise ValueError("reponse vide")
            ancien = len(brut.get(cle) or {})
            if ancien and len(v) < 0.9 * ancien:
                # Une source qui MAIGRIT d'un coup ment plus souvent qu'elle ne
                # corrige : on garde l'ancienne (cf. project_garde_richesse_piege).
                print("[reserves] " + cle + " : " + str(len(v)) + " points contre "
                      + str(ancien) + " au run precedent -> collecte rejetee",
                      file=sys.stderr)
                continue
            brut[cle] = v
            print("[reserves] " + cle + " : " + str(len(v)) + " points",
                  file=sys.stderr)
        except Exception as e:
            if brut.get(cle):
                print("[reserves] " + cle + " EN ECHEC (" + str(e)
                      + ") -> reprise du run precedent", file=sys.stderr)
            else:
                print("[reserves] " + cle + " EN ECHEC (" + str(e)
                      + ") et rien en reserve", file=sys.stderr)
    brut["collecte_le"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return brut


# --------------------- 2. CONSTRUCTION (calcul seul) ------------------------

FIN_TRIM = ("03-31", "06-30", "09-30", "12-31")


def _fixing(px_dates, px, d):
    """Dernier fixing connu au plus tard le jour d (le 31/12 tombe ferie)."""
    i = bisect.bisect_right(px_dates, d) - 1
    return px[px_dates[i]] if i >= 0 else None


def construire(brut):
    px = brut.get("or_lbma") or {}
    cofer = brut.get("change_cofer") or {}
    bm = dict((int(k), v) for k, v in (brut.get("banque_mondiale") or {}).items())
    z1 = brut.get("ust_z1") or {}
    tic = brut.get("ust_tic") or {}
    if not (px and cofer and bm and z1):
        raise SystemExit("[reserves] sources critiques manquantes - rien n'est ecrit")
    pxd = sorted(px)

    # -- Tonnage d'or mondial, deduit des annuels Banque mondiale ------------
    tonnage, couverture = {}, {}
    for a in sorted(bm):
        d = bm[a]
        if "tot" not in d or "xg" not in d:
            continue
        valeur_or = d["tot"] - d["xg"]
        fix = _fixing(pxd, px, str(a) + "-12-31")
        if not fix or valeur_or <= 0:
            continue
        tonnage[a] = valeur_or / fix / OZ_PAR_TONNE
        couverture[a] = d.get("tot_n", 0)

    # Garde de couverture : une annee qui perd plus de 10 % de ses declarants
    # n'est pas une annee de baisse, c'est une collecte inachevee.
    annees, derniere_valide, rejetees = sorted(tonnage), None, []
    for a in annees:
        prec = couverture.get(a - 1, 0)
        if prec and couverture.get(a, 0) < 0.9 * prec:
            rejetees.append({"annee": a, "pays": couverture.get(a, 0),
                             "pays_prec": prec})
            continue
        derniere_valide = a
    for r in rejetees:
        tonnage.pop(r["annee"], None)
    annees = sorted(tonnage)
    if not annees:
        raise SystemExit("[reserves] aucun tonnage d'or exploitable")

    def tonnage_a(a, q):
        """Interpolation trimestrielle entre deux 31 decembre ; report plat
        au-dela de la derniere annee valide."""
        if a > derniere_valide:
            return tonnage[derniere_valide]
        av, ap = tonnage.get(a - 1), tonnage.get(a)
        if ap is None:
            return tonnage.get(min(annees, key=lambda y: abs(y - a)))
        if av is None:
            return ap
        return av + (ap - av) * (q / 4.0)

    def change_bm(a, q):
        av = bm.get(a - 1, {}).get("xg")
        ap = bm.get(a, {}).get("xg")
        if av is None or ap is None:
            return None
        return av + (ap - av) * (q / 4.0)

    # -- Raccord Banque mondiale -> COFER, mesure sur le recouvrement ---------
    num = den = 0.0
    for a in range(1999, 2004):
        for q in (1, 2, 3, 4):
            c = cofer.get(str(a) + "-Q" + str(q))
            w = change_bm(a, q)
            if c and w:
                num += c
                den += w
    raccord = (num / den) if den else 1.0

    def ust_a(a, q):
        """Z.1 d'abord (serie de fond trimestrielle depuis 1945), TIC ensuite
        pour les trimestres que la Z.1 n'a pas encore publies."""
        v = z1.get("%d-%02d-01" % (a, (q - 1) * 3 + 1))
        if v is not None:
            return v * 1e6, "Z"
        v = tic.get("%d-%02d-01" % (a, q * 3))
        if v is not None:
            return v * 1e6, "T"
        return None, None

    # -- Serie trimestrielle -------------------------------------------------
    aujourdhui = date.today().isoformat()
    hist = []
    for a in range(DEBUT, date.today().year + 1):
        for q in (1, 2, 3, 4):
            d = str(a) + "-" + FIN_TRIM[q - 1]
            if d > aujourdhui:
                break
            fx = cofer.get(str(a) + "-Q" + str(q))
            src = "C"
            if fx is None:
                w = change_bm(a, q)
                if w is None:
                    continue
                fx, src = w * raccord, "B"
            t = tonnage_a(a, q)
            fix = _fixing(pxd, px, d)
            if not t or not fix:
                continue
            ust, src_ust = ust_a(a, q)
            if ust is None:
                continue
            valeur_or = t * OZ_PAR_TONNE * fix
            total = valeur_or + fx
            hist.append({"d": d,
                         "g": round(valeur_or / total * 100, 2),
                         "u": round(ust / total * 100, 2),
                         "px": round(fix, 2),
                         "or_md": round(valeur_or / 1e9, 1),
                         "fx_md": round(fx / 1e9, 1),
                         "ust_md": round(ust / 1e9, 1),
                         "t": round(t),
                         "s": src + src_ust})
    if len(hist) < 100:
        raise SystemExit("[reserves] serie trop courte (" + str(len(hist))
                         + " trimestres)")

    # -- Croisements des deux courbes ----------------------------------------
    croisements = []
    for i in range(1, len(hist)):
        av, ap = hist[i - 1], hist[i]
        if (av["g"] - av["u"]) * (ap["g"] - ap["u"]) < 0:
            croisements.append({"d": ap["d"],
                                "sens": "or" if ap["g"] > ap["u"] else "ust"})

    dernier = hist[-1]
    ecart = round(dernier["g"] - dernier["u"], 2)
    # Variation de l'ecart sur un an (4 trimestres) : c'est le RYTHME de la
    # bascule qui informe, pas le niveau, qui bouge de quelques dixiemes.
    ref = hist[-5] if len(hist) >= 5 else hist[0]
    d_ecart = round(ecart - (ref["g"] - ref["u"]), 2)

    if ecart >= 2:
        tone, label = "warn", "L'or devant"
    elif ecart > -2:
        tone, label = "eq", "Au coude a coude"
    else:
        tone, label = "pos", "Le Tresor devant"

    # Dernier croisement dans le sens de l'or, et celui d'avant : c'est la
    # phrase de la capture de reference (« pour la premiere fois depuis 1996 »).
    vers_or = [c for c in croisements if c["sens"] == "or"]
    dernier_croisement = vers_or[-1]["d"] if vers_or else None
    precedent_or = vers_or[-2]["d"] if len(vers_or) >= 2 else None

    # -- Point « live » : le fixing du jour sur les derniers agregats connus --
    fix_live_d = pxd[-1]
    fix_live = px[fix_live_d]
    t_live = tonnage[derniere_valide]
    fx_live = cofer[max(cofer)]
    ust_live_d = max(tic) if tic else None
    ust_live = (tic[ust_live_d] * 1e6) if ust_live_d else dernier["ust_md"] * 1e9
    or_live = t_live * OZ_PAR_TONNE * fix_live
    tot_live = or_live + fx_live

    chg = (("+" if d_ecart >= 0 else "") + ("%.1f" % d_ecart).replace(".", ",")
           + " pt sur un an")

    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "current": {
            "date": dernier["d"],
            "gold_pct": dernier["g"],
            "ust_pct": dernier["u"],
            "ecart_pt": ecart,
            "ecart_chg_1a": d_ecart,
            "gold_md": dernier["or_md"],
            "fx_md": dernier["fx_md"],
            "ust_md": dernier["ust_md"],
            "gold_t": dernier["t"],
            "gold_px": dernier["px"],
            "tone": tone,
            "label": label,
            "chg_txt": chg,
            "croisement": dernier_croisement,
            "croisement_precedent": precedent_or,
        },
        "live": {
            "date": fix_live_d,
            "gold_px": round(fix_live, 2),
            "gold_pct": round(or_live / tot_live * 100, 2),
            "ust_pct": round(ust_live / tot_live * 100, 2),
            "ust_date": ust_live_d,
            "gold_t": round(t_live),
            "fx_date": max(cofer),
        },
        "history": hist,
        "croisements": croisements,
        "meta": {
            "definition": "part = composante / (or officiel mondial au marche "
                          "+ reserves de change mondiales hors or)",
            "raccord_change": round(raccord, 4),
            "raccord_fenetre": "1999-2003 (20 trimestres de recouvrement COFER/BM)",
            "tonnage_derniere_annee": derniere_valide,
            "tonnage_reporte_t": round(tonnage[derniere_valide]),
            "tonnage_annees_rejetees": rejetees,
            "sources": [
                "LBMA - fixing or USD quotidien depuis 1968 (prices.lbma.org.uk)",
                "FMI COFER - G001.TFXRA.CI_T.NV_USD.Q, change mondial trimestriel 1999+",
                "Banque mondiale - FI.RES.TOTL.CD / FI.RES.XGLD.CD, somme des pays declarants",
                "FRED BOGZ1FL263061130Q - Z.1, bons du Tresor des institutions officielles etrangeres",
                "FRED FORTREASPOS99990 - enquete TIC, prolongation mensuelle",
            ],
            "n_trimestres": len(hist),
            "debut": hist[0]["d"],
        },
    }


# ----------------------------- 3. ECRITURE ----------------------------------

def ecrire(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    corps = json.dumps(cache, ensure_ascii=False, separators=(",", ":"))
    js = "window.__RESERVES_OR_UST__ = " + corps + ";\n"
    ecrits = []
    for base in (CACHE_DIR, SITE_DIR):
        if not os.path.isdir(base):
            continue
        for nom, contenu in (("reserves_or_ust_cache.js", js),
                             ("reserves_or_ust_cache.json", corps + "\n")):
            p = os.path.join(base, nom)
            # Un fetcher ne doit jamais RETRECIR son cache sans le dire.
            if os.path.exists(p) and os.path.getsize(p) > 3 * len(contenu.encode()):
                print("[reserves] " + p + " : sortie 3x plus petite que "
                      "l'existant -> ecriture refusee", file=sys.stderr)
                continue
            with open(p, "w", encoding="utf-8") as f:
                f.write(contenu)
            ecrits.append(p)
    return ecrits


def main():
    rejouer = "--rejouer" in sys.argv
    brut_prec = None
    if os.path.exists(RAW_PATH):
        try:
            with open(RAW_PATH, encoding="utf-8") as f:
                brut_prec = json.load(f)
        except Exception:
            brut_prec = None
    if rejouer:
        if not brut_prec:
            raise SystemExit("[reserves] --rejouer : aucun butin brut sous " + RAW_PATH)
        brut = brut_prec
        print("[reserves] rejeu hors ligne depuis " + RAW_PATH, file=sys.stderr)
    else:
        brut = collecter(brut_prec)
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(RAW_PATH, "w", encoding="utf-8") as f:
            json.dump(brut, f, ensure_ascii=False, separators=(",", ":"))

    cache = construire(brut)
    c, l, m = cache["current"], cache["live"], cache["meta"]
    print("[reserves] " + str(m["n_trimestres"]) + " trimestres, " + m["debut"]
          + " -> " + c["date"], file=sys.stderr)
    print("[reserves] publie : or " + str(c["gold_pct"]) + "%  Tresor "
          + str(c["ust_pct"]) + "%  ecart " + ("%+.2f" % c["ecart_pt"])
          + " pt  (" + c["label"] + ")", file=sys.stderr)
    print("[reserves] live   : or " + str(l["gold_pct"]) + "%  Tresor "
          + str(l["ust_pct"]) + "%  fixing " + str(l["gold_px"]) + " $/oz au "
          + l["date"], file=sys.stderr)
    print("[reserves] raccord change = " + str(m["raccord_change"])
          + " - tonnage reporte depuis " + str(m["tonnage_derniere_annee"])
          + " (" + str(m["tonnage_reporte_t"]) + " t)", file=sys.stderr)
    if c["croisement"]:
        print("[reserves] dernier croisement vers l'or : " + c["croisement"]
              + ((" (precedent : " + c["croisement_precedent"] + ")")
                 if c["croisement_precedent"] else ""), file=sys.stderr)
    for p in ecrire(cache):
        print("[reserves] ecrit " + p + " (" + str(os.path.getsize(p) // 1024)
              + " Ko)", file=sys.stderr)


if __name__ == "__main__":
    main()
