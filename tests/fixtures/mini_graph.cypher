// Mini-graphe de test : données FICTIVES qui respectent exactement docs/schema.md.
// Owner : A. Chargé par tests/conftest.py dans le Neo4j de test (vidé avant chaque session de tests).
// Les réponses attendues sont listées dans tests/fixtures/README.md : ne pas les casser.
// Aucun point-virgule dans les chaînes : le chargeur découpe le fichier sur ce caractère.

// ---------- Pays et compétitions ----------
CREATE (:Country {code: 'FRA', name: 'France'}),
       (:Country {code: 'ENG', name: 'England'}),
       (:Country {code: 'POR', name: 'Portugal'}),
       (:Country {code: 'BRA', name: 'Brazil'});

MATCH (fra:Country {code: 'FRA'}), (eng:Country {code: 'ENG'}), (por:Country {code: 'POR'})
CREATE (:Competition {competition_id: 'FR1', name: 'Ligue 1'})-[:IN_COUNTRY]->(fra),
       (:Competition {competition_id: 'GB1', name: 'Premier League'})-[:IN_COUNTRY]->(eng),
       (:Competition {competition_id: 'PO1', name: 'Liga Portugal'})-[:IN_COUNTRY]->(por);

// ---------- Clubs (même championnat de 2019 à 2024, une relation PLAYS_IN par saison) ----------
UNWIND [
  {id: 1, name: 'Olympique Rivage', comp: 'FR1'},
  {id: 2, name: 'AS Montclair', comp: 'FR1'},
  {id: 3, name: 'Stade Brumaire', comp: 'FR1'},
  {id: 4, name: 'Kingsbridge United', comp: 'GB1'},
  {id: 5, name: 'Harrowgate FC', comp: 'GB1'},
  {id: 6, name: 'Wexley Athletic', comp: 'GB1'},
  {id: 7, name: 'SC Alvorada', comp: 'PO1'},
  {id: 8, name: 'CD Mares', comp: 'PO1'}
] AS row
MATCH (comp:Competition {competition_id: row.comp})
CREATE (c:Club {club_id: row.id, name: row.name})
WITH c, comp
UNWIND range(2019, 2024) AS season
CREATE (c)-[:PLAYS_IN {season: season}]->(comp);

// ---------- Joueurs (club actuel et nationalité) ----------
UNWIND [
  {id: 1, name: 'Lucas Marchand', pos: 'Defender', sub: 'Left-Back', dob: '2003-04-12', value: 12000000, country: 'FRA', club: 1},
  {id: 2, name: 'Theo Garnier', pos: 'Midfield', sub: 'Central Midfield', dob: '2001-09-03', value: 25000000, country: 'FRA', club: 4},
  {id: 3, name: 'Joao Ferreira', pos: 'Attack', sub: 'Centre-Forward', dob: '2000-02-17', value: 40000000, country: 'POR', club: 4},
  {id: 4, name: 'Rui Almeida', pos: 'Midfield', sub: 'Defensive Midfield', dob: '1998-11-30', value: 18000000, country: 'POR', club: 5},
  {id: 5, name: 'Diogo Santos', pos: 'Defender', sub: 'Left-Back', dob: '2004-06-21', value: 9000000, country: 'POR', club: 7},
  {id: 6, name: 'Tiago Costa', pos: 'Attack', sub: 'Left Winger', dob: '2002-01-08', value: 15000000, country: 'POR', club: 1},
  {id: 7, name: 'Pedro Lima', pos: 'Goalkeeper', sub: 'Goalkeeper', dob: '1996-05-14', value: 6000000, country: 'BRA', club: 8},
  {id: 8, name: 'Gabriel Souza', pos: 'Defender', sub: 'Centre-Back', dob: '2001-07-29', value: 22000000, country: 'BRA', club: 5},
  {id: 9, name: 'Gabriel Rocha', pos: 'Attack', sub: 'Centre-Forward', dob: '2003-03-02', value: 11000000, country: 'BRA', club: 7},
  {id: 10, name: 'Oliver Hughes', pos: 'Midfield', sub: 'Attacking Midfield', dob: '1999-12-01', value: 30000000, country: 'ENG', club: 6},
  {id: 11, name: 'Harry Whitfield', pos: 'Defender', sub: 'Right-Back', dob: '2000-08-19', value: 14000000, country: 'ENG', club: 3},
  {id: 12, name: 'Jack Moreland', pos: 'Attack', sub: 'Right Winger', dob: '1997-10-10', value: 8000000, country: 'ENG', club: 6},
  {id: 13, name: 'Hugo Lefevre', pos: 'Goalkeeper', sub: 'Goalkeeper', dob: '2002-02-25', value: 5000000, country: 'FRA', club: 2},
  {id: 14, name: 'Nathan Roux', pos: 'Defender', sub: 'Left-Back', dob: '2005-01-15', value: 4000000, country: 'FRA', club: 2},
  {id: 15, name: 'Andre Martins', pos: 'Midfield', sub: 'Central Midfield', dob: '2000-04-04', value: 7000000, country: 'POR', club: 8},
  {id: 16, name: 'Samuel Okafor', pos: 'Attack', sub: 'Centre-Forward', dob: '2006-09-09', value: 1500000, country: 'ENG', club: 3}
] AS row
MATCH (club:Club {club_id: row.club}), (country:Country {code: row.country})
CREATE (p:Player {player_id: row.id, name: row.name, position: row.pos, sub_position: row.sub,
                  date_of_birth: date(row.dob), market_value_eur: row.value})
