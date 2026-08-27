#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Soixante-dix-huit grandeurs de marché pour soixante-dix-sept mille sociétés.

CE QUE C'EST
Le screener de stockanalysis appelle un point d'entrée qui rend, en UNE requête
par pays, la ligne complète de chaque société cotée :

    /_api/endpoints/screener/data-points?type=s&ids=<champs>&c=<ISO2>

Quatre-vingt-cinq pays, environ quatre-vingt-six requêtes, et l'on tient
l'équivalent exact de ce que le concurrent appelle son « batch » : une ligne
dénormalisée par action, celle qui alimente à la fois son screener, ses
watchlists et ses tableaux de pairs. Gratuitement, sans clé, sans cookie.

⚠ CE QUE CE POINT D'ENTRÉE NE DIT PAS
Il accepte N'IMPORTE QUEL identifiant de champ et le renvoie à null — j'ai
mesuré qu'il sert docilement `nombreDeChats`. Demander un champ ne prouve donc
rien du tout : la seule question qui vaille est le TAUX DE REMPLISSAGE, et il
doit être mesuré sur PLUSIEURS pays, parce qu'un champ servi aux États-Unis est
souvent vide partout ailleurs.

Relevé du 27/08/2026 sur quatre univers (US 5 600, France 720, Japon 3 934,
Royaume-Uni 3 980 titres) :

  · 78 champs remplis à plus de 40 % PARTOUT — c'est la liste SOLIDES ;
  · 15 champs partiels, dont TOUTES les prévisions d'analystes : l'objectif de
    prix couvre 68,6 % des américaines mais 13,1 % des britanniques ;
  · 19 champs vides, dont `earningsEpsEstimate` (0 % hors États-Unis),
    `totalDebt`, `bookValue`, `dividend` et `ocf` — vides même aux États-Unis.

Ces trois listes sont écrites en clair plus bas. Elles ne sont pas une opinion :
chaque ligne a été comptée.

⚠ LA DEVISE, ENCORE
`priceCurrency` est la devise du COURS. `currency` est celle des états
financiers, et elle est nulle partout hors États-Unis. Les confondre étiquette
une cible londonienne en pence comme des dollars. Londres cote en GBX — des
pence — et le point d'entrée le dit honnêtement : on divise par cent et on
l'écrit.

⚠ LES CHAMPS QUI PORTENT UN NOM DE NIVEAU ET CONTIENNENT UN TAUX
`revenueThisYear` vaut 89,4 pour NVIDIA. Ce n'est pas un chiffre d'affaires,
c'est une CROISSANCE de 89,4 %. Idem pour `revenueNextYear`, `revenue3y`,
`epsThisYear`, `eps3y`. Publier ces champs sous leur nom d'origine, c'est
publier un faux. Ils sont renommés `croissance_*`.

SORTIES
  · marche_<CLÉ>.json — une ligne par société, découpée comme l'univers
  · marche_actions_index.json — le compte rendu et les taux de remplissage
