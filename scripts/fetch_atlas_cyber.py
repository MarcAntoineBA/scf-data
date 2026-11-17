#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_atlas_cyber.py — Atlas Économique, vue « Cyber & pannes ».

LA QUESTION POSÉE : les attaques informatiques et les pannes des grands réseaux
coûtent-elles de plus en plus cher, et à qui ? Elle a plusieurs réponses, qui ne
viennent pas des mêmes gens et ne se contredisent donc pas :

  1. les pannes        — ce que ~30 grands services DÉCLARENT eux-mêmes sur leur page
                          de statut officielle (format Statuspage : historique mois par
                          mois depuis 2020). C'est l'historique que Downdetector ne
                          publie pas : lui compte des plaintes, eux signent un incident.
  2. les rançongiciels — les revendications publiées sur les sites des groupes,
                          agrégées par ransomware.live (date d'attaque, pays, secteur).
  3. la crypto         — les vols recensés par DefiLlama (montant, technique, cible).
  4. les fuites        — Have I Been Pwned (Troy Hunt) : chaque fuite vérifiée, sa date,
                          le nombre de comptes, la nature des données.
  5. les failles       — CISA (failles EXPLOITÉES pour de vrai, catalogue KEV) et NIST
                          (failles PUBLIÉES, base NVD) : la différence entre les deux dit
                          si le danger suit le volume.
  6. le brouillage GPS — gpsjam.org, à partir des positions ADS-B des avions.
  7. l'aviation US     — état du réseau de la FAA (en direct seulement).
  8. le coût           — séries ANNUELLES saisies à la parution (FBI IC3, IBM).

RÈGLES (celles de la vue Ukraine) : on ne publie que ce qu'une institution ou une
source identifiée signe, ou ce que le site recalcule lui-même. Rien n'est estimé.
Une valeur manquante reste VIDE, jamais zéro. Les mois récents des rançongiciels sont
marqués provisoires (les revendications arrivent en retard). On ne republie JAMAIS le
nom des victimes revendiquées : c'est le levier du chantage, et une revendication
n'est pas une preuve.

Deux régimes :
  (défaut)  tout, toutes les 6 h  → atlas_cyber_cache.{json,js}  window.__ATLAS_CYBER__
  --live    l'instant, toutes les 10 min → atlas_cyber_live.{json,js}  window.__ATLAS_CYBER_LIVE__

Bibliothèque standard seule (urllib). Incrémental : les mois anciens des séries lentes
(rançongiciels, NVD) sont relus dans le cache précédent ; un rattrapage borné par
passage complète ce qui manque.
"""
import calendar
import collections
import concurrent.futures as cf
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126 Safari/537.36")
HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "Desktop", "Site_Crypto_Finance")
BASES = [
    os.path.join(REPO, "{n}"),
    os.path.join(REPO, "public", "{n}"),
    os.path.join(HOME, "Library", "Caches", "site_crypto_finance", "{n}"),
]
NOW = dt.datetime.now(dt.timezone.utc)
START_MONTH = "2020-01"          # profondeur des séries mensuelles « pannes » et « rançongiciels »
NVD_START = "2016-01"
MAX_NVD_REQ = int(os.environ.get("CYBER_MAX_NVD", "40"))      # rattrapage NVD borné par passage (5 req. / 30 s sans clé)
MAX_RL_MONTHS = int(os.environ.get("CYBER_MAX_RL", "30"))     # rattrapage ransomware.live borné par passage
PROVISOIRE_MOIS = 2              # derniers mois des rançongiciels encore en train d'arriver
ERRORS = []


def log(m):
    print("[cyber] " + m, flush=True)


def warn(m):
    ERRORS.append(m[:220])
    print("[cyber] ⚠ " + m, flush=True)


# ── Réseau ────────────────────────────────────────────────────────────────────
def get(url, timeout=45, tries=3, raw=False, headers=None):
    last = None
    for k in range(tries):
        try:
            h = {"User-Agent": UA, "Accept": "application/json, text/xml, */*",
                 "Accept-Encoding": "gzip"}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or b[:2] == b"\x1f\x8b":
                    b = gzip.decompress(b)
                return b if raw else b.decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                break
            time.sleep(2 + 3 * k)
        except Exception as e:  # réseau, délai
            last = e
            time.sleep(2 + 3 * k)
    raise RuntimeError("%s → %s" % (url[:90], last))


def get_json(url, **kw):
    return json.loads(get(url, **kw))


# ── Mois ──────────────────────────────────────────────────────────────────────
def month_list(start, end=None):
    end = end or NOW.strftime("%Y-%m")
    y, m = map(int, start.split("-"))
    ey, em = map(int, end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append("%04d-%02d" % (y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def month_add(ym, k):
    y, m = map(int, ym.split("-"))
    m += k
    while m > 12:
        y, m = y + 1, m - 12
    while m < 1:
        y, m = y - 1, m + 12
    return "%04d-%02d" % (y, m)


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")


# ── Cache précédent ───────────────────────────────────────────────────────────
def load_prev(name):
    for b in BASES:
        p = b.format(n=name) + ".json"
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    return {}


def write_out(name, var, out, title):
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    hdr = "/* %s.js — %s */" % (name, title)
    wrote = 0
    for b in BASES:
        base = b.format(n=name)
        try:
            os.makedirs(os.path.dirname(base), exist_ok=True)
            for ext, txt in ((".json", blob), (".js", hdr + "\nwindow." + var + " = " + blob + ";\n")):
                tmp = base + ext + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(txt)
                os.replace(tmp, base + ext)
            wrote += 1
        except Exception as e:
            print("[cyber] écriture %s : %s" % (base, e))
    if not wrote:
        raise RuntimeError("aucune copie écrite")
    log("écrit %s (%d Ko, %d copies)" % (name, len(blob) // 1024, wrote))


# ═══════════════════════════════════════════════════════════════════════════════
# 1 · PANNES DÉCLARÉES — pages de statut officielles
# ═══════════════════════════════════════════════════════════════════════════════
# sec : cloud · logiciel · social · ia · finance · jeux
# hist : True = page Statuspage avec historique (history.json), sinon on ACCUMULE à
#        partir d'aujourd'hui (l'historique de ces sources-là commence avec la vue).
SERVICES = [
    # Nuage et Internet
    dict(id="cloudflare", nom="Cloudflare", sec="cloud", dom="www.cloudflarestatus.com", hist=False),
    dict(id="akamai", nom="Akamai", sec="cloud", dom="www.akamaistatus.com", hist=True),
    dict(id="digitalocean", nom="DigitalOcean", sec="cloud", dom="status.digitalocean.com", hist=True),
    dict(id="linode", nom="Linode (Akamai Cloud)", sec="cloud", dom="status.linode.com", hist=True),
    dict(id="vercel", nom="Vercel", sec="cloud", dom="www.vercel-status.com", hist=True),
    dict(id="netlify", nom="Netlify", sec="cloud", dom="www.netlifystatus.com", hist=True),
    dict(id="gcp", nom="Google Cloud", sec="cloud", kind="gcp", url="https://status.cloud.google.com/", hist=False),
    dict(id="aws", nom="Amazon Web Services", sec="cloud", kind="aws", url="https://health.aws.amazon.com/health/status", hist=False),
    # Logiciels et outils de travail
    dict(id="github", nom="GitHub", sec="logiciel", dom="www.githubstatus.com", hist=True),
    dict(id="npm", nom="npm", sec="logiciel", dom="status.npmjs.org", hist=True),
    dict(id="dropbox", nom="Dropbox", sec="logiciel", dom="status.dropbox.com", hist=True),
    dict(id="box", nom="Box", sec="logiciel", dom="status.box.com", hist=True),
    dict(id="figma", nom="Figma", sec="logiciel", dom="status.figma.com", hist=True),
    dict(id="apple", nom="Apple (iCloud, App Store…)", sec="logiciel", kind="apple", url="https://www.apple.com/support/systemstatus/", hist=False),
    # Réseaux sociaux et messageries
    dict(id="discord", nom="Discord", sec="social", dom="discordstatus.com", hist=True),
    dict(id="reddit", nom="Reddit", sec="social", dom="www.redditstatus.com", hist=True),
    dict(id="zoom", nom="Zoom", sec="social", dom="status.zoom.us", hist=True),
    dict(id="slack", nom="Slack", sec="social", kind="slack", url="https://slack-status.com/", hist=False),
    # Intelligence artificielle
    dict(id="openai", nom="OpenAI (ChatGPT)", sec="ia", dom="status.openai.com", hist=False),
    dict(id="anthropic", nom="Anthropic (Claude)", sec="ia", dom="status.anthropic.com", hist=True),
    # Finance, paiement, crypto
    dict(id="coinbase", nom="Coinbase", sec="finance", dom="status.coinbase.com", hist=True),
    dict(id="kraken", nom="Kraken", sec="finance", dom="status.kraken.com", hist=True),
    dict(id="gemini", nom="Gemini", sec="finance", dom="status.gemini.com", hist=True),
    dict(id="cryptocom", nom="Crypto.com", sec="finance", dom="status.crypto.com", hist=True),
    dict(id="bitstamp", nom="Bitstamp", sec="finance", dom="status.bitstamp.net", hist=True),
    dict(id="wise", nom="Wise", sec="finance", dom="status.wise.com", hist=True),
    # Ledger déclare un incident « majeur » par réseau crypto intégré (200 à 780 par an) : gardé dans le
    # direct et dans sa propre fiche, écarté des agrégats où il pèserait plus que tous les autres réunis.
    dict(id="ledger", nom="Ledger", sec="finance", dom="status.ledger.com", hist=True, bruit=True),
    dict(id="square", nom="Square", sec="finance", dom="www.issquareup.com", hist=False),
    dict(id="plaid", nom="Plaid (connexions bancaires)", sec="finance", dom="status.plaid.com", hist=False),
    # Jeux
    dict(id="epic", nom="Epic Games", sec="jeux", dom="status.epicgames.com", hist=True),
]
IMPACT_CODE = {"none": 0, "minor": 1, "major": 2, "critical": 3, "maintenance": 0}
MOIS_EN = {m: i for i, m in enumerate(calendar.month_abbr) if m}
MOIS_LONG = {m: i for i, m in enumerate(calendar.month_name) if m}
TZ = {"UTC": 0, "GMT": 0, "PDT": -7, "PST": -8, "MDT": -6, "MST": -7, "CDT": -5, "CST": -6,
      "EDT": -4, "EST": -5, "CET": 1, "CEST": 2, "BST": 1, "IST": 5.5, "JST": 9, "AEST": 10,
      "AEDT": 11, "SGT": 8, "HKT": 8, "WET": 0, "WEST": 1, "EET": 2, "EEST": 3}
TS_RE = re.compile(r"^\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})\s*-\s*"
                   r"(?:([A-Z][a-z]{2})\s+(\d{1,2}),\s+)?(\d{1,2}):(\d{2})\s*([A-Z]{2,5})?\s*$")


def parse_sp_ts(txt, year, month_ctx):
    """« Nov 16, 11:59 - 16:26 UTC » ou « Oct 30, 20:59 - Nov 2, 10:00 PDT » → (début, fin) UTC.
    L'année vient du bloc de mois de la page ; une fin dans un mois antérieur au début
    passe à l'année suivante (incident à cheval sur le Nouvel An)."""
    t = re.sub(r"<[^>]+>", "", txt or "")
    m = TS_RE.match(t)
    if not m:
        return None
    mo1 = MOIS_EN.get(m.group(1))
    if not mo1:
        return None
    y1 = year
    if mo1 > month_ctx + 1:          # bloc « janvier » qui affiche un incident né en décembre
        y1 = year - 1
    off = TZ.get(m.group(9) or "UTC", 0)
    try:
        d1 = dt.datetime(y1, mo1, int(m.group(2)), int(m.group(3)), int(m.group(4)))
        if m.group(5):
            mo2 = MOIS_EN.get(m.group(5)) or mo1
            y2 = y1 + (1 if mo2 < mo1 else 0)
            d2 = dt.datetime(y2, mo2, int(m.group(6)), int(m.group(7)), int(m.group(8)))
        else:
            d2 = dt.datetime(y1, mo1, int(m.group(2)), int(m.group(7)), int(m.group(8)))
            if d2 < d1:
                d2 += dt.timedelta(days=1)
    except ValueError:
        return None
    d1 -= dt.timedelta(hours=off)
    d2 -= dt.timedelta(hours=off)
    return d1.replace(tzinfo=dt.timezone.utc), d2.replace(tzinfo=dt.timezone.utc)


def sp_history(svc):
    """Tout l'historique d'une page Statuspage, depuis START_MONTH. Une page = 3 mois."""
    dom = svc["dom"]
    incs, created, months_seen = [], None, set()
    stop = START_MONTH
    for page in range(1, 40):
        try:
            j = get_json("https://%s/history.json?page=%d" % (dom, page), timeout=40)
        except Exception as e:
            if page == 1:
                raise
            warn("%s historique page %d : %s" % (svc["id"], page, e))
            break
        created = created or ((j.get("page_status") or {}).get("page") or {}).get("created_at", "")[:7]
        ms = j.get("months") or []
        if not ms:
            break
        older = False
        page_months = set()
        for mo in ms:
            mi0, yr0 = MOIS_LONG.get(mo.get("name")), mo.get("year")
            if mi0 and yr0:
                page_months.add("%04d-%02d" % (yr0, mi0))
        if page > 1 and page_months and page_months <= months_seen:
            break                      # la page ignore la pagination : l'historique s'arrête là
        for mo in ms:
            mi = MOIS_LONG.get(mo.get("name"))
            yr = mo.get("year")
            if not mi or not yr:
                continue
            ym = "%04d-%02d" % (yr, mi)
            if ym < stop:
                older = True
                continue
            months_seen.add(ym)
            for i in mo.get("incidents") or []:
                imp = IMPACT_CODE.get(i.get("impact"), 0)
                p = parse_sp_ts(i.get("timestamp"), yr, mi)
                if not p:
                    continue
                d1, d2 = p
                incs.append(dict(c=i.get("code"), t=d1.strftime("%Y-%m-%dT%H:%MZ"),
                                 m=max(0, int((d2 - d1).total_seconds() // 60)), i=imp,
                                 n=(i.get("name") or "")[:140]))
        if older or (created and min(months_seen or {"9999"}) <= max(created, stop)):
            break
        time.sleep(0.15)
    # Profondeur RÉELLE : le plus ancien mois effectivement servi, jamais la date supposée.
    since = max(created or stop, stop, min(months_seen) if months_seen else stop)
    return incs, since


def sp_incidents_recent(dom):
    """Les 50 derniers incidents (API v2) — pour les pages sans historique publié."""
    j = get_json("https://%s/api/v2/incidents.json" % dom, timeout=30)
    out = []
    for i in j.get("incidents") or []:
        s = i.get("started_at") or i.get("created_at")
        e = i.get("resolved_at") or i.get("updated_at") or s
        if not s:
            continue
        d1, d2 = _p(s), _p(e)
        if not d1:
            continue
        out.append(dict(c=i.get("id"), t=d1.strftime("%Y-%m-%dT%H:%MZ"),
                        m=max(0, int(((d2 or d1) - d1).total_seconds() // 60)) if i.get("resolved_at") else None,
                        i=IMPACT_CODE.get(i.get("impact"), 0), n=(i.get("name") or "")[:140],
                        ouvert=(i.get("status") not in ("resolved", "postmortem"))))
    return out


def _p(s):
    if not s:
        return None
    try:
        if isinstance(s, (int, float)) or (isinstance(s, str) and s.isdigit()):
            return dt.datetime.fromtimestamp(int(s), dt.timezone.utc)
        s = s.replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def gcp_incidents():
    j = get_json("https://status.cloud.google.com/incidents.json", timeout=40)
    sev = {"high": 3, "medium": 2, "low": 1}
    out = []
    for x in j:
        d1, d2 = _p(x.get("begin")), _p(x.get("end"))
        if not d1:
            continue
        out.append(dict(c=x.get("id"), t=d1.strftime("%Y-%m-%dT%H:%MZ"),
                        m=int((d2 - d1).total_seconds() // 60) if d2 else None,
                        i=sev.get(x.get("severity"), 1),
                        n=((x.get("external_desc") or "").strip().split("\n")[0])[:140],
                        ouvert=d2 is None))
    return out


def aws_events():
    b = get("https://health.aws.amazon.com/public/currentevents", raw=True, timeout=40)
    try:
        txt = b.decode("utf-16")
    except Exception:
        txt = b.decode("utf-8", "replace")
    j = json.loads(txt or "[]")
    lvl = {"1": 1, "2": 2, "3": 3}
    out = []
    for e in j:
        d1 = _p(e.get("date"))
        if not d1:
            continue
        out.append(dict(c=e.get("arn") or (e.get("service") or "") + str(e.get("date")),
                        t=d1.strftime("%Y-%m-%dT%H:%MZ"), m=None, i=lvl.get(str(e.get("status")), 1),
                        n=("%s — %s (%s)" % (e.get("service_name") or "", e.get("summary") or "",
                                             e.get("region_name") or ""))[:140],
                        ouvert=True))
    return out


def apple_events():
    t = get("https://www.apple.com/support/systemstatus/data/system_status_en_US.js", timeout=30)
    t = t.strip()
    if not t.startswith("{"):
        t = t[t.find("{"):t.rfind("}") + 1]
    j = json.loads(t)
    out = []
    lvl = {"Outage": 2, "Issue": 1, "Performance": 1, "Maintenance": 0}

    def pd(s):
        if not s:
            return None
        m = re.match(r"(\d{2})/(\d{2})/(\d{4}) (\d{2}):(\d{2}) ([A-Z]{3})", s)
        if not m:
            return None
        d = dt.datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), int(m.group(4)), int(m.group(5)))
        return (d - dt.timedelta(hours=TZ.get(m.group(6), 0))).replace(tzinfo=dt.timezone.utc)
    for s in j.get("services") or []:
        for e in s.get("events") or []:
            d1, d2 = pd(e.get("startDate")), pd(e.get("endDate"))
            if not d1:
                continue
            out.append(dict(c="%s|%s" % (s.get("serviceName"), e.get("messageId")),
                            t=d1.strftime("%Y-%m-%dT%H:%MZ"),
                            m=int((d2 - d1).total_seconds() // 60) if d2 else None,
                            i=lvl.get(e.get("statusType"), 1),
                            n=("%s — %s" % (s.get("serviceName"), e.get("usersAffected") or ""))[:140],
                            ouvert=(e.get("eventStatus") != "resolved")))
    return out


def slack_events():
    j = get_json("https://slack-status.com/api/v2.0.0/history", timeout=30)
    out = []
    for x in j:
        d1, d2 = _p(x.get("date_created")), _p(x.get("date_updated"))
        if not d1 or x.get("type") == "notice":
            continue
        out.append(dict(c=x.get("id"), t=d1.strftime("%Y-%m-%dT%H:%MZ"),
                        m=int((d2 - d1).total_seconds() // 60) if (d2 and x.get("status") == "resolved") else None,
                        i=3 if x.get("type") == "outage" else 2, n=(x.get("title") or "")[:140],
                        ouvert=x.get("status") != "resolved"))
    return out


def fetch_one_service(svc, prev_acc):
    """Renvoie (incidents, depuis, mode). mode = 'historique' | 'accumulé'."""
    kind = svc.get("kind", "sp")
    if kind == "sp" and svc.get("hist"):
        incs, since = sp_history(svc)
        return incs, since, "historique"
    if kind == "gcp":
        new = gcp_incidents()
    elif kind == "aws":
        new = aws_events()
    elif kind == "apple":
        new = apple_events()
    elif kind == "slack":
        new = slack_events()
    else:
        new = sp_incidents_recent(svc["dom"])
    # Accumulation : l'archive du cache précédent + ce qu'on voit maintenant (même identifiant =
    # la version la plus récente l'emporte, elle porte la fin de l'incident).
    old = {str(x.get("c")): x for x in (prev_acc.get(svc["id"]) or {}).get("inc", [])}
    for x in new:
        old[str(x.get("c"))] = x
    incs = sorted(old.values(), key=lambda x: x["t"])
    since = (prev_acc.get(svc["id"]) or {}).get("since")
    if not since:
        since = min([x["t"][:7] for x in new] + [NOW.strftime("%Y-%m")])
    return incs, since, "accumulé"


def build_outages(prev):
    prev_acc = (prev.get("pannes") or {}).get("acc") or {}
    months = month_list(START_MONTH)
    res = {}

    def job(s):
        try:
            return s["id"], fetch_one_service(s, prev_acc), None
        except Exception as e:
            return s["id"], None, str(e)
    with cf.ThreadPoolExecutor(6) as ex:
        for sid, r, err in ex.map(job, SERVICES):
            if err:
                warn("panne %s : %s" % (sid, err))
                # repli : ce que le cache précédent savait (jamais un service qui disparaît)
                po = ((prev.get("pannes") or {}).get("par_service") or {}).get(sid)
                if po:
                    res[sid] = ("prev", po)
                continue
            res[sid] = ("ok", r)

    par_service, acc, top, recents = {}, {}, [], []
    cal = collections.defaultdict(lambda: [0, 0, 0])      # jour → [mineurs, majeurs, critiques]
    cal_svc = collections.defaultdict(set)
    jours_maj = {}                                        # service → jours où une panne majeure ou critique a été déclarée
    propres = set()                                       # services à historique complet, hors « bruit »
    lim12 = (NOW - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    for s in SERVICES:
        sid = s["id"]
        if sid not in res:
            continue
        st, r = res[sid]
        if st == "prev":
            par_service[sid] = dict(r, perime=True)
            continue
        incs, since, mode = r
        propre = mode == "historique" and not s.get("bruit")
        if propre:
            propres.add(sid)
        jm = set()
        if mode == "accumulé":
            acc[sid] = dict(since=since, inc=incs[-400:])
        per = {m: [0, 0, 0, 0] for m in months}         # mineurs, majeurs, critiques, heures majeures+
        for x in incs:
            ym = x["t"][:7]
            if x["i"] >= 1 and ym in per:
                per[ym][x["i"] - 1] += 1
                if x["i"] >= 2 and x.get("m"):
                    per[ym][3] += round(min(x["m"], 72 * 60) / 60.0, 2)
            if x["i"] >= 2:
                jm.add(x["t"][:10])
            if x["i"] >= 1 and propre:
                d = x["t"][:10]
                cal[d][x["i"] - 1] += 1
                if x["i"] >= 2:
                    cal_svc[d].add(sid)
            if x["i"] >= 2 and not s.get("bruit"):
                top.append(dict(s=sid, t=x["t"], m=x.get("m"), i=x["i"], n=x.get("n", "")))
            if x["t"] >= (NOW - dt.timedelta(days=45)).strftime("%Y-%m-%d") and x["i"] >= 1:
                recents.append(dict(s=sid, t=x["t"], m=x.get("m"), i=x["i"], n=x.get("n", ""),
                                    o=bool(x.get("ouvert"))))
        first = since if since > START_MONTH else START_MONTH
        jours_maj[sid] = jm
        jpm = collections.Counter(d[:7] for d in jm)
        par_service[sid] = dict(
            depuis=first, mode=mode, bruit=bool(s.get("bruit")),
            jours=[jpm.get(m, 0) if m >= first else None for m in months],
            j12=sum(1 for d in jm if d >= lim12),
            mineurs=[per[m][0] if m >= first else None for m in months],
            majeurs=[per[m][1] if m >= first else None for m in months],
            critiques=[per[m][2] if m >= first else None for m in months],
            heures=[round(per[m][3], 1) if m >= first else None for m in months],
            n12=sum(per[m][1] + per[m][2] for m in months[-12:] if m >= first),
            h12=round(sum(per[m][3] for m in months[-12:] if m >= first), 1))

    # ── INDICE ROBUSTE AUX HABITUDES DE DÉCLARATION ─────────────────────────────
    # Chaque entreprise a sa propre échelle de gravité (Ledger : 200 à 780 « majeurs » par an,
    # Coinbase : de 47 à 1 sans changer de métier). Additionner leurs incidents mesurerait leurs
    # manières de déclarer. On compte donc des SERVICES et des JOURS : chaque service pèse pour un.
    #   touches : part du panel qui a déclaré au moins une panne majeure ou critique dans le mois
    #   simult  : jours du mois où au moins 3 services du panel étaient en panne majeure le même jour
    # Panel CONSTANT : les services propres qui publient depuis janvier 2021 au plus tard.
    panel = sorted(sid for sid in propres if (par_service.get(sid) or {}).get("depuis", "9999") <= "2021-01")
    jour_n = collections.Counter()
    for sid in panel:
        for d in jours_maj.get(sid, ()):
            jour_n[d] += 1
    cur = NOW.strftime("%Y-%m")
    touches, simult, simult2 = [], [], []
    for m in months:
        if m >= cur or not panel:
            touches.append(None); simult.append(None); simult2.append(None)
            continue
        t = sum(1 for sid in panel if any(d[:7] == m for d in jours_maj.get(sid, ())))
        touches.append(round(100.0 * t / len(panel), 1))
        simult.append(sum(1 for d, n in jour_n.items() if d[:7] == m and n >= 3))
        simult2.append(sum(1 for d, n in jour_n.items() if d[:7] == m and n >= 2))
    pires = sorted(((d, n) for d, n in jour_n.items()), key=lambda x: (-x[1], x[0]), reverse=False)[:15]
    indice = dict(panel=panel, touches=touches, simult=simult, simult2=simult2,
                  pires=[[d, n, sorted(sid for sid in panel if d in jours_maj.get(sid, ()))] for d, n in pires])

    # Calendrier des 2 dernières années (jours noirs) — services propres seulement
    d0 = (NOW - dt.timedelta(days=730)).date()
    days, cmin, cmaj, ccrit, nsvc = [], [], [], [], []
    d = d0
    while d <= NOW.date():
        k = d.isoformat()
        v = cal.get(k, [0, 0, 0])
        days.append(k)
        cmin.append(v[0])
        cmaj.append(v[1])
        ccrit.append(v[2])
        nsvc.append(len(cal_svc.get(k, ())))
        d += dt.timedelta(days=1)
    top.sort(key=lambda x: (-(x["m"] or 0) * (1.6 if x["i"] == 3 else 1)))
    recents.sort(key=lambda x: x["t"], reverse=True)
    svc_meta = [dict(id=s["id"], nom=s["nom"], sec=s["sec"],
                     url=s.get("url") or ("https://" + s["dom"] + "/"),
                     dom=s.get("dom"), kind=s.get("kind", "sp"), hist=bool(s.get("hist")), bruit=bool(s.get("bruit")))
                for s in SERVICES]
    return dict(mois=months, services=svc_meta, par_service=par_service, acc=acc, indice=indice,
                propres=sorted(propres),
                cal=dict(jours=days, mineurs=cmin, majeurs=cmaj, critiques=ccrit, services=nsvc,
                         qui={k: sorted(v) for k, v in cal_svc.items() if k >= d0.isoformat()}),
                plus_longues=top[:40], recents=recents[:80])


# ═══════════════════════════════════════════════════════════════════════════════
# 2 · RANÇONGICIELS — ransomware.live
# ═══════════════════════════════════════════════════════════════════════════════
def build_ransom(prev):
    pr = prev.get("rancon") or {}
    old = {m: v for m, v in (pr.get("par_mois") or {}).items()}
    months = month_list(START_MONTH)
    # Les mois récents se remplissent encore : on les refait à chaque passage.
    frais = set(months[-(PROVISOIRE_MOIS + 2):])
    todo = [m for m in months if m in frais or m not in old]
    todo = sorted(todo, key=lambda m: (m not in frais, m), reverse=False)
    # d'abord les mois frais, puis le rattrapage, borné
    todo = [m for m in todo if m in frais] + [m for m in todo if m not in frais][-MAX_RL_MONTHS:]
    for ym in todo:
        y, mo = ym.split("-")
        try:
            v = get_json("https://api.ransomware.live/v2/victims/%s/%s" % (y, mo), timeout=90)
        except Exception as e:
            warn("ransomware.live %s : %s" % (ym, e))
            continue
        c, s, g = collections.Counter(), collections.Counter(), collections.Counter()
        n = 0
        for x in v or []:
            ad = (x.get("attackdate") or x.get("discovered") or "")[:7]
            if ad and ad != ym:
                continue                  # on range par mois d'ATTAQUE, comme la source
            n += 1
            c[(x.get("country") or "").upper()[:2] or "?"] += 1
            s[(x.get("activity") or "").strip() or "?"] += 1
            g[(x.get("group") or "").strip().lower() or "?"] += 1
        old[ym] = dict(n=n, c=dict(c), s=dict(s), g=dict(g.most_common(25)))
        time.sleep(0.6)
    par_mois = {m: old[m] for m in months if m in old}
    # Dernières 24 h / 7 j — compte seulement, jamais les noms
    recent = {}
    try:
        rv = get_json("https://api.ransomware.live/v2/recentvictims", timeout=60)
        lim24 = (NOW - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        r24 = [x for x in rv if (x.get("discovered") or "")[:16] >= lim24]
        recent = dict(n24=len(r24),
                      groupes24=collections.Counter((x.get("group") or "?").lower() for x in r24).most_common(8),
                      pays24=collections.Counter((x.get("country") or "?").upper() for x in r24).most_common(8),
                      secteurs24=collections.Counter((x.get("activity") or "?") for x in r24).most_common(8),
                      derniers=[dict(t=(x.get("discovered") or "")[:16], g=x.get("group"),
                                     p=(x.get("country") or "").upper(), s=x.get("activity"))
                                for x in rv[:25]])
    except Exception as e:
        warn("ransomware.live récents : %s" % e)
    try:
        info = get_json("https://api.ransomware.live/v2/info", timeout=30)
    except Exception:
        info = pr.get("info") or {}
    return dict(mois=months, par_mois=par_mois, provisoire_depuis=months[-PROVISOIRE_MOIS],
                recent=recent or pr.get("recent") or {}, info=info)


# ═══════════════════════════════════════════════════════════════════════════════
# 3 · CRYPTO — DefiLlama hacks
# ═══════════════════════════════════════════════════════════════════════════════
def build_crypto(prev):
    try:
        h = get_json("https://api.llama.fi/hacks", timeout=60)
    except Exception as e:
        warn("DefiLlama : %s" % e)
        return prev.get("crypto")
    rows = []
    for x in h:
        if not x.get("date"):
            continue
        rows.append(dict(d=iso(x["date"]), n=(x.get("name") or "")[:60],
                         a=x.get("amount") if isinstance(x.get("amount"), (int, float)) else None,
                         k=x.get("classification") or "", q=x.get("technique") or "",
                         c=x.get("targetType") or "", ch=(x.get("chain") or [])[:3],
                         r=x.get("returnedFunds") if isinstance(x.get("returnedFunds"), (int, float)) else None,
                         b=bool(x.get("bridgeHack"))))
    rows.sort(key=lambda r: r["d"])
    months = month_list("2016-01")
    nm = collections.Counter()
    am = collections.defaultdict(float)
    for r in rows:
        ym = r["d"][:7]
        if ym in months or ym >= "2016-01":
            nm[ym] += 1
            am[ym] += r["a"] or 0
    return dict(mois=months, n=[nm.get(m, 0) for m in months], usd=[round(am.get(m, 0)) for m in months],
                vols=[[r["d"], r["n"], r["a"], r["k"], r["q"], r["c"], r["ch"], r["r"], 1 if r["b"] else 0] for r in rows],
                total=len(rows))


# ═══════════════════════════════════════════════════════════════════════════════
# 4 · FUITES — Have I Been Pwned
# ═══════════════════════════════════════════════════════════════════════════════
def build_breaches(prev):
    try:
        b = get_json("https://haveibeenpwned.com/api/v3/breaches", timeout=60)
    except Exception as e:
        warn("HIBP : %s" % e)
        return prev.get("fuites")
    rows = []
    for x in b:
        if x.get("IsFabricated") or x.get("IsSpamList"):
            continue                       # fausses fuites et listes de spam : hors du décompte
        rows.append([x.get("BreachDate") or "", (x.get("AddedDate") or "")[:10], x.get("Title") or x.get("Name"),
                     x.get("Domain") or "", int(x.get("PwnCount") or 0),
                     1 if (x.get("IsStealerLog") or x.get("IsMalware")) else 0,
                     1 if x.get("IsVerified") else 0, 1 if x.get("IsSensitive") else 0,
                     (x.get("DataClasses") or [])[:12]])
    rows.sort(key=lambda r: r[1])
    return dict(lignes=rows, total=len(rows),
                ecartees=sum(1 for x in b if x.get("IsFabricated") or x.get("IsSpamList")))


# ═══════════════════════════════════════════════════════════════════════════════
# 5 · FAILLES — CISA KEV + NIST NVD
# ═══════════════════════════════════════════════════════════════════════════════
def build_kev(prev):
    try:
        k = get_json("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json", timeout=60)
    except Exception as e:
        warn("CISA KEV : %s" % e)
        return prev.get("kev")
    v = k.get("vulnerabilities") or []
    rows = [[x.get("dateAdded"), x.get("cveID"), x.get("vendorProject"), x.get("product"),
             (x.get("vulnerabilityName") or "")[:110], 1 if x.get("knownRansomwareCampaignUse") == "Known" else 0]
            for x in v]
    rows.sort(key=lambda r: r[0] or "")
    return dict(lignes=rows, total=len(rows), version=k.get("catalogVersion"), publie=k.get("dateReleased"))


def build_nvd(prev):
    pn = prev.get("nvd") or {}
    old = dict(pn.get("par_mois") or {})
    months = month_list(NVD_START)
    frais = set(months[-3:])
    todo = [m for m in months if m in frais] + [m for m in months if m not in old and m not in frais]
    todo = todo[:MAX_NVD_REQ]
    for i, ym in enumerate(todo):
        y, mo = map(int, ym.split("-"))
        last = calendar.monthrange(y, mo)[1]
        url = ("https://services.nvd.nist.gov/rest/json/cves/2.0?noRejected&resultsPerPage=1"
               "&pubStartDate=%04d-%02d-01T00:00:00.000&pubEndDate=%04d-%02d-%02dT23:59:59.999" % (y, mo, y, mo, last))
        try:
            j = get_json(url, timeout=60, tries=4)
            old[ym] = int(j.get("totalResults"))
        except Exception as e:
            warn("NVD %s : %s" % (ym, e))
        if i < len(todo) - 1:
            time.sleep(6.5)            # 5 requêtes par 30 s sans clé
    return dict(mois=months, par_mois={m: old[m] for m in months if m in old}, en_cours=NOW.strftime("%Y-%m"))


# ═══════════════════════════════════════════════════════════════════════════════
# 6 · BROUILLAGE GPS — gpsjam.org
# ═══════════════════════════════════════════════════════════════════════════════
def build_gps(prev):
    try:
        t = get("https://gpsjam.org/data/manifest.csv", timeout=40)
    except Exception as e:
        warn("GPSJam : %s" % e)
        return prev.get("gps")
    rows = []
    for ln in t.strip().splitlines()[1:]:
        p = ln.split(",")
        if len(p) < 4:
            continue
        try:
            rows.append([p[0], int(p[2]), 1 if p[1] == "true" else 0, p[3]])
        except ValueError:
            continue
    changes = []
    prv = None
    for r in rows:
        if r[3] != prv:
            changes.append([r[0], r[3]])
            prv = r[3]
    return dict(jours=[r[0] for r in rows], hex=[r[1] for r in rows], suspects=[r[0] for r in rows if r[2]],
                sources=changes)


# ═══════════════════════════════════════════════════════════════════════════════
# 7 · SÉRIES ANNUELLES ET ÉVÉNEMENTS — saisis à la parution, chacun avec sa source
# ═══════════════════════════════════════════════════════════════════════════════
ANNUEL = {
    "ic3": dict(
        titre="Pertes déclarées au FBI (plaintes pour cybercriminalité)",
        source="FBI — Internet Crime Complaint Center, rapports annuels", url="https://www.ic3.gov/AnnualReport/Reports",
        parution="avril", unite="Md$",
        annees=[2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        pertes=[1.07, 1.45, 1.42, 2.71, 3.5, 4.2, 6.9, 10.3, 12.5, 16.6, 20.88],
        plaintes=[288012, 298728, 301580, 351937, 467361, 791790, 847376, 800944, 880418, 859532, 1008597],
        note="Ce que les victimes ont DÉCLARÉ, surtout aux États-Unis : un plancher, pas le coût total. "
             "2025 : 20,877 Md$ et 1 008 597 plaintes, premier exercice au-dessus du million."),
    "ibm": dict(
        titre="Coût moyen d'une fuite de données pour l'entreprise touchée",
        source="IBM / Ponemon Institute — Cost of a Data Breach Report",
        url="https://www.ibm.com/reports/data-breach", parution="juillet", unite="M$",
        annees=[2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025],
        cout=[3.62, 3.86, 3.92, 3.86, 4.24, 4.35, 4.45, 4.88, 4.44],
        note="Moyenne mondiale par incident, sur un échantillon d'entreprises interrogées (~600 par an). "
             "Aux États-Unis, 10,22 M$ en 2025."),
}

EVENEMENTS = [
    # date, catégorie, titre, ce qui s'est passé (court), chiffre, source, url
    ("2017-05-12", "rancon", "WannaCry", "Un ver rançongiciel paralyse hôpitaux britanniques, usines et opérateurs dans 150 pays.",
     "plus de 200 000 ordinateurs", "Europol", "https://www.europol.europa.eu/"),
    ("2017-06-27", "attaque", "NotPetya", "Un faux rançongiciel, lancé via un logiciel comptable ukrainien, détruit les systèmes de Maersk, Merck, Saint-Gobain.",
     "≈ 10 Md$ de dégâts (estimation de la Maison-Blanche)", "Wired (T. Bossert, ex-conseiller de la Maison-Blanche)", "https://www.wired.com/story/notpetya-cyberattack-ukraine-russia-code-crashed-the-world/"),
    ("2017-09-07", "fuite", "Equifax", "L'agence de crédit américaine révèle le vol des données de crédit de la moitié des adultes américains.",
     "147 millions de personnes", "FTC", "https://www.ftc.gov/enforcement/refunds/equifax-data-breach-settlement"),
    ("2020-12-13", "attaque", "SolarWinds", "Une mise à jour piégée d'un logiciel de supervision ouvre les réseaux d'administrations américaines.",
     "≈ 18 000 clients ont reçu la mise à jour", "CISA", "https://www.cisa.gov/news-events/cybersecurity-advisories/aa20-352a"),
    ("2021-05-07", "rancon", "Colonial Pipeline", "Le premier oléoduc de carburant de la côte Est américaine s'arrête six jours.",
     "4,4 M$ de rançon payés", "Sénat américain (audition)", "https://www.hsgac.senate.gov/"),
    ("2021-10-04", "panne", "Facebook, Instagram, WhatsApp", "Une erreur de configuration du routage efface Meta d'Internet.",
     "≈ 6 heures", "Meta (rapport d'incident)", "https://engineering.fb.com/2021/10/05/networking-traffic/outage-details/"),
    ("2022-03-23", "crypto", "Ronin (Axie Infinity)", "Le pont de la blockchain du jeu est vidé par le groupe nord-coréen Lazarus.",
     "≈ 625 M$", "FBI", "https://www.fbi.gov/news/press-releases/fbi-statement-on-attribution-of-malicious-cyber-activity-posed-by-the-democratic-peoples-republic-of-korea"),
    ("2023-01-11", "panne", "FAA — NOTAM", "La panne du système d'avis aux navigants immobilise tous les départs aux États-Unis.",
     "premier arrêt national depuis le 11-Septembre", "FAA", "https://www.faa.gov/"),
    ("2023-05-31", "fuite", "MOVEit", "Le groupe Cl0p exploite une faille du logiciel de transfert de fichiers MOVEit chez des milliers d'organisations.",
     "plus de 2 700 organisations touchées", "Emsisoft ; CISA", "https://www.cisa.gov/news-events/cybersecurity-advisories/aa23-158a"),
    ("2024-02-21", "rancon", "Change Healthcare", "Le rançongiciel bloque le traitement des ordonnances et remboursements aux États-Unis pendant des semaines.",
     "≈ 190 millions de personnes", "HHS (OCR)", "https://www.hhs.gov/hipaa/for-professionals/special-topics/change-healthcare-cybersecurity-incident-frequently-asked-questions/index.html"),
    ("2024-07-19", "panne", "CrowdStrike", "Une mise à jour défectueuse d'un antivirus fait planter Windows dans les aéroports, banques et hôpitaux.",
     "8,5 millions d'ordinateurs ; ≈ 5,4 Md$ de pertes pour le Fortune 500", "Microsoft ; Parametrix",
     "https://blogs.microsoft.com/blog/2024/07/20/helping-our-customers-through-the-crowdstrike-outage/"),
    ("2025-02-21", "crypto", "Bybit", "La plateforme d'échange est vidée de son portefeuille froid d'ether, via la tromperie de ses signataires.",
     "≈ 1,46 Md$", "FBI", "https://www.ic3.gov/PSA/2025/PSA250226"),
    ("2025-04-28", "panne", "Panne d'électricité ibérique", "L'Espagne et le Portugal perdent tout leur réseau électrique en quelques secondes.",
     "≈ 10 heures de coupure", "ENTSO-E", "https://www.entsoe.eu/"),
    ("2025-04-22", "attaque", "Marks & Spencer", "Le distributeur britannique suspend sa vente en ligne pendant sept semaines.",
     "≈ 300 M£ d'impact sur le résultat", "Marks & Spencer", "https://corporate.marksandspencer.com/"),
    ("2025-09-01", "attaque", "Jaguar Land Rover", "Les usines du constructeur s'arrêtent cinq semaines ; les sous-traitants vacillent.",
     "≈ 1,9 Md£ pour l'économie britannique", "Cyber Monitoring Centre", "https://cybermonitoringcentre.com/"),
    ("2025-09-19", "rancon", "Collins Aerospace (aéroports)", "Le logiciel d'enregistrement des passagers tombe à Heathrow, Bruxelles, Berlin.",
     "enregistrement manuel plusieurs jours", "ENISA", "https://www.enisa.europa.eu/"),
    ("2025-10-20", "panne", "Amazon Web Services (us-east-1)", "Une erreur de résolution de noms paralyse la première région du nuage d'Amazon.",
     "≈ 15 heures", "AWS", "https://aws.amazon.com/message/101925/"),
    ("2025-11-18", "panne", "Cloudflare", "Un fichier de configuration trop gros fait tomber une partie du Web (X, ChatGPT…).",
     "≈ 3 heures", "Cloudflare", "https://blog.cloudflare.com/18-november-2025-outage/"),
    ("2026-10-29", "crypto", "Binance", "Le premier échange mondial est vidé sur dix chaînes en une vingtaine de minutes, via l'ordinateur d'un ingénieur.",
     "≈ 6,3 Md$, plus gros vol de l'histoire", "Binance ; FBI (attribution à la Corée du Nord)",
     "https://www.cnbc.com/2026/10/30/billions-in-crypto-frozen-in-binance-hack-heres-what-happens-next-.html"),
    ("2026-11-12", "panne", "Microsoft 365 (Outlook, Teams…)", "Un défaut introduit lors d'une augmentation de capacité réseau coupe la messagerie de banques, d'États et de tribunaux.",
     "74 pays, trois jours", "Al Jazeera ; Microsoft",
     "https://www.aljazeera.com/news/2026/11/13/microsoft-hit-by-global-outages-what-happened-and-why"),
    ("2026-11-13", "panne", "FAA — arrêt national des vols", "Un routeur neuf sature le réseau de télécommunications de la FAA : radars et liaisons décrochent.",
     "≈ 12 460 vols annulés (FAA)", "FAA", "https://www.faa.gov/newsroom/nationwide-ground-stop-questions-and-answers"),
    ("2026-11-16", "panne", "Banques américaines (Chase…)", "Paiements et applications bloqués ; Chase invoque un bogue logiciel, sans indice de cyberattaque.",
     "pic de ≈ 4 800 signalements pour Chase", "IBTimes (d'après AP)",
     "https://www.ibtimes.co.uk/us-bank-outages-chase-software-bug-cyberattack-fears-1832680"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# 8 · L'INSTANT — mode --live
# ═══════════════════════════════════════════════════════════════════════════════
def faa_status():
    t = get("https://nasstatus.faa.gov/api/airport-status-information", timeout=30)
    root = ET.fromstring(t)
    out = dict(maj=(root.findtext("Update_Time") or ""), arrets=[], programmes=[], retards=[], fermetures=[])
    for dtp in root.findall("Delay_type"):
        nom = dtp.findtext("Name") or ""
        if "Ground Stop" in nom:
            for p in dtp.iter("Program"):
                out["arrets"].append(dict(a=p.findtext("ARPT"), r=p.findtext("Reason"), fin=p.findtext("End_Time")))
        elif "Ground Delay" in nom:
            for p in dtp.iter("Ground_Delay"):
                out["programmes"].append(dict(a=p.findtext("ARPT"), r=p.findtext("Reason"), moy=p.findtext("Avg"),
                                              max=p.findtext("Max")))
        elif "Arrival/Departure" in nom:
            for p in dtp.iter("Delay"):
                ad = p.find("Arrival_Departure")
                out["retards"].append(dict(a=p.findtext("ARPT"), r=p.findtext("Reason"),
                                           type=ad.get("Type") if ad is not None else "",
                                           min=ad.findtext("Min") if ad is not None else "",
                                           max=ad.findtext("Max") if ad is not None else ""))
        elif "Closure" in nom:
            for p in dtp.iter("Airport"):
                r = p.findtext("Reason") or ""
                # Les fermetures « aux avions privés non programmés » ne sont pas des pannes.
                if "NON SKED" in r or "TRANSIENT" in r:
                    continue
                out["fermetures"].append(dict(a=p.findtext("ARPT"), r=r[:120], reouv=p.findtext("Reopen")))
    return out


def sp_summary(dom):
    j = get_json("https://%s/api/v2/summary.json" % dom, timeout=25)
    st = (j.get("status") or {}).get("indicator") or "none"
    deg = [c.get("name") for c in (j.get("components") or [])
           if c.get("status") not in ("operational", None) and not c.get("group")][:6]
    inc = [dict(n=i.get("name"), i=i.get("impact"), t=i.get("started_at") or i.get("created_at"))
           for i in (j.get("incidents") or [])][:3]
    return dict(st=st, deg=deg, inc=inc)


def build_live(prev_live):
    t0 = time.time()
    etats = {}

    def job(s):
        try:
            k = s.get("kind", "sp")
            if k == "sp":
                return s["id"], sp_summary(s["dom"])
            if k == "gcp":
                ev = [x for x in gcp_incidents() if x.get("ouvert")]
            elif k == "aws":
                ev = [x for x in aws_events() if x.get("ouvert")]
            elif k == "apple":
                ev = [x for x in apple_events() if x.get("ouvert")]
            else:
                ev = [x for x in slack_events() if x.get("ouvert")]
            mx = max([x["i"] for x in ev] + [0])
            return s["id"], dict(st=["none", "minor", "major", "critical"][mx],
                                 deg=[], inc=[dict(n=x["n"], i=["none", "minor", "major", "critical"][x["i"]], t=x["t"])
                                              for x in ev[:3]])
        except Exception as e:
            return s["id"], dict(st="inconnu", err=str(e)[:120])
    with cf.ThreadPoolExecutor(6) as ex:
        for sid, v in ex.map(job, SERVICES):
            etats[sid] = v
    # Journal des changements d'état sur 14 jours (fait l'historique « au fil de l'eau »)
    journal = [x for x in (prev_live.get("journal") or [])
               if x[0] >= (NOW - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M")]
    dernier = {}
    for x in journal:
        dernier[x[1]] = x[2]
    stamp = NOW.strftime("%Y-%m-%dT%H:%MZ")
    for sid, v in etats.items():
        if v.get("st") == "inconnu":
            continue
        if dernier.get(sid) != v["st"]:
            journal.append([stamp, sid, v["st"]])
    faa = None
    try:
        faa = faa_status()
    except Exception as e:
        warn("FAA : %s" % e)
    rec = {}
    try:
        rv = get_json("https://api.ransomware.live/v2/recentvictims", timeout=40)
        lim = (NOW - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
        r24 = [x for x in rv if (x.get("discovered") or "")[:16] >= lim]
        rec = dict(n24=len(r24), groupes=collections.Counter((x.get("group") or "?").lower() for x in r24).most_common(5),
                   pays=collections.Counter((x.get("country") or "?").upper() for x in r24).most_common(6))
    except Exception as e:
        warn("rançongiciels récents : %s" % e)
    return dict(meta=dict(built_at=stamp, duree_s=round(time.time() - t0, 1), erreurs=ERRORS[:20]),
                etats=etats, journal=journal[-1500:], faa=faa, rancon24=rec)


# ═══════════════════════════════════════════════════════════════════════════════
def main():
    live = "--live" in sys.argv[1:]
    if live:
        prev_live = load_prev("atlas_cyber_live")
        out = build_live(prev_live)
        ok = sum(1 for v in out["etats"].values() if v.get("st") != "inconnu")
        if ok < len(SERVICES) // 2:
            raise SystemExit("[cyber] moins de la moitié des services lisibles (%d/%d) : cache gardé" % (ok, len(SERVICES)))
        write_out("atlas_cyber_live", "__ATLAS_CYBER_LIVE__", out, "Cyber & pannes — l'instant (pages de statut, FAA)")
        return
    t0 = time.time()
    prev = load_prev("atlas_cyber_cache")
    blocs = {}
    for nom, fn in (("pannes", build_outages), ("rancon", build_ransom), ("crypto", build_crypto),
                    ("fuites", build_breaches), ("kev", build_kev), ("nvd", build_nvd), ("gps", build_gps)):
        t = time.time()
        try:
            blocs[nom] = fn(prev)
        except Exception as e:
            warn("bloc %s : %s" % (nom, e))
            blocs[nom] = prev.get(nom)
        log("%s en %.0f s" % (nom, time.time() - t))
    # Contrôles avant publication : un bloc vide n'écrase jamais un bloc plein.
    for nom in list(blocs):
        if not blocs[nom] and prev.get(nom):
            warn("bloc %s vide : celui du passage précédent est gardé" % nom)
            blocs[nom] = prev[nom]
    pn = blocs.get("pannes") or {}
    n_hist = sum(1 for v in (pn.get("par_service") or {}).values() if v.get("mode") == "historique")
    if n_hist < 10 and prev.get("pannes"):
        warn("pannes : %d historiques seulement, bloc précédent gardé" % n_hist)
        blocs["pannes"] = prev["pannes"]
    out = dict(meta=dict(built_at=NOW.strftime("%Y-%m-%dT%H:%MZ"), duree_s=round(time.time() - t0),
                         erreurs=ERRORS[:40], version=1),
               annuel=ANNUEL,
               evenements=[dict(d=e[0], cat=e[1], t=e[2], q=e[3], v=e[4], src=e[5], url=e[6]) for e in EVENEMENTS],
               **blocs)
    write_out("atlas_cyber_cache", "__ATLAS_CYBER__", out,
              "Cyber & pannes (pages de statut · ransomware.live · DefiLlama · HIBP · CISA · NVD · GPSJam)")
    log("terminé en %.0f s, %d avertissements" % (time.time() - t0, len(ERRORS)))


if __name__ == "__main__":
    main()
