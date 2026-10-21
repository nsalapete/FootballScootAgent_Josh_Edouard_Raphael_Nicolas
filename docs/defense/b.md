# Fiche de soutenance · Rôle B (API, Docker, performance)

Objectif : pouvoir défendre chaque ligne du code de B sans l'avoir écrit. Les choix détaillés sont listés dans
`docs/decisions/b.md` ; ici, le pourquoi, les alternatives rejetées, le coût et les questions probables.

## 1. Ce qui est livré

| Tâche | Contenu | Fichiers |
| --- | --- | --- |
| B1 | Recherche de joueurs filtrée et paginée par curseur, fiche joueur, historique de valeur, résolution de nom | `app/services/players.py`, `tests/test_b_players.py` |
| B2 | Plus court chemin de coéquipiers (≤ 6 sauts), filière de transferts par compétition, résolution de club | `app/services/{players,clubs}.py`, `tests/test_b_clubs.py` |
| B3 | 404 joueur ou club inconnu, 422 uniformes, exemples Swagger, 4 routes de lecture en plus | `app/main.py`, `app/routers/`, `app/models/`, `tests/test_b_api.py` |
| B4 | Stack Docker : proxy nginx seul exposé, API derrière, Neo4j sain ; Makefile compatible Windows | `docker-compose.yml`, `nginx/nginx.conf`, `Dockerfile`, `.dockerignore`, `Makefile`, `tests/test_b_containers.py` |
| B5 | Image de l'API publiée sur GHCR à chaque push sur `main` (et à chaque tag `v*`) | `.github/workflows/publish.yml`, `docs/architecture.md` |
| B6 | Architecture : services, ports, flux, diagramme draw.io et Mermaid, exemples `curl` de chaque endpoint | `docs/architecture.md`, `docs/architecture.drawio` |
| B7 | Graphe synthétique de 50 000 joueurs, benchmarks p50/p95, `PROFILE` avant/après index, `SKIP` contre curseur, chemins bornés ; réécriture de `search` pour paginer dans l'ordre de l'index | `scripts/bench_data.py`, `scripts/benchmark.py`, `docs/perf.md`, `docs/perf_results.json`, `app/services/players.py` |

## 2. Les requêtes et leur coût

| Méthode | Point d'entrée dans le graphe | Index utilisé | Coût |
| --- | --- | --- | --- |
| `search` (poste + âge) | `NodeIndexSeek` | composite `Player(position, date_of_birth)` | proportionnel au nombre de joueurs du poste dans la tranche d'âge, puis tri des `limit + 1` meilleurs (12 506 db hits, 14,9 ms p50 sur 50 000 joueurs) |
| `search` (sans poste ni âge) | `NodeIndexScan` / `NodeIndexSeekByRange` dans l'ordre | `Player(market_value_eur)` | lit l'index dans l'ordre et s'arrête après la page : ~600 db hits, quelle que soit la profondeur |
| `get`, `value_trend` | `NodeUniqueIndexSeek` sur `player_id` | contrainte d'unicité | constant, plus une expansion `HAS_VALUATION` |
| `resolve` | procédure full-text (Lucene) | `player_name_ft` / `club_name_ft` | index inversé, indépendant du nombre de nœuds |
| `teammate_path` | 2 `NodeUniqueIndexSeek` puis BFS bidirectionnel | contrainte d'unicité | ≈ d^(h/2) nœuds visités par côté (d = coéquipiers moyens, h = sauts) |
| `pipeline` | `NodeUniqueIndexSeek` sur le club | contrainte d'unicité (+ `Transfer(date)` possible avec `since`) | nombre de transferts sortants du club, une sous-requête courte par transfert |

