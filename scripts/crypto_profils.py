# -*- coding: utf-8 -*-
"""LES PROFILS — ce qu'on mesure, coin par coin.

POURQUOI CE FICHIER EXISTE, ET CE QU'IL REMPLACE
-------------------------------------------------
La note crypto jugeait chaque jeton selon SA FAMILLE : six familles, six
grilles. C'était déjà mieux qu'une grille unique, et le cas Akash le montrait —
revenus nuls, réseau à 59 % d'occupation. Mais six grilles pour deux cents
jetons laissent passer l'essentiel, et la mesure le dit :

  · BITCOIN et un memecoin partagent la famille « monnaie » ou « spéculatif »
    selon le narratif qui gagne, alors que l'un se juge sur sa rareté, son
    budget de sécurité et quinze ans de survie, et l'autre sur sa liquidité.
  · STETH, WSTETH, WBTC, CBBTC, WETH, EETH, FRXETH sont des ENVELOPPES. Elles
    n'ont ni revenus, ni TVL, ni économie propre : elles valent ce que vaut le
    jeton qu'elles emballent. Les noter sur un « prix / revenus » revient à
    noter un ticket de vestiaire sur le chiffre d'affaires du théâtre.
  · XAUT, PAXG, KAU, KAG sont de l'OR TOKENISÉ. Leur seule question est celle
    de l'arrimage et de la garde — pas la croissance.
  · STRCX, CRCLON sont des ACTIONS tokenisées. Elles se jugent comme des
    actions, et le site a déjà une page pour ça.
  · UNISWAP encaisse 872 M$ par an et n'en reverse que 4,4 % au jeton, quand
    HYPERLIQUID en reverse 55 %. C'est LE fait qui sépare ces deux jetons, et
    aucune grille par famille ne le regardait.

Ce fichier remplace donc « une grille par famille » par « un profil par
jeton », avec héritage : un profil nommé pour les jetons qui méritent qu'on
s'arrête sur eux, un archétype raisonné pour les autres, et jamais un défaut
muet.

CE QU'EST UN PROFIL
--------------------
Un profil dit trois choses :
  1. AXES — les six grandeurs du radar, celles qui décrivent CE jeton. Elles
     changent d'un jeton à l'autre parce qu'on ne mesure pas les mêmes choses :
     Bitcoin n'a pas de TVL, un memecoin n'a pas de revenus, Akash n'a pas de
     valeur immobilisée mais a un taux d'occupation.
  2. CRITÈRES — les grandeurs notées, avec leurs seuils.
  3. THÈSE — en une phrase, ce que ce jeton prétend être, et donc ce qu'il faut
     regarder pour savoir s'il tient sa promesse. Ce n'est pas de la décoration :
     sans elle, six axes ne sont que six nombres.

LA RÈGLE QUI NE SE DISCUTE PAS
-------------------------------
Un axe n'entre dans un profil que si la donnée EXISTE pour ce jeton dans les
caches. On ne dessine pas un axe « décentralisation » parce qu'il ferait bien
sur un hexagone : il n'est mesuré nulle part, il n'existe pas. Un profil qui
promet plus que ce que le dépôt sait est un profil qui ment six fois par radar.
"""

