# Étendre le TradFi Tracker à l'univers coté — ce que la mesure dit

*Mesuré le 28/08/2026 sur les 39 narratifs, 812 actions suivies et 20 096
cotations principales.*

## La question

Le Tracker suit trente-neuf narratifs bâtis sur **812 actions choisies à la
main**, dans deux dictionnaires Python de `fetch_tradfi.py`. L'univers en compte
désormais **20 096**. Peut-on peupler les narratifs automatiquement, depuis le
champ `industry` rempli à 99,9 % ?

## La réponse : non, pas pour 29 d'entre eux

Pour chaque narratif, on a mesuré l'industrie majoritaire de ses actions
actuelles et la part de ses actions qui la partagent — sa *pureté*.

| Famille | Nombre | Ce que ça veut dire |
|---|---|---|
| Dérivables | **10** | une industrie dominante à 60 % ou plus, non revendiquée par un autre narratif |
| Non dérivables | **29** | pas d'industrie dominante, ou industrie partagée |

### Les dix dérivables

| Narratif | Pureté | Industrie | Sociétés dans l'univers |
|---|---|---|---|
| Restauration & Fast Food | 93 % | Restaurants | 132 |
| Shipping & Maritime | 88 % | Marine Shipping | 144 |
| Auto & EV | 81 % | Auto Manufacturers | 103 |
| Mineurs d'or mondial | 77 % | Gold | 167 |
| Télécoms | 74 % | Telecom Services | 150 |
| E-commerce & Retail tech | 73 % | Internet Retail | 53 |
| Biotech | 71 % | Biotechnology | 596 |
| Quantum Computing | 67 % | Computer Hardware | 160 |
| Sportswear & Fitness | 64 % | Footwear & Accessories | 61 |
| Voyages & Hôtellerie | 60 % | Airlines | 67 |

### Pourquoi même ces dix demandent un arbitrage

**« Quantum Computing » est le contre-exemple qui doit servir d'avertissement.**
Son industrie dominante est « Computer Hardware », et cette industrie compte cent
soixante sociétés. Les y verser toutes ferait d'un narratif de quatorze titres
soigneusement choisis un catalogue de fabricants de matériel informatique.
L'industrie décrit ce que la société VEND ; le narratif décrit une THÈSE. Une
pureté de 67 % ne prouve pas que l'inverse soit vrai.

Même remarque pour « Biotech » : 596 sociétés dans l'industrie contre 21 dans le
panier. Le rapport n'est pas de un à trois, il est de un à vingt-huit.

### Pourquoi les vingt-neuf autres ne le sont pas

Deux causes distinctes, mesurées :

**Industrie partagée** — cinq narratifs revendiquent une industrie que d'autres
revendiquent aussi. « Cybersécurité » est à **100 %** « Software -
Infrastructure », et « AI Software & Data », « Big Tech », « Cloud & SaaS »,
« Fintech » et « Quantum Computing » y puisent également. Une règle « une
industrie, un narratif » viderait quatre de ces cinq narratifs.

**Pas d'industrie dominante** — « Banques » est à 48 %, « Pétrole & Gaz » à
45 %, « Big Tech & Électronique » à 15 %. Ces paniers rassemblent des métiers
que la nomenclature sépare : une banque de détail et une banque d'investissement
ne portent pas la même étiquette.

## Ce qui a DÉJÀ été fait, et qui répond à une partie du besoin

L'univers élargi sert déjà le Tracker sans qu'on ait touché aux paniers :

- **les médianes de comparaison** viennent désormais des 38 061 sociétés de la
  collecte de marché, par INDUSTRIE — 20 711 couples industrie × grandeur. Un
  ratio du Tracker se lit donc contre son industrie réelle, plus contre un
  échantillon de huit cents titres ;
- **la croissance du chiffre d'affaires** passe de 12 secteurs sur 39 à
  **39 sur 39**, parce que sa source Yahoo est fermée et que la collecte de
  marché la sert pour 774 des 783 titres suivis qu'elle contient ;
- **onze champs** — secteur, industrie, pays, PEA, valeur d'entreprise sur
  EBITDA, les quatre marges, rendement de l'actif, taux de distribution, dette
  sur EBITDA — étaient vides à 100 % et sont remplis sur 613 à 686 lignes ;
- **le score composite** ne repose plus sur trois titres par narratif mais sur
  tout le panier. Mesure de l'effet : le pourcentage de largeur passe de quatre
  valeurs distinctes à trente-quatre.

## Ce qui reste à arbitrer, et par qui

Verser les industries dans les paniers est une décision de PRODUIT, pas une
question technique. Trois voies, avec leurs conséquences mesurées :

1. **Ne rien verser.** Les narratifs restent éditoriaux — c'est leur valeur, et
   c'est ce qui les distingue d'un simple découpage sectoriel. Le Tracker gagne
   déjà la comparaison à l'univers entier.

2. **Verser les grandes capitalisations manquantes des dix industries pures.**
   Chaque panier gagnerait de dix à quarante titres, sans changer sa nature.
   Il faut un plafond — par exemple les vingt plus grosses de l'industrie
   absentes du panier — sinon Biotech passe de 21 à 596.

3. **Ajouter un axe SÉPARÉ.** Garder les trente-neuf narratifs tels quels et
   publier à côté les agrégats par industrie, sur les 20 096. Les deux axes
   répondent à deux questions différentes — « où va le thème » et « où va le
   métier » — et les mélanger fait perdre les deux.

La troisième voie est celle que le plan de bataille recommande déjà, sous le nom
de volet D : « ajouter l'axe classification d'abord, sans rien retirer ».

## Le piège à ne pas oublier

`fetch_new_listings.py` ne lit pas un cache : il **découpe le TEXTE SOURCE** de
`fetch_tradfi.py` entre `src.index("STOCKS = {")` et le premier `"\n}\n"`, et le
parse est enveloppé dans un `try/except`. Déplacer `STOCKS` dans un JSON, le
renommer, ou seulement insérer une accolade fermante en colonne zéro avant la
fin du dictionnaire rendrait la liste des « titres déjà connus » **vide, sans
une ligne d'erreur** — et le détecteur signalerait alors comme « nouveau géant »
chaque grande société de l'univers.

C'est le premier maillon à traiter avant toute réécriture des paniers.
