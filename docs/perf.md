# Performance

Owner : B. Tous les chiffres viennent d'un même passage de `make bench` (fichier brut :
[`perf_results.json`](perf_results.json)) sur le graphe synthétique de `scripts/bench_data.py`.

## Protocole

| | |
| --- | --- |
| Base | Neo4j 5.26.32 + GDS dans Docker (`make bench-db`, port 7689, tas 2 Go), jamais la base de test (7688) |
| Graphe | 50 000 joueurs, 400 clubs (7 championnats de 20 clubs + 260 clubs de divisions inférieures), 80 000 transferts, 149 339 valorisations, 220 000 `PLAYED_WITH`, 63 165 `SENDS_TO`, 3 000 articles, 5 000 rumeurs, 700 `PREDICTED_LINK`, 97 206 `PREDICTED_MOVE` ; 287 756 nœuds, 890 236 relations ; graine fixe |
| Machine | portable Windows 11, Docker Desktop, pilote Python `neo4j` 6.4, Python 3.12 ; la stack Compose arrêtée pendant les mesures |
| Latence | temps mur de l'appel Python (pilote + Neo4j + Pydantic), 3 appels d'échauffement puis 30 mesures : p50 et p95 |
| Plans | `PROFILE` des requêtes **exactes** envoyées par le service, capturées pendant l'appel (pas de copie à la main) ; `db hits` = unités de travail dans le stockage, le meilleur indicateur de coût ; `lignes` = lignes renvoyées ; le temps serveur d'un seul `PROFILE` est indicatif |

Reproduire : `make bench-db`, attendre ~30 s, `make bench-data` (~1 min), `make bench` (~5 min) ;
`python -m scripts.benchmark --report-only` réaffiche les tableaux. Le graphe synthétique fait des championnats très
connectés et une **chaîne** de clubs de divisions inférieures non reliée aux championnats : on obtient des paires
proches, des paires très éloignées et des paires sans chemin, les cas qui coûtent dans un plus court chemin.

## 1. Latence de chaque méthode de service

### Services de B

