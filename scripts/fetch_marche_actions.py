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
# Ce qu'on PUBLIE. Le point d'entrée sert quatre-vingt-trois mille lignes ;
# les publier toutes ferait vingt et un gigaoctets par an dans un dépôt git.
#
# Le critère est un SEUIL DE CAPITALISATION EN DOLLARS, pas un rang. « Toutes
# les sociétés au-dessus de deux cents millions de dollars » se comprend et se
# vérifie ; « les dix mille premières » ne dit rien à personne et dépend de qui
# d'autre est coté ce jour-là.
#
# ⚠ EN DOLLARS, et c'est tout le correctif. Trier `marketCap` tel qu'il arrive
# revient à comparer des wons à des euros : Eramet, 1,28 milliard d'euros, était
# écartée alors que le seuil descendait à 139 — les dix mille premières places
# étaient trustées par le yen, le won et le rupiah.
SEUIL_CAPI_USD = 200e6

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
# Mesuré le 27/08/2026 sur cinq univers : 98,7 % des lignes portent un
# domaine exploitable. C'est ce champ qui débloque les logos du monde
# entier — le collecteur de pastilles savait faire, il n'avait pas d'entrée.
SOLIDES += ["website"]

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
    "website": "site_web",
    "revenueThisYear": "croissance_ca_exercice_pct",
    "revenueNextYear": "croissance_ca_suivant_pct",
    "revenue3y": "croissance_ca_3a_pct",
    "revenueGrowth": "croissance_ca_pct",
    "revenueGrowthQ": "croissance_ca_trim_pct",
    "epsGrowth": "croissance_bpa_pct",
    "epsGrowthQ": "croissance_bpa_trim_pct",
    "dividendGrowth": "croissance_dividende_pct",
    "netCashGrowth": "croissance_tresorerie_nette_pct",
    # Ce champ ne mesure PAS une variation de l'objectif dans le temps.
    # Mesuré sur les 3 774 sociétés qui portent les trois champs : dans
    # 98,3 % des cas il vaut exactement l'écart entre l'objectif et le
    # cours. Le garder sous son nom d'origine faisait afficher deux fois le
    # même nombre, dont une fois sous un libellé faux.
    "priceTargetChange": "ecart_objectif_pct",
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




# Les « devises » qui n'en sont pas : des sous-unités, comme le pence de Londres.
# Une place qui cote en sous-unité multiplie toutes ses capitalisations par cent
# ou par mille, et le seul signe est le code à trois lettres.
SOUS_UNITES = {
    "GBX": ("GBP", 100.0),    # pence, Londres
    "ILA": ("ILS", 100.0),    # agorot, Tel-Aviv
    "ZAC": ("ZAR", 100.0),    # cents, Johannesburg
    "KWF": ("KWD", 1000.0),   # fils, Koweït
}


def taux_bce():
    """Les taux de référence quotidiens de la BCE, gratuits et sans clé.

    Trente et une devises que le cache du dépôt n'a pas — zloty, couronne
    tchèque, forint, leu, livre turque, shekel, dollar néo-zélandais, couronne
    islandaise. Sans elles, des pays entiers disparaissent du classement.

    ⚠ La BCE cote PAR EURO. Il faut retourner le taux et le composer avec
    l'euro-dollar, qui est dans la même réponse.
    """
    import re
    import urllib.request
    url = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            xml = r.read().decode("utf-8", "replace")
    except Exception as e:
        print("[warn] taux BCE indisponibles : %s" % e, file=sys.stderr)
        return {}
    par_euro = {}
    for m in re.finditer(r"currency=.([A-Z]{3}).\s+rate=.([0-9.]+).", xml):
        try:
            par_euro[m.group(1)] = float(m.group(2))
        except Exception:
            pass
    eur_usd = par_euro.get("USD")
    if not eur_usd:
        return {}
    # 1 devise = (1 / taux_par_euro) euros = (eur_usd / taux_par_euro) dollars
    out = {"EUR": eur_usd}
    for dev, t in par_euro.items():
        if t > 0:
            out[dev] = eur_usd / t
    return out


