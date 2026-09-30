"""
geo_zones.py - classement des libelles geographiques en grandes regions.

Un libelle vient soit d'un membre XBRL ("country:TW", "aapl:GreaterChinaSegmentMember",
libelle "Greater China"), soit d'une cellule de tableau ("Europe (hors France)",
"dont Espagne", "Autres pays"). On le range dans une grande region normalisee :

    AMN  Amerique du Nord          AML  Amerique latine
    AMR  Ameriques (Nord + Sud, zone composite telle que publiee)
    EUR  Europe                    EMEA Europe + Moyen-Orient + Afrique (composite)
    MOA  Moyen-Orient & Afrique    CHN  Chine (Grande Chine : Chine, Hong Kong, Macao)
    APAC Asie-Pacifique (hors Chine SI la Chine est publiee a part ; sinon elle y est)
    RDM  Reste du monde / autres pays / non ventile

Regle : on ne DECOUPE jamais une zone composite publiee (EMEA, Ameriques) : on la
garde telle quelle avec son code composite. La page affiche le libelle de la societe ;
le code sert aux comparaisons entre societes et au regroupement.
"""
import functools
import re
import unicodedata

REGIONS = {
    "AMN": "Amérique du Nord",
    "AML": "Amérique latine",
    "AMR": "Amériques",
    "EUR": "Europe",
    "EMEA": "Europe, Moyen-Orient, Afrique",
    "MOA": "Moyen-Orient & Afrique",
    "CHN": "Chine",
    "APAC": "Asie-Pacifique",
    "RDM": "Reste du monde",
}

# ISO 3166 alpha-2 -> grande region (les pays qui apparaissent reellement dans les depots)
ISO_REGION = {}
for _r, _codes in {
    "AMN": "US CA PR",
    "AML": "MX BR AR CL CO PE VE EC UY PY BO CR PA GT DO",
    "EUR": ("FR DE GB UK IT ES PT NL BE LU CH AT IE SE NO DK FI IS PL CZ SK HU RO BG GR "
            "HR SI RS UA RU TR CY MT EE LV LT BY"),
    "MOA": ("SA AE QA KW BH OM IL EG MA DZ TN ZA NG KE GH CI SN CM CD IR IQ JO LB AO ET "
            "TZ UG ZM ZW MU"),
    "CHN": "CN HK MO",
    "APAC": "JP KR TW IN SG AU NZ ID MY TH VN PH PK BD LK KZ",
}.items():
    for _c in _codes.split():
        ISO_REGION[_c] = _r

# noms de pays (FR / EN / DE / ES / IT / NL, sans accents, en minuscules) -> ISO
COUNTRY_NAMES = {
    "US": "united states|usa|u s|us|etats unis|etats-unis|vereinigte staaten|usa and canada|estados unidos|stati uniti|verenigde staten",
    "CA": "canada|kanada",
    "MX": "mexico|mexique|mexiko|messico",
    "BR": "brazil|bresil|brasilien|brasil|brasile",
    "AR": "argentina|argentine|argentinien",
    "CL": "chile|chili",
    "CO": "colombia|colombie|kolumbien",
    "PE": "peru|perou",
    "FR": "france|frankreich|francia|frankrijk",
    "DE": "germany|allemagne|deutschland|alemania|germania|duitsland",
    "GB": "united kingdom|uk|royaume uni|royaume-uni|great britain|grande bretagne|grossbritannien|regno unito|verenigd koninkrijk|england",
    "IT": "italy|italie|italien|italia",
    "ES": "spain|espagne|spanien|espana|spagna|spanje",
    "PT": "portugal|portogallo",
    "NL": "netherlands|pays bas|pays-bas|niederlande|paesi bassi|nederland|the netherlands|holland",
    "BE": "belgium|belgique|belgien|belgio|belgie",
    "LU": "luxembourg|luxemburg",
    "CH": "switzerland|suisse|schweiz|svizzera|zwitserland",
    "AT": "austria|autriche|osterreich|oesterreich",
    "IE": "ireland|irlande|irland",
    "SE": "sweden|suede|schweden",
    "NO": "norway|norvege|norwegen",
    "DK": "denmark|danemark|daenemark|danimarca",
    "FI": "finland|finlande|finnland",
    "PL": "poland|pologne|polen|polonia",
    "CZ": "czech republic|republique tcheque|tschechien|czechia",
    "HU": "hungary|hongrie|ungarn",
    "RO": "romania|roumanie|rumanien",
    "GR": "greece|grece|griechenland",
    "TR": "turkey|turquie|turkei|turkiye",
    "RU": "russia|russie|russland",
    "UA": "ukraine",
    "CN": "china|chine|mainland china|prc|people s republic of china|volksrepublik china|cina",
    "HK": "hong kong",
    "TW": "taiwan",
    "JP": "japan|japon|giappone|japon",
    "KR": "korea|south korea|coree|coree du sud|sudkorea|republic of korea",
    "IN": "india|inde|indien",
    "SG": "singapore|singapour|singapur",
    "AU": "australia|australie|australien",
    "NZ": "new zealand|nouvelle zelande",
    "ID": "indonesia|indonesie",
    "MY": "malaysia|malaisie",
    "TH": "thailand|thailande",
    "VN": "vietnam|viet nam",
    "PH": "philippines",
    "IL": "israel",
    "SA": "saudi arabia|arabie saoudite",
    "AE": "united arab emirates|emirats arabes unis|uae",
    "ZA": "south africa|afrique du sud|sudafrika",
    "EG": "egypt|egypte",
    "MA": "morocco|maroc",
    "NG": "nigeria",
}
_NAME2ISO = {}
for _iso, _names in COUNTRY_NAMES.items():
    for _n in _names.split("|"):
        _NAME2ISO[_n.strip()] = _iso