# ── Le vocabulaire des grandeurs ─────────────────────────────────────────
# Chaque clé décrit une grandeur exploitable : son libellé, son sens (« haut »
# = plus c'est grand, mieux c'est), son unité, et la phrase qui dit ce qu'elle
# mesure. Les seuils vivent dans les profils, pas ici : le même Capi/TVL ne
# se juge pas au même niveau sur une chaîne et sur un protocole de prêt.
GRANDEURS = {
    # — la captation, le cœur du sujet —
    "part_detenteurs_pct": (
        "Part qui revient au jeton", "haut", " %",
        "Sur cent dollars payés par les utilisateurs, ce qui revient au "
        "détenteur du jeton. C'est la question qui décide de tout : un "
        "protocole peut encaisser des fortunes sans en reverser un centime."),
    "taux_captation_pct": (
        "Part gardée par le protocole", "haut", " %",
        "Sur cent dollars payés, ce que le protocole conserve — le reste "
        "rémunère ceux qui fournissent le service (mineurs, prêteurs, "
        "fournisseurs de liquidité)."),
    "rendement_detenteurs_pct": (
        "Rendement de la captation", "haut", " %",
        "Ce que le jeton reverse en un an, rapporté à sa capitalisation. La "
        "seule de ces grandeurs comparable au rendement d'une action."),
    "capt_nette_pct": (
        "Captation nette de l'inflation", "haut", " %",
        "Ce que le réseau reverse MOINS ce que l'émission retire. Négatif : "
        "la dilution mange la captation."),
    "real_yield": (
        "Rendement réel du staking", "haut", " %",
        "Le rendement du staking une fois l'inflation déduite. Un APY de 7 % "
        "sur une chaîne qui émet 7 % ne rapporte rien."),
    "frais_m": (
        "Frais payés par les utilisateurs", "haut", " M$",
        "Ce que le réseau facture en un an. Mesure l'usage, indépendamment "
        "de ce que le jeton en retire."),
    "detenteurs_m": (
        "Revenu revenant au jeton", "haut", " M$",
        "En millions de dollars par an, ce qui remonte jusqu'au détenteur."),

    # — la valorisation —
    # ⚠ CE N'EST PAS UN « PRIX / REVENUS », et le nommer ainsi était faux.
    # Le dénominateur est `rev_m_1y`, que le cache amont remplit depuis
    # `dataType=dailyFees` : ce que PAIENT les utilisateurs, pas ce que garde
    # le protocole. Sur Uniswap, l'écart est de vingt fois — 872 M$ de frais
    # contre 52 M$ de revenu. Un multiple appelé « revenus » et calculé sur des
    # frais donne un chiffre vingt fois trop flatteur sous un nom rassurant.
    "ps_ttm": ("Prix / frais", "bas", " ×",
               "La capitalisation rapportée aux FRAIS annuels — ce que paient "
               "les utilisateurs, avant tout partage. Le vrai multiple des "
               "revenus serait plus élevé : voir « ce que le jeton capte »."),
    "mc_tvl": ("Capitalisation / valeur immobilisée", "bas", " ×",
               "Ce que le marché paie pour un dollar déposé sur le protocole."),
    "fdv_tvl": ("FDV / valeur immobilisée", "bas", " ×",
                "Le même, une fois tous les jetons à venir émis."),
    "nvt_ratio": ("Valorisation / transactions", "bas", "",
                  "Le NVT : la capitalisation rapportée à ce qui transite "
                  "vraiment. L'équivalent d'un P/E pour un réseau."),
    "mcap_fdv": ("Part déjà émise (capi / FDV)", "haut", "",
                 "Proche de 1 : presque tous les jetons existent. Loin de 1 : "
                 "une émission à venir pèsera sur le cours."),
    "circ_pct": ("Offre en circulation", "haut", " %",
                 "Ce qui reste à émettre diluera les détenteurs actuels."),

    # — l'usage réel —
    "tvl_b": ("Valeur immobilisée", "haut", " Md$",
              "Les fonds réellement déposés : la taille de ce que le "
              "protocole garde."),
    "adresses_actives_k": ("Adresses actives", "haut", " k",
                           "Moyenne sur sept jours. L'usage réel, indifférent "
                           "au cours."),
    "usage_taux": ("Taux d'utilisation", "haut", " %",
                   "La part de la capacité du réseau réellement louée."),
    "usage_frais_m": ("Dépense des utilisateurs", "haut", " M$",
                      "Ce que versent les utilisateurs du réseau physique, "
                      "même si le protocole n'en garde rien."),
    "vol_mcap_pct": ("Liquidité", "haut", " %",
                     "Le volume quotidien rapporté à la capitalisation : ce "
                     "qui permet d'entrer et surtout de sortir."),

    # — la taille et le marché —
    "mcap_b": ("Taille", "haut", " Md$",
               "La capitalisation en circulation. Un actif minuscule se "
               "manipule ; un actif énorme monte moins vite."),
    "vol_b": ("Volume quotidien", "haut", " Md$", "Ce qui s'échange en 24 h."),

    # — le temps, et la survie —
    # C'est la famille de grandeurs que la note ignorait complètement, alors
    # qu'elle est parfois la SEULE chose mesurable : sur un memecoin sans
    # revenus ni TVL, avoir traversé un cycle est un fait, et le seul.
    "cycles": ("Cycles traversés", "haut", "",
               "En unités de quatre ans — le rythme du halving, qui cadence "
               "le marché depuis quinze ans. Au-dessus de 1, le jeton a déjà "
               "vu un marché baissier entier."),
    "age_annees": ("Ancienneté", "haut", " ans",
                   "Depuis la genèse quand elle est publiée, sinon depuis la "
                   "première cotation observée."),
    # ⚠ LE SENS DE CETTE GRANDEUR A ÉTÉ TRANCHÉ, PAS SUBI.
    # L'écart au plus haut est toujours négatif (c'est un repli depuis un
    # sommet). Deux lectures s'opposent, et il fallait choisir :
    #   · « une chute profonde est un défaut » → sens "haut" (−20 % note mieux
    #     que −90 %) ;
    #   · « une chute profonde SURVÉCUE est une preuve » → sens "bas".
    # On retient la première, et voici pourquoi : la grandeur mesure ce que le
    # jeton a PERDU, pas ce qu'il a prouvé. Un actif qui n'est jamais tombé de
    # 90 % est effectivement moins risqué que celui qui l'a fait, et le radar
    # doit dire « favorable », jamais « intéressant ». La preuve de survie, elle,
    # est portée par `cycles` et par `age_annees`, qui sont les axes faits pour
    # ça — et qui accompagnent toujours celui-ci dans les profils.
    #
    # ⚠ `pire_chute_pct` A DISPARU DES AXES, et c'est une conséquence de fait.
    # Elle se calculait sur la série de cours ; or le palier gratuit de
    # CoinGecko a cessé de servir plus d'un an d'historique (mesuré le
    # 03/09/2026 : `days=730` rend un 401, `days=365` passe). Sur douze mois, la
    # « pire chute » d'un jeton qui a perdu 94 % en 2022 vaudrait −22 % — faux,
    # et faux dans le sens rassurant. Le collecteur la renseigne donc depuis
    # `ath_chg_pct`, qui mesure la même chose sur TOUTE la vie cotée. Les deux
    # clés portant le même nombre, les profils n'en gardent qu'une, sans quoi
    # le radar dessinerait deux fois le même axe.
    "ath_chg_pct": ("Distance au plus haut", "haut", " %",
                    "L'écart au record historique : soit une occasion, soit un "
                    "réseau auquel le marché a cessé de croire."),
    "perf_1y": ("Performance 1 an", "haut", " %", ""),
    "perf_30d": ("Performance 30 jours", "haut", " %", ""),

    # — l'activité, quand il ne reste que ça —
    #
    # ⚠ AUCUN PROFIL N'UTILISE `dev_commits_4s` NI `dev_contributeurs`, et ce
    # n'est pas un oubli. Le palier gratuit de CoinGecko a cessé de servir les
    # blocs `developer_data` et `community_data` : mesuré sur les dix premiers
    # jetons collectés, zéro sur dix pour les commits, les contributeurs, les
    # étoiles, les abonnés Twitter et Reddit — quand `suivi_cg`, lui, sort dix
    # fois sur dix.
    #
    # Les définitions restent ici parce qu'elles sont justes et que la donnée
    # reviendrait telle quelle si la source la republiait. Mais un axe qu'on
    # sait vide ne se dessine pas : il occuperait un sixième du radar pour
    # n'afficher qu'un tiret, et ferait croire à un jeton inactif là où c'est
    # la SOURCE qui se tait. Cette règle est écrite en tête de fichier ; c'est
    # ici qu'elle s'applique pour la première fois.
    "dev_commits_4s": ("Commits sur 4 semaines", "haut", "",
                       "L'activité du dépôt public. Sur un jeton sans revenus, "
                       "c'est parfois la seule preuve que quelqu'un travaille "
                       "encore."),
    "dev_contributeurs": ("Contributeurs au code", "haut", "",
                          "Le nombre de personnes ayant proposé du code. Un "
                          "réseau tenu par une seule main est un risque."),
    # Le suffixe n'est pas décoratif : « 48 016 » seul ne dit pas de quoi il
    # s'agit, et la fiche affichait ce nombre nu à côté de valeurs en % et en
    # M$. Le mot manquant est l'unité.
    "suivi_cg": ("Portefeuilles qui le suivent", "haut", " suivis",
                 "Le nombre d'utilisateurs qui l'ont mis en liste de suivi : "
                 "de la notoriété, pas un fondamental."),
}