**Point clé à défendre : pourquoi la clause `WHERE` de `search` est assemblée.** La version « une seule requête »
`WHERE ($position IS NULL OR p.position = $position) AND ...` est plus courte mais le planificateur ne peut pas
utiliser d'index sur un `OR` qui dépend d'un paramètre : `EXPLAIN` montre `NodeByLabelScan` + `Filter`. En ne gardant
que les prédicats des critères renseignés, `EXPLAIN` montre `NodeIndexSeek`. Les fragments sont des **constantes du
module** ; les valeurs de l'utilisateur ne passent que par `params`. Ce n'est donc pas de l'injection : l'utilisateur
choisit *quels* fragments constants sont gardés, jamais leur texte.

## 3. Pydantic et codes HTTP

| Situation | Qui la détecte | Code | Corps |
| --- | --- | --- | --- |
| Paramètre absent, mauvais type (`from=abc`, `since=pas-une-date`), hors bornes (`max_age=10`, `limit=0`, `max_hops=7`), nom vide ou > 100 caractères | Pydantic / FastAPI, **avant** toute requête | 422 | `ErrorResponse` : `detail` (phrase) + `errors` (`location`, `field`, `message` par entrée) |
| Curseur illisible ou falsifié | `decode_cursor` (service) | 422 | `detail` « Curseur de pagination invalide » |
| Joueur inconnu (`/players/{id}`, `/valuations`, extrémité de `/path`) | routeur, après la requête | 404 | `detail` « Joueur inconnu : id » |
| Aucun chemin dans la borne | routeur | 404 | `detail` « Aucun chemin de coéquipiers en N sauts ou moins » |
| Club inconnu (`/clubs/{id}/pipeline`) | routeur (`ClubService.exists`) | 404 | `detail` « Club inconnu : id » ; un club connu qui n'a rien vendu donne `200 []` |
| Clé d'API absente ou fausse | `require_api_key` | 401 | |
| `API_KEY` non configurée sur le serveur | `require_api_key` | 503 | on refuse plutôt que d'ouvrir (fail closed) |
| Méthode d'un autre rôle pas encore écrite | `NotImplementedError` | 501 | |
| Neo4j arrêté | `ServiceUnavailable` | 503 | « Neo4j est injoignable » |
| Trop de requêtes (proxy) | nginx `limit_req` | 429 | |

Pourquoi 422 et pas 400 : la requête HTTP est bien formée, c'est son **contenu** qui est invalide ; c'est la convention
de FastAPI. Pourquoi uniformiser : `ErrorResponse` est documenté comme « corps de toute erreur », mais le 422 par défaut
de FastAPI renvoie une liste dans `detail`. Un client (ou l'agent) lit maintenant toujours `detail` comme une chaîne.

## 4. `shortestPath` et la borne de 6

- **Fonctionnement** : Neo4j lance un parcours en largeur **bidirectionnel** depuis les deux joueurs, en étendant à chaque
  tour la frontière la plus petite ; le premier nœud commun donne un plus court chemin. Le motif est **non orienté**
  (`-[:PLAYED_WITH*..6]-`) parce que `PLAYED_WITH` est stockée une seule fois, du plus petit id vers le plus grand.
- **Pourquoi 6** : six degrés de séparation, et surtout le coût. Si un joueur a en moyenne d coéquipiers, un parcours de h
  sauts touche de l'ordre de d^h joueurs (d^(h/2) par côté en bidirectionnel). Sans borne, une recherche entre deux
  joueurs non reliés explore toute leur composante connexe avant de répondre « aucun » : c'est le cas le plus cher, et
  c'est justement celui qui renvoie 404. Le mini-graphe vérifie la borne : 13 → 8 = 6 sauts (trouvé), 14 → 7 = 7 sauts
  (aucun résultat).
- **Pourquoi `max_hops` n'est pas une injection** : Cypher n'accepte pas de paramètre dans `*..N`. On construit à l'import
  six requêtes constantes (`*..1` à `*..6`) à partir d'entiers ; `max_hops` est validé (entier, pas booléen, 1 à 6,
  sinon `ValueError`, et 422 côté API grâce à `Query(ge=1, le=6)`) puis sert de **clé de dictionnaire**. Aucun caractère
  venu de la requête HTTP n'entre dans le texte Cypher.