# nom francais d'affichage (quand le depot ne donne qu'un code : "country:US" -> "États-Unis")
ISO_FR = dict(US="États-Unis", CA="Canada", MX="Mexique", BR="Brésil", AR="Argentine", CL="Chili",
              CO="Colombie", PE="Pérou", FR="France", DE="Allemagne", GB="Royaume-Uni", IT="Italie",
              ES="Espagne", PT="Portugal", NL="Pays-Bas", BE="Belgique", LU="Luxembourg", CH="Suisse",
              AT="Autriche", IE="Irlande", SE="Suède", NO="Norvège", DK="Danemark", FI="Finlande",
              PL="Pologne", CZ="Tchéquie", HU="Hongrie", RO="Roumanie", GR="Grèce", TR="Turquie",
              RU="Russie", UA="Ukraine", CN="Chine", HK="Hong Kong", TW="Taïwan", JP="Japon",
              KR="Corée du Sud", IN="Inde", SG="Singapour", AU="Australie", NZ="Nouvelle-Zélande",
              ID="Indonésie", MY="Malaisie", TH="Thaïlande", VN="Vietnam", PH="Philippines", IL="Israël",
              SA="Arabie saoudite", AE="Émirats arabes unis", ZA="Afrique du Sud", EG="Égypte",
              MA="Maroc", NG="Nigeria", MO="Macao", PR="Porto Rico")

# mots de region, du plus specifique au plus general (l'ordre compte)
REGION_PATTERNS = [
    ("EMEA", r"\bemea\b|\beame\b|\bemena\b|europe\W+(the )?middle east\W+(and )?africa|europe\W+moyen\W+orient\W+(et )?afrique|"
             r"europa\W+naher osten\W+afrika|europe\W+africa|europa medio oriente"),
    ("AMN", r"north america|amerique du nord|nordamerika|norteamerica|nord america|noord amerika|pohjois amerikka|"
            r"u ?s and canada|united states and canada|etats unis et canada|us canada|ucan"),
    ("AML", r"latin america|amerique latine|lateinamerika|latinamerika|latinoamerica|america latina|south america|"
            r"amerique du sud|sudamerika|central and south america|centro sud america|latam|central america|amerique centrale|caribbean"),
    ("AMR", r"\bamericas\b|\bameriques\b|\bamerika\b|\bamerikka\b|\bamerique\b|\bamerica\b|western hemisphere"),
    ("MOA", r"sub\W?saharan|subsaharienne|middle east|moyen\W+orient|naher osten|mellanostern|medio oriente|\bafrica\b|\bafrique\b|\bafrika\b|"
            r"\bmea\b|\bmena\b|gulf|golfe"),
    ("CHN", r"greater china|grande chine|\bchina\b|\bchine\b|\bkiina\w*|hong kong|macau|macao"),
    ("APAC", r"asia|asie|asien|aasia|\bapac\b|\bapj\b|\bapjc\b|\bpacific\b|pacifique|oceania|oceanie|oceanien|"
             r"australasia|far east|extreme orient|\bjapan\b|\bjapon\b|sydostasien|nordostasien|sapmena|\bapmea\b"),
    ("EUR", r"europe|europa|europ|eurooppa|eurasi|\beu\b|eurozone|zone euro|\beea\b|nordics|nordic|scandinavi|benelux|"
            r"iberia|dach|cis\b|\bcei\b"),
]
REST_PATTERN = (r"rest of (the )?world|reste du monde|resto del mondo|autres pays|other countries|other regions|"
                r"autres regions|\bother\b|\bautres?\b|\bothers\b|ubrige|sonstige|\brow\b|international|non us|"
                r"non-us|nonus|outside|foreign|etranger|hors de france|export|all other|unallocated|non affecte|"
                r"non ventile|ovriga|muut")
# libelle qui COMMENCE par un de ces mots : c'est un reste, meme s'il cite un pays ("Outside United States")
REST_PREFIX = r"^(outside|non|foreign|international|all other|other than|rest of the world|rest of world|hors de|en dehors)\b"
NON_GEO_PATTERN = (r"holding|fonctions globales|global functions|corporate|elimination|intersegment|inter segment|"
                   r"reconcil|unallocated corporate|siege|head office|bottling|global ventures|digital|"
                   r"autres activites|other activities|other businesses|consolidation|consolidated|"
                   # raisons sociales : un tableau de FILIALES n'est pas une ventilation geographique
                   r"\b(ltd|limited|inc|gmbh|llc|plc|sas|spa|s p a|bv|b v|nv|n v|corp|corporation|pty|srl|kk|oyj)\b")