# ── Les archétypes ────────────────────────────────────────────────────────
# Un archétype n'est PAS une famille : c'est un profil complet, avec sa thèse
# et ses axes, dont un jeton hérite quand il n'a pas de profil nommé. Il y en a
# davantage que de familles, précisément parce que les familles mélangeaient
# des choses qui ne se mesurent pas pareil.
ARCHETYPES = {

    "reserve_valeur": {
        "lib": "Réserve de valeur",
        "these": (
            "Ce jeton ne prétend pas encaisser des revenus : il prétend être "
            "rare, difficile à censurer, et toujours là dans dix ans. On le "
            "juge donc sur son ancienneté, sur ce qu'il a traversé, sur la "
            "sécurité que ses frais financent — et non sur un multiple."),
        "axes": ["cycles", "age_annees", "frais_m", "vol_mcap_pct",
                 "circ_pct", "ath_chg_pct"],
        "criteres": [
            ("cycles", 2.0, 1.0), ("age_annees", 8, 4),
            ("circ_pct", 90, 70), ("vol_mcap_pct", 2, 0.7),
            ("frais_m", 50, 5), ("mcap_b", 5, 0.5),
            ("ath_chg_pct", -40, -75),
        ],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm", "detenteurs_m",
                  "part_detenteurs_pct", "rendement_detenteurs_pct"},
        "raison": (
            "Une réserve de valeur ne capte pas de revenus : elle en "
            "transporte. Ni valeur immobilisée, ni prix/revenus — les lui "
            "réclamer serait mesurer ce qu'elle ne prétend pas être."),
    },

    "chaine": {
        "lib": "Blockchain applicative",
        "these": (
            "Une chaîne facture l'usage de son espace de bloc. Sa question "
            "est double : combien d'activité attire-t-elle, et combien de "
            "cette activité revient-elle au jeton une fois l'émission payée ?"),
        "axes": ["part_detenteurs_pct", "capt_nette_pct", "tvl_b",
                 "adresses_actives_k", "mc_tvl", "circ_pct"],
        "criteres": [
            ("part_detenteurs_pct", 30, 8), ("capt_nette_pct", 0, -3),
            ("real_yield", 3, 0), ("mc_tvl", 15, 40),
            ("ps_ttm", 30, 120), ("tvl_b", 1, 0.1),
            ("adresses_actives_k", 500, 50), ("nvt_ratio", 0.10, 0.30),
            ("circ_pct", 80, 55), ("vol_mcap_pct", 5, 2),
            ("cycles", 1.5, 0.5), ("perf_1y", 20, -20),
        ],
        "muets": set(),
        "raison": None,
    },

    "protocole": {
        "lib": "Protocole DeFi",
        "these": (
            "Un protocole encaisse des frais sur un service. La seule "
            "question qui vaille est celle du partage : sur cent dollars "
            "payés, combien atteignent le jeton ? Un protocole peut être "
            "immense et ne rien reverser."),
        "axes": ["part_detenteurs_pct", "rendement_detenteurs_pct", "frais_m",
                 "mc_tvl", "ps_ttm", "circ_pct"],
        "criteres": [
            ("part_detenteurs_pct", 25, 5),
            ("rendement_detenteurs_pct", 3, 0.5),
            ("ps_ttm", 20, 60), ("mc_tvl", 2, 8),
            ("frais_m", 50, 10), ("circ_pct", 70, 45),
            ("vol_mcap_pct", 5, 2), ("cycles", 1.0, 0.25),
            ("perf_1y", 20, -20),
        ],
        "muets": set(),
        "raison": None,
    },

    "reseau_physique": {
        "lib": "Réseau physique (DePIN / IA)",
        "these": (
            "Un réseau physique loue des machines. Ce qu'il facture ne lui "
            "appartient pas — il le reverse aux fournisseurs. On le juge donc "
            "sur son taux d'occupation et sur la dépense réelle de ses "
            "utilisateurs, jamais sur ce que le protocole garde."),
        "axes": ["usage_taux", "usage_frais_m", "frais_m", "circ_pct",
                 "vol_mcap_pct", "cycles"],
        "criteres": [
            ("usage_taux", 60, 30), ("usage_frais_m", 5, 0.5),
            ("frais_m", 5, 0.5), ("circ_pct", 70, 45),
            ("vol_mcap_pct", 5, 2), ("cycles", 1.0, 0.25),
            # ⚠ CE SEUIL VALAIT 30 ET 5, ET N'A JAMAIS PU ÉCHOUER.
            # La grandeur se compte en portefeuilles qui SUIVENT le jeton chez
            # CoinGecko : Chainlink en a 647 312. Un seuil favorable à trente
            # donnait le point à tout le monde, et la fiche affichait
            # « 647 312 suivis · ≥ 30,00 suivis · 1 pt » — un critère qui ne
            # discrimine rien pèse quand même dans la note sur vingt.
            # Distribution mesurée le 05/09/2026 sur les 38 jetons qui portent
            # la grandeur : médiane 120 400, premier quartile 56 860, neuvième
            # décile 393 600 ; et elle ne varie pas d'un archétype à l'autre
            # (médianes 102 300, 138 500 et 148 200). Les seuils du profil
            # spéculatif — cent mille et vingt mille — s'appliquent donc tels
            # quels ici, et c'est le même barème pour la même grandeur.
            ("suivi_cg", 100000, 20000), ("perf_1y", 20, -20),
        ],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm"},
        "raison": (
            "Un réseau physique se mesure à son usage, pas aux fonds qu'il "
            "immobilise. Akash facture 1,9 M$ par an et n'en garde rien : tout "
            "va aux fournisseurs de machines."),
    },

    "speculatif": {
        "lib": "Actif spéculatif",
        "these": (
            "Ni revenus, ni fonds immobilisés : ce jeton vaut ce que "
            "l'attention lui accorde. Alors on mesure ce qui existe vraiment — "
            "a-t-il duré, peut-on en sortir, reste-t-il des jetons à émettre — "
            "et on ne feint pas d'y trouver une valeur fondamentale."),
        "axes": ["cycles", "age_annees", "vol_mcap_pct", "circ_pct",
                 "suivi_cg", "ath_chg_pct"],
        "criteres": [
            ("cycles", 1.0, 0.25), ("age_annees", 4, 1),
            ("vol_mcap_pct", 10, 3), ("circ_pct", 90, 60),
            ("mcap_b", 1, 0.2), ("perf_30d", 10, -15),
            ("perf_1y", 20, -40), ("suivi_cg", 100000, 20000),
        ],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm", "detenteurs_m",
                  "part_detenteurs_pct", "rendement_detenteurs_pct",
                  "capt_nette_pct", "real_yield"},
        "raison": (
            "Un actif spéculatif n'immobilise pas de fonds et ne facture "
            "rien : la valeur immobilisée et les revenus n'existent pas. Ce "
            "n'est pas un trou dans la donnée, c'est la nature de l'actif."),
    },

    "enveloppe": {
        "lib": "Enveloppe (jeton emballé)",
        "these": (
            "Ce jeton n'a pas d'économie propre : il représente un autre actif, "
            "un pour un. Sa valeur est celle du sous-jacent, et sa seule "
            "question est celle du dépositaire — qui garde le vrai, et que "
            "vaut sa promesse. Il n'y a rien à noter ici, et prétendre le "
            "contraire tromperait."),
        "axes": ["mcap_b", "vol_mcap_pct", "circ_pct", "cycles",
                 "ath_chg_pct", "suivi_cg"],
        "criteres": [
            ("vol_mcap_pct", 3, 0.5), ("mcap_b", 5, 0.5),
            ("cycles", 1.0, 0.25),
        ],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm", "detenteurs_m",
                  "part_detenteurs_pct", "rendement_detenteurs_pct",
                  "capt_nette_pct", "real_yield", "perf_1y", "perf_30d",
                  "ath_chg_pct"},
        "raison": (
            "Une enveloppe suit son sous-jacent : sa performance est celle de "
            "l'actif emballé, pas une performance propre. On ne la note donc "
            "pas — on renvoie à la fiche du jeton qu'elle représente."),
        "sans_note": True,
    },

    "matiere_tokenisee": {
        "lib": "Matière première tokenisée",
        "these": (
            "Ce jeton représente un gramme de métal en coffre. Il ne "
            "croît pas, ne verse rien, et ne prétend rien de tel : sa seule "
            "promesse est que le métal existe et qu'on peut le reprendre. On "
            "juge donc la taille de l'encours et la liquidité, rien d'autre."),
        "axes": ["mcap_b", "vol_mcap_pct", "circ_pct", "cycles",
                 "ath_chg_pct", "suivi_cg"],
        "criteres": [
            ("vol_mcap_pct", 2, 0.3), ("mcap_b", 1, 0.1),
            ("cycles", 1.0, 0.25),
        ],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm", "detenteurs_m",
                  "part_detenteurs_pct", "rendement_detenteurs_pct",
                  "capt_nette_pct", "real_yield"},
        "raison": (
            "Une matière première tokenisée ne capte aucune valeur : elle "
            "en conserve. Ni revenus, ni fonds immobilisés — et c'est "
            "exactement ce qu'on lui demande."),
        "sans_note": True,
    },

    "action_tokenisee": {
        "lib": "Action tokenisée",
        "these": (
            "Sous ce jeton il y a une action cotée, et c'est elle qu'il faut "
            "juger — sur ses comptes, son secteur et ses multiples. Le site le "
            "fait déjà, dans l'onglet Analyse fondamentale. Ici on ne mesure "
            "que l'enveloppe : sa taille et sa liquidité."),
        "axes": ["mcap_b", "vol_mcap_pct", "circ_pct", "cycles",
                 "ath_chg_pct", "suivi_cg"],
        "criteres": [("vol_mcap_pct", 2, 0.3), ("mcap_b", 0.5, 0.05)],
        "muets": {"tvl_b", "mc_tvl", "ps_ttm", "detenteurs_m",
                  "part_detenteurs_pct", "rendement_detenteurs_pct",
                  "capt_nette_pct", "real_yield"},
        "raison": (
            "L'analyse fondamentale d'une action tokenisée est celle de "
            "l'action. Elle ne se fait pas avec des grandeurs crypto."),
        "sans_note": True,
    },

    "plateforme": {
        "lib": "Jeton de plateforme",
        "these": (
            "Ce jeton est adossé à une entreprise — une bourse d'échange, le "
            "plus souvent — qui rachète ou brûle une part de ses bénéfices. "
            "Sa valeur dépend donc du volume de cette entreprise et de la "
            "sincérité de son engagement de rachat, que la chaîne ne prouve "
            "pas toujours."),
        "axes": ["part_detenteurs_pct", "frais_m", "circ_pct", "mcap_fdv",
                 "vol_mcap_pct", "cycles"],
        "criteres": [
            ("part_detenteurs_pct", 25, 5), ("ps_ttm", 25, 80),
            ("circ_pct", 70, 45), ("mcap_fdv", 0.9, 0.65),
            ("vol_mcap_pct", 4, 1.5), ("mcap_b", 2, 0.3),
            ("cycles", 1.5, 0.5), ("perf_1y", 20, -20),
        ],
        "muets": {"tvl_b", "mc_tvl"},
        "raison": (
            "Un jeton de plateforme n'immobilise pas de fonds : il vit du "
            "chiffre d'affaires d'une société. Sa valeur immobilisée n'a pas "
            "de sens."),
    },

    "staking_liquide": {
        "lib": "Jeton de staking liquide",
        "these": (
            "Ce jeton porte un dépôt qui rapporte : il vaut le sous-jacent "
            "plus les récompenses accumulées. Le protocole qui l'émet prélève "
            "une commission — c'est cette commission, et elle seule, qui fait "
            "vivre son jeton de gouvernance."),
        "axes": ["tvl_b", "part_detenteurs_pct", "mc_tvl", "vol_mcap_pct",
                 "circ_pct", "cycles"],
        "criteres": [
            ("tvl_b", 1, 0.1), ("part_detenteurs_pct", 20, 4),
            ("mc_tvl", 0.5, 2), ("ps_ttm", 25, 70),
            ("circ_pct", 70, 45), ("vol_mcap_pct", 3, 1),
            ("cycles", 1.0, 0.25),
        ],
        "muets": set(),
        "raison": None,
    },

    "infrastructure": {
        "lib": "Infrastructure et données",
        "these": (
            "Ce réseau vend un service aux autres protocoles — des prix, du "
            "calcul, du stockage, de l'indexation. Il ne détient pas de fonds : "
            "sa mesure est le volume de service rendu et la part qui revient "
            "à ceux qui le font tourner."),
        "axes": ["part_detenteurs_pct", "frais_m", "circ_pct",
                 "suivi_cg", "vol_mcap_pct", "cycles"],
        "criteres": [
            ("part_detenteurs_pct", 25, 5), ("frais_m", 20, 2),
            ("circ_pct", 70, 45), ("vol_mcap_pct", 3, 1),
            ("suivi_cg", 100000, 20000), ("cycles", 1.5, 0.5),
            ("perf_1y", 20, -20),
        ],
        "muets": {"mc_tvl"},
        "raison": (
            "Un réseau d'infrastructure ne garde pas les fonds de ses "
            "utilisateurs : sa valeur immobilisée, quand elle existe, ne "
            "mesure pas sa taille."),
    },
}