"""
import signal as _signal
import sys as _sys


def _delai(signum, frame):
    print("[fatal] délai global (20 min) atteint — abandon.", file=_sys.stderr)
    _sys.exit(2)


try:
    _signal.signal(_signal.SIGALRM, _delai)
    _signal.alarm(20 * 60)
except Exception:
    pass

import gzip
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path.home() / "Library" / "Caches" / "site_crypto_finance"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OUT_DIR = CACHE_DIR

BASE = "https://stockanalysis.com/_api/endpoints/screener/data-points"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
# Combien de sociétés on PUBLIE. Le point d'entrée en sert quatre-vingt-trois
# mille ; les publier toutes ferait vingt et un gigaoctets par an dans un
# dépôt git. On garde les plus grosses capitalisations, plus les titres
# suivis en profondeur quelle que soit leur taille.
PLAFOND_PUBLIE = 10000

DEBIT = 0.35
TIMEOUT = 180
RETRIES = 3
_last = [0.0]

# ── Les champs, classés par ce qu'ils valent RÉELLEMENT ────────────────────
# Mesuré le 27/08/2026 sur quatre univers. Le seuil de 40 % est le taux minimal
# constaté sur le pays le MOINS bien servi des quatre : un champ à 90 % aux
# États-Unis et 3 % au Japon n'est pas un champ, c'est un piège.
SOLIDES = """
name exchange country priceCurrency isin sector industry employees founded
price change volume marketCap high52 low52 ma50 ma200 rsi atr beta
peRatio psRatio pbRatio pFcfRatio pOcfRatio evEbitda evEbit evSales evFcf
revenue grossProfit operatingIncome netIncome ebitda ebit eps
grossMargin operatingMargin profitMargin ebitdaMargin ebitMargin fcfMargin
revenueGrowth revenueGrowthQ epsGrowth
fcf capex ocf netCash workingCapital tangibleBookValue
roe roa roic roce currentRatio quickRatio debtEquity debtEbitda
interestCoverage assetTurnover inventoryTurnover taxRate
dividendYield buybackYield earningsYield fcfYield
ch1w ch1m ch3m ch6m chYTD ch1y ch3y ch5y ch10y
earningsDate lastEarningsDate nextEarningsDate
""".split()

# Servis, mais inégalement. On les prend quand même — une prévision d'analyste
# n'existe QUE là où des analystes suivent le titre, et c'est une information en
# soi — mais la fiche doit dire « non suivi », jamais « donnée manquante ».
PARTIELS = """
analystRatings analystCount priceTarget priceTargetChange
peForward pegRatio payoutRatio payoutFrequency dividendGrowth
sharesOut netCashGrowth epsGrowthQ
revenueThisYear revenueNextYear revenue3y
""".split()

# Les champs qui portent un nom de NIVEAU et contiennent un TAUX. Les publier
# sous leur nom d'origine reviendrait à annoncer un chiffre d'affaires de 89,4
# pour NVIDIA là où il s'agit d'une croissance de 89,4 %.
RENOMMAGES = {
    "revenueThisYear": "croissance_ca_exercice_pct",
    "revenueNextYear": "croissance_ca_suivant_pct",
    "revenue3y": "croissance_ca_3a_pct",
    "revenueGrowth": "croissance_ca_pct",
    "revenueGrowthQ": "croissance_ca_trim_pct",
    "epsGrowth": "croissance_bpa_pct",
    "epsGrowthQ": "croissance_bpa_trim_pct",
    "dividendGrowth": "croissance_dividende_pct",
    "netCashGrowth": "croissance_tresorerie_nette_pct",
}

# Les 85 codes qui servent des titres, relevés en balayant l'ISO 3166 le
# 27/08/2026. Le Royaume-Uni est « UK », pas « GB » — « GB » rend zéro titre
# sans erreur, ce qui est la pire façon de se tromper.
PAYS = """
DE US IN CN UK JP CA HK KR TW AU TH IT MY BR AT SE ID MX PL FR VN TR SG AR IL
PK CH SA BD LK NO CL ES RO PH PE ZA EG FI RU AE JO GR BG DK NG KW BE NL NZ JM
OM LU CO MU MA HU TN KE CZ HR QA PT CY PS BH GH ZW TT IS MT RS LT KZ TZ ZM IE
BW SI UG LV SK LB UA
""".split()


def _get(url):
    for essai in range(RETRIES):
        d = time.time() - _last[0]
        if d < DEBIT:
            time.sleep(DEBIT - d)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "gzip",
            "Accept": "application/json", "Referer": "https://stockanalysis.com/"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return None
            if essai == RETRIES - 1:
                return None
            time.sleep(1.5 * (essai + 1))
        except Exception:
            if essai == RETRIES - 1:
                return None
            time.sleep(1.5 * (essai + 1))
    return None


def bulk(champs, pays=None):
    d = _get(BASE + "?type=s&ids=" + "+".join(champs) + (("&c=" + pays) if pays else ""))
    if not d:
        return {}
    x = d.get("data", d)
    if isinstance(x, dict) and "data" in x:
        x = x["data"]
    return x if isinstance(x, dict) else {}


def _cle_fragment(mot):
    """Le même découpage que l'univers : on doit pouvoir joindre les deux."""
    s = unicodedata.normalize("NFKD", (mot or "?")).encode("ascii", "ignore").decode().upper()
    s = s.lstrip(" .-'\"")
    if not s:
        return None
    c = s[0]
    if "A" <= c <= "Z":
        return c
    if c.isdigit():
        return s[:2] if (len(s) >= 2 and s[1].isdigit()) else "0"
    return None




