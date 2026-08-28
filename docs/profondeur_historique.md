# Profondeur d'historique — ce qui est atteignable, et ce qui ne l'est pas

Mesuré le 28/08/2026. Ce document existe pour qu'on ne repaie pas deux fois la
même mesure — et surtout pour qu'on ne rebâtisse pas un collecteur dont on a déjà
démontré qu'il ne rapporte rien.

## L'état des lieux

| Source | Sociétés | Exercices | Étendue |
|---|---|---|---|
| SEC XBRL (`sec_detail_*`) | 3 140 | médiane **13**, max **21** | 2007 → 2026 |
| International (`intl_detail_*`) | 11 635 | **5**, sans exception | 2008 → 2026 |

Les 2 096 sociétés présentes dans les deux caches lisent déjà la version
profonde : `paquetsDe()` interroge la SEC d'abord pour un ticker sans point.
Vérifié, rien à corriger de ce côté.

## Pourquoi l'international plafonne à cinq exercices

**Ce n'est pas un réglage du collecteur, c'est le plafond de la source.**
Éprouvé sur LVMH (`quote/epa/MC`), sur la bonne URL — celle des sous-pages
d'états, la page de synthèse ne portant pas le bloc `financialData` :

| Paramètre essayé | Colonnes rendues |
|---|---|
| aucun | 5 exercices + TTM |
| `?p=annual`, `?period=annual` | 5 + TTM |
| `?range=10Y`, `?r=10`, `?f=10`, `?limit=20` | 5 + TTM |
| `?p=quarterly` | 10 trimestres (2,5 ans) |
| `?p=trailing` | 10 périodes glissantes |

Aucun paramètre n'ouvre au-delà. Le site détient dix ans — `full_count` l'indique
— mais n'en sert que cinq à un visiteur anonyme.

## La piste 20-F, et pourquoi elle est abandonnée

**L'idée.** Une société étrangère cotée aux États-Unis dépose un formulaire 20-F,
dont les `companyfacts` XBRL sont publics comme ceux d'Apple. Sur douze géants
choisis à la main, la profondeur médiane est de **9 exercices** contre 5 — Sony
15, Toyota 14, Alibaba 12. Prometteur.

**Le rapprochement.** Les symboles ne se correspondent pas d'une place à l'autre
(`7203.T` à Tokyo, `TM` à New York). Le rapprochement se fait donc par nom
normalisé : 429 candidates sur les 11 635.

**Le danger.** Sur les vingt premiers appariements, un était faux : `LWB.WA`
(Lubelski Węgiel Bogdanka, minière polonaise) vers Mesoblast (biotech
australienne). Attribuer les états d'une société à une autre ne laisse aucune
trace visible sur la fiche — c'est le pire défaut possible.

**La parade, et elle est automatique.** Les deux sources se recouvrent sur cinq
exercices. Si le chiffre d'affaires concorde sur les années communes, alors
(a) c'est la même société et (b) les méthodologies coïncident, donc raccorder des
exercices plus anciens ne créera pas de marche dans la série. Sinon, on refuse.

**Le verdict, sur un échantillon de 60 tirées à intervalle régulier :**

| Issue | Nombre | Part |
|---|---|---|
| aucun dépôt XBRL (coquille ADR) | 41 | 68,3 % |
| chiffre d'affaires discordant → refusé | 10 | 16,7 % |
| devise des états ≠ devise du cache | 7 | 11,7 % |
| recouvrement < 2 ans, invérifiable | 2 | 3,3 % |
| **acceptées** | **0** | **0 %** |

Zéro sur soixante. Les géants choisis à la main étaient l'exception ; le tirage
régulier donne la règle. La grande majorité des certificats américains sont des
coquilles qui déposent un 6-K sans états XBRL.

**Ce que la validation a évité.** Parmi les dix refus pour discordance, plusieurs
étaient de BONS appariements de société mais de mauvais appariements de chiffres :
Ferrari (`FERGR.AS` → `RACE`) à 1 561 % d'écart, Berkshire à 24 %, Suncor à 36 %,
Pearson à 13 %. Sans le test de recouvrement, ces séries auraient été corrompues
silencieusement — et Ferrari aurait affiché un chiffre d'affaires seize fois trop
grand sur ses exercices anciens.

**Conclusion.** La piste est fermée. Ce n'est pas « à refaire plus tard avec un
meilleur rapprochement » : le rapprochement n'est pas le facteur limitant, c'est
l'absence de dépôts XBRL chez 68 % des candidates. Un rapprochement parfait
donnerait toujours zéro sur ces 68 %.

## Ce qui a été gagné, et qui reste acquis

1. **Les cours vont jusqu'à l'introduction en bourse.** `/live/chart` accepte
   désormais `max` (pas mensuel), en plus de `10y`. La fiche propose six
   horizons : 6 M · 1 an · 2 ans · 5 ans · 10 ans · Max.
2. **L'onglet Quantitatif s'ouvre sur TOUS les exercices déposés**, et non plus
   sur onze. Mesuré sur NVIDIA : 19 exercices tracés au lieu de 11. Les fenêtres
   courtes restent à un clic.
3. **Aucune régression de mise en page** : mesuré à 390, 768 et 1280 px —
   0 chevauchement d'étiquettes sur 10 graphiques, 0 débordement, 0 erreur JS.

## La règle qui vaut au-delà de ce cas

Un rapprochement entre deux sources hétérogènes doit porter son propre test de
validité. Ici le test était gratuit : les sources se recouvraient déjà. Quand
elles ne se recouvrent pas, il faut fabriquer le recouvrement avant de fusionner —
jamais fusionner sur une ressemblance de nom.
