# Performance

Owner : B. Tous les chiffres viennent d'un même passage de `make bench` (fichier brut :
[`perf_results.json`](perf_results.json)) sur le graphe synthétique de `scripts/bench_data.py`.

## Protocole

| | |
| --- | --- |
| Base | Neo4j 5.26.32 + GDS dans Docker (`make bench-db`, port 7689, tas 2 Go), jamais la base de test (7688) |
| Graphe | 50 000 joueurs, 400 clubs (7 championnats de 20 clubs + 260 clubs de divisions inférieures), 80 000 transferts, 149 339 valorisations, 220 000 `PLAYED_WITH`, 63 165 `SENDS_TO` ; 279 756 nœuds, 773 841 relations ; graine fixe |
| Machine | portable Windows 11, Docker Desktop, pilote Python `neo4j` 6.4, Python 3.12 ; la stack Compose arrêtée pendant les mesures |
| Latence | temps mur de l'appel Python (pilote + Neo4j + Pydantic), 3 appels d'échauffement puis 30 mesures : p50 et p95 |
| Plans | `PROFILE` des requêtes **exactes** envoyées par le service, capturées pendant l'appel (pas de copie à la main) ; `db hits` = unités de travail dans le stockage, le meilleur indicateur de coût ; le temps serveur d'un seul `PROFILE` est indicatif |

Reproduire : `make bench-db`, attendre ~30 s, `make bench-data` (45 s), `make bench` (~5 min) ;
`python -m scripts.benchmark --report-only` réaffiche les tableaux. Le graphe synthétique fait des championnats très
connectés et une **chaîne** de clubs de divisions inférieures non reliée aux championnats : on obtient des paires
proches, des paires très éloignées et des paires sans chemin, les cas qui coûtent dans un plus court chemin.

## 1. Latence des méthodes de service