- Même joueur aux deux bouts : `shortestPath` lève une erreur dans Neo4j ; on renvoie un chemin de 0 saut.

## 5. Pourquoi le curseur bat `SKIP`

- `SKIP n LIMIT k` : la base doit produire, trier et **jeter** n lignes avant de rendre la page. La page p coûte
  O(p × k) : plus on avance, plus c'est lent. Et si un joueur est inséré entre deux appels, tout se décale : doublon ou
  joueur sauté.
- Curseur (keyset) : on mémorise la clé de tri de la dernière ligne (`valeur`, `player_id`) et on demande « strictement
  après » : `valeur <= v AND (valeur < v OR player_id > id)`. Le `<= v` est une **plage** que l'index
  `market_value_eur` sait servir dans l'ordre : Neo4j démarre à la bonne valeur et s'arrête après la page. Chaque page
  coûte pareil, et une insertion ne décale rien.
- **Ce que le benchmark a corrigé** : la première version triait sur `coalesce(market_value_eur, -1)` pour placer les
  joueurs sans valeur à la fin. Une expression calculée n'est servie par aucun index : chaque page, curseur ou non,
  lisait les 50 000 joueurs (~180 000 db hits, 40 à 50 ms). Maintenant les joueurs valorisés sont lus dans l'ordre de
  l'index, et les joueurs sans valeur viennent d'une seconde requête lancée seulement quand les premiers sont épuisés.
  Mesure : page 2 000, `SKIP` 82 657 db hits (25 ms), curseur 3 774 (7,5 ms), ancien curseur 182 254 (42 ms).
- Limite connue : à valeur égale, l'index ne trie pas par `player_id` ; un gros groupe d'égalité (beaucoup de joueurs
  à 50 000 €) doit être lu en entier. Un index composite `(market_value_eur, player_id)` réglerait ce cas.
- `player_id` en second critère rend l'ordre **total** (deux joueurs à la même valeur restent ordonnés) : sans lui, le
  curseur pourrait sauter ou répéter des joueurs à égalité.
- On lit `limit + 1` lignes : la ligne en trop dit s'il existe une page suivante, sans requête de comptage et sans
  dernière page vide.
- Le curseur est opaque (base64 URL-safe d'un petit JSON) : le client le renvoie tel quel. S'il le modifie, 422.

## 6. Lire un `PROFILE`

- Le plan se lit **de bas en haut** : les feuilles produisent les lignes, chaque opérateur les transforme.
- Feuilles : `NodeUniqueIndexSeek` (une clé unique), `NodeIndexSeek` / `NodeIndexSeekByRange` (index), `NodeByLabelScan`
  (tous les nœuds du label : à éviter sur un gros label), `AllNodesScan` (pire cas).
- `Filter` juste au-dessus d'un scan : prédicats que l'index n'a pas servis. `Expand(All)` / `VarLengthExpand` : parcours
  de relations. `Top` : `ORDER BY` + `LIMIT` sans tri complet. `Eager` : matérialisation de toutes les lignes.
- Colonnes : `Rows` (lignes produites), `DB Hits` (unités de travail dans le stockage : le bon indicateur de coût),
  `Estimated Rows` (si très loin de `Rows`, les statistiques sont fausses), temps total en ms.
- `EXPLAIN` donne le plan sans exécuter ; `PROFILE` l'exécute et mesure.

## 7. Ce que protègent le proxy et la clé d'API

- **Proxy nginx** (seul port publié, 80) : l'API n'a pas de port publié (`expose` seulement), Neo4j n'écoute que sur
  `127.0.0.1` (navigateur et Bolt pour les développeurs de la machine). Limites de débit par IP : 20 req/s en général,
  10 req/min pour l'agent et les routes d'administration (elles coûtent des crédits LLM ou des minutes de calcul GDS),
  réponse 429 au-delà. Corps limité à 1 Mo, délais d'attente, en-têtes de sécurité (`nosniff`, `DENY` pour les iframes,
  pas de `Referer`), version de nginx masquée.