CREATE (p)-[:PLAYS_FOR]->(club)
CREATE (p)-[:CITIZEN_OF]->(country);

// ---------- Transferts (t07 est un prêt sans montant, t12 a un montant inconnu) ----------
UNWIND [
  {id: 't01', player: 3, src: 7, dst: 4, date: '2022-07-15', season: 2022, fee: 35000000, loan: false},
  {id: 't02', player: 4, src: 7, dst: 5, date: '2021-08-01', season: 2021, fee: 15000000, loan: false},
  {id: 't03', player: 8, src: 8, dst: 5, date: '2023-07-10', season: 2023, fee: 20000000, loan: false},
  {id: 't04', player: 2, src: 1, dst: 4, date: '2023-08-20', season: 2023, fee: 22000000, loan: false},
  {id: 't05', player: 10, src: 2, dst: 6, date: '2020-07-05', season: 2020, fee: 12000000, loan: false},
  {id: 't06', player: 6, src: 7, dst: 1, date: '2023-01-20', season: 2022, fee: 9000000, loan: false},
  {id: 't07', player: 15, src: 7, dst: 8, date: '2022-08-30', season: 2022, fee: null, loan: true},
  {id: 't08', player: 11, src: 6, dst: 3, date: '2024-07-01', season: 2024, fee: 6000000, loan: false},
  {id: 't09', player: 9, src: 8, dst: 7, date: '2024-01-15', season: 2023, fee: 3000000, loan: false},
  {id: 't10', player: 1, src: 2, dst: 1, date: '2024-08-10', season: 2024, fee: 5000000, loan: false},
  {id: 't11', player: 12, src: 5, dst: 6, date: '2019-07-01', season: 2019, fee: 4000000, loan: false},
  {id: 't12', player: 14, src: 3, dst: 2, date: '2023-07-01', season: 2023, fee: null, loan: false}
] AS row
MATCH (p:Player {player_id: row.player}), (src:Club {club_id: row.src}), (dst:Club {club_id: row.dst})
CREATE (t:Transfer {transfer_id: row.id, date: date(row.date), season: row.season, fee_eur: row.fee, is_loan: row.loan})
CREATE (p)-[:MADE]->(t)
CREATE (t)-[:FROM]->(src)
CREATE (t)-[:TO]->(dst);

// ---------- Relation dérivée SENDS_TO (agrégée depuis les transferts, montants nuls exclus des sommes) ----------
MATCH (src:Club)<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club)
WITH src, dst, count(t) AS n, sum(coalesce(t.fee_eur, 0)) AS total
CREATE (src)-[:SENDS_TO {count: n, total_fee_eur: total}]->(dst);

