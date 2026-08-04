#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_atlas_quarterly.py — PIB TRIMESTRIEL de l'Atlas Économique.

POURQUOI (demande MA 2026-08-02) : l'Atlas ne connaissait du PIB que le millésime
ANNUEL (Banque mondiale, dernier = 2025) plus la prévision FMI. Le dernier chiffre
« réel » avait donc jusqu'à 18 mois. Les comptes nationaux TRIMESTRIELS ramènent ce
délai à ~5 semaines (T2 2026 publié fin juillet 2026).

CE QU'ON PUBLIE — deux taux de croissance du PIB en VOLUME (corrigé de l'inflation) :
  • `qoq` — trimestre sur trimestre, série corrigée des variations saisonnières.
  • `yoy` — glissement annuel (même trimestre de l'année précédente).

CE QU'ON NE PUBLIE PAS, ET POURQUOI : aucun NIVEAU de PIB trimestriel en dollars
courants. Vérifié le 2026-08-02 : l'OCDE ne diffuse le niveau trimestriel qu'en USD
*PPA* (incomparable au PIB nominal de l'Atlas) et le FMI DÉCLARE la série
B1GQ.V.NSA.USD mais ne la remplit d'aucune observation. Publier un montant en
dollars supposerait de convertir soi-même au taux de change du trimestre : ce serait
une estimation maison, pas une donnée officielle. On s'en abstient (cf. doctrine
« données réelles, le vide se surface »).

TROIS SOURCES, choisies par FRAÎCHEUR puis par priorité :
  1. OCDE — Comptes nationaux trimestriels, dataflow SDMX
     `DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD`. 46 pays + 7 agrégats, taux DÉJÀ
     calculés par l'OCDE (transformations G1 = T/T, GY = sur un an), historique
     depuis 1960 pour les plus anciens. C'est la source de référence.
  2. Eurostat — `namq_10_gdp`, unités CLV_PCH_PRE (T/T) et CLV_PCH_SM (sur un an),
     s_adj=SCA. Ajoute les européens hors OCDE (Bulgarie, Roumanie, Croatie, Chypre,
     Malte, Balkans, Moldavie, Kosovo).
  3. FMI — QNEA (National Economic Accounts, Quarterly). Volumes trimestriels bruts :
     on DÉRIVE les taux. Ajoute ~36 pays hors OCDE/UE (Russie, Singapour, Hong Kong,
     Malaisie, Thaïlande, Philippines, Pakistan, Pérou, Ukraine, Kenya…).
  + FMI QGDP_WCA agrégat `G001` = Monde, pour la vue « Monde & continents ».

RÈGLE DE RIGUEUR N°1 — on ne désaisonnalise JAMAIS soi-même. Chez le FMI, le T/T
n'est dérivé QUE d'une série déjà corrigée des variations saisonnières (S_ADJUSTMENT
= SA) ; le glissement annuel, lui, peut venir d'une série brute (NSA) puisque
comparer un trimestre au même trimestre neutralise la saisonnalité. Un pays qui n'a
que du brut aura donc `yoy` et PAS `qoq` — c'est voulu, pas un trou.

RÈGLE DE RIGUEUR N°2 — la source retenue par pays est celle dont le DERNIER trimestre
est le plus récent (départage OCDE > Eurostat > FMI). Sans ça la Russie serait figée
au T3 2021 (l'OCDE a cessé de la publier) alors que le FMI la suit jusqu'au T1 2026.

Produit `atlas_quarterly_cache.{json,js}` (window.__ATLAS_QUARTERLY__), chargé en
LAZY par la page (~200 Ko). Copies : Desktop + public/ + ~/Library/Caches/.
Auto : launchd `scf.atlasquarterly`.

Options : --dry-run (n'écrit rien, affiche le tableau de contrôle).
"""
import os
import sys, os, re, json, time

try:
    from curl_cffi import requests as cr
    def _get(url, timeout=180):
        return cr.get(url, impersonate="chrome120", timeout=timeout)
except Exception:                                        # repli si curl_cffi absent
    import requests as _rq
    def _get(url, timeout=180):
        return _rq.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "Desktop", "Site_Crypto_Finance")
CACHES = [
    os.path.join(REPO, "atlas_quarterly_cache"),
    os.path.join(REPO, "public", "atlas_quarterly_cache"),
    os.path.join(HOME, "Library", "Caches", "site_crypto_finance", "atlas_quarterly_cache"),
]

OECD_URL = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.NAD,"
            "DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD,1.1/"
            "Q.Y..S1..B1GQ......G1+GY.?format=csvfile")
ES_URL = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
          "namq_10_gdp?format=JSON&lang=EN&na_item=B1GQ&unit={unit}&s_adj=SCA")
IMF_QNEA = ("https://api.imf.org/external/sdmx/2.1/data/IMF.STA,QNEA/"
            ".B1GQ.Q.{adj}.XDC.Q?startPeriod=1990-Q1")
IMF_WORLD = ("https://api.imf.org/external/sdmx/2.1/data/IMF.STA,QGDP_WCA/"
             "G001.B1GQ_S1_Q.{tr}.Q")

# Agrégats OCDE : gardés à part (`groups`), ils servent de repère sur les graphes —
# jamais mélangés au classement des pays.
OECD_GROUPS = {"EA": "Zone euro", "EU": "Union européenne", "G7": "G7", "G20": "G20",
               "OECD": "OCDE", "OECDE": "OCDE — Europe", "USMCA": "USMCA"}

# Eurostat travaille en ISO2 (avec deux exceptions historiques : EL = Grèce, UK = R-U).
ES_ISO = {"AL": "ALB", "AT": "AUT", "BA": "BIH", "BE": "BEL", "BG": "BGR", "CH": "CHE",
          "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DK": "DNK", "EE": "EST", "EL": "GRC",
          "ES": "ESP", "FI": "FIN", "FR": "FRA", "HR": "HRV", "HU": "HUN", "IE": "IRL",
          "IS": "ISL", "IT": "ITA", "LT": "LTU", "LU": "LUX", "LV": "LVA", "MD": "MDA",
          "ME": "MNE", "MK": "MKD", "MT": "MLT", "NL": "NLD", "NO": "NOR", "PL": "POL",
          "PT": "PRT", "RO": "ROU", "RS": "SRB", "SE": "SWE", "SI": "SVN", "SK": "SVK",
          "TR": "TUR", "UK": "GBR", "XK": "XKX"}
# Le FMI code la Cisjordanie/Gaza WBG et le Kosovo KOS ; l'Atlas suit l'ISO (PSE, XKX).
# Vérifié le 2026-08-02 : ce sont les deux SEULS codes des trois sources qui divergent
# du méta de l'Atlas — tous les autres sont des ISO3 stricts.
IMF_ALIAS = {"WBG": "PSE", "KOS": "XKX"}

WEO_ALIAS = {"UVK": "XKX", "WBG": "PSE"}
WEO_URL = "https://www.imf.org/external/datamapper/api/v1/NGDP_RPCH"
# Écart, en points de croissance annuelle, au-delà duquel une série dérivée n'est plus
# une économie qui bouge mais une RUPTURE de série (rebasage, changement d'unité,
# redénomination). Calibré sur les cas réels du 2026-08-02 : les vrais écarts de
# millésime entre comptes trimestriels et WEO restent < 10 pts, les ruptures font
# +43 (Russie 2014), +64 (Rwanda 2017), +5 699 (Salvador 2005).
BREAK_PTS = 10.0
# Garde-fou de QUEUE : les trimestres trop récents pour être recoupés à une année WEO
# complète. Aucun PIB trimestriel réel ne bouge autant hors rupture (le pire trimestre
# du COVID, Macao T2 2020, est couvert par le recoupement annuel, pas par cette borne).
TAIL_MAX_QOQ, TAIL_MAX_YOY = 25.0, 30.0

SRC_PRIO = {"OECD": 3, "EUROSTAT": 2, "IMF": 1}
SRC_LAB = {
    "OECD": "OCDE — Comptes nationaux trimestriels",
    "EUROSTAT": "Eurostat — comptes nationaux trimestriels (namq_10_gdp)",
    "IMF": "FMI — National Economic Accounts, Quarterly (QNEA)",
    "IMF_WCA": "FMI — Quarterly GDP, World and Country Aggregates",
}

QRE = re.compile(r"^(\d{4})-?Q([1-4])$")


def qidx(period):
    """« 2026-Q2 » → index entier de trimestre (année*4 + n° − 1). Trie et se soustrait."""
    m = QRE.match((period or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 4 + (int(m.group(2)) - 1)


def qlabel(qi):
    return "T%d %d" % (qi % 4 + 1, qi // 4)


def pack(d):
    """{index: valeur} → série compacte {s: premier index, v: [valeurs, trous = null]}.

    ⚠ 3 DÉCIMALES, PAS 2. Piège vécu le 2026-08-02 : l'OCDE publie −0,146 % pour la
    France au T1 2026 ; arrondi à 2 décimales cela donne −0,15, que l'affichage à une
    décimale transforme ensuite en **−0,2 %** — alors qu'Eurostat et l'INSEE publient
    **−0,1 %**. Un double arrondi ne doit jamais s'intercaler entre la source et
    l'écran : on garde assez de précision pour que l'arrondi final tombe juste."""
    if not d:
        return None
    ks = sorted(d)
    s, e = ks[0], ks[-1]
    v = [None] * (e - s + 1)
    for k in ks:
        x = d[k]
        if x is not None:
            v[k - s] = round(float(x), 3)
    if not any(x is not None for x in v):
        return None
    return {"s": s, "v": v}


def growth_from_levels(levels, lag):
    """Taux de croissance dérivé de NIVEAUX en volume. `lag` = 1 (T/T) ou 4 (sur un an).
    Ne calcule que si le trimestre de référence existe VRAIMENT (pas d'interpolation
    à travers un trou : un taux inventé sur une série discontinue est un faux chiffre).
    Niveaux nuls ou négatifs ignorés — un PIB en volume ne l'est jamais, et une valeur
    à zéro produisait un « −100 % » fantôme (cas Jamaïque vu le 2026-08-02)."""
    out = {}
    for k, v in levels.items():
        p = levels.get(k - lag)
        if v is None or p is None or p <= 0 or v <= 0:
            continue
        out[k] = (float(v) / float(p) - 1.0) * 100.0
    return out


def annual_from_levels(levels):
    """Somme des 4 trimestres, années COMPLÈTES seulement (une année partielle
    comparée à une année pleine donnerait un faux effondrement)."""
    ys = {}
    for qi, v in levels.items():
        if v is not None:
            ys.setdefault(qi // 4, {})[qi % 4] = v
    return {y: sum(q.values()) for y, q in ys.items() if len(q) == 4}


def trim_at_break(levels, weo_c, a3, log):
    """Coupe une série de niveaux à sa DERNIÈRE rupture, en la recoupant à la
    croissance annuelle officielle du FMI (WEO NGDP_RPCH).

    C'est le garde-fou central du tiers FMI : là où l'OCDE et Eurostat publient des
    taux déjà calculés, ici on les DÉRIVE de niveaux bruts — et un rebasage ou une
    redénomination y ressemble à une croissance de 5 700 % (Salvador 2005). Plutôt
    que d'inventer un seuil sur l'amplitude, on compare ce que la série IMPLIQUE
    pour chaque année civile à ce que le FMI PUBLIE pour cette même année, et on ne
    garde que le segment postérieur au dernier désaccord majeur.

    Retourne (niveaux conservés, première et dernière année recoupées) — les deux
    bornes délimitent la fenêtre où la série est PROUVÉE juste."""
    full = annual_from_levels(levels)
    cut, first_ok, last_ok = None, None, None
    for y in sorted(full):
        if (y - 1) not in full:
            continue
        ref = weo_c.get(str(y))
        try:
            ref = float(ref)
        except (TypeError, ValueError):
            continue
        implied = (full[y] / full[y - 1] - 1.0) * 100.0
        if abs(implied - ref) > BREAK_PTS:
            cut = y
            first_ok = None                       # tout ce qui précède est invalidé
            log.append("%s : rupture en %d (série implique %+.1f %%, FMI publie %+.1f %%)"
                       % (a3, y, implied, ref))
        else:
            last_ok = y
            if first_ok is None:
                first_ok = y
    if cut is not None:
        levels = {qi: v for qi, v in levels.items() if qi // 4 > cut}
        if last_ok is not None and last_ok <= cut:
            first_ok = last_ok = None
    return levels, first_ok, last_ok


def drop_unvalidated_outliers(series, first_ok, last_ok, limit, a3, kind, log):
    """Applique une borne de vraisemblance AUX SEULS points hors de la fenêtre recoupée
    au WEO — des deux côtés.

    En queue, ce sont les ruptures trop récentes pour qu'une année complète les révèle
    (Pakistan, +309 % en glissement annuel au T1 2026). En tête, c'est le trimestre
    d'ANCRAGE : quand une série débute au milieu d'une année, sa première observation
    n'est jamais recoupable et peut être partielle — elle fabriquait alors un
    « +75,9 % » pour l'Égypte, dont le FMI publie 3,3 % sur l'année.

    Dans la fenêtre recoupée, on ne touche à RIEN, même à −37 % (Ukraine 2022) ou
    −54 % (Macao 2020) : ces trimestres sont réels et confirmés par le WEO annuel."""
    if not series:
        return series
    out = {}
    for qi, v in series.items():
        y = qi // 4
        outside = (first_ok is None or y < first_ok) or (last_ok is None or y > last_ok)
        if outside and abs(v) > limit:
            log.append("%s : %s T%d %d = %+.1f %% écarté (hors fenêtre recoupée)"
                       % (a3, kind, qi % 4 + 1, y, v))
            continue
        out[qi] = v
    return out


def wait_for_network(max_wait=240):
    """ANTI WAKE-RACE : launchd rejoue le job au réveil du Mac, souvent AVANT que le WiFi
    soit remonté → toutes les sources échouent et le cache resterait figé jusqu'au run
    suivant. On sonde, on patiente, et si le réseau ne revient pas on sort PROPREMENT
    sans rien écrire (le watchdog horaire relancera). Cf. V17 de l'Atlas."""
    probes = ("https://sdmx.oecd.org/public/rest/dataflow/OECD.SDD.NAD",
              "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/namq_10_gdp?format=JSON&geo=FR&na_item=B1GQ&unit=CLV_PCH_PRE&s_adj=SCA&lastTimePeriod=1")
    waited, delay = 0, 3
    while True:
        for u in probes:
            try:
                r = _get(u, timeout=10)
                if r.status_code and r.status_code < 500:
                    if waited:
                        sys.stderr.write("[net] réseau prêt après %ds\n" % waited)
                    return True
            except Exception:
                pass
        if waited >= max_wait:
            sys.stderr.write("[net] toujours hors-ligne après %ds — run abandonné sans écriture\n" % waited)
            return False
        time.sleep(delay)
        waited += delay
        delay = min(20, delay * 2)


# ─────────────────────────── 1. OCDE ───────────────────────────
def fetch_oecd():
    """46 pays + 7 agrégats, taux calculés par l'OCDE, historique long (1960 pour FRA)."""
    r = _get(OECD_URL, timeout=300)
    r.raise_for_status()
    import csv, io
    rows = csv.DictReader(io.StringIO(r.text))
    out = {}
    n = 0
    for row in rows:
        if row.get("TRANSACTION") != "B1GQ":
            continue
        val = (row.get("OBS_VALUE") or "").strip()
        if not val:
            continue
        qi = qidx(row.get("TIME_PERIOD"))
        if qi is None:
            continue
        tf = row.get("TRANSFORMATION")
        key = {"G1": "qoq", "GY": "yoy"}.get(tf)
        if not key:
            continue
        area = row.get("REF_AREA")
        try:
            fv = float(val)
        except ValueError:
            continue
        out.setdefault(area, {"qoq": {}, "yoy": {}})[key][qi] = fv
        n += 1
    print("   OCDE      : %d entités, %d observations" % (len(out), n))
    return out


# ─────────────────────────── 2. Eurostat ───────────────────────────
def _es_one(unit):
    r = _get(ES_URL.format(unit=unit), timeout=180)
    r.raise_for_status()
    j = r.json()
    geo = j["dimension"]["geo"]["category"]["index"]
    tim = j["dimension"]["time"]["category"]["index"]
    inv_t = {v: k for k, v in tim.items()}
    nt = len(tim)
    vals = j.get("value") or {}
    out = {}
    for gcode, gi in geo.items():
        a3 = ES_ISO.get(gcode)
        if not a3:                       # EA/EA12/EA19/EA20/EA21/EU27_2020 : agrégats
            continue
        d = {}
        for ti in range(nt):
            v = vals.get(str(gi * nt + ti))
            if v is None:
                continue
            qi = qidx(inv_t[ti])
            if qi is not None:
                d[qi] = v
        if d:
            out[a3] = d
    return out, j.get("updated")


def fetch_eurostat():
    qoq, up = _es_one("CLV_PCH_PRE")
    yoy, _ = _es_one("CLV_PCH_SM")
    out = {}
    for a3 in set(qoq) | set(yoy):
        out[a3] = {"qoq": qoq.get(a3, {}), "yoy": yoy.get(a3, {})}
    print("   Eurostat  : %d pays (maj source %s)" % (len(out), up))
    return out, up


# ─────────────────────────── 3. FMI QNEA ───────────────────────────
SERIES_RE = re.compile(r"<Series\b([^>]*?)(/>|>(.*?)</Series>)", re.S)
OBS_RE = re.compile(r'TIME_PERIOD="([^"]+)"\s+OBS_VALUE="([^"]*)"')


def _imf_levels(adj):
    """Niveaux du PIB en VOLUME, monnaie nationale. adj = 'SA' (désaisonnalisé) ou 'NSA'."""
    r = _get(IMF_QNEA.format(adj=adj), timeout=240)
    r.raise_for_status()
    out = {}
    for m in SERIES_RE.finditer(r.text):
        hdr, body = m.group(1), (m.group(3) or "")
        cm = re.search(r'COUNTRY="([^"]*)"', hdr)
        if not cm:
            continue
        a3 = IMF_ALIAS.get(cm.group(1), cm.group(1))
        d = {}
        for p, v in OBS_RE.findall(body):
            if not v:
                continue
            qi = qidx(p)
            if qi is None:
                continue
            try:
                d[qi] = float(v)
            except ValueError:
                pass
        if d:
            out[a3] = d
    return out


def fetch_weo():
    """Croissance annuelle officielle du FMI (WEO NGDP_RPCH) — sert de VÉRITÉ TERRAIN
    pour valider les séries dérivées. Un seul appel (le DataMapper ignore le filtre pays
    et renvoie les 229 économies)."""
    try:
        r = _get(WEO_URL, timeout=90)
        r.raise_for_status()
        raw = (r.json().get("values") or {}).get("NGDP_RPCH") or {}
        # Le WEO code le Kosovo UVK là où QNEA dit KOS et l'Atlas XKX : on aligne tout
        # sur l'ISO, sans quoi le Kosovo serait « sans recoupement » et écarté à tort.
        return {WEO_ALIAS.get(k, k): v for k, v in raw.items()}
    except Exception as e:
        sys.stderr.write("[WARN] WEO indisponible (%s) — les séries dérivées ne seront "
                         "PAS recoupées ; le tiers FMI est écarté par prudence.\n" % e)
        return None


def fetch_imf(weo):
    sa = _imf_levels("SA")
    nsa = _imf_levels("NSA")
    out, log = {}, []
    n_trim = 0
    for a3 in sorted(set(sa) | set(nsa)):
        weo_c = (weo or {}).get(a3)
        if not weo_c:
            # Sans recoupement possible, on ne publie pas une série dérivée « à l'aveugle ».
            log.append("%s : aucune série WEO pour recouper — pays écarté" % a3)
            continue
        lv_sa, f_sa, l_sa = trim_at_break(dict(sa.get(a3) or {}), weo_c, a3, log)
        lv_ns, f_ns, l_ns = trim_at_break(dict(nsa.get(a3) or {}), weo_c, a3, log)
        before = len(sa.get(a3) or {}) + len(nsa.get(a3) or {})
        if len(lv_sa) + len(lv_ns) < before:
            n_trim += 1
        e = {"qoq": {}, "yoy": {}}
        # T/T UNIQUEMENT depuis le désaisonnalisé (cf. règle de rigueur n°1).
        if lv_sa:
            e["qoq"] = drop_unvalidated_outliers(growth_from_levels(lv_sa, 1), f_sa, l_sa,
                                                 TAIL_MAX_QOQ, a3, "T/T", log)
        # Glissement annuel : le brut suffit (même trimestre → saisonnalité neutralisée).
        base, f_b, l_b = (lv_ns, f_ns, l_ns) if lv_ns else (lv_sa, f_sa, l_sa)
        if base:
            e["yoy"] = drop_unvalidated_outliers(growth_from_levels(base, 4), f_b, l_b,
                                                 TAIL_MAX_YOY, a3, "sur 1 an", log)
        if e["qoq"] or e["yoy"]:
            out[a3] = e
    n_sa = sum(1 for v in out.values() if v["qoq"])
    print("   FMI QNEA  : %d pays (%d avec série désaisonnalisée → T/T) · "
          "%d série(s) coupée(s) à une rupture" % (len(out), n_sa, n_trim))
    for line in log:
        sys.stderr.write("   [QNEA] %s\n" % line)
    return out


def fetch_world():
    """Monde (agrégat FMI G001). IX = indice de volume → glissement annuel dérivé ;
    POP_PCH_PT = variation d'une période à l'autre, déjà publiée."""
    try:
        r = _get(IMF_WORLD.format(tr="POP_PCH_PT"), timeout=120)
        r.raise_for_status()
        qoq = {}
        for p, v in OBS_RE.findall(r.text):
            qi = qidx(p)
            if qi is not None and v:
                qoq[qi] = float(v)
        r2 = _get(IMF_WORLD.format(tr="IX"), timeout=120)
        r2.raise_for_status()
        idx = {}
        for p, v in OBS_RE.findall(r2.text):
            qi = qidx(p)
            if qi is not None and v:
                idx[qi] = float(v)
        yoy = growth_from_levels(idx, 4)
        if qoq or yoy:
            print("   FMI Monde : %d trimestres T/T, %d en glissement annuel" % (len(qoq), len(yoy)))
            return {"qoq": qoq, "yoy": yoy}
    except Exception as e:
        sys.stderr.write("[WARN] agrégat Monde indisponible : %s\n" % e)
    return None


# ─────────────────────────── assemblage ───────────────────────────
def last_q(e):
    ks = [k for k in list(e.get("qoq") or {}) + list(e.get("yoy") or {})]
    return max(ks) if ks else None


def build_entry(e, src):
    qoq, yoy = e.get("qoq") or {}, e.get("yoy") or {}
    o = {"src": src}
    pq, py = pack(qoq), pack(yoy)
    if pq:
        o["qoq"] = pq
    if py:
        o["yoy"] = py
    # Dernières valeurs + LEUR trimestre : c'est ce que lisent la carte et le classement.
    if qoq:
        k = max(qoq)
        o["lq"], o["lq_q"] = round(qoq[k], 3), k
    if yoy:
        k = max(yoy)
        o["ly"], o["ly_q"] = round(yoy[k], 3), k
    lq = last_q(e)
    if lq is not None:
        o["last"] = lq
    return o


def assemble(oecd, euro, imf, world, es_updated):
    countries, groups = {}, {}
    per_src = {"OECD": 0, "EUROSTAT": 0, "IMF": 0}
    cands = {}
    for src, data in (("OECD", oecd), ("EUROSTAT", euro), ("IMF", imf)):
        for a3, e in data.items():
            if src == "OECD" and a3 in OECD_GROUPS:
                groups[a3] = build_entry(e, "OECD")
                groups[a3]["name"] = OECD_GROUPS[a3]
                continue
            if len(a3) != 3:
                continue
            cands.setdefault(a3, []).append((src, e))

    for a3, lst in cands.items():
        # FRAÎCHEUR d'abord, priorité de source ensuite (cf. règle de rigueur n°2 : la
        # Russie doit venir du FMI, l'OCDE l'ayant gelée au T3 2021).
        best = max(lst, key=lambda t: ((last_q(t[1]) or -1), SRC_PRIO[t[0]]))
        src, e = best
        entry = build_entry(e, src)
        if "qoq" not in entry and "yoy" not in entry:
            continue
        # Si la source retenue n'a PAS de T/T (FMI sans désaisonnalisé) mais qu'une autre
        # en a un d'une fraîcheur comparable, on le récupère plutôt que d'afficher un trou.
        if "qoq" not in entry:
            for s2, e2 in lst:
                if s2 == src or not (e2.get("qoq")):
                    continue
                if (last_q(e2) or -1) >= (entry.get("last", -1) - 1):
                    pq = pack(e2["qoq"])
                    if pq:
                        entry["qoq"] = pq
                        k = max(e2["qoq"])
                        entry["lq"], entry["lq_q"] = round(e2["qoq"][k], 3), k
                        entry["src_qoq"] = s2
                    break
        countries[a3] = entry
        per_src[src] += 1

    if world:
        countries["WLD"] = build_entry(world, "IMF_WCA")

    lastq = max([v["last"] for v in countries.values() if "last" in v] or [0])
    meta = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_quarter": qlabel(lastq) if lastq else None,
        "last_quarter_idx": lastq or None,
        "n_countries": len([a for a in countries if a != "WLD"]),
        "per_source": per_src,
        "src_lab": SRC_LAB,
        "eurostat_updated": es_updated,
        "note_no_level": ("Aucun niveau de PIB en dollars courants n'est publié en trimestriel "
                          "par ces sources (l'OCDE ne diffuse que de la PPA, le FMI laisse la "
                          "série USD vide) — l'Atlas n'affiche donc que des taux de croissance."),
    }
    return {"meta": meta, "countries": countries, "groups": groups}


# ─────────────────────────── garde-fous ───────────────────────────
def audit(out):
    """Recoupements sur valeurs RÉELLES connues. Un écart ici = source qui a changé de
    forme (unité, signe, dimension) — on préfère abandonner le run que publier faux."""
    C = out["countries"]
    problems = []

    # France T1 2026 = −0,1 % et T2 2026 = +0,2 % (OCDE et Eurostat concordants, 02/08/2026).
    fr = C.get("FRA") or {}
    if not fr.get("qoq"):
        problems.append("FRA sans série T/T")
    else:
        s = fr["qoq"]
        got = {s["s"] + i: s["v"][i] for i in range(len(s["v"])) if s["v"][i] is not None}
        for per, exp in (("2026-Q1", -0.1), ("2026-Q2", 0.2)):
            qi = qidx(per)
            if qi in got and abs(got[qi] - exp) > 0.35:
                problems.append("FRA %s = %.2f (attendu ≈ %.1f)" % (per, got[qi], exp))

    # Tripwire d'unité. La borne est VOLONTAIREMENT haute : des trimestres réels
    # dépassent largement ±35 % (Ukraine −37 % en 2022, Macao −54 % en 2020 puis son
    # rebond, Maldives +37 % en 2021 — tous confirmés par le WEO annuel). Au-delà de
    # ±80 %, en revanche, ce n'est plus une économie, c'est un changement d'unité.
    for a3, e in C.items():
        for k in ("qoq", "yoy"):
            s = e.get(k)
            if not s:
                continue
            for v in s["v"]:
                if v is not None and abs(v) > 80:
                    problems.append("%s %s hors plage : %.1f %%" % (a3, k, v))
                    break

    n = len([a for a in C if a != "WLD"])
    if n < 60:
        problems.append("seulement %d pays (attendu ≥ 60)" % n)
    return problems


def write_out(out, dry=False):
    C = out["countries"]
    MIN = 60
    n = len([a for a in C if a != "WLD"])
    if n < MIN:
        sys.stderr.write("[ABORT] %d pays (< %d) — sources probablement en panne. "
                         "Cache existant CONSERVÉ, aucune écriture.\n" % (n, MIN))
        sys.exit(1)
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    if dry:
        print("[dry-run] %d Ko — rien écrit" % (len(blob) // 1024))
        return
    hdr = "/* atlas_quarterly_cache.js — PIB trimestriel (OCDE / Eurostat / FMI) */"
    for base in CACHES:
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            for ext, txt in ((".json", blob),
                             (".js", hdr + "\nwindow.__ATLAS_QUARTERLY__ = " + blob + ";\n")):
                tmp = base + ext + ".tmp"          # écriture ATOMIQUE : jamais de JSON tronqué
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(txt)
                os.replace(tmp, base + ext)
            print("écrit %s.json (%d Ko)" % (base, len(blob) // 1024))
        except Exception as e:
            sys.stderr.write("[WARN] écriture %s : %s\n" % (base, e))


def main():
    dry = "--dry-run" in sys.argv[1:]
    if not wait_for_network():
        sys.exit(0)
    t0 = time.time()
    print("PIB trimestriel — collecte")
    oecd = euro = imf = {}
    es_up = None
    ok = 0
    try:
        oecd = fetch_oecd(); ok += 1
    except Exception as e:
        sys.stderr.write("[WARN] OCDE : %s\n" % e)
    try:
        euro, es_up = fetch_eurostat(); ok += 1
    except Exception as e:
        sys.stderr.write("[WARN] Eurostat : %s\n" % e)
    weo = fetch_weo()
    try:
        if weo:
            imf = fetch_imf(weo); ok += 1
        else:
            sys.stderr.write("[WARN] tiers FMI sauté (pas de référence WEO pour valider).\n")
    except Exception as e:
        sys.stderr.write("[WARN] FMI QNEA : %s\n" % e)
    if ok == 0:
        sys.stderr.write("[ABORT] aucune source jointe — cache conservé.\n")
        sys.exit(1)
    world = fetch_world()

    out = assemble(oecd, euro, imf, world, es_up)
    m = out["meta"]
    print("\nTABLEAU DE CONTRÔLE")
    print("   pays classés      : %d" % m["n_countries"])
    print("   dernier trimestre : %s" % m["last_quarter"])
    print("   par source        : %s" % m["per_source"])
    nq = sum(1 for a, e in out["countries"].items() if e.get("qoq"))
    ny = sum(1 for a, e in out["countries"].items() if e.get("yoy"))
    print("   séries T/T        : %d · glissement annuel : %d" % (nq, ny))

    problems = audit(out)
    if problems:
        sys.stderr.write("[AUDIT] %d anomalie(s) :\n" % len(problems))
        for p in problems[:20]:
            sys.stderr.write("   - %s\n" % p)
        if any("FRA" in p or "pays (attendu" in p for p in problems):
            sys.stderr.write("[ABORT] garde-fou de référence en échec — rien écrit.\n")
            sys.exit(1)
    else:
        print("   audit             : OK")

    write_out(out, dry)
    print("terminé en %ds" % int(time.time() - t0))


if __name__ == "__main__":
    main()