- **Clé d'API** (`X-API-Key`) : sur les routes qui écrivent dans le graphe ou dépensent du crédit LLM (recalcul GDS,
  import de rumeurs). Comparaison en temps constant (`secrets.compare_digest`) contre les attaques par chronométrage.
  Si `API_KEY` est vide côté serveur, on renvoie 503 au lieu d'ouvrir la route.
- **L'agent** (`/api/agent/ask`, route de D) exige aussi la clé quand le serveur tourne avec
  `AGENT_REQUIRE_API_KEY=true` (production) ; sans ce réglage (développement, tests, démonstration), seule la limite
  de débit de nginx le protège (10 questions par minute et par IP).
- **Ce qu'ils ne protègent pas** (à dire avant qu'on le demande) : pas de TLS (en production : certificat sur le proxy).

## 7 bis. Image publiée et documentation (B5, B6)

- **Ce que fait `publish.yml`** : à chaque push sur `main` (et à chaque tag `v*`), GitHub Actions construit l'image avec le
  `Dockerfile` et la pousse sur GitHub Container Registry. Étiquettes : `latest` (dernier `main`), `sha-<commit>` (une
  image par commit, pour revenir en arrière), `v1.0` (le rendu). `docker/metadata-action` calcule ces étiquettes et les
  labels OCI ; le label `org.opencontainers.image.source` rattache le paquet au dépôt.
- **Authentification** : le `GITHUB_TOKEN` éphémère du workflow, avec `permissions: packages: write` limité à ce
  workflow (les autres gardent les droits par défaut). Aucun secret à créer ni à faire tourner.
- **Nom en minuscules** : GHCR refuse les majuscules ; `${GITHUB_REPOSITORY_OWNER,,}` (bash) convertit le propriétaire.
- **Paquet public** : étape manuelle unique dans les réglages du paquet (GitHub → Packages → football-scout-api →
  Package settings → Change visibility → Public) ; sans elle, `docker pull` demande une connexion.
- **Bug trouvé en route (B4)** : le healthcheck Neo4j lisait le mot de passe dans le shell du conteneur, où la variable
  n'existe pas ; avec un mot de passe différent du défaut, Neo4j restait « unhealthy » et l'API ne démarrait jamais.
  Corrigé en laissant Compose substituer la valeur de `.env`.
- **Diagrammes** : draw.io pour l'édition (XML non compressé, donc lisible dans un diff), Mermaid pour l'affichage
  direct dans GitHub ; les deux décrivent la même architecture.