// ---------- Relation dérivée PLAYED_WITH (stockée une fois, du plus petit player_id vers le plus grand) ----------
UNWIND [
  {a: 1, b: 6, matches: 12, first: 2024, last: 2024},
  {a: 1, b: 13, matches: 35, first: 2022, last: 2023},
  {a: 1, b: 14, matches: 20, first: 2023, last: 2023},
  {a: 13, b: 14, matches: 28, first: 2023, last: 2024},
  {a: 2, b: 6, matches: 10, first: 2022, last: 2022},
  {a: 2, b: 3, matches: 30, first: 2023, last: 2024},
  {a: 3, b: 4, matches: 25, first: 2020, last: 2021},
  {a: 4, b: 8, matches: 33, first: 2023, last: 2024},
  {a: 5, b: 6, matches: 8, first: 2022, last: 2022},
  {a: 5, b: 9, matches: 18, first: 2024, last: 2024},
  {a: 7, b: 8, matches: 30, first: 2021, last: 2022},
  {a: 7, b: 15, matches: 40, first: 2022, last: 2024},
  {a: 10, b: 11, matches: 45, first: 2021, last: 2023},
  {a: 10, b: 12, matches: 60, first: 2020, last: 2024}
] AS row
MATCH (a:Player {player_id: row.a}), (b:Player {player_id: row.b})
CREATE (a)-[:PLAYED_WITH {matches: row.matches, first_season: row.first, last_season: row.last}]->(b);

// ---------- Valorisations ----------
UNWIND [
  {player: 1, date: '2022-06-30', value: 3000000},
  {player: 1, date: '2023-06-30', value: 7000000},
  {player: 1, date: '2024-12-15', value: 12000000},
  {player: 3, date: '2021-06-30', value: 15000000},
  {player: 3, date: '2023-06-30', value: 40000000}
] AS row
MATCH (p:Player {player_id: row.player})
CREATE (p)-[:HAS_VALUATION]->(:Valuation {valuation_id: toString(row.player) + '-' + row.date,
                                           date: date(row.date), value_eur: row.value});

// ---------- Rumeurs : r1 en attente, r2 confirmée par le transfert t01 ----------
MATCH (p5:Player {player_id: 5}), (c6:Club {club_id: 6}), (p3:Player {player_id: 3}), (c4:Club {club_id: 4}),
      (t01:Transfer {transfer_id: 't01'})
CREATE (a1:Article {url: 'https://example.org/news/1', source: 'example-sport', title: 'Wexley watching Santos',
                    published_at: date('2024-07-20')})
CREATE (a2:Article {url: 'https://example.org/news/2', source: 'example-sport', title: 'Kingsbridge close in on Ferreira',
                    published_at: date('2022-07-01')})
CREATE (r1:Rumour {rumour_id: 'r1', status: 'pending', confidence: 0.6,
                   quote: 'Wexley Athletic are monitoring left-back Diogo Santos'})
CREATE (r2:Rumour {rumour_id: 'r2', status: 'confirmed', confidence: 0.8,
                   quote: 'Kingsbridge United are close to signing Joao Ferreira'})
CREATE (a1)-[:REPORTS]->(r1), (r1)-[:ABOUT]->(p5), (r1)-[:TARGET]->(c6)
CREATE (a2)-[:REPORTS]->(r2), (r2)-[:ABOUT]->(p3), (r2)-[:TARGET]->(c4), (r2)-[:CONFIRMED_BY]->(t01);

// ---------- Prédiction stockée (hypothèse du modèle, jamais un fait) ----------
MATCH (p9:Player {player_id: 9}), (c5:Club {club_id: 5}), (c7:Club {club_id: 7})
CREATE (p9)-[:PREDICTED_MOVE {prob: 0.42, model_version: 'fixture'}]->(c5)
CREATE (c7)-[:PREDICTED_LINK {prob: 0.37, model_version: 'fixture'}]->(c5);
