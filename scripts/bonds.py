#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Module « détail obligataire » de fetch_tech_debt.py.

Trois couches, de la plus robuste à la plus fine — chacune dégradant proprement :

  1. ÉCHÉANCIER (structuré, fiable) : faits XBRL
     `LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths` … `AfterYearFive`.
     Aucun parsing, disponible pour 13 sociétés sur 14 vérifiées.

  2. DÉTAIL PAR TRANCHE (parsing, meilleur effort) : la note « Debt / Borrowings »
     du 10-K liste chaque obligation avec son montant, son taux nominal et sa date
     d'échéance. Permet le COUPON MOYEN PONDÉRÉ réel et le coupon par millésime.
     Si le parsing échoue pour une société, elle garde simplement l'échéancier.

  3. ÉMISSIONS RÉCENTES (fraîcheur) : prospectus `424B` déposés à la SEC dans les
     jours suivant chaque émission — comble le trou entre deux publications
     trimestrielles (jusqu'à quatre mois).

PIÈGE DEVISE : Oracle, Alphabet et Broadcom émettent en euros. Le montant nominal
d'une tranche EUR n'est PAS comparable à une tranche USD. Ces tranches sont
détectées (symbole €) et comptées à part, jamais additionnées naïvement.
"""
import os
import json, re, time, gzip, html as H
import datetime as dt
from urllib.request import Request, urlopen

UA = os.environ.get("SCF_CONTACT_UA", "CapitalAntifragile research")


def _get(url, txt=False, retry=3, timeout=60):
    for i in range(retry):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
            with urlopen(req, timeout=timeout) as r:
                d = r.read()
                if r.info().get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return d.decode("utf-8", "replace") if txt else json.loads(d)
        except Exception:
            if i == retry - 1:
                return None
            time.sleep(1.2 * (i + 1))
    return None


def _clean(c):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", H.unescape(c))).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 1. ÉCHÉANCIER — faits XBRL, aucune interprétation
# ─────────────────────────────────────────────────────────────────────────────
LADDER_TAGS = [
    ("y1", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths"),
    ("y2", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo"),
    ("y3", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree"),
    ("y4", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour"),
    ("y5", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive"),
    ("after", "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive"),
]


def ladder(gaap):
    """Échéancier du dernier bilan publié. Toutes les tranches doivent partager la
    MÊME date : un échéancier composé de bouts d'exercices différents ne veut rien
    dire. On retient donc la date la plus récente où au moins `y1` existe, et on ne
    complète qu'avec des postes de cette même date."""
    per_tag = {}
    for key, tag in LADDER_TAGS:
        node = gaap.get(tag)
        if not node:
            continue
        best = {}
        for x in node.get("units", {}).get("USD", []):
            if x.get("form") not in ("10-K", "10-Q") or "start" in x or x.get("val") is None:
                continue
            e = x["end"]
            if e not in best or x["filed"] > best[e][1]:
                best[e] = (float(x["val"]), x["filed"])
        per_tag[key] = best
    if "y1" not in per_tag or not per_tag["y1"]:
        return None
    asof = max(per_tag["y1"])
    out = {"asof": asof}
    total = 0.0
    for key, _ in LADDER_TAGS:
        v = per_tag.get(key, {}).get(asof)
        if v is not None:
            out[key] = round(v[0])
            total += v[0]
    out["total"] = round(total)
    return out if total > 0 else None


# ─────────────────────────────────────────────────────────────────────────────
# 2. DÉTAIL PAR TRANCHE — parsing de la note de dette du 10-K
# ─────────────────────────────────────────────────────────────────────────────
MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
RE_DATE = re.compile(r"([A-Z][a-z]{2})\w*\.?\s+(\d{1,2}),\s+(20\d\d)")
RE_RATE = re.compile(r"(\d{1,2}\.\d{1,4})\s*%")
RE_MONEY = re.compile(r"([\d,]+(?:\.\d+)?)")


def _latest_10k(cik):
    s = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if not s:
        return None
    r = s["filings"]["recent"]
    for i in range(len(r["form"])):
        if r["form"][i] == "10-K":
            return {"report": r["reportDate"][i], "filed": r["filingDate"][i],
                    "acc": r["accessionNumber"][i].replace("-", "")}
    return None


def _debt_reports(cik, acc):
    fs = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/FilingSummary.xml", txt=True)
    if not fs:
        return []
    out = []
    for m in re.findall(r"<Report[^>]*>(.*?)</Report>", fs, re.S):
        sn = re.search(r"<ShortName>(.*?)</ShortName>", m, re.S)
        hf = re.search(r"<HtmlFileName>(R\d+\.htm)</HtmlFileName>", m)
        if not (sn and hf):
            continue
        n = _clean(sn.group(1))
        if not re.search(r"debt|notes payable|borrowing", n, re.I):
            continue
        if re.search(r"polic|parenthetical|investment|securit|lease", n, re.I):
            continue
        out.append((n, hf.group(1)))
    return out


RE_MEMBER_MAT = re.compile(
    r"due\s+([A-Z][a-z]{2})\w*\.?\s+(20\d\d)|due\s+(20\d\d)|(20\d\d)\s+notes", re.I)


def _cells(row):
    """Rend [(classe, texte)]. Les R-files SEC balisent chaque cellule : `pl` =
    libellé, `nump`/`num` = nombre, `text` = valeur textuelle, `fn` = renvoi de
    note. Se fier à ces classes évite de deviner la colonne."""
    out = []
    for m in re.finditer(r"<t[dh]([^>]*)>(.*?)</t[dh]>", row, re.S):
        attrs, inner = m.group(1), m.group(2)
        k = re.search(r'class="([^"]*)"', attrs)
        out.append(((k.group(1).split()[0] if k else ""), _clean(inner)))
    return out


def _mat_from_name(name):
    """L'échéance est presque toujours DANS le nom de la tranche (« Fixed-Rate
    Senior Notes Due July 2025 »), et bien plus fiablement que dans une colonne :
    Oracle publie « Date of issuance », pas la date d'échéance."""
    m = RE_MEMBER_MAT.search(name)
    if not m:
        return None
    if m.group(1) and m.group(2):
        mo = MONTHS.get(m.group(1).lower())
        return f"{m.group(2)}-{mo:02d}-01" if mo else f"{m.group(2)}-06-01"
    y = m.group(3) or m.group(4)
    return f"{y}-06-01" if y else None


def _parse_tranches(page):
    """Table VERTICALE : une ligne nomme la tranche, les lignes suivantes portent
    ses attributs. Machine à états, pilotée par les classes de cellules.

    Trois pièges corrigés après lecture du HTML réel :
      · les montants sont en DOLLARS ENTIERS dans les cellules `nump`, pas en
        millions comme le laisse croire l'en-tête « $ in Millions » (qui ne décrit
        que le rendu visuel) ;
      · le taux s'appelle « Effective interest rate » chez Oracle, « Stated interest
        rate » ailleurs — les deux doivent être acceptés ;
      · la date d'échéance ne figure pas toujours en colonne ; elle est dans le NOM.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)
    tranches, cur = [], None

    def flush():
        if cur and cur.get("rate") is not None and cur.get("amt"):
            tranches.append(cur)

    for r in rows:
        cs = _cells(r)
        if not cs:
            continue
        label = cs[0][1]
        vals = [(k, v) for k, v in cs[1:] if v and k != "fn"]
        if not label:
            continue

        # En-tête de tranche : un libellé seul, qui ressemble à un titre d'obligation.
        if not vals:
            if re.search(r"note|bond|debenture|senior|loan|facilit|due\s+20\d\d", label, re.I) \
               and not re.search(r"line items|abstract", label, re.I):
                flush()
                nm = re.sub(r"\s*\[Member\]\s*$", "", label)
                cur = {"name": nm, "eur": False, "mat": _mat_from_name(nm)}
            continue
        if cur is None:
            continue

        joined = " ".join(v for _, v in vals)
        if re.search(r"interest rate", label, re.I) and not re.search(r"swap|hedge", label, re.I):
            m = RE_RATE.search(joined)
            if m and cur.get("rate") is None:
                cur["rate"] = float(m.group(1))
        elif re.search(r"maturity date", label, re.I):
            m = RE_DATE.search(joined)
            if m:
                mo = MONTHS.get(m.group(1).lower())
                if mo:
                    cur["mat"] = f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}"
        else:
            # Montant : première cellule numérique significative du bloc. On ne se
            # fie pas au libellé (« Notes payable and other borrowings », « par
            # value », « Total »… varient d'un émetteur à l'autre).
            if cur.get("amt"):
                continue
            if "€" in joined:
                cur["eur"] = True
            for k, v in vals:
                if k not in ("nump", "num"):
                    continue
                mm = RE_MONEY.search(v.replace("€", "").replace("$", "").strip())
                if not mm:
                    continue
                try:
                    x = float(mm.group(1).replace(",", ""))
                except ValueError:
                    continue
                if x >= 1e7:                       # au moins 10 M$ : exclut % et compteurs
                    cur["amt"] = x
                    break
    flush()
    return tranches


RE_RANGE_YEARS = re.compile(r"(20\d\d)\s*[-–—]\s*(20\d\d)")
RE_RANGE_RATE = re.compile(r"(\d{1,2}\.\d{1,4})\s*%\s*[-–—]\s*(\d{1,2}\.\d{1,4})\s*%")


def _parse_vintages(page):
    """SECONDE FAMILLE DE TABLES. Amazon (et d'autres) ne listent pas obligation par
    obligation : ils groupent par MILLÉSIME D'ÉMISSION, avec des fourchettes —
    « 2021 Notes issuance of $18.5 bn | 2026-2061 | 1.00%-3.25% | … | 15,000 ».

    On en tire un point médian par millésime : coupon = milieu de la fourchette,
    échéance = milieu de la plage d'années. C'est moins fin qu'une tranche unitaire,
    et c'est dit à l'écran ; mais c'est la granularité que l'émetteur publie, et elle
    reste très supérieure à un taux moyen global.
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)
    out = []
    for r in rows:
        cs = [v for _, v in _cells(r) if v]
        if len(cs) < 4:
            continue
        name = cs[0]
        if not re.search(r"note|bond|debenture|issuance|loan", name, re.I):
            continue
        joined = " ".join(cs[1:])
        yr = RE_RANGE_YEARS.search(joined)
        rr = RE_RANGE_RATE.search(joined)
        if not (yr and rr):
            continue
        amts = []
        for v in cs[1:]:
            vv = v.replace("$", "").replace(",", "").strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", vv):
                try:
                    amts.append(float(vv))
                except ValueError:
                    pass
        amts = [a for a in amts if a >= 100]        # exclut années et pourcentages
        if not amts:
            continue
        y1, y2 = int(yr.group(1)), int(yr.group(2))
        r1, r2 = float(rr.group(1)), float(rr.group(2))
        out.append({"name": name[:70], "eur": "€" in joined,
                    "amt": amts[-1],                # colonne la plus récente
                    "rate": round((r1 + r2) / 2, 3),
                    "mat": f"{(y1+y2)//2}-06-01",
                    "span": f"{y1}-{y2}", "rspan": f"{r1}-{r2}", "approx": 1})
    return out


def tranches(cik, xbrl_debt=None, name=""):
    """Essaie les deux familles de tables sur tous les R-files candidats, puis
    CALIBRE ET VALIDE contre la dette réelle connue par XBRL.

    Les R-files n'expriment pas tous la même échelle : la même grandeur vaut
    « 68 836 » dans un fichier (millions) et « 68 836 000 000 » dans un autre
    (dollars entiers), et rien dans le balisage ne le dit. On essaie donc les trois
    échelles plausibles et on retient celle qui retombe sur le total XBRL.

    Si AUCUNE combinaison n'approche la dette réelle à 12 % près, le parsing est
    REJETÉ EN BLOC : mieux vaut ne rien afficher qu'un détail obligataire faux.
    """
    f = _latest_10k(cik)
    if not f:
        return None
    cands = []
    for short, rf in _debt_reports(cik, f["acc"])[:8]:
        page = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc']}/{rf}", txt=True)
        if not page:
            continue
        for tr in (_parse_tranches(page), _parse_vintages(page)):
            scored = [t for t in tr if t.get("rate") is not None and t.get("amt")]
            if len(scored) >= 2:
                cands.append({"table": short, "rfile": rf, "tranches": scored})
        time.sleep(0.12)
    if not cands:
        return None

    best, best_err = None, None
    for c in cands:
        usd = [t for t in c["tranches"] if not t["eur"]]
        raw = sum(t["amt"] for t in usd)
        if raw <= 0:
            continue
        for scale in (1.0, 1e3, 1e6):
            tot = raw * scale
            if xbrl_debt:
                err = abs(tot - xbrl_debt) / xbrl_debt
                if err > 0.12:
                    continue
            else:
                err = 0.0 if 1e9 <= tot <= 1e13 else 9.9
                if err > 1:
                    continue
            # à erreur comparable, préférer le détail le plus fin
            score = (err, -len(usd))
            if best is None or score < best_err:
                best, best_err = ({**c, "scale": scale, "err": err}, score)
    if best is None:
        return None

    sc = best["scale"]
    for t in best["tranches"]:
        t["amt"] *= sc
    usd = [t for t in best["tranches"] if not t["eur"]]
    tot = sum(t["amt"] for t in usd)
    wac = (sum(t["amt"] * t["rate"] for t in usd) / tot) if tot else None
    approx = any(t.get("approx") for t in best["tranches"])
    return {
        "asof": f["report"], "filed": f["filed"], "table": best["table"],
        "n": len(best["tranches"]), "n_usd": len(usd), "n_eur": len(best["tranches"]) - len(usd),
        "par_usd": round(tot), "err": round(100 * best["err"], 2), "approx": 1 if approx else 0,
        "wac": round(wac, 3) if wac is not None else None,
        "items": [{"n": t["name"][:70], "a": round(t["amt"]), "r": t["rate"],
                   "m": t.get("mat"), **({"eur": 1} if t["eur"] else {}),
                   **({"span": t["span"], "rspan": t["rspan"]} if t.get("approx") else {})}
                  for t in sorted(best["tranches"], key=lambda x: x.get("mat") or "9999")],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2 bis. COMPOSITION DE LA TRESORERIE — note « Financial Instruments » du 10-K
#
# « Tresorerie » est un mot trompeur : chez Apple, 28 Md$ sur 132 sont du cash au
# sens courant, le reste est un PORTEFEUILLE OBLIGATAIRE. Cette note en donne le
# detail par type d'instrument. Publiee UNE FOIS PAR AN seulement (10-K).
#
# DEUX PIEGES, tous deux verifies sur les tables reelles :
#  · Les categories sont repetees par NIVEAU DE VALORISATION (Level 1/2/3).
#    Sommer naivement compte double. Mais prendre le maximum perd des lignes :
#    Microsoft eclate ses obligations d'entreprises entre Level 2 (10 660) et
#    Level 3 (1 738) SANS ligne combinee. Regle retenue : s'il existe une variante
#    SANS niveau, elle fait foi seule ; sinon on somme les variantes par niveau,
#    qui sont disjointes.
#  · Les libelles varient d'un emetteur a l'autre (« U.S. Treasury securities »,
#    « U.S. government securities », « Government bonds »). D'ou la normalisation
#    ci-dessous. Un libelle non reconnu tombe dans « autres » plutot que d'etre
#    ignore silencieusement — sinon le total ne boucle plus et on ne le voit pas.
# ─────────────────────────────────────────────────────────────────────────────
TREASURY_CATS = [
    ("gov_us",  r"u\.?s\.? (?:treasury|government)|government bonds|treasury securit"),
    ("agency",  r"agency securit|u\.?s\.? agenc"),
    ("gov_for", r"foreign government|non-u\.?s\.? government"),
    ("corp",    r"corporate (?:debt|notes|bond)"),
    ("mbs",     r"mortgage|asset-backed"),
    ("muni",    r"municipal"),
    ("mmf",     r"money market"),
    ("depo",    r"certificate[s]? of deposit|time deposit|commercial paper"),
    ("equity",  r"equity securit|mutual fund|marketable equity"),
]
TREASURY_LABELS = {
    "gov_us":  "Dette d'État américaine",
    "agency":  "Agences américaines",
    "gov_for": "Dette d'États étrangers",
    "corp":    "Obligations d'entreprises",
    "mbs":     "Titres hypothécaires et adossés à des créances",
    "muni":    "Obligations municipales",
    "mmf":     "Fonds monétaires",
    "depo":    "Dépôts et papier commercial",
    "equity":  "Actions et fonds",
    "other":   "Autres",
}
RE_LEVEL = re.compile(r"level\s*[123]|quoted prices|significant other|unobservable", re.I)


def _classify(member):
    for key, pat in TREASURY_CATS:
        if re.search(pat, member, re.I):
            return key
    return None


def treasury(cik, total_known=None):
    """Composition du portefeuille de tresorerie, validee contre le total connu."""
    f = _latest_10k(cik)
    if not f:
        return None
    fs = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc']}/FilingSummary.xml", txt=True)
    if not fs:
        return None
    cands = []
    for m in re.findall(r"<Report[^>]*>(.*?)</Report>", fs, re.S):
        sn = re.search(r"<ShortName>(.*?)</ShortName>", m, re.S)
        hf = re.search(r"<HtmlFileName>(R\d+\.htm)</HtmlFileName>", m)
        if not (sn and hf):
            continue
        n = _clean(sn.group(1))
        if not re.search(r"financial instrument|investment|marketable securit|cash, cash equivalent", n, re.I):
            continue
        if re.search(r"polic|\(tables\)|derivative|maturit|offsetting|narrative|gain|loss", n, re.I):
            continue
        cands.append((n, hf.group(1)))

    best = None
    for short, rf in cands[:8]:
        page = _get(f"https://www.sec.gov/Archives/edgar/data/{cik}/{f['acc']}/{rf}", txt=True)
        if not page:
            continue
        # member -> valeur, en ne gardant que les lignes de MESURE (juste valeur /
        # base comptable / total), jamais les plus ou moins-values latentes.
        vals, cur = {}, None
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
            cs = _cells(row)
            if not cs:
                continue
            lab = cs[0][1]
            cells = [(k, v) for k, v in cs[1:] if v and k != "fn"]
            if not cells and lab and not re.search(r"line items|abstract", lab, re.I):
                cur = lab
                continue
            if cur is None:
                continue
            if re.search(r"unrealized|gross|gain|loss|amortized|adjusted cost", lab, re.I):
                continue
            if not re.search(r"fair value|recorded basis|^total|estimated", lab, re.I):
                continue
            nums = [v for k, v in cells if k in ("nump", "num")]
            if not nums:
                continue
            try:
                x = float(re.sub(r"[^\d.\-]", "", nums[0].replace(",", "")))
            except ValueError:
                continue
            if x <= 0:
                continue
            vals.setdefault(cur, x)
        if not vals:
            time.sleep(0.1)
            continue

        # Regroupement par categorie, avec la regle « sans niveau > somme des niveaux »
        groups = {}
        for member, x in vals.items():
            key = _classify(member)
            if key is None:
                continue
            groups.setdefault(key, {"plain": None, "levels": []})
            if RE_LEVEL.search(member):
                groups[key]["levels"].append(x)
            else:
                if groups[key]["plain"] is None or x > groups[key]["plain"]:
                    groups[key]["plain"] = x
        comp = {}
        for key, g in groups.items():
            comp[key] = g["plain"] if g["plain"] is not None else sum(g["levels"])
        tot = sum(comp.values())
        if tot <= 0:
            time.sleep(0.1)
            continue
        # Les tables sont en millions ou en dollars entiers selon l'emetteur : on
        # calibre sur le total connu, comme pour les tranches obligataires.
        for scale in (1.0, 1e6):
            t2 = tot * scale
            if total_known:
                err = abs(t2 - total_known) / total_known
            else:
                err = 0.0 if 1e9 <= t2 <= 1e13 else 9.9
            if err <= 0.12 and (best is None or err < best["err"]):
                best = {"table": short, "comp": {k: v * scale for k, v in comp.items()},
                        "total": t2, "err": err, "asof": f["report"]}
        time.sleep(0.1)

    if best is None:
        return None
    return {"asof": best["asof"], "table": best["table"],
            "err": round(100 * best["err"], 1),
            "total": round(best["total"]),
            "comp": {k: round(v) for k, v in sorted(best["comp"].items(), key=lambda kv: -kv[1])}}


# ─────────────────────────────────────────────────────────────────────────────
# 3. ÉMISSIONS RÉCENTES — prospectus 424B
# ─────────────────────────────────────────────────────────────────────────────
def recent_issues(cik, limit=6, since_days=900):
    s = _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if not s:
        return []
    r = s["filings"]["recent"]
    cut = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
    out = []
    for i in range(len(r["form"])):
        if not r["form"][i].startswith("424B"):
            continue
        if r["filingDate"][i] < cut:
            continue
        out.append({"d": r["filingDate"][i], "form": r["form"][i],
                    "acc": r["accessionNumber"][i],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                           f"{r['accessionNumber'][i].replace('-','')}/"
                           f"{r['primaryDocument'][i]}"})
        if len(out) >= limit:
            break
    return out
