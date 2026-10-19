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