| Méthode | Cas | p50 (ms) | p95 (ms) | db hits | Lecture du graphe |
| --- | --- | ---: | ---: | ---: | --- |
| `search` | poste + âge (Defender, ≤ 23 ans) | 17,5 | 33,7 | 12 443 | `NodeIndexSeek` (position, date_of_birth) |
| `search` | sous-poste + budget (Left-Back, ≤ 5 M€) | 22,9 | 45,1 | 1 022 | `NodeIndexSeekByRange` (market_value_eur) |
| `search` | compétition (FR1) | 34,7 | 67,4 | 9 662 | saison la plus récente, puis index de valeur dans l'ordre |
| `search` | sans filtre, page 1 | 9,2 | 16,4 | 543 | `NodeIndexScan` (market_value_eur, dans l'ordre) |
| `search` | sans filtre, page 2 (curseur) | 7,7 | 22,7 | 521 | `NodeIndexSeekByRange` |
| `get` | joueur au hasard | 6,8 | 12,1 | 21 | `NodeUniqueIndexSeek` (player_id) |
| `value_trend` | joueur au hasard | 6,1 | 11,1 | 19 | `NodeUniqueIndexSeek` |
| `resolve` | nom exact | 15,2 | 45,1 | 2 585 | index full-text `player_name_ft` |
| `resolve` | nom avec une faute | 10,6 | 17,3 | 1 585 | index full-text (`~1`) |
| `teammate_path` | 4 sauts | 6,8 | 18,6 | 172 | 2 `NodeUniqueIndexSeek` + BFS bidirectionnel |
| `teammate_path` | aucun chemin (borne 6) | 6,6 | 9,2 | 1 786 | idem |
| `pipeline` | plus gros vendeur | 7,6 | 18,4 | 3 627 | `NodeUniqueIndexSeek` (club_id) |
| `pipeline` | plus gros vendeur, `since` 2024-01-01 | 5,4 | 9,1 | 922 | idem |
| `pipeline` | club au hasard | 7,0 | 19,6 | 2 791 | idem |
| `ClubService.resolve` | nom de club | 7,4 | 9,4 | 405 | index full-text `club_name_ft` |

Toutes les méthodes de B répondent en moins de 35 ms au p50 et 70 ms au p95. Aucune ne lit tout un label de nœuds :
chaque requête part d'une contrainte d'unicité, d'un index de propriété ou d'un index full-text. La plus lente,
la recherche par compétition, lit d'abord toutes les relations `PLAYS_IN` pour trouver la saison la plus récente
(`DirectedRelationshipTypeScan`, 1 330 relations ici, quelques milliers sur les vraies données), puis fait une
sous-requête `EXISTS` par joueur candidat ; les candidats viennent de l'index de valeur, dans l'ordre.

## 2. `EXPLAIN` / `PROFILE` avant et après index

Méthode : l'index est supprimé (`DROP INDEX … IF EXISTS`), la requête profilée et chronométrée, puis l'index est
recréé à partir de **sa** ligne de `scripts/constraints.cypher` et attendu (`db.awaitIndexes`).

### Recherche par poste et âge (Defender, ≤ 23 ans)

| Index présents | Requête | Opérateur de lecture | db hits | p50 (ms) |
| --- | --- | --- | ---: | ---: |
| les deux | `count(*)` de toutes les correspondances | `NodeIndexSeek` (position, date_of_birth) | 4 077 | 5,3 |
| sans (position, date_of_birth) | `count(*)` | `NodeByLabelScan` + `Filter` | 132 830 | 30,1 |
| les deux | page 1 de `search` (20 joueurs) | `NodeIndexSeek` (position, date_of_birth) puis tri | 12 443 | 10,5 |
| sans (position, date_of_birth) | page 1 de `search` | `NodeIndexScan` (market_value_eur, dans l'ordre) + `Filter` | 1 103 | 7,1 |
| aucun des deux | page 1 de `search` | `NodeByLabelScan` + `Filter` + tri | 141 196 | 34,6 |
| recréés | page 1 / `count(*)` | `NodeIndexSeek` | 12 443 / 4 077 | 9,5 / 5,3 |

Lecture :

- Quand il faut lire **toutes** les correspondances, l'index composite divise les db hits par 33 et le temps par 6 :
  `NodeIndexSeek` lit les ~4 000 défenseurs nés après la date limite au lieu des 50 000 joueurs.
- Pour **une page**, deux index se concurrencent. Le planificateur choisit le composite (puis trie les ~4 000
  candidats) ; sans lui, il parcourt l'index de valeur dans l'ordre et s'arrête dès 21 correspondances
  (1 103 db hits). Les deux plans ont la même latence à 3,5 ms près : on garde les deux index et on ne force rien
  (`USING INDEX`), car le meilleur choix dépend de la sélectivité (un poste rare favorise le composite).
- Sans aucun index, c'est le pire cas : `NodeByLabelScan` sur 50 000 joueurs, 11 fois plus de db hits et 3,3 fois
  plus lent que le plan choisi avec les deux index (34,6 ms contre 10,5).

### Filière avec `since` et index `Transfer(date)`

| Index | Requête | Opérateur de lecture | db hits | p50 (ms) |
| --- | --- | --- | ---: | ---: |
| avec | `pipeline` du plus gros vendeur depuis 2024-01-01 | `NodeUniqueIndexSeek` (club_id) | 922 | 4,6 |
| sans | même requête | `NodeUniqueIndexSeek` (club_id) | 922 | 4,0 |
| avec | `count(*)` des transferts depuis 2025-01-01 (sans club) | `NodeIndexSeekByRange` (date) | 3 910 | 4,8 |
| sans | même requête | `NodeByLabelScan` + `Filter` | 160 001 | 16,6 |

Lecture : la filière part du club (contrainte d'unicité) puis suit ses 234 transferts sortants ; c'est plus sélectif
que n'importe quelle plage de dates, donc le planificateur n'utilise pas `Transfer(date)`, avec ou sans lui. L'index
sert dès qu'il n'y a pas de club pour ancrer la requête (« tous les transferts depuis… », requête typique du repli
Cypher de l'agent) : 41 fois moins de db hits.

## 3. Pagination : `SKIP` contre curseur

Pages de 20 joueurs sans filtre, triées par valeur décroissante puis `player_id`.

| Page | `SKIP` db hits | `SKIP` p50 (ms) | curseur db hits | curseur p50 (ms) | curseur v1 (coalesce) db hits | v1 p50 (ms) |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 43 | 4,9 | 543 | 6,3 | 200 002 | 43,2 |
| 10 | 403 | 4,3 | 544 | 5,6 | 166 473 | 39,1 |
| 100 | 4 009 | 8,5 | 529 | 6,2 | 199 390 | 42,6 |
| 500 | 20 085 | 13,3 | 782 | 6,0 | 168 984 | 40,2 |
| 2 000 | 82 657 | 27,1 | 3 752 | 9,4 | 182 254 | 97,3 |

Lecture :

- `SKIP n` doit produire puis jeter n lignes : le coût croît linéairement avec la profondeur (×1 900 en db hits entre
  la page 1 et la page 2 000).
- Le curseur demande « strictement après (valeur, id) » : un `NodeIndexSeekByRange` sur `market_value_eur` qui démarre
  à la bonne valeur et s'arrête après 21 lignes. Le coût reste quasi constant. À la page 1, `SKIP` est moins cher
  parce que la requête de test ne renvoie que 2 propriétés, quand `search` projette les 21 joueurs complets et leur
  club.
- La hausse à la page 2 000 (3 752 db hits) vient des **égalités** : les valeurs synthétiques sont arrondies à
  50 000 € et des milliers de joueurs partagent la valeur minimale ; l'index ne trie pas ces joueurs par `player_id`,
  il faut donc lire tout le groupe d'égalité. Un index composite `(market_value_eur, player_id)` supprimerait cet
  effet (proposition pour A : `constraints.cypher` est à lui).
- **v1** est la première version de `search` (clé de tri `coalesce(market_value_eur, -1)`) : aucune expression
  calculée n'est servie par un index, chaque page lisait les 50 000 joueurs (`NodeByLabelScan`), 40 à 97 ms quelle que
  soit la page. Ce benchmark a conduit à la réécrire (voir `docs/decisions/b.md`, B7) : 7 à 10 fois plus rapide,
  et 50 à 380 fois moins de db hits.

## 4. Plus court chemin : borné `*..6` contre non borné

| Paire | Distance réelle | `*..6` : trouvé | `*..6` : db hits | `*` : trouvé | `*` : db hits |
| --- | ---: | :---: | ---: | :---: | ---: |
| championnats | 2 | oui | 32 | oui | 24 |
| championnats | 4 | oui | 172 | oui | 158 |
| championnats | 6 | oui | 1 698 | oui | 1 678 |
| chaîne | 12 | non | 2 465 | oui | 8 465 |
| chaîne | 30 | non | 2 108 | oui | 24 376 |
| deux composantes séparées | aucune | non | 1 786 | non | **284 607** |

Lecture : jusqu'à 6 sauts les deux requêtes font le même parcours en largeur bidirectionnel. Au-delà, la borne
arrête la recherche (et le service répond 404) tandis que la version non bornée continue. Le pire cas est l'absence de
chemin : sans borne, Neo4j explore **toute** la plus petite des deux composantes (ici ~17 500 joueurs) avant de
conclure, 160 fois plus de db hits (84 ms côté serveur contre 5). Sur le vrai graphe, plus dense (C mesure
719 414 relations `PLAYED_WITH` projetées pour 17 856 joueurs), ce cas lirait toute la composante géante.

Sans `shortestPath`, énumérer les chemins coûte (degré moyen)^k :

| Longueur maximale | Chemins trouvés | db hits | Temps serveur (ms) |
| ---: | ---: | ---: | ---: |
| 2 | 0 | 69 | — |
| 3 | 0 | 557 | 68 |
| 4 | 1 | 4 847 | 77 |
| 5 | 4 | 42 932 | 118 |
| 6 | 12 | 382 115 | 203 |
| 7 | 124 | 3 413 501 | 1 657 |

Chaque saut multiplie le travail par ~9 (le degré moyen est de 8,8). `shortestPath` visite chaque joueur au plus une
fois : c'est lui, et la borne, qui rendent `teammate_path` sûr. (Le temps à 2 sauts, 210 ms, comprend la compilation
de la requête : non significatif.)

## 5. GDS (méthodes de C, mesures promises en relecture)

Recalcul complet (`GdsService.refresh`) avec le client longue durée de l'API, chronométré étape par étape :

| Étape | 1er recalcul après démarrage du serveur (s) | 2e recalcul (s) |
| --- | ---: | ---: |
| projection clubs | 1,61 | 0,11 |
| PageRank | 0,55 | 0,14 |
| Louvain | 1,48 | 1,29 |
| projection coéquipiers | 0,65 | 0,17 |
| FastRP | 1,41 | 0,75 |
| suppressions des projections | 0,11 | 0,04 |
| **total** | **5,81** | **2,50** |

La projection de 400 clubs coûte 1,61 s à froid et 0,11 s ensuite : le premier appel GDS d'un serveur paie un
échauffement (chargement et compilation du code GDS). C mesure 5,2 s pour la projection des clubs sur le vrai graphe
(12,6 s au total) : une partie est probablement cet échauffement. Chaque étape reste sous les 10 s des lectures, et
l'API utilise de toute façon une limite de 600 s pour ce recalcul.

| Lecture GDS | p50 (ms) | p95 (ms) | db hits | Remarque |
| --- | ---: | ---: | ---: | --- |
| `hubs` | 8,3 | 13,7 | 1 221 | lit les 400 clubs |
| `communities` | 9,5 | 15,6 | 2 001 | lit les 400 clubs |
| `similar_players` (cosinus exhaustif) | 176,4 | 276,9 | 200 004 | `NodeByLabelScan` + cosinus sur 50 000 vecteurs |
| idem par **index vectoriel** (essai, base de benchmark seulement) | 8,5 | 11,7 | 51 | rappel@10 = 0,95 face au calcul exact |

`similar_players` est la seule lecture qui parcourt tout un label ; C mesure p50 307 ms et p95 1,1 s sur les vraies
données. Un index vectoriel Neo4j (`CREATE VECTOR INDEX … OPTIONS {indexConfig: {vector.dimensions: 64,
vector.similarity_function: 'cosine'}}` puis `db.index.vector.queryNodes`) le rend 20 fois plus rapide pour 95 % des
mêmes voisins (recherche approchée). Il n'est pas dans le schéma : à proposer à A et C, pas ajouté par B.

## 6. Lire ces plans

Un `PROFILE` se lit de bas en haut. La feuille dit **comment** Neo4j entre dans le graphe :
`NodeUniqueIndexSeek` (une clé unique), `NodeIndexSeek` / `NodeIndexSeekByRange` (valeur ou plage dans un index),
`NodeIndexScan` (tout l'index, éventuellement dans l'ordre, utile avec `ORDER BY … LIMIT`), `NodeByLabelScan` (tous
les nœuds du label : à éviter sur un gros label). Un `Filter` juste au-dessus signale des prédicats que l'index n'a pas
servis. `db hits` mesure le travail dans le stockage ; `rows` ce que chaque opérateur produit ; un grand écart entre
`Estimated Rows` et `Rows` signale des statistiques trompeuses.

## Limites

- Données synthétiques : transferts tirés au hasard, valeurs arrondies (d'où les égalités), structure de chaîne voulue.
  Elles fixent des ordres de grandeur et comparent des plans ; les vraies données ne sont pas sur la machine de B
  (fichier DuckDB absent), d'où les chiffres réels de C cités en section 5.
- Une seule machine, Docker sous Windows : les p95 sont bruités ; les db hits, eux, sont déterministes.
- Le temps serveur d'un `PROFILE` isolé inclut parfois la planification ; les comparaisons s'appuient sur les db hits
  et sur les p50 mesurés sur 30 appels.