- **Exemples `curl`** : réponses réelles sur le mini-graphe (reproductibles par n'importe qui avec `make test-db`) ;
  pour l'agent et le recalcul GDS, seulement la forme de la réponse, signalée comme telle.

## 8. Alternatives rejetées

| Choix | Alternative rejetée | Raison |
| --- | --- | --- |
| Fragments `WHERE` constants | `$x IS NULL OR ...` | `NodeByLabelScan` au lieu de `NodeIndexSeek` |
| Tri sur `market_value_eur` (index) + seconde requête pour les joueurs sans valeur | clé `coalesce(market_value_eur, -1)` (première version) | expression non indexable : ~180 000 db hits à chaque page, mesuré en B7 |
| Compétition = celle du club à la saison la plus récente **du graphe** | dernière saison **du club** | un club relégué hors des championnats suivis resterait affiché dans son ancien championnat |
| Joueurs sans valeur triés en dernier | les exclure | la recherche ne doit pas cacher de joueurs |
| Requête Lucene reconstruite mot par mot avec `~1` | passer le texte brut | un `"` ou un `:` tapé par l'utilisateur ou le LLM ferait planter Lucene ; `~1` absorbe un accent manquant (« Mbappe ») |
| Six requêtes constantes pour la borne | f-string sur `max_hops` | même résultat mais aucune chaîne dynamique ; validation explicite |
| 404 vérifié seulement si le résultat est vide | vérifier l'existence avant chaque requête | une requête de moins sur le cas normal |
| Gestionnaire 422 global | 422 par défaut de FastAPI | corps d'erreur unique pour tous les clients |
| `GITHUB_TOKEN` du workflow | jeton personnel (PAT) en secret | rien à créer ni à révoquer, droits limités au dépôt et au workflow |
| Publication sur push vers `main` | attendre la fin du workflow `tests` (`workflow_run`) | consigne de la checklist ; `main` ne reçoit que des PR à CI verte |
| Neo4j publié sur `127.0.0.1` | publié sur toutes les interfaces | la base n'a pas à être joignable depuis le réseau |

## 9. Questions probables

- **« Votre `WHERE` est construit par concaténation : c'est de l'injection ? »** Non : on concatène des constantes du
  module, choisies selon les critères non nuls. Les valeurs vont dans `params`. Démonstration : chercher
  `position="Defender' OR 1=1"` renvoie simplement zéro joueur.
- **« Pourquoi l'âge est calculé dans Cypher et pas en Python ? »** `date() - duration({years: $max_age + 1})` reste un
  prédicat de plage sur la propriété brute `date_of_birth`, donc l'index composite sert. Calculer l'âge de chaque joueur
  (`duration.between(...)`) obligerait à lire tous les joueurs.
- **« Que se passe-t-il si deux joueurs ont la même valeur ? »** Départage par `player_id`, l'ordre est total, la
  pagination reste exacte (testé en parcourant toutes les pages du mini-graphe sans doublon ni trou).
- **« Pourquoi `shortestPath` et pas `allShortestPaths` ? »** On veut une chaîne à montrer, pas toutes ; `allShortestPaths`
  coûte plus et renvoie un nombre de chemins non borné.
- **« Comment savez-vous que l'index est utilisé ? »** `PROFILE` (voir `docs/perf.md`, section 2) : `NodeIndexSeek` sur
  `player_position_dob`, 4 077 db hits pour compter les défenseurs de 23 ans ou moins ; sans l'index,
  `NodeByLabelScan` et 132 830 db hits (×33), 30 ms au lieu de 6.
- **« Pourquoi l'index `Transfer(date)` n'apparaît pas dans le plan de la filière ? »** Parce que la requête part du
  club (contrainte d'unicité) : ses 234 transferts sortants sont plus sélectifs que n'importe quelle plage de dates.
  L'index sert quand il n'y a pas de club pour ancrer la requête (« tous les transferts depuis… ») : 3 910 db hits
  au lieu de 160 001.
- **« Le planificateur fait-il toujours le bon choix ? »** Pas toujours en db hits : pour une page de défenseurs, il
  choisit l'index composite (12 506 db hits) alors que l'index de valeur dans l'ordre en ferait 1 166 ; la latence est
  la même à 5 ms près. On ne force pas `USING INDEX` : avec un poste rare, c'est le composite qui gagne.
- **« Vos benchmarks portent sur des données inventées ? »** Oui, un graphe synthétique conforme au schéma, à graine
  fixe, dans une base séparée (port 7689) : il fixe des ordres de grandeur et compare des plans ; les db hits sont
  déterministes. Les chiffres réels de GDS viennent de C. Le script refuse de vider la base de test ou de l'application.
- **« Pourquoi un proxy si l'API tourne déjà ? »** Un seul point d'entrée, l'API et Neo4j ne sont pas joignables
  directement, limites de débit et en-têtes sans toucher au code Python, et c'est là qu'on mettrait TLS.
- **« À quoi sert l'image publiée ? »** Déployer sans reconstruire : `API_IMAGE=ghcr.io/nsalapete/football-scout-api:latest
  docker compose up -d --no-build` ; l'étiquette `sha-<commit>` permet de revenir à une version précise.
- **« Comment l'API trouve-t-elle Neo4j dans Docker ? »** Par le nom du service sur le réseau Compose
  (`NEO4J_URI=bolt://neo4j:7687`, imposé dans `docker-compose.yml`) ; en local, `bolt://localhost:7687`.
- **« Que se passe-t-il au démarrage si Neo4j est lent ? »** `depends_on: condition: service_healthy` : l'API attend que
  `cypher-shell 'RETURN 1'` réussisse, le proxy attend que `/api/health` réponde.
- **« Pourquoi `--proxy-headers` ? »** Pour qu'uvicorn lise la vraie IP du client dans `X-Forwarded-For` posé par nginx ;
  on fait confiance à tous les émetteurs parce que le port 8000 n'est joignable que depuis le réseau Compose.

## 10. Chiffres de performance

Détail et protocole dans `docs/perf.md` (50 000 joueurs, 220 000 `PLAYED_WITH`, Neo4j 5.26, 30 mesures par cas).

| À retenir | Chiffre |
| --- | --- |
| Méthodes de B | p50 < 21 ms et p95 < 40 ms pour toutes ; aucune ne parcourt un label de nœuds entier |
| Index composite (position, date_of_birth) | 4 077 db hits contre 132 830 sans (×33), 6 ms contre 30 |
| Pagination, page 2 000 | `SKIP` 82 657 db hits / 25 ms ; curseur 3 774 / 7,5 ms |
| Réécriture de `search` (B7) | ancien curseur ~180 000 db hits à chaque page ; nouveau 48 à 336 fois moins, 5,6 à 7,5 fois plus rapide |
| Chemin sans solution | borné `*..6` : 1 952 db hits ; non borné : 286 509 (×147) |
| Tous les chemins sans `shortestPath` | ×9 par saut (degré moyen 8,8) : 382 115 db hits à 6 sauts |
| Recalcul GDS (C) | 5,8 à 6,5 s au premier appel après démarrage, 2,4 à 2,5 s ensuite ; chaque étape < 10 s |
| `similar_players` (C) | 176 à 295 ms p50 en cosinus exhaustif ; 8,5 à 12,3 ms avec un index vectoriel (rappel@10 = 0,95 à 0,96) |
| Rumeurs (D) | `for_player` 30 ms p50 mais `NodeByLabelScan` sur `Article` (pas d'index sur `source`) : à signaler à D et A |

## 11. Relecture des PR de C (#10, #11, #12)

Points remontés à C, utiles si le jury interroge sur la cohérence entre rôles :

- Le recalcul GDS suppose que chaque étape tient sous le délai de 10 s du client : à mesurer sur le gros graphe,
  sinon `TransactionTimedOut` donne un 500 ; un client dédié avec un délai plus long est proposé.
- `similar_players` parcourt tous les joueurs à chaque appel ; un index vectoriel Neo4j (`CREATE VECTOR INDEX`) le
  remplacerait par une recherche approchée.
- Le jeu de données ML définit les clubs par leur championnat **actuel** (`domestic_competition_id`) et ne filtre ni la
  saison de départ ni la qualité des transferts, alors que le graphe part des matchs joués depuis `SCOPE_FROM_SEASON` :
  les deux univers de clubs diffèrent.
- Le graphe contient aussi des clubs « légers » hors périmètre (acheteurs étrangers) : `hubs` peut les classer.

## 12. Seconde relecture de C (#23, #25, #27, #28, #29)

- Les fenêtres de caractéristiques du modèle n'ont plus la même longueur (6 saisons en entraînement, 8 en test) depuis
  le filtre `SCOPE_FROM_SEASON` : les degrés grossissent avec la fenêtre.
- Le nettoyage dépend de `date.today()` : le jeu de données change d'un jour à l'autre.
- `PREDICTED_MOVE` est un score de **paire de clubs** recopié sur chaque joueur : à signaler dans l'API et à l'agent ;
  la probabilité n'a pas d'horizon stocké (deux saisons).
- Les 5,2 s de la projection des clubs mesurées par C contiennent probablement l'échauffement de GDS (×15 à ×35 entre le
  premier et le second recalcul sur le graphe de benchmark).