| Méthode | Cas | p50 (ms) | p95 (ms) | db hits | Lignes | Lecture du graphe |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `search` | poste + âge (Defender, ≤ 23 ans) | 14,9 | 21,9 | 12 506 | 21 | `NodeIndexSeek` (position, date_of_birth) |
| `search` | sous-poste + budget (Left-Back, ≤ 5 M€) | 10,1 | 20,0 | 1 085 | 21 | `NodeIndexSeekByRange` (market_value_eur) |
| `search` | compétition (FR1) | 20,1 | 26,6 | 10 303 | 22 | saison la plus récente, puis index de valeur dans l'ordre |
| `search` | sans filtre, page 1 | 7,6 | 12,7 | 609 | 21 | `NodeIndexScan` (market_value_eur, dans l'ordre) |
| `search` | sans filtre, page 2 (curseur) | 9,6 | 20,9 | 584 | 21 | `NodeIndexSeekByRange` |
| `get` | joueur au hasard | 5,2 | 9,9 | 24 | 1 | `NodeUniqueIndexSeek` (player_id) |
| `value_trend` | joueur au hasard | 5,4 | 12,7 | 22 | 2 | `NodeUniqueIndexSeek` |
| `resolve` | nom exact | 14,3 | 36,7 | 2 585 | 5 | index full-text `player_name_ft` |
| `resolve` | nom avec une faute | 17,2 | 34,7 | 1 585 | 5 | index full-text (`~1`) |
| `teammate_path` | 4 sauts | 6,2 | 11,5 | 206 | 1 | 2 `NodeUniqueIndexSeek` + BFS bidirectionnel |
| `teammate_path` | aucun chemin (borne 6) | 7,9 | 12,1 | 1 952 | 0 | idem |
| `latest_season` | saison la plus récente | 3,8 | 4,7 | 2 661 | 1 | `DirectedRelationshipTypeScan` (`PLAYS_IN`) |
| `ClubService.pipeline` | plus gros vendeur | 6,6 | 13,0 | 3 630 | 7 | `NodeUniqueIndexSeek` (club_id) |
| `ClubService.pipeline` | plus gros vendeur, `since` 2024-01-01 | 5,8 | 13,3 | 923 | 6 | idem |
| `ClubService.pipeline` | club au hasard | 7,2 | 9,1 | 2 793 | 7 | idem |
| `ClubService.resolve` | nom de club | 7,4 | 13,1 | 405 | 5 | index full-text `club_name_ft` |
| `ClubService.exists` | club au hasard | 3,5 | 6,6 | 2 | 1 | `NodeUniqueIndexSeek` |

Toutes les méthodes de B répondent en moins de 21 ms au p50 et de 40 ms au p95. Aucune ne lit tout un
label de nœuds : chaque requête part d'une contrainte d'unicité, d'un index de propriété ou d'un index full-text.
`latest_season` (utilisée par la recherche par compétition) lit toutes les relations `PLAYS_IN` (1 330 ici, quelques
milliers sur les vraies données) ; la recherche par compétition fait ensuite une sous-requête `EXISTS` par joueur
candidat, les candidats venant de l'index de valeur, dans l'ordre.

### Lectures des autres rôles (mesurées pour l'équipe)

| Méthode | Cas | p50 (ms) | p95 (ms) | db hits | Lignes | Lecture du graphe |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `GdsService.predicted_moves` (C) | joueur de championnat | 4,0 | 7,8 | 37 | 3 | `NodeUniqueIndexSeek` |
| `GdsService.hubs` (C) | 20 clubs | 6,3 | 9,7 | 1 221 | 20 | `NodeByLabelScan` sur 400 clubs |
| `GdsService.communities` (C) | taille ≥ 2 | 7,8 | 12,6 | 2 001 | 8 | `NodeByLabelScan` sur 400 clubs |
| `GdsService.similar_players` (C) | joueur au hasard | 295,0 | 549,2 | 200 004 | 10 | `NodeByLabelScan` + cosinus sur 50 000 vecteurs |
| `RumourService.for_player` (D) | joueur avec rumeurs | 29,8 | 64,0 | 32 896 | 7 | `NodeIndexSeek` + **`NodeByLabelScan` sur `Article`** |
| `RumourService.for_player` (D) | rumeurs en attente | 18,8 | 25,4 | 22 714 | 5 | idem |
| `RumourService.source_reliability` (D) | 8 sources | 28,2 | 53,6 | 82 950 | 9 | **`NodeByLabelScan` sur `Article`** pour chaque source |

Non mesurées ici : `GdsService.refresh` (chronométré étape par étape en section 5) et
`RumourService.import_articles` (le temps dépend du réseau et du LLM, pas du graphe).

Constat pour D : la fiabilité des sources (`source_reliability`, appelée aussi par `for_player`) cherche
`(:Article {source: $source})` sans index sur `Article.source`, donc parcourt tous les articles pour chaque source.
Sur 3 000 articles, c'est encore 30 ms ; le coût grandit avec la presse importée. Un index `Article(source)` (ajout
permis par le contrat, dans `scripts/constraints.cypher`, fichier de A) le transformerait en `NodeIndexSeek`.

## 2. `EXPLAIN` / `PROFILE` avant et après index

Méthode : l'index est supprimé (`DROP INDEX … IF EXISTS`), la requête profilée et chronométrée, puis l'index est
recréé à partir de **sa** ligne de `scripts/constraints.cypher` et attendu (`db.awaitIndexes`).

### Recherche par poste et âge (Defender, ≤ 23 ans)