# ── Les profils nommés ────────────────────────────────────────────────────
# Un jeton nommé ici hérite d'un archétype et le corrige. C'est là que vit le
# cas par cas : chaque entrée dit pourquoi CE jeton ne se mesure pas comme ses
# voisins de famille. On n'écrit une entrée que si l'on a quelque chose à
# corriger — un profil nommé qui répéterait son archétype ne serait que du
# bruit à maintenir.
#
# `axes` remplace les six axes ; `these` remplace la phrase ; `criteres_plus`
# ajoute des critères ; `criteres` remplace toute la liste.
PROFILS = {

    # ═══ LES MONNAIES ET RÉSERVES ═══════════════════════════════════════
    "bitcoin": {
        "base": "reserve_valeur",
        "lib": "Réserve de valeur",
        "these": (
            "Bitcoin ne capte pas de valeur et ne cherche pas à le faire : "
            "ses 83 M$ de frais annuels rémunèrent les mineurs, c'est-à-dire "
            "achètent sa sécurité. Le juger sur un rendement serait le juger "
            "sur ce qu'il refuse d'être. Ses vraies grandeurs sont la rareté "
            "(émission connue d'avance, bientôt achevée), la survie (quinze "
            "ans, quatre cycles, plusieurs chutes de plus de 80 %) et le "
            "budget de sécurité que ces frais financent."),
        "axes": ["cycles", "circ_pct", "frais_m", "age_annees",
                 "vol_mcap_pct", "ath_chg_pct"],
        "note_axes": (
            "Aucun axe de captation : les frais de Bitcoin vont aux mineurs, "
            "et aucun mécanisme ne les reverse au détenteur. L'afficher à zéro "
            "laisserait croire à un défaut ; c'est un choix de conception."),
    },
    "ethereum": {
        "base": "chaine",
        "these": (
            # ⚠ Aucune capitalisation en dur dans une thèse : elle bouge chaque
            # jour, et un texte figé finirait par contredire le chiffre affiché
            # deux centimètres plus haut. On cite ce qui ne bouge pas — le
            # mécanisme, et l'ordre de grandeur du rendement.
            "Ethereum est la seule grande chaîne dont la captation soit "
            "directe et vérifiable : une part des frais est BRÛLÉE, ce qui "
            "réduit l'offre au bénéfice de tous les détenteurs. La question "
            "n'est donc pas s'il capte — il capte environ un tiers de ce qu'il "
            "facture — mais si ce tiers pèse quelque chose face à sa "
            "capitalisation. Rapporté à elle, le rendement se compte en "
            "centièmes de pour cent."),
        "axes": ["part_detenteurs_pct", "capt_nette_pct", "tvl_b",
                 "adresses_actives_k", "rendement_detenteurs_pct", "mc_tvl"],
    },
    "ripple": {
        "base": "reserve_valeur",
        "lib": "Réseau de paiement",
        "these": (
            "XRP se présente comme un actif de règlement interbancaire. Le "
            "réseau facture 0,1 M$ par an pour des dizaines de milliards de "
            "capitalisation : tout "
            "multiple de revenus y est arithmétiquement juste et vide de sens. "
            "Ce qui se mesure est l'usage du réseau, la part émise — une large "
            "réserve reste aux mains de l'émetteur — et la liquidité."),
        "axes": ["circ_pct", "mcap_fdv", "vol_mcap_pct", "frais_m",
                 "cycles", "ath_chg_pct"],
        "note_axes": (
            "Pas d'axe « prix / revenus » : à 0,1 M$ de frais annuels, le "
            "multiple dépasse le million. Il mesurerait la petitesse du "
            "dénominateur, pas la cherté de l'actif."),
    },
    "monero": {
        "base": "reserve_valeur",
        "lib": "Monnaie confidentielle",
        "these": (
            "Monero ne vise ni la TVL ni les revenus : il vise l'impossibilité "
            "de tracer une transaction. Sa mesure est sa durée de vie, la "
            "constance de son développement et sa liquidité — laquelle est "
            "sous pression permanente, les bourses le retirant régulièrement."),
        "axes": ["cycles", "suivi_cg", "vol_mcap_pct", "circ_pct",
                 "age_annees", "ath_chg_pct"],
    },
    "zcash": {"base": "reserve_valeur", "lib": "Monnaie confidentielle",
              "these": (
                  "Zcash offre la confidentialité en option, ce qui fait sa "
                  "force théorique et sa faiblesse mesurée : la part des "
                  "transactions réellement protégées est longtemps restée "
                  "faible. On le juge sur sa survie, son développement et sa "
                  "liquidité."),
              "axes": ["cycles", "suivi_cg", "vol_mcap_pct",
                       "circ_pct", "age_annees", "ath_chg_pct"]},
    "litecoin": {"base": "reserve_valeur", "lib": "Monnaie de paiement",
                 "these": (
                     "Litecoin est le plus ancien clone de Bitcoin encore "
                     "debout. Il ne capte rien, n'innove plus guère, et vaut "
                     "surtout par sa longévité et sa liquidité — deux "
                     "grandeurs qu'on mesure vraiment.")},
    "bitcoin-cash": {"base": "reserve_valeur", "lib": "Monnaie de paiement"},
    "dogecoin": {
        "base": "speculatif",
        "lib": "Memecoin historique",
        "these": (
            "Dogecoin est le memecoin qui a duré : douze ans, plusieurs "
            "cycles, une capitalisation qui résiste. Son émission est "
            "perpétuelle — cinq milliards de jetons par an, sans plafond — ce "
            "qui est le fait central le concernant et que sa notoriété fait "
            "oublier. On le juge sur la survie, la liquidité et cette "
            "dilution permanente."),
        "axes": ["cycles", "vol_mcap_pct", "age_annees", "suivi_cg",
                 "mcap_b", "ath_chg_pct"],
    },

    # ═══ LES CHAÎNES ════════════════════════════════════════════════════
    "solana": {
        "base": "chaine",
        "these": (
            "Solana facture beaucoup — 251 M$ par an — mais n'en reverse que "
            "12 %, et son émission de 3,7 % efface le reste : sa captation "
            "nette est négative. Sa vraie force est ailleurs, dans le nombre "
            "d'utilisateurs et l'activité applicative, et c'est cela qu'on "
            "regarde en premier."),
        "axes": ["adresses_actives_k", "part_detenteurs_pct", "capt_nette_pct",
                 "tvl_b", "real_yield", "mc_tvl"],
    },
    "binancecoin": {
        "base": "plateforme",
        "lib": "Jeton de plateforme et chaîne",
        "these": (
            "BNB est double : la chaîne BSC facture des frais, et Binance "
            "brûle des jetons sur ses propres bénéfices. C'est la seule "
            "captation du top 10 qui soit NETTEMENT POSITIVE — l'offre se "
            "réduit de 3,7 % l'an. En contrepartie, tout dépend d'une société "
            "privée dont les comptes ne sont pas publics."),
        "axes": ["capt_nette_pct", "part_detenteurs_pct", "tvl_b",
                 "circ_pct", "frais_m", "mc_tvl"],
        # BNB hérite de « plateforme », dont les muets déclarent la valeur
        # immobilisée « sans objet » — ce qui est vrai d'un jeton de bourse,
        # et faux de celui-ci : BSC porte 5,4 Md$ de TVL réelle. Sans cette
        # ligne, le radar traçait deux axes que le bloc juste en dessous
        # déclarait dépourvus de sens. On lève donc la déclaration héritée.
        "muets": set(),
        "raison": None,
    },
    "tron": {
        "base": "chaine",
        "these": (
            "Tron transporte plus de stablecoins qu'aucune autre chaîne, et "
            "ses 361 M$ de frais reviennent intégralement au réseau. Sa "
            "faiblesse n'est pas économique mais structurelle : très peu de "
            "validateurs, un fondateur omniprésent. Les axes mesurent ce "
            "qu'on sait mesurer ; la centralisation ne l'est pas ici."),
        "axes": ["part_detenteurs_pct", "frais_m", "tvl_b",
                 "adresses_actives_k", "circ_pct", "mc_tvl"],
    },
    "cardano": {"base": "chaine", "these": (
        "Cardano affiche une recherche académique abondante et une activité "
        "économique modeste : 1,3 M$ de frais annuels pour 7,7 Md$ de "
        "capitalisation. L'écart entre l'ambition et l'usage mesuré est le "
        "fait central, et les axes le montrent sans le commenter.")},
    "avalanche-2": {"base": "chaine"},
    "polkadot": {"base": "chaine", "these": (
        "Polkadot vend de la sécurité partagée à des chaînes filles. Son "
        "économie ne se lit donc pas sur sa propre TVL mais sur le nombre de "
        "parachaînes actives — que les caches ne publient pas. Restent "
        "l'inflation (7,5 %, forte) et le rendement du staking, qui la "
        "compense mal.")},
    "the-open-network": {"base": "chaine", "these": (
        "TON tient son pari sur la distribution : l'audience de Telegram. "
        "Sa mesure est donc l'adoption réelle rapportée à cette audience "
        "théorique, et l'écart reste large.")},
    "sui": {"base": "chaine"},
    "aptos": {"base": "chaine"},
    "near": {"base": "chaine"},
    "hedera-hashgraph": {"base": "chaine", "these": (
        "Hedera est gouvernée par un conseil de grandes entreprises : c'est "
        "son argument commercial et sa limite. Elle facture 0,3 M$ par an "
        "pour une capitalisation mille fois supérieure — l'usage mesuré "
        "reste très en "
        "dessous de la promesse institutionnelle.")},
    "algorand": {"base": "chaine"},
    "internet-computer": {"base": "chaine"},
    "cosmos": {"base": "chaine", "these": (
        "Cosmos a produit le standard d'interopérabilité que tout le monde "
        "utilise, et un jeton qui n'en capte presque rien : les chaînes "
        "construites avec son outillage ne lui doivent aucun péage. C'est le "
        "cas d'école du logiciel réussi et du jeton qui ne capte pas.")},
    "canton-network": {"base": "chaine", "these": (
        "Canton affiche 579 M$ de frais dont la totalité revient au réseau — "
        "un taux de 100 % qui, à lui seul, mérite prudence : la chaîne est "
        "récente et son activité provient d'un petit nombre d'institutions. "
        "Un chiffre exact n'est pas forcément un chiffre représentatif.")},
    "hyperliquid": {
        "base": "chaine",
        "lib": "Chaîne applicative (bourse de dérivés)",
        "these": (
            "Hyperliquid est la démonstration qu'un jeton PEUT capter : "
            "1 309 M$ facturés, 55 % reversés aux détenteurs, un rendement de "
            "3,9 % — sans commune mesure avec le reste du marché. La question "
            "n'est donc pas la captation mais sa durabilité : ces revenus "
            "viennent du négoce de dérivés, une activité cyclique, et "
            "l'émission de 12 % l'an rend la captation nette négative."),
        "axes": ["part_detenteurs_pct", "rendement_detenteurs_pct", "frais_m",
                 "capt_nette_pct", "tvl_b", "circ_pct"],
    },
    "polygon-ecosystem-token": {"base": "chaine"},
    "arbitrum": {"base": "chaine", "lib": "Extension d'Ethereum (L2)", "these": (
        "Arbitrum traite les transactions hors de la chaîne principale et lui "
        "paie un loyer. Sa marge est donc l'écart entre ce qu'il facture et "
        "ce qu'il reverse à Ethereum — un écart mince, et qui se resserre à "
        "mesure que les L2 se concurrencent.")},
    "optimism": {"base": "chaine", "lib": "Extension d'Ethereum (L2)"},
    "mantle": {"base": "chaine", "lib": "Extension d'Ethereum (L2)"},
    "starknet": {"base": "chaine", "lib": "Extension d'Ethereum (L2)"},
    "celestia": {"base": "chaine", "lib": "Couche de disponibilité", "these": (
        "Celestia ne vend pas de l'exécution mais de la PLACE : elle garantit "
        "que les données d'une autre chaîne sont publiées. Son revenu dépend "
        "donc du nombre de chaînes qui l'utilisent, et son émission élevée "
        "pèse lourd dans l'attente que ce nombre croisse.")},

    # ═══ LES PROTOCOLES ═════════════════════════════════════════════════
    "uniswap": {
        "base": "protocole",
        "these": (
            "Uniswap est le cas le plus net du marché : 872 M$ payés par les "
            "utilisateurs en un an, et 4,4 % qui atteignent le jeton. "
            "L'essentiel rémunère les fournisseurs de liquidité — ce qui est "
            "légitime — mais signifie que détenir UNI ne donne presque aucun "
            "droit sur cette activité. Le « fee switch » qui changerait cela "
            "est voté depuis des années sans être activé. C'est LE fait, et "
            "l'axe de captation le met en premier."),
        "axes": ["part_detenteurs_pct", "frais_m", "rendement_detenteurs_pct",
                 "mc_tvl", "tvl_b", "circ_pct"],
    },
    "aave": {
        "base": "protocole",
        "these": (
            "Aave prête et emprunte à grande échelle : 17,6 Md$ immobilisés, "
            "la plus grosse TVL du marché. Mais sur 805 M$ payés par les "
            "emprunteurs, 2,9 % reviennent au jeton — le reste va aux "
            "déposants. La taille du protocole et la valeur du jeton sont "
            "deux choses différentes, et l'écart se voit d'un coup d'œil."),
        "axes": ["tvl_b", "part_detenteurs_pct", "frais_m",
                 "rendement_detenteurs_pct", "mc_tvl", "circ_pct"],
    },
    "curve-dao-token": {"base": "protocole", "these": (
        "Curve reverse la moitié de ce qu'il encaisse à ceux qui bloquent "
        "leur jeton : c'est l'un des rares mécanismes de captation "
        "réellement en vigueur. Le prix à payer est une émission continue "
        "qui dilue ceux qui ne bloquent pas.")},
    "sky": {"base": "protocole", "lib": "Émetteur de stablecoin", "these": (
        "Sky (ex-MakerDAO) émet un dollar synthétique et vit de l'intérêt "
        "perçu sur les garanties. Son revenu suit donc les taux courts "
        "américains bien plus que l'activité crypto — une exposition macro "
        "que sa catégorie ne laisse pas deviner.")},
    "ethena": {"base": "protocole", "lib": "Émetteur de stablecoin", "these": (
        "Ethena verse un rendement financé par les taux de financement des "
        "contrats perpétuels : il est élevé quand le marché est haussier, et "
        "peut devenir négatif. Le rendement affiché n'est pas un intérêt, "
        "c'est une position de marché.")},
    "lido-dao": {"base": "staking_liquide", "these": (
        "Lido prélève 10 % des récompenses de staking qu'il intermédie. Son "
        "revenu est donc proportionnel à l'ether qu'il gère — 658 M$ de "
        "frais — mais seuls 0,7 % atteignent le jeton de gouvernance.")},
    "jito-governance-token": {"base": "staking_liquide"},
    "ether-fi": {"base": "staking_liquide"},
    "pancakeswap-token": {"base": "protocole"},
    "aerodrome-finance": {"base": "protocole", "these": (
        "Aerodrome reverse 74 % de ce qu'il encaisse à ceux qui bloquent "
        "leur jeton — l'un des taux les plus élevés mesurés. En contrepartie, "
        "son émission est massive : le rendement affiché est en partie payé "
        "en jetons neufs.")},
    "jupiter-exchange-solana": {"base": "protocole"},
    "raydium": {"base": "protocole"},
    "morpho": {"base": "protocole"},
    "pendle": {"base": "protocole", "these": (
        "Pendle sépare le capital de son rendement futur et les fait "
        "négocier séparément. Son activité dépend donc de l'existence de "
        "rendements à échanger : elle s'effondre quand le marché ne rapporte "
        "plus rien.")},
    "compound-governance-token": {"base": "protocole"},
    "pump-fun": {"base": "protocole", "lib": "Usine à memecoins", "these": (
        "Pump.fun facture 1 116 M$ par an — plus qu'Ethereum — en émettant "
        "des memecoins à la chaîne. C'est un revenu réel et une activité "
        "dont rien ne garantit la reconduction : elle dépend entièrement de "
        "l'appétit spéculatif du moment.")},
    "hastra-prime": {"base": "protocole"},
    "syrup": {"base": "protocole", "lib": "Crédit institutionnel"},
    "ondo-finance": {"base": "protocole", "lib": "Actifs du monde réel", "these": (
        "Ondo tokenise des bons du Trésor américain. Son encours suit donc "
        "les taux courts et l'appétit institutionnel, pas le cycle crypto — "
        "ce qui en fait l'une des rares lignes décorrélées de la cote.")},
    "chainlink": {"base": "infrastructure", "these": (
        "Chainlink fournit les prix dont dépendent presque tous les "
        "protocoles de prêt : une panne de son service liquiderait des "
        "milliards. Sa position est donc systémique, et sa captation — 93 % "
        "de ce qu'il facture — élevée, mais sur des montants encore modestes "
        "au regard de sa capitalisation."),
        "axes": ["part_detenteurs_pct", "frais_m", "rendement_detenteurs_pct",
                 "circ_pct", "suivi_cg", "cycles"]},
    "pyth-network": {"base": "infrastructure"},
    "the-graph": {"base": "infrastructure"},
    "filecoin": {"base": "infrastructure", "lib": "Stockage décentralisé", "these": (
        "Filecoin vend du stockage. Sa mesure devrait être la capacité "
        "réellement utilisée — longtemps très inférieure à la capacité "
        "offerte — que les caches ne publient pas ; on se rabat sur le "
        "service facturé et l'activité de développement.")},
    "arweave": {"base": "infrastructure", "lib": "Stockage permanent"},

    # ═══ LES RÉSEAUX PHYSIQUES ET L'IA ══════════════════════════════════
    "akash-network": {
        "base": "reseau_physique",
        "these": (
            "Akash loue des GPU. C'est le cas qui a fait comprendre que la "
            "note devait dépendre du jeton : revenus nuls, 1,9 M$ de frais "
            "intégralement reversés aux fournisseurs de machines, et un "
            "réseau qui tourne pourtant à 59 % d'occupation avec 262 GPU "
            "actifs. La bonne question n'est pas ce que le protocole garde, "
            "c'est si les machines tournent."),
        "axes": ["usage_taux", "usage_frais_m", "suivi_cg",
                 "circ_pct", "vol_mcap_pct", "cycles"],
    },
    "bittensor": {"base": "reseau_physique", "lib": "Réseau d'incitation IA",
                  "these": (
                      "Bittensor paie des modèles en concurrence sur des "
                      "sous-réseaux. Son émission de 7,2 % l'an EST son "
                      "mécanisme — c'est ainsi qu'il rémunère la production — "
                      "ce qui rend la dilution inséparable du service rendu, "
                      "et la captation nette structurellement négative.")},
    "render-token": {"base": "reseau_physique", "lib": "Calcul graphique"},
    "grass": {"base": "reseau_physique"},
    "helium": {"base": "reseau_physique"},
    "worldcoin-wld": {"base": "reseau_physique", "lib": "Identité biométrique",
                      "these": (
                          "Worldcoin distribue des jetons contre un scan de "
                          "l'iris. Sa grandeur pertinente est le nombre "
                          "d'humains vérifiés, que les caches ne publient "
                          "pas ; restent l'émission — très importante et "
                          "programmée sur des années — et la liquidité.")},

    # ═══ LES PLATEFORMES ════════════════════════════════════════════════
    "leo-token": {"base": "plateforme"},
    "whitebit": {"base": "plateforme"},
    "okb": {"base": "plateforme"},
    "crypto-com-chain": {"base": "plateforme"},
    "bitget-token": {"base": "plateforme"},
    "kucoin-shares": {"base": "plateforme"},
    "gatechain-token": {"base": "plateforme"},
    "nexo": {"base": "plateforme"},
    "htx-dao": {"base": "plateforme"},

    # ═══ LES ENVELOPPES ═════════════════════════════════════════════════
    # Sept jetons du top 200. Aucun n'a d'économie propre, et les noter sur
    # des grandeurs de protocole produisait des radars à moitié vides sans
    # jamais dire pourquoi.
    "staked-ether": {"base": "enveloppe", "sousjacent": "ETH",
                     "these": (
                         "stETH est de l'ether déposé chez Lido : sa valeur "
                         "est celle de l'ether, augmentée des récompenses "
                         "accumulées. Il n'a ni revenus ni TVL propres — le "
                         "protocole à juger est Lido, le jeton à juger est "
                         "ETH.")},
    "wrapped-steth": {"base": "enveloppe", "sousjacent": "ETH"},
    "wrapped-bitcoin": {"base": "enveloppe", "sousjacent": "BTC",
                        "these": (
                            "WBTC est du bitcoin gardé par un dépositaire et "
                            "représenté sur Ethereum. Toute sa question tient "
                            "en un mot : la garde. Qui détient le vrai "
                            "bitcoin, et que vaut sa promesse ?")},
    "coinbase-wrapped-btc": {"base": "enveloppe", "sousjacent": "BTC"},
    "weth": {"base": "enveloppe", "sousjacent": "ETH"},
    "ether-fi-staked-eth": {"base": "enveloppe", "sousjacent": "ETH"},
    "frax-ether": {"base": "enveloppe", "sousjacent": "ETH"},
    "lido-earn-eth": {"base": "enveloppe", "sousjacent": "ETH"},

    # ═══ LES MATIÈRES TOKENISÉES ════════════════════════════════════════
    "tether-gold": {"base": "matiere_tokenisee", "sousjacent": "once d'or"},
    "pax-gold": {"base": "matiere_tokenisee", "sousjacent": "once d'or"},
    "kinesis-gold": {"base": "matiere_tokenisee", "sousjacent": "gramme d'or"},
    "kinesis-silver": {"base": "matiere_tokenisee", "sousjacent": "gramme d'argent"},

    # ═══ LES ACTIONS TOKENISÉES ═════════════════════════════════════════
    "strategy-pp-variable-xstock": {"base": "action_tokenisee",
                                    "sousjacent": "Strategy (MSTR)"},
    "circle-internet-group-ondo-tokenized-stock": {
        "base": "action_tokenisee", "sousjacent": "Circle (CRCL)"},

    # ═══ LES SPÉCULATIFS ════════════════════════════════════════════════
    "shiba-inu": {"base": "speculatif"},
    "pepe": {"base": "speculatif"},
    "bonk": {"base": "speculatif"},
    "dogwifcoin": {"base": "speculatif"},
    "official-trump": {"base": "speculatif", "these": (
        "Un jeton lancé autour d'une personnalité politique, dont "
        "l'essentiel de l'offre reste entre les mains de l'émetteur et se "
        "libère par tranches. La part en circulation est ici la grandeur "
        "décisive : chaque déblocage est une vente potentielle.")},
    "floki": {"base": "speculatif"},
    "spx6900": {"base": "speculatif"},
    "fartcoin": {"base": "speculatif"},
    "pudgy-penguins": {"base": "speculatif", "lib": "Jeton de collection"},
    "apecoin": {"base": "speculatif", "lib": "Jeton de collection"},
    "terra-luna": {"base": "speculatif", "these": (
        "LUNC est ce qui reste après l'effondrement de Terra en mai 2022 — "
        "quarante milliards de dollars effacés en une semaine. Le jeton "
        "existe encore, et sa pire chute traversée est la seule statistique "
        "qui le décrive honnêtement.")},

    # ═══ LE JEU ET LE SOCIAL ════════════════════════════════════════════
    "axie-infinity": {"base": "speculatif", "lib": "Jeu et univers virtuel"},
    "the-sandbox": {"base": "speculatif", "lib": "Univers virtuel"},
    "decentraland": {"base": "speculatif", "lib": "Univers virtuel"},
    "immutable-x": {"base": "chaine", "lib": "Chaîne dédiée au jeu"},
    "chiliz": {"base": "plateforme", "lib": "Jetons de supporters"},
    "ethereum-name-service": {"base": "infrastructure", "lib": "Noms de domaine"},
}