# "dont Espagne", mais aussi "dontBrésil" (espace perdu a la conversion HTML)
SUB_PATTERN = r"^(dont|of which\b|o w\b|incl\b|including\b|thereof\b|davon\b|di cui\b|de los cuales\b|waarvan\b)"


def norm(s):
    """minuscules, sans accents, ponctuation -> espace."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"\(\s*\d+\s*\)|\(\s*[a-z]\s*\)|\*+", " ", s)      # appels de note (1) (a) *
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def camel_label(member_qname):
    """'aapl:RestOfAsiaPacificSegmentMember' -> 'Rest Of Asia Pacific'. Dernier recours
    quand le depot ne fournit pas de libelle."""
    loc = member_qname.split(":")[-1]
    loc = re.sub(r"(Segment)?Member$", "", loc)
    loc = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", loc)
    return loc.replace(".", " ").strip()


def is_sub_label(label):
    """'dont Espagne', 'of which: Brazil' -> sous-zone deja comprise dans la ligne precedente."""
    return re.search(SUB_PATTERN, norm(label)) is not None


@functools.lru_cache(maxsize=20000)
def classify(label, member=None, domicile=None):
    """Retourne dict(region, iso, kind) ; kind in pays|region|composite|reste|non_geo.

    member   : QName XBRL eventuel (country:XX est decisif)
    domicile : ISO du pays du siege (pour ifrs-full:CountryOfDomicileMember, 'France' implicite...)
    """
    if member:
        m = re.match(r"^country:([A-Z]{2})$", member)
        if m:
            iso = m.group(1)
            return dict(region=ISO_REGION.get(iso, "RDM"), iso=iso, kind="pays")
        if member.endswith("CountryOfDomicileMember") and domicile:
            return dict(region=ISO_REGION.get(domicile, "RDM"), iso=domicile, kind="pays")
    n = norm(label)
    n = re.sub(SUB_PATTERN, "", n).strip()
    if not n:
        return dict(region=None, iso=None, kind="non_geo")
    if re.search(NON_GEO_PATTERN, n):
        return dict(region=None, iso=None, kind="non_geo")
    # "Europe (hors France)", "Asia Pacific excluding Japan" : on juge sur la partie avant hors/excl
    n = re.sub(r"^(in|within|to) (the )?", "", n)                   # "In the U.S." -> "u s"
    head = re.split(r"\b(hors|excluding|excl|except|sauf|ohne|ex|other than|autre que)\b", n)[0].strip() or n
    # pays exact (libelle entier) ?
    if head in _NAME2ISO:
        iso = _NAME2ISO[head]
        return dict(region=ISO_REGION.get(iso, "RDM"), iso=iso, kind="pays")
    found = []
    for code, pat in REGION_PATTERNS:
        m = re.search(pat, head)
        if m:
            found.append((code, m.start()))
    if found:
        codes = [c for c, _ in found]
        if codes[0] == "EMEA" or len(set(codes) - {"AMR"}) <= 1:
            # "Latin America" matche aussi AMR : la plus specifique (premiere de la liste) l'emporte
            return dict(region=codes[0], iso=None, kind="region")
        # zone composite ("Asia, Oceania, Africa") : on la range sous la region citee en premier
        first = sorted(found, key=lambda x: x[1])[0][0]
        return dict(region=first, iso=None, kind="composite")
    if re.search(REST_PREFIX, n):
        return dict(region="RDM", iso=None, kind="reste")
    # pays cite dans un libelle plus long ("United States and Canada", "China including Hong Kong")
    for name, iso in sorted(_NAME2ISO.items(), key=lambda kv: -len(kv[0])):
        if len(name) > 3 and re.search(r"\b" + re.escape(name) + r"\b", head):
            return dict(region=ISO_REGION.get(iso, "RDM"), iso=iso, kind="pays")
    if re.search(REST_PATTERN, n):
        return dict(region="RDM", iso=None, kind="reste")
    # domicile implicite : "Domestic" / "Home country"
    if domicile and re.search(r"domestic|home country|country of domicile|inland|pays du siege", n):
        return dict(region=ISO_REGION.get(domicile, "RDM"), iso=domicile, kind="pays")
    return dict(region=None, iso=None, kind="non_geo")


if __name__ == "__main__":
    for t in ["Europe (hors France)", "dont Espagne", "Amérique latine", "Fonctions globales", "Greater China",
              "Rest of Asia Pacific", "Other countries", "Non-US", "EMEA", "Americas excluding United States",
              "China (including Hong Kong)", "Asie (hors Japon)", "Autres pays", "Reste de l'Europe",
              "Bottling Investments", "Asia Pacific", "Etats-Unis", "Japon", "Mode et Maroquinerie"]:
        print(f"{t:40s}", classify(t))
    print(classify("", member="country:TW"), classify("Germany", member="ifrs-full:CountryOfDomicileMember", domicile="DE"))
