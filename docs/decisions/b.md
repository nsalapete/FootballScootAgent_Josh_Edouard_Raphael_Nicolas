# Décisions du rôle B (API, Docker, performance)

Une ligne par choix non tranché par `CLAUDE.md`, dans l'ordre où il a été pris.

- **B1 · filtres de `search`** : clause `WHERE` assemblée à partir de fragments Cypher **constants** (un par critère
  renseigné), valeurs toujours dans `params`. `EXPLAIN` : `NodeIndexSeek` sur `(position, date_of_birth)` au lieu de
  `NodeByLabelScan` avec la forme `$x IS NULL OR ...`. Aucune donnée utilisateur n'entre dans le texte de la requête.
- **B1 · âge maximal** : `p.date_of_birth > date() - duration({years: $max_age + 1})` (âge ≤ `max_age`), prédicat de
  plage sur la propriété brute, donc compatible avec l'index composite.
- **B1 · compétition** : celle du club actuel (`PLAYS_FOR`) à la saison la plus récente **du graphe**
  (`max(PLAYS_IN.season)`) ; un club absent des compétitions suivies cette saison-là ne correspond pas.
- **B1 · joueurs sans valeur** : inclus, triés en dernier (clé de tri `coalesce(market_value_eur, -1)`).
- **B1 · curseur** : base64 URL-safe sans `=` d'un JSON `{"v": valeur, "id": player_id}` ; curseur illisible →
  `InvalidCursorError` (sous-classe de `ValueError`), traduite en 422 par l'API.
- **B1 · page suivante** : on lit `limit + 1` lignes ; `next_cursor` n'existe que si la ligne en trop existe
  (pas de dernière page vide).
- **B1 · `resolve`** : requête Lucene reconstruite à partir des seuls mots (`\w+`), en minuscules, avec `~1` pour les
  mots de 4 lettres ou plus (accent manquant, faute de frappe) ; les opérateurs Lucene tapés par un humain ou un LLM
  ne passent pas.
- **B1 · `value_trend`** : `LIMIT 500` (règle « LIMIT sur toute liste »), points sans date ou sans montant ignorés.
- **Environnement Windows** : `git config core.autocrlf false` dans le dépôt, sinon pylint compte chaque fin de ligne
  CRLF (note -3,5/10 au lieu de 10/10) ; `make` absent, commandes du Makefile lancées à la main.
- **B2 · borne de `teammate_path`** : la longueur d'un motif variable ne peut pas être un paramètre Cypher. Six
  requêtes constantes (`*..1` à `*..6`) sont construites à l'import à partir d'entiers ; `max_hops` est validé
  (entier, pas booléen, de 1 à 6, sinon `ValueError`) puis sert de **clé** pour choisir l'une d'elles : aucune valeur
  venue d'une requête HTTP n'est écrite dans le texte Cypher, donc pas d'injection possible.
- **B2 · même joueur aux deux bouts** : `shortestPath` lève une erreur ; on renvoie un chemin de 0 saut (le joueur
  seul) s'il existe, `None` sinon.
- **B2 · `matches_together`** : `coalesce(r.matches, 0)` pour rester un `list[int]` même si une arête n'a pas le compte.
- **B2 · filière** : transfert vers un club sans `PLAYS_IN` à la saison du transfert (compétition non suivie) exclu
  de l'agrégat ; prêts comptés ; montant inconnu compté 0 ; tri `transfers` desc, puis montant desc, puis code.
- **B2 · `since`** : inclusif (`t.date >= $since`) ; deux variantes constantes de la requête (avec et sans le filtre)
  au lieu de `$since IS NULL OR ...`, qui cacherait l'index `Transfer(date)` au planificateur.
- **B2 · `ClubService.resolve`** : même construction de requête Lucene que pour les joueurs (`fulltext_query`,
  importée depuis `app/services/players.py`).
- **B3 · 422 propres** : un gestionnaire global de `RequestValidationError` (dans `app/main.py`) renvoie un
  `ErrorResponse` comme toutes les autres erreurs : `detail` lisible (une phrase) et `errors` (champ optionnel ajouté :
  `location`, `field`, `message` par entrée invalide). Swagger documente ce corps pour le 422 de chaque routeur à
  paramètres (`include_router(..., responses=...)`), `HTTPValidationError` disparaît du schéma.
- **B3 · curseur invalide** : `InvalidCursorError` → 422 avec `detail` « Curseur de pagination invalide » (sans `errors` :
  ce n'est pas une erreur de type, le paramètre est une chaîne valide).
- **B3 · 404** : joueur inconnu sur `/path` (chaque extrémité testée seulement si aucun chemin n'est trouvé), club
  inconnu sur `/pipeline` (vérifié seulement si la filière est vide : un club connu qui n'a rien vendu donne `[]`).
  Nouvelle méthode `ClubService.exists`.
- **B3 · routes ajoutées dans mes routeurs** : `GET /api/players/{id}`, `/api/players/{id}/valuations`,
  `/api/players/resolve`, `/api/clubs/resolve` : elles exposent `get`, `value_trend` et `resolve`, donnent de vrais cas
  de 404 et permettent de trouver un id à partir d'un nom pour les exemples `curl`. Aucune route existante modifiée.
- **B3 · code 422** : `HTTPStatus.UNPROCESSABLE_ENTITY` (bibliothèque standard) plutôt que la constante Starlette,
  renommée dans Starlette 1.x et absente des versions plus anciennes.
- **B3 · limites de taille** : `name` de 1 à 100 caractères, `limit` de résolution de 1 à 20.
- **B4 · healthcheck Neo4j** : identifiants substitués par Compose (`${NEO4J_PASSWORD}`) et non par le shell du conteneur
  (`$${...}`), où la variable n'existe pas : avec un autre mot de passe que celui par défaut, Neo4j restait « unhealthy »
  et l'API ne démarrait jamais. Pas de `NEO4J_PASSWORD` dans l'environnement du conteneur Neo4j : l'image transforme
  toute variable `NEO4J_*` en réglage et refuserait de démarrer.
- **B4 · exposition** : seul le proxy publie un port (80) ; l'API n'a qu'un `expose` ; Neo4j (7474, 7687) n'est publié
  que sur `127.0.0.1` pour les développeurs. `restart: unless-stopped` sauf `ingest` (exécution unique).
- **B4 · nginx** : 20 req/s par IP (rafale 40) en général, 10 req/min (rafale 5) pour l'agent, l'admin GDS et l'import
  de rumeurs, 429 au-delà ; délai de 120 s (600 s pour admin et import) ; corps 1 Mo ; `server_tokens off` et en-têtes
  `nosniff`, `DENY`, `no-referrer`.
- **B4 · uvicorn** : `--proxy-headers --forwarded-allow-ips *` pour lire la vraie IP cliente dans `X-Forwarded-For` ;
  acceptable car le port 8000 n'est joignable que depuis le réseau Compose.
- **B4 · `.dockerignore`** (nouveau, fichier Docker donc rôle B) : sans lui, le contexte de build embarquait `.venv`,
  `data/` (fichier DuckDB de plusieurs Go) et `.env`.
- **B4 · Makefile** : chemins du virtualenv selon l'OS (`Scripts/` et `python` sous Windows), cibles `docker-up`
  (arrière-plan, pour les tests de conteneurs) et `docker-logs`, `make help` complet.
- **B4 · tests de conteneurs** : ignorés si rien ne répond sur `http://localhost/api/health` (CI) ; `STACK_URL` pour
  viser une autre adresse.