def profil_de(cid, famille_repli):
    """Rend le profil complet d'un jeton : archétype + corrections nommées.

    `famille_repli` est la famille déduite du narratif par le collecteur. Elle
    ne sert que si le jeton n'a NI profil nommé NI archétype déductible — et
    la table de correspondance ci-dessous dit lequel, plutôt que de laisser
    « spéculatif » attraper tout ce qui n'a pas été prévu. C'était le défaut
    de la version précédente : un jeton inconnu tombait en « spéculatif » et
    se voyait reprocher de n'avoir ni TVL ni revenus, alors que personne
    n'avait jamais regardé s'il en avait.
    """
    REPLI = {
        "chaine": "chaine",
        "protocole": "protocole",
        "reseau": "reseau_physique",
        "speculatif": "speculatif",
        "monnaie": "reserve_valeur",
        "plateforme": "plateforme",
    }
    nomme = PROFILS.get(cid) or {}
    base = nomme.get("base") or REPLI.get(famille_repli) or "speculatif"
    arch = dict(ARCHETYPES[base])
    arch["archetype"] = base
    # Les corrections nommées écrasent l'archétype, champ par champ.
    for k, v in nomme.items():
        if k == "base":
            continue
        arch[k] = v
    if "criteres_plus" in arch:
        arch["criteres"] = list(arch["criteres"]) + list(arch.pop("criteres_plus"))
    arch["nomme"] = bool(nomme)
    return arch
