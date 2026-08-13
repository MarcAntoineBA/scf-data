#!/usr/bin/env python3
"""Récolte les vraies adresses Investing.com de chaque indicateur macro.

POURQUOI CE SCRIPT EXISTE
─────────────────────────
`fetch_macro_calendar.py` interroge Investing.com en premier, mais Investing
refuse les adresses IP de datacenter (403) : sur GitHub Actions, c'est donc
toujours le repli Nasdaq qui sert. Or l'API Nasdaq ne donne AUCUNE adresse par
événement — le collecteur mettait la même page d'accueil du calendrier sur les
137 lignes, si bien qu'un clic menait à un site où la donnée était introuvable.
Nasdaq n'indique pas non plus la période : quatre lignes « GDP » identiques pour
le Royaume-Uni, sans moyen de distinguer le trimestriel de l'annuel.

Investing.com donne les deux (`/economic-calendar/gdp-121`, « GDP (QoQ) »).
Ce script se lance depuis une machine à IP résidentielle (le PC ou le Mac), où
Investing répond, et fige le résultat dans `investing_event_urls.json`. Le
collecteur, lui, lit ce fichier — y compris depuis Actions, où le réseau vers
Investing est fermé.

Les identifiants de slug (`gdp-121`) sont stables dans le temps ; ce sont les
valeurs `prev`/`cons`, qui servent à départager les variantes d'un même nom, qui
se périment. Relancer périodiquement (une fois par jour suffit largement).

    python3 scripts/harvest_investing_urls.py
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://www.investing.com/economic-calendar/Service/getCalendarFilteredData"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
SORTIE = Path(__file__).resolve().parent / "investing_event_urls.json"

# Identifiants pays Investing.com. Doit couvrir au moins NASDAQ_PAYS du collecteur,
# sinon les pays manquants retombent sur l'ancien lien générique.
PAYS_IDS = [5, 72, 17, 4, 35, 37, 12, 6, 25, 22, 10, 26]

# data-img_key du drapeau Investing → libellé FR, aligné sur NASDAQ_PAYS
# (fetch_macro_calendar.py). La clé de jointure est ce libellé, pas le drapeau.
DRAPEAU_FR = {
    "United_States": "USA", "Euro_Zone": "Zone Euro", "Europe": "Zone Euro",
    "Germany": "Allemagne", "United_Kingdom": "Royaume-Uni", "Japan": "Japon",
    "China": "Chine", "Switzerland": "Suisse", "Canada": "Canada",
    "Australia": "Australie", "France": "France", "Italy": "Italie",
    "Spain": "Espagne",
}

JOURS_AVANT = 5     # remonte un peu : Nasdaq range certaines publications la veille
JOURS_APRES = 60    # même horizon que le calendrier affiché
PAGES_MAX = 12      # garde-fou : l'API pagine par 200 lignes


def _page(date_debut, date_fin, limit_from):
    corps = [("country[]", str(c)) for c in PAYS_IDS]
    corps += [("importance[]", "1"), ("importance[]", "2"), ("importance[]", "3"),
              ("timeZone", "8"), ("timeFilter", "timeOnly"),
              ("dateFrom", date_debut), ("dateTo", date_fin),
              ("submitFilters", "1"), ("limit_from", str(limit_from))]
    req = urllib.request.Request(
        API, data=urllib.parse.urlencode(corps).encode("utf-8"), headers={
            "User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json", "Origin": "https://www.investing.com",
            "Referer": "https://www.investing.com/economic-calendar/"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _cellule(bloc, motif):
    m = re.search(motif, bloc, re.DOTALL)
    if not m:
        return ""
    return re.sub(r"<[^>]+>", "", m.group(1)).replace("&nbsp;", "").strip()


def recolte():
    aujourdhui = datetime.now()
    debut = (aujourdhui - timedelta(days=JOURS_AVANT)).strftime("%Y-%m-%d")
    fin = (aujourdhui + timedelta(days=JOURS_APRES)).strftime("%Y-%m-%d")

    lignes, vus = [], set()
    for page in range(PAGES_MAX):
        try:
            rep = _page(debut, fin, page)
        except Exception as e:
            sys.stderr.write(f"[URLs] Investing indisponible (page {page}) : {e}\n")
            break
        html = rep.get("data", "")
        nouvelles = 0
        for m in re.finditer(
                r'<tr id="eventRowId_(\d+)"[^>]+data-event-datetime="([^"]+)"[^>]*>(.*?)</tr>',
                html, re.DOTALL):
            rid, quand, bloc = m.groups()
            if rid in vus:
                continue
            vus.add(rid)
            nouvelles += 1
            drapeau = re.search(r'class="ceFlags ([A-Za-z_]+)"', bloc)
            lien = re.search(r'<a\s+([^>]*)>([^<]+)</a>', bloc)
            if not (drapeau and lien):
                continue
            pays = DRAPEAU_FR.get(drapeau.group(1))
            if not pays:
                continue
            libelle = lien.group(2).strip()
            href = re.search(r'href="([^"]+)"', lien.group(1))
            if not (href and href.group(1).startswith("/")):
                continue
            # « GDP (QoQ)  (Q2) » → base « GDP », période « QoQ »
            base = re.sub(r"\s*\([^)]*\)", "", libelle).strip()
            parentheses = re.findall(r"\(([^)]*)\)", libelle)
            lignes.append({
                "pays": pays, "base": base,
                "periode": parentheses[0].strip() if parentheses else "",
                "libelle": libelle,
                "url": "https://www.investing.com" + href.group(1),
                "prev": _cellule(bloc, r'<td[^>]*id="eventPrevious_\d+"[^>]*>(.*?)</td>'),
                "cons": _cellule(bloc, r'<td[^>]*id="eventForecast_\d+"[^>]*>(.*?)</td>'),
            })
        sys.stderr.write(f"[URLs] page {page} : {nouvelles} nouvelles lignes\n")
        if not nouvelles or not rep.get("bind_scroll_handler"):
            break
        time.sleep(1)
    return lignes


def main():
    lignes = recolte()
    if not lignes:
        sys.stderr.write("[URLs] rien récolté — fichier existant laissé intact\n")
        return 1

    # Regroupe par (pays, nom de base). Une même adresse peut revenir plusieurs fois
    # (l'indicateur sort tous les mois) : on ne garde qu'une entrée par URL, la plus
    # récente, pour que `prev`/`cons` servent à départager les variantes homonymes.
    table = {}
    for ligne in lignes:
        cle = f"{ligne['pays']}|{ligne['base'].lower()}"
        variantes = table.setdefault(cle, {})
        variantes[ligne["url"]] = {
            "periode": ligne["periode"], "libelle": ligne["libelle"],
            "url": ligne["url"], "prev": ligne["prev"], "cons": ligne["cons"],
        }
    table = {cle: list(v.values()) for cle, v in table.items()}

    charge = {
        "genere": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "investing.com/economic-calendar",
        "lignes_lues": len(lignes),
        "indicateurs": len(table),
        "entrees": dict(sorted(table.items())),
    }
    SORTIE.write_text(json.dumps(charge, ensure_ascii=False, indent=1), encoding="utf-8")
    homonymes = sum(1 for v in table.values() if len(v) > 1)
    sys.stderr.write(
        f"[URLs] {len(table)} indicateurs ({homonymes} avec plusieurs périodes) "
        f"→ {SORTIE.name}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