| Index présents | Requête | Opérateur de lecture | db hits | Lignes | p50 (ms) |
| --- | --- | --- | ---: | ---: | ---: |
| les deux | `count(*)` de toutes les correspondances | `NodeIndexSeek` (position, date_of_birth) | 4 077 | 1 | 5,7 |
| sans (position, date_of_birth) | `count(*)` | `NodeByLabelScan` + `Filter` | 132 830 | 1 | 30,0 |
| les deux | page 1 de `search` (20 joueurs) | `NodeIndexSeek` (position, date_of_birth) puis tri | 12 506 | 21 | 11,9 |
| sans (position, date_of_birth) | page 1 de `search` | `NodeIndexScan` (market_value_eur, dans l'ordre) + `Filter` | 1 166 | 21 | 6,9 |
| aucun des deux | page 1 de `search` | `NodeByLabelScan` + `Filter` + tri | 141 259 | 21 | 31,3 |
| recréés | page 1 / `count(*)` | `NodeIndexSeek` | 12 506 / 4 077 | 21 / 1 | 12,0 / 4,9 |

Lecture :

- Quand il faut lire **toutes** les correspondances, l'index composite divise les db hits par 33 et le temps par 5 :
  `NodeIndexSeek` lit les ~4 000 défenseurs nés après la date limite au lieu des 50 000 joueurs.
- Pour **une page**, deux index se concurrencent. Le planificateur choisit le composite (puis trie les ~4 000
  candidats) ; sans lui, il parcourt l'index de valeur dans l'ordre et s'arrête dès 21 correspondances
  (1 166 db hits). Les deux plans ont la même latence à 5 ms près : on garde les deux index et on ne force rien
  (`USING INDEX`), car le meilleur choix dépend de la sélectivité (un poste rare favorise le composite).
- Sans aucun index, c'est le pire cas : `NodeByLabelScan` sur 50 000 joueurs, 11 fois plus de db hits et 2,6 fois
  plus lent que le plan choisi avec les deux index (31,3 ms contre 11,9).

### Filière avec `since` et index `Transfer(date)`

| Index | Requête | Opérateur de lecture | db hits | Lignes | p50 (ms) |
| --- | --- | --- | ---: | ---: | ---: |
| avec | `pipeline` du plus gros vendeur depuis 2024-01-01 | `NodeUniqueIndexSeek` (club_id) | 923 | 6 | 5,2 |
| sans | même requête | `NodeUniqueIndexSeek` (club_id) | 923 | 6 | 5,7 |
| avec | `count(*)` des transferts depuis 2025-01-01 (sans club) | `NodeIndexSeekByRange` (date) | 3 910 | 1 | 3,9 |
| sans | même requête | `NodeByLabelScan` + `Filter` | 160 001 | 1 | 19,4 |

Lecture : la filière part du club (contrainte d'unicité) puis suit ses 234 transferts sortants ; c'est plus sélectif
que n'importe quelle plage de dates, donc le planificateur n'utilise pas `Transfer(date)`, avec ou sans lui. L'index
sert dès qu'il n'y a pas de club pour ancrer la requête (« tous les transferts depuis… », requête typique du repli
Cypher de l'agent) : 41 fois moins de db hits, 5 fois plus rapide.

## 3. Pagination : `SKIP` contre curseur

Pages de 20 joueurs sans filtre, triées par valeur décroissante puis `player_id` (20 lignes pour `SKIP`, 21 pour les
curseurs, qui lisent une ligne de plus pour savoir s'il existe une page suivante).

| Page | `SKIP` db hits | `SKIP` p50 (ms) | curseur db hits | curseur p50 (ms) | curseur v1 (coalesce) db hits | v1 p50 (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 43 | 4,9 | 609 | 6,3 | 200 002 | 46,3 |
| 10 | 403 | 3,9 | 607 | 6,1 | 166 473 | 43,9 |
| 100 | 4 009 | 10,0 | 594 | 6,6 | 199 390 | 47,0 |
| 500 | 20 085 | 11,2 | 842 | 5,6 | 168 984 | 41,8 |
| 2 000 | 82 657 | 25,4 | 3 774 | 7,5 | 182 254 | 42,1 |

Lecture :

- `SKIP n` doit produire puis jeter n lignes : le coût croît linéairement avec la profondeur (×1 900 en db hits entre
  la page 1 et la page 2 000).
- Le curseur demande « strictement après (valeur, id) » : un `NodeIndexSeekByRange` sur `market_value_eur` qui démarre
  à la bonne valeur et s'arrête après 21 lignes. Le coût reste quasi constant. À la page 1, `SKIP` est moins cher
  parce que la requête de test ne renvoie que 2 propriétés, quand `search` projette les 21 joueurs complets et leur
  club.
- La hausse à la page 2 000 (3 774 db hits) vient des **égalités** : les valeurs synthétiques sont arrondies à
  50 000 € et des milliers de joueurs partagent la valeur minimale ; l'index ne trie pas ces joueurs par `player_id`,
  il faut donc lire tout le groupe d'égalité. Un index composite `(market_value_eur, player_id)` supprimerait cet
  effet (proposition pour A : `constraints.cypher` est à lui).
- **v1** est la première version de `search` (clé de tri `coalesce(market_value_eur, -1)`) : aucune expression
  calculée n'est servie par un index, chaque page lisait les 50 000 joueurs (`NodeByLabelScan`), 40 à 50 ms quelle
  que soit la page. Ce benchmark a conduit à la réécrire (voir `docs/decisions/b.md`, B7) : 5,6 à 7,5 fois plus
  rapide, et 48 à 336 fois moins de db hits.

## 4. Plus court chemin : borné `*..6` contre non borné

| Paire | Distance réelle | `*..6` : trouvé | `*..6` : db hits | `*` : trouvé | `*` : db hits |
| --- | ---: | :---: | ---: | :---: | ---: |
| championnats | 2 | oui | 38 | oui | 30 |
| championnats | 4 | oui | 206 | oui | 192 |
| championnats | 6 | oui | 2 002 | oui | 1 982 |
| chaîne | 12 | non | 2 484 | oui | 8 515 |
| chaîne | 30 | non | 2 124 | oui | 24 501 |
| deux composantes séparées | aucune | non | 1 952 | non | **286 509** |

Chaque requête renvoie une ligne quand un chemin est trouvé, aucune sinon. Lecture : jusqu'à 6 sauts les deux
requêtes font le même parcours en largeur bidirectionnel. Au-delà, la borne arrête la recherche (et le service répond
404) tandis que la version non bornée continue. Le pire cas est l'absence de chemin : sans borne, Neo4j explore
**toute** la plus petite des deux composantes (ici ~17 500 joueurs) avant de conclure, 147 fois plus de db hits
(47 ms côté serveur contre 2). Sur le vrai graphe, plus dense (C mesure 719 414 relations `PLAYED_WITH` projetées pour
17 856 joueurs), ce cas lirait toute la composante géante.

Sans `shortestPath`, énumérer les chemins coûte (degré moyen)^k (mesuré jusqu'à la borne de 6 sauts, la limite du
projet pour tout motif variable) :

| Longueur maximale | Chemins trouvés | db hits | Lignes |
| ---: | ---: | ---: | ---: |
| 2 | 0 | 69 | 1 |
| 3 | 0 | 557 | 1 |
| 4 | 1 | 4 847 | 1 |
| 5 | 4 | 42 932 | 1 |
| 6 | 12 | 382 115 | 1 |

Chaque saut multiplie le travail par ~9 (le degré moyen est de 8,8). `shortestPath` visite chaque joueur au plus une
fois : c'est lui, et la borne, qui rendent `teammate_path` sûr. (Une ligne : la requête renvoie un `count`.)

## 5. GDS (méthodes de C, mesures promises en relecture)

Recalcul complet (`GdsService.refresh`) avec le client longue durée de l'API, chronométré étape par étape :

| Étape | 1er recalcul après démarrage du serveur (s) | 2e recalcul (s) |
| --- | ---: | ---: |
| projection clubs | 2,50 | 0,07 |
| PageRank | 0,55 | 0,09 |
| Louvain | 1,47 | 1,66 |
| projection coéquipiers | 0,45 | 0,17 |
| FastRP | 1,51 | 0,41 |
| suppressions des projections | 0,06 | 0,02 |
| **total** | **6,55** | **2,44** |

La projection de 400 clubs coûte 2,5 s à froid et 0,07 s ensuite : le premier appel GDS d'un serveur paie un
échauffement (chargement et compilation du code GDS). Un passage précédent donnait 5,8 s puis 2,5 s : l'écart à froid
varie d'un démarrage à l'autre, pas l'ordre de grandeur. C mesure 5,2 s pour la projection des clubs sur le vrai graphe
(12,6 s au total) : une partie est probablement cet échauffement. Chaque étape reste sous les 10 s des lectures, et
l'API utilise de toute façon une limite de 600 s pour ce recalcul.

| `similar_players` | p50 (ms) | p95 (ms) | db hits | Lignes | Remarque |
| --- | ---: | ---: | ---: | ---: | --- |
| cosinus exhaustif (code actuel de C) | 295,0 | 549,2 | 200 004 | 10 | `NodeByLabelScan` + cosinus sur 50 000 vecteurs |
| **index vectoriel** (essai, base de benchmark seulement) | 12,3 | 17,0 | 51 | 10 | rappel@10 = 0,96 face au calcul exact |

`similar_players` est la seule lecture de C qui parcourt tout un label de joueurs ; C mesure p50 307 ms et p95 1,1 s
sur les vraies données (un passage précédent ici : 176 ms au p50, contre 8,5 ms avec l'index vectoriel). Un index
vectoriel Neo4j (`CREATE VECTOR INDEX … OPTIONS {indexConfig: {vector.dimensions: 64,
vector.similarity_function: 'cosine'}}` puis `db.index.vector.queryNodes`) le rend 20 à 24 fois plus rapide selon le
passage, pour 95 à 96 % des mêmes voisins (recherche approchée). Il n'est pas dans le schéma : à proposer à A et C, pas ajouté par B.

## 6. Lire ces plans

Un `PROFILE` se lit de bas en haut. La feuille dit **comment** Neo4j entre dans le graphe :
`NodeUniqueIndexSeek` (une clé unique), `NodeIndexSeek` / `NodeIndexSeekByRange` (valeur ou plage dans un index),
`NodeIndexScan` (tout l'index, éventuellement dans l'ordre, utile avec `ORDER BY … LIMIT`), `NodeByLabelScan` (tous
les nœuds du label : à éviter sur un gros label), `DirectedRelationshipTypeScan` (toutes les relations d'un type).
Un `Filter` juste au-dessus signale des prédicats que l'index n'a pas servis. `db hits` mesure le travail dans le
stockage ; `rows` ce que chaque opérateur produit ; un grand écart entre `Estimated Rows` et `Rows` signale des
statistiques trompeuses.

## Limites

- Données synthétiques : transferts et rumeurs tirés au hasard, valeurs arrondies (d'où les égalités), structure de
  chaîne voulue. Elles fixent des ordres de grandeur et comparent des plans ; les vraies données ne sont pas sur la
  machine de B (fichier DuckDB absent), d'où les chiffres réels de C cités en sections 4 et 5.
- Une seule machine, Docker sous Windows : les p50 et p95 varient de quelques millisecondes d'un passage à l'autre
  (jusqu'à ×1,7 pour `similar_players`) ; les db hits, eux, sont déterministes.
- Le temps serveur d'un `PROFILE` isolé inclut parfois la planification ; les comparaisons s'appuient sur les db hits
  et sur les p50 mesurés sur 30 appels.