def charger_taux():
    """{DEVISE: valeur d'une unité en dollars}, au dernier jour connu.

    Le même cache que celui du collecteur d'univers : vingt-trois devises. Une
    devise absente vaut zéro capitalisation en dollars — donc la société n'est
    pas publiée, ce qui est préférable à un classement au hasard.
    """
    for nom in ("fx_rates_cache.json", "tradfi_fx_cache.json"):
        f = CACHE_DIR / nom
        if not f.exists():
            continue
        try:
            with f.open(encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        out = {"USD": 1.0}
        for dev, par_jour in d.items():
            if isinstance(par_jour, dict) and par_jour:
                out[dev] = par_jour[max(par_jour)]
        # La BCE complète, elle n'écrase pas : le cache du dépôt est daté et
        # aligné sur les séries de cours, la BCE ne sert qu'à boucher les trous.
        bce = taux_bce()
        for dev, t in bce.items():
            out.setdefault(dev, t)
        print("[info] taux : %d du cache, %d ajoutés par la BCE"
              % (len(out) - len(bce) + sum(1 for d2 in bce if d2 in out), len(bce)))
        # Les sous-unités se déduisent de leur devise mère.
        for sous, (mere, div) in SOUS_UNITES.items():
            if mere in out:
                out.setdefault(sous, out[mere] / div)
        return out
    return {"USD": 1.0}


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


# Au-delà de ce seuil, une « croissance » n'est plus une prévision mais un
# rapport sur une base proche de zéro. PT Merdeka Gold Resources ressort à
# 360 268 %, Helixmith à 55 517 % : une société qui passe de mille à deux
# millions de chiffre d'affaires affiche deux cent mille pour cent, et le nombre
# n'apprend rien. Il écrase en revanche toutes les jauges de la page.
PLAFOND_CROISSANCE = 1000.0


def _nettoyer(v, champs_pence, compte):
    """Une ligne brute devient une ligne publiable."""
    out = {}
    # Londres cote en pence, Tel-Aviv en agorot, Johannesburg en cents, Koweït
    # en fils. Quatre places, un seul mécanisme : la cotation est dans une
    # sous-unité de la devise, et tout ce qui est un COURS doit être ramené.
    su = SOUS_UNITES.get(v.get("priceCurrency"))
    pence = su is not None
    for k, val in v.items():
        if val is None:
            continue
        # Londres cote en pence. Diviser par cent et le dire vaut mieux que
        # publier un cours cent fois trop grand à côté d'un bénéfice en livres.
        if pence and k in champs_pence and isinstance(val, (int, float)):
            val = val / su[1]
        cle = RENOMMAGES.get(k, k)
        if cle.startswith("croissance_") and isinstance(val, (int, float)):
            if abs(val) > PLAFOND_CROISSANCE:
                compte[0] += 1
                continue
        out[cle] = val
    if pence:
        out["_converti_de"] = v.get("priceCurrency")
        out["priceCurrency"] = su[0]
    return out


def _publie_precedemment(cache):
    """Combien de sociétés la collecte précédente avait publiées.

    On relit les fragments déjà sur le disque. S'il n'y en a pas, on rend zéro
    et le garde-fou se tait : un premier run n'a rien à quoi se comparer.
    """
    import glob as _g
    n = 0
    for f in _g.glob(str(cache / "marche_[0-9]*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                n += len(json.load(fh).get("societes") or {})
        except Exception:
            pass
    return n


def _refuser_effondrement(avant, maintenant, muets, appels):
    """Refuse d'écrire une collecte manifestement amputée.

    Écrit après avoir vu, sur cette machine, une collecte bridée écraser 37 986
    sociétés par 24 435 — vingt-sept pays muets — et se terminer par « [ok] ».
    Mieux vaut la donnée de la veille, qui est datée, que celle d'aujourd'hui
    amputée d'un tiers, qui est fausse.
    """
    if appels and len(muets) > max(3, appels // 10):
        raise SystemExit(
            "[fatal] %d pays sur %d n'ont pas répondu (%s...). La source bride "
            "ou est en panne : on ne réécrit pas les fragments avec un "
            "échantillon. Relancer plus tard."
            % (len(muets), appels, ", ".join(sorted(muets)[:8])))
    if avant and maintenant < avant * 0.75:
        raise SystemExit(
            "[fatal] %d sociétés collectées contre %d au passage précédent, soit "
            "%.0f %% de moins. Aucune cause normale — jour férié, place fermée — "
            "ne fait perdre un quart de l'univers. On garde les fragments "
            "existants."
            % (maintenant, avant, 100.0 * (1 - maintenant / float(avant))))


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
    croissances_ecartees = [0]
    for k, v in brut.items():
        sym = par_sa.get(k) or par_sa.get(k.split("/")[-1]) or par_sa.get(k.lower())
        if not sym:
            sans_symbole += 1
            continue
        if sym in lignes:
            continue
        lignes[sym] = _nettoyer(v, champs_pence, croissances_ecartees)

    print("[ok] %d lignes rattachées à un symbole, %d sans correspondance"
          % (len(lignes), sans_symbole))

    pence = sum(1 for v in lignes.values() if v.get("_converti_de") == "GBX")
    print("[ok] %d cotations converties de pence en livres" % pence)
    print("[ok] %d croissances écartées pour invraisemblance (au-delà de %d %%)"
          % (croissances_ecartees[0], int(PLAFOND_CROISSANCE)))

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
    ordre = [RENOMMAGES.get(c, c) for c in champs] + ["_converti_de", "marketCapUsd"]

    # ── Ce qu'on publie ──
    # Les titres suivis en profondeur d'abord, sans condition. Puis les plus
    # grosses capitalisations, jusqu'au plafond. Le reste reste dans le fichier
    # de travail : une société hors liste garde sa fiche légère, et la fiche le
    # dit plutôt que d'afficher des cases vides.
    suivis = charger_suivis()
    taux = charger_taux()
    sans_taux = set()

    # Combien de lignes la source donnait dans une AUTRE devise que celle
    # qu'elle annonce, et combien on n'a pas pu vérifier.
    _capi_incoherentes = [0]
    _capi_non_verifiables = [0]

    def capi_usd(v):
        """La capitalisation en dollars — recalculée quand on peut la vérifier.

        `priceCurrency` est la devise du COURS. `marketCap`, lui, est parfois
        dans la devise de PUBLICATION de la société. Mesuré sur Grupo Argos,
        coté à Santiago : cours 6,792 USD, capitalisation 3,85 × 10¹² en pesos
        colombiens. Multiplier par le taux du dollar publiait un holding
        colombien à 3 850 milliards de dollars.

        Le contrôle est interne à la ligne : `marketCap` doit valoir
        `price × sharesOut`, les deux étant dans la devise du cours. Mesuré sur
        les 20 031 cotations principales — `sharesOut` en couvre 93,5 %, et
        98,6 % des vérifiables concordent. On recalcule donc à partir du cours
        et du nombre d'actions, ce qui est cohérent PAR CONSTRUCTION : les
        justes ne bougent pas, les 258 fausses deviennent justes.
        """
        dev = (v.get("priceCurrency") or "USD").upper()
        r = taux.get(dev)
        if r is None:
            sans_taux.add(dev)
            return 0.0

        mc = v.get("marketCap")
        px, sh = v.get("price"), v.get("sharesOut")
        verifiable = (isinstance(px, (int, float)) and isinstance(sh, (int, float))
                      and px > 0 and sh > 0)
        if verifiable:
            calculee = px * sh
            if isinstance(mc, (int, float)) and mc > 0:
                rapport = mc / calculee
                if rapport < 0.8 or rapport > 1.25:
                    _capi_incoherentes[0] += 1
            return calculee * r

        _capi_non_verifiables[0] += 1
        if not isinstance(mc, (int, float)):
            return 0.0
        return mc * r

    for sym, v in lignes.items():
        v["marketCapUsd"] = round(capi_usd(v))

    retenus = [(sym, v) for sym, v in lignes.items()
               if sym in suivis or v["marketCapUsd"] >= SEUIL_CAPI_USD]
    retenus.sort(key=lambda kv: -kv[1]["marketCapUsd"])
    seuil_capi = min((v["marketCapUsd"] for _, v in retenus), default=None)
    ecartes = len(lignes) - len(retenus)
    suivis_retenus = sum(1 for sym, _ in retenus if sym in suivis)
    print("[ok] %d publiés (%d suivis en profondeur), %d écartés — seuil %d M$, "
          "plus petite capitalisation retenue %s"
          % (len(retenus), suivis_retenus, ecartes,
             int(SEUIL_CAPI_USD / 1e6), seuil_capi))
    print("[ok] capitalisations : %d recalculées depuis cours x actions, dont %d "
          "que la source donnait dans une autre devise ; %d non vérifiables "
          "(pas de nombre d'actions)"
          % (len(lignes) - _capi_non_verifiables[0], _capi_incoherentes[0],
             _capi_non_verifiables[0]))
    if sans_taux:
        print("[ok] devises sans taux, sociétés non publiées faute de comparaison : %s"
              % ", ".join(sorted(x for x in sans_taux if x)))

    # ── On ne réécrit pas les fragments avec un échantillon ──
    _refuser_effondrement(_publie_precedemment(CACHE_DIR), len(retenus),
                          muets, len(appels))

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
            "suivis en profondeur, plus toute société pesant au moins deux cents "
            "millions de DOLLARS. Le seuil est en dollars et non en devise "
            "locale : trier marketCap tel qu'il arrive revient à comparer des "
            "wons à des euros, et écartait Eramet, 1,28 milliard d'euros, quand "
            "le seuil brut descendait à 139.",
            "Au-delà de mille pour cent, une croissance n'est plus une "
            "prévision mais un rapport sur une base proche de zéro : elle est "
            "écartée, et le compte des écartées est publié.",
            "priceTargetChange ne mesure PAS une variation dans le temps : dans "
            "98,3 % des cas il vaut l'écart entre l'objectif et le cours. "
            "Renommé ecart_objectif_pct.",
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
            "croissances_ecartees": croissances_ecartees[0],
            "plafond_croissance_pct": PLAFOND_CROISSANCE,
            "publiees": len(retenus),
            "ecartees": ecartes,
            "seuil_capitalisation_usd": SEUIL_CAPI_USD,
            "plus_petite_capitalisation_usd_publiee": seuil_capi,
            "devises_sans_taux": sorted(x for x in sans_taux if x),
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
