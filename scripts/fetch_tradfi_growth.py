#!/usr/bin/env python3
"""Fetch Revenue Growth YoY pour les tickers TradFi via yfinance.
Calcule depuis les revenus trimestriels : (revenue_4Q_last - revenue_4Q_prev) / revenue_4Q_prev.
Injecte dans Comparaison_PER_Crypto_TradFi.html via markers __TRADFI_GROWTH__.
"""
import yfinance as yf
import json, sys, warnings, re
from pathlib import Path
from datetime import datetime

warnings.filterwarnings('ignore')

CACHES_DIR = Path.home() / "Library/Caches/site_crypto_finance"
CACHES_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = CACHES_DIR / "tradfi_growth_cache.json"
HTML_FILE  = Path.home() / "Desktop/Site_Crypto_Finance/Comparaison_PER_Crypto_TradFi.html"
CACHE_MAX_HOURS = 12

TICKERS = {
    # Mapping affichage → ticker yfinance
    'Apple': 'AAPL', 'NVIDIA': 'NVDA', 'Microsoft': 'MSFT',
    'Tesla': 'TSLA', 'Alphabet': 'GOOGL', 'Meta': 'META',
    'Amazon': 'AMZN',
    'Alibaba': 'BABA', 'Tencent': 'TCEHY', 'Baidu': 'BIDU'
}

def compute_growth(ticker):
    """Revenue Growth sur 12 mois glissants. Retourne (valeur, methode).

    ORDRE DES VOIES — il a son importance, il a ete paye par un defaut muet.

    Mesure du 01/09/2026 : yfinance ne rend plus que 5 trimestres par societe.
    La voie trimestrielle exige `len(rev) >= 8` (deux fenetres de 4) : elle
    echouait donc pour LES DIX societes, en silence. Le repli annuel prenait le
    relais — mais il compare deux exercices CLOS, pas 12 mois glissants :

        Apple  annuel  6,4 %   TTM reel  16,4 %
        NVIDIA annuel 65,5 %   TTM reel 105,9 %

    La page affichait ces chiffres sous le libelle « Revenue Growth (YoY) » en
    citant « rapports trimestriels 10-Q » comme source. Le chiffre etait vieux
    d'un exercice, et sa provenance affichee etait fausse.

    On place donc `info.revenueGrowth` (un vrai TTM cote Yahoo) AVANT l'annuel,
    et on renvoie la methode employee pour que l'ecran puisse la dire.
    """
    try:
        t = yf.Ticker(ticker)
        # 1. Trimestriel maison : la seule voie entierement verifiable.
        qfin = t.quarterly_financials
        if qfin is not None and 'Total Revenue' in qfin.index:
            rev = qfin.loc['Total Revenue'].dropna().sort_index(ascending=False)
            if len(rev) >= 8:
                last4 = rev.iloc[:4].sum()
                prev4 = rev.iloc[4:8].sum()
                if prev4 > 0:
                    return round(100 * (last4 - prev4) / prev4, 1), 'trimestriel'
        # 2. TTM de Yahoo : 12 mois glissants, la bonne grandeur.
        try:
            g = t.info.get('revenueGrowth')
            if g is not None:
                return round(100 * float(g), 1), 'ttm'
        except Exception:
            pass
        # 3. Annuel : deux exercices CLOS. Juste, mais perime — donc etiquete.
        afin = t.financials
        if afin is not None and 'Total Revenue' in afin.index:
            rev = afin.loc['Total Revenue'].dropna().sort_index(ascending=False)
            if len(rev) >= 2 and rev.iloc[1] > 0:
                return round(100 * (rev.iloc[0] - rev.iloc[1]) / rev.iloc[1], 1), 'annuel'
    except Exception as e:
        sys.stderr.write(f'{ticker} err: {e}\n')
    return None, None

def fetch():
    if CACHE_FILE.exists():
        age_h = (datetime.now().timestamp() - CACHE_FILE.stat().st_mtime) / 3600
        if age_h < CACHE_MAX_HOURS:
            sys.stderr.write(f'[TradFi Growth] Cache fresh ({age_h:.1f}h)\n')
            payload = json.load(open(CACHE_FILE))
            inject_into_html(payload)
            return payload

    sys.stderr.write('[TradFi Growth] Fetching live data...\n')
    result = {}
    methodes = {}
    for name, ticker in TICKERS.items():
        g, methode = compute_growth(ticker)
        if g is not None:
            result[name] = g
            methodes[name] = methode
            sys.stderr.write(f'{name} ({ticker}): {g}% [{methode}]\n')
        else:
            sys.stderr.write(f'{name} ({ticker}): no growth data\n')

    # ── GARDE D'APPAUVRISSEMENT ───────────────────────────────────────────────
    # Le cache publie le 01/09/2026 ne contenait qu'UNE societe (Apple) alors que
    # la collecte en vise dix : une collecte partiellement en echec avait ecrase
    # un cache complet, sans un mot. Un fichier ecrit est pris pour un succes.
    # On refuse desormais d'ecrire un cache nettement plus pauvre que celui qu'on
    # remplace : mieux vaut servir la donnee d'hier, entiere, que celle
    # d'aujourd'hui, amputee.
    ancien = {}
    if CACHE_FILE.exists():
        try:
            ancien = (json.load(open(CACHE_FILE)) or {}).get('data', {}) or {}
        except Exception:
            ancien = {}
    if ancien and len(result) < len(ancien) * 0.8:
        sys.stderr.write(
            f'[TradFi Growth] REFUS : {len(result)} societes collectees contre '
            f'{len(ancien)} dans le cache existant. Cache conserve, rien ecrit.\n')
        payload = json.load(open(CACHE_FILE))
        inject_into_html(payload)
        return payload

    payload = {'updated': datetime.now().isoformat(), 'data': result,
               'methodes': methodes}
    with open(CACHE_FILE, 'w') as f:
        json.dump(payload, f)
    sys.stderr.write(f'[TradFi Growth] Wrote {len(result)} tickers\n')
    inject_into_html(payload)
    return payload

def inject_into_html(payload):
    try:
        if not HTML_FILE.exists():
            sys.stderr.write(f'[TradFi Growth] {HTML_FILE} introuvable\n'); return
        html = HTML_FILE.read_text()
        new_block = (
            "// __TRADFI_GROWTH_START__\n"
            "window.__TRADFI_GROWTH__ = " + json.dumps(payload, separators=(',',':')) + ";\n"
            "// __TRADFI_GROWTH_END__"
        )
        pattern = re.compile(r"// __TRADFI_GROWTH_START__.*?// __TRADFI_GROWTH_END__", re.DOTALL)
        if not pattern.search(html):
            sys.stderr.write('[TradFi Growth] markers absents, skip\n'); return
        html2 = pattern.sub(new_block, html)
        HTML_FILE.write_text(html2)
        sys.stderr.write(f'[TradFi Growth] Injected into {HTML_FILE.name}\n')
    except (PermissionError, OSError) as e:
        sys.stderr.write(f'[TradFi Growth] HTML injection skipped (TCC): {e}\n')
        sys.stderr.write('[TradFi Growth] Browser will fetch cache JSON via symlink instead\n')

if __name__ == '__main__':
    fetch()