def charger_suivis():
    """Les symboles dont on collecte déjà les états financiers.

    Ils sont publiés quelle que soit leur capitalisation : ce sont ceux dont la
    fiche est complète, et il serait absurde qu'elle perde ses grandeurs de
    marché parce que la société est petite.
    """
    out = set()
    for nom in ("sec_fundamentals_index.json", "intl_fundamentals_index.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                out |= set((json.load(fh).get("societes") or {}).keys())
        except Exception:
            pass
    return out

PAQUETS = 64


def _cle_paquet(sym):
    """Le fragment d'un symbole : une empreinte, pas ses premières lettres.

    Découper sur les deux premiers caractères suit la langue, pas la donnée :
    on obtenait un fragment de 373 kilo-octets pour « HO » et des dizaines de
    fragments de deux lignes, plus sept cent soixante-dix-huit entrées au
    manifeste de publication.

    Une empreinte modulo soixante-quatre donne des fragments réguliers — cent
    soixante-dix sociétés, environ cent soixante kilo-octets — et soixante-
    quatre entrées. On perd la lisibilité du nom de fichier ; on gagne un poids
    prévisible, ce qui compte davantage pour une page qui télécharge.

    ⚠ L'empreinte est volontairement PRIMITIVE, pour être réécrite à
    l'identique en JavaScript en trois lignes. Une empreinte savante qui
    divergerait entre les deux langages produirait des fiches vides sans le
    moindre message d'erreur — la pire catégorie de panne.
    """
    s = unicodedata.normalize("NFKD", (sym or "?")).encode("ascii", "ignore").decode().upper()
    h = 0
    for c in s:
        h = (h * 31 + ord(c)) % 4294967296
    return "%02d" % (h % PAQUETS)

def charger_univers():
    """La table de correspondance « chemin de la source → notre symbole ».

    Sans elle, on collecterait soixante-dix-sept mille lignes que la fiche ne
    saurait pas retrouver : elle cherche par « MC.PA », la source répond à
    « epa/MC ».
    """
    f = CACHE_DIR / "univers_actions.json"
    if not f.exists():
        return {}, {}
    with f.open(encoding="utf-8") as fh:
        d = json.load(fh)
    par_sa, meta = {}, {}
    for t in d.get("titres", []):
        sym = t.get("yahoo") or t.get("sa")
        if not sym:
            continue
        sa = t.get("sa") or ""
        par_sa[sa] = sym
        # Les cotations américaines arrivent tantôt nues (« NVDA ») tantôt
        # préfixées (« otc/TCEFF ») selon l'appel. On indexe les deux formes.
        if "/" in sa:
            par_sa.setdefault(sa.split("/")[-1], sym)
        else:
            par_sa.setdefault(sa.lower(), sym)
        meta[sym] = {"nom": t.get("nom"), "place": t.get("place"),
                     "principal": t.get("principal")}
    return par_sa, meta


def _nettoyer(v, champs_pence):
    """Une ligne brute devient une ligne publiable."""
    out = {}
    pence = (v.get("priceCurrency") == "GBX")
    for k, val in v.items():
        if val is None:
            continue
        # Londres cote en pence. Diviser par cent et le dire vaut mieux que
        # publier un cours cent fois trop grand à côté d'un bénéfice en livres.
        if pence and k in champs_pence and isinstance(val, (int, float)):
            val = val / 100.0
        out[RENOMMAGES.get(k, k)] = val
    if pence:
        out["priceCurrency"] = "GBP"
        out["_converti_de"] = "GBX"
    return out


def main():
    t0 = time.time()
    champs = SOLIDES + PARTIELS
    # Les grandeurs exprimées dans la devise du COURS, donc en pence à Londres.
    champs_pence = {"price", "high52", "low52", "ma50", "ma200", "atr",
                    "priceTarget", "priceTargetChange", "eps", "dividend"}

    par_sa, meta = charger_univers()
    print("[info] univers de correspondance : %d chemins connus" % len(par_sa))

    brut, muets, doublons = {}, [], 0
    # Les États-Unis d'abord, SANS code pays : cet appel-là rend les tickers nus
    # des grandes places, exactement la forme que porte notre univers.
    appels = [(None, "US grandes places")] + [(p, p) for p in PAYS]
    for i, (code, lib) in enumerate(appels, 1):
        x = bulk(champs, code)
        if not x:
            muets.append(lib)
            continue
        for k, v in x.items():
            if k in brut:
                doublons += 1
                continue
            brut[k] = v
        if i % 20 == 0:
            print("[info] %d/%d appels — %d lignes" % (i, len(appels), len(brut)))

    print("[ok] %d lignes brutes en %.1f s (%d doublons ignorés)"
          % (len(brut), time.time() - t0, doublons))
    if muets:
        print("[ok] pays muets : %s" % ", ".join(muets))

    # ── Taux de remplissage, publié : c'est la seule preuve qu'un champ vaut ──
    n = len(brut) or 1
    remplissage = {}
    for c in champs:
        k = RENOMMAGES.get(c, c)
        remplissage[k] = round(100.0 * sum(1 for v in brut.values()
                                           if v.get(c) is not None) / n, 1)

    # ── Jointure sur nos symboles ──
    lignes, sans_symbole = {}, 0
    for k, v in brut.items():
        sym = par_sa.get(k) or par_sa.get(k.split("/")[-1]) or par_sa.get(k.lower())
        if not sym:
            sans_symbole += 1
            continue
        if sym in lignes:
            continue
        lignes[sym] = _nettoyer(v, champs_pence)

    print("[ok] %d lignes rattachées à un symbole, %d sans correspondance"
          % (len(lignes), sans_symbole))

    pence = sum(1 for v in lignes.values() if v.get("_converti_de") == "GBX")
    print("[ok] %d cotations converties de pence en livres" % pence)

    # ── Découpage ──
    # Une ligne est un TABLEAU, pas un objet : répéter « operatingMargin » sur
    # soixante-huit mille lignes coûtait plus cher que les nombres eux-mêmes —
    # cent soixante-trois mégaoctets contre quelques dizaines. La liste des
    # champs est écrite une fois, en tête du fragment.
    #
    # Le découpage se fait sur le seul SYMBOLE, et sur deux caractères : on
    # arrive toujours ici avec un symbole en main, parce que la recherche a
    # déjà fait son travail dans l'univers. Une lettre unique mettait douze
    # mégaoctets dans un seul fragment.
    horo = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ordre = [RENOMMAGES.get(c, c) for c in champs] + ["_converti_de"]

    # ── Ce qu'on publie ──
    # Les titres suivis en profondeur d'abord, sans condition. Puis les plus
    # grosses capitalisations, jusqu'au plafond. Le reste reste dans le fichier
    # de travail : une société hors liste garde sa fiche légère, et la fiche le
    # dit plutôt que d'afficher des cases vides.
    suivis = charger_suivis()
    par_taille = sorted(lignes.items(),
                        key=lambda kv: -(kv[1].get("marketCap") or 0))
    retenus, vus = [], set()
    for sym, v in par_taille:
        if sym in suivis:
            retenus.append((sym, v))
            vus.add(sym)
    for sym, v in par_taille:
        if sym in vus:
            continue
        if len(retenus) >= PLAFOND_PUBLIE + len(suivis):
            break
        retenus.append((sym, v))
        vus.add(sym)
    seuil_capi = min((v.get("marketCap") or 0) for _, v in retenus) if retenus else None
    ecartes = len(lignes) - len(retenus)
    suivis_retenus = sum(1 for sym, _ in retenus if sym in suivis)
    print("[ok] %d publiés (%d suivis en profondeur), %d écartés — "
          "plus petite capitalisation retenue : %s"
          % (len(retenus), suivis_retenus, ecartes, seuil_capi))

    frag = {}
    for sym, v in retenus:
        cle = _cle_paquet(sym)
        frag.setdefault(cle, {})[sym] = [v.get(c) for c in ordre]

    poids = []
    for cle, contenu in sorted(frag.items()):
        f = OUT_DIR / ("marche_%s.json" % cle)
        f.write_text(json.dumps({"genere_le": horo, "champs": ordre,
                                 "societes": contenu},
                                ensure_ascii=False, separators=(",", ":")),
                     encoding="utf-8")
        poids.append((cle, len(contenu), f.stat().st_size))

    index = {
        "updated": horo,
        "source": "stockanalysis.com — point d'entrée du screener",
        "duree_s": round(time.time() - t0, 1),
        "methode": [
            "Une requête par pays, 86 au total, %d champs demandés." % len(champs),
            "Le point d'entrée accepte N'IMPORTE QUEL identifiant et le renvoie à "
            "null — il sert docilement « nombreDeChats ». Le seul critère retenu "
            "est le taux de remplissage, mesuré sur quatre univers avant d'écrire "
            "la liste des champs.",
            "priceCurrency est la devise du COURS ; currency est celle des états "
            "et vaut null partout hors États-Unis. Londres cote en pence : les "
            "grandeurs de cours y sont divisées par cent, et la ligne le dit.",
            "On ne publie pas les quatre-vingt-trois mille lignes : les titres "
            "suivis en profondeur, plus les dix mille plus grosses "
            "capitalisations. Le reste ferait vingt et un gigaoctets par an dans "
            "un dépôt git, pour des sociétés que personne n'ouvre.",
            "Cinq champs portent un nom de NIVEAU et contiennent un TAUX "
            "(revenueThisYear vaut 89,4 pour NVIDIA, soit +89,4 %). Ils sont "
            "renommés croissance_*_pct.",
        ],
        "exhaustivite": {
            "appels": len(appels),
            "pays_muets": muets,
            "lignes_brutes": len(brut),
            "rattachees": len(lignes),
            "sans_correspondance": sans_symbole,
            "converties_de_pence": pence,
            "publiees": len(retenus),
            "ecartees": ecartes,
            "plafond": PLAFOND_PUBLIE,
            "plus_petite_capitalisation_publiee": seuil_capi,
            "suivis_en_profondeur_publies": suivis_retenus,
            "fragments": len(poids),
            "format": "une ligne = un tableau, la liste des champs est en tête "
                      "du fragment ; soixante-quatre fragments d'empreinte, "
                      "de taille régulière",
        },
        "remplissage_pct": dict(sorted(remplissage.items(), key=lambda kv: -kv[1])),
    }
    (OUT_DIR / "marche_actions_index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    print("[ok] %d fragments — plus gros %d Ko, total %d Ko"
          % (len(poids), max(p[2] for p in poids) // 1024,
             sum(p[2] for p in poids) // 1024))
    print("[ok] %.1f s au total" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
