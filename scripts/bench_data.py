"""Synthetic graph for the benchmarks, conforming to `docs/schema.md`. Owner: B.

Run with `python -m scripts.bench_data` (or `make bench-data`) after `make bench-db`. The target database is
**wiped first**, so the script refuses the application database (port 7687) and the test database (port 7688):
the test database must stay small, and the benchmarks run on their own Neo4j (port 7689).

Shape with the default options (fixed seed, so two runs give the same graph):

- 7 competitions of 20 clubs (`PLAYS_IN` for seasons 2015 to 2024; the last two clubs of each league go down
  after 2019) and 260 lower-division clubs outside the competitions;
- 50,000 players (positions in real proportions, about 5 % without a market value);
- 220,000 `PLAYED_WITH`: mostly within a club, then within a league; the lower-division clubs form a chain
  (each one only shares players with its neighbours) that is not connected to the leagues, which gives far-apart
  pairs and pairs without any path, the two expensive cases of a shortest-path search;
- 80,000 transfers (15 % loans, 15 % unknown fees), about 150,000 valuations, `SENDS_TO` aggregated as in the
  ingestion.
"""

import argparse
import random
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.database.neo4j import Neo4jClient
from scripts.cypher_files import run_file

CONSTRAINTS = Path(__file__).resolve().parent / "constraints.cypher"
DEFAULT_URI = "bolt://localhost:7689"
DEFAULT_PASSWORD = "benchpassword"
PROTECTED_PORTS = {7687: "la base de l'application", 7688: "la base de test"}
BATCH_SIZE = 10_000
SEASONS = range(2015, 2025)
FIRST_LEAGUE_ID = 1

COUNTRIES = [
    ("ENG", "England"),
    ("ESP", "Spain"),
    ("ITA", "Italy"),
    ("GER", "Germany"),
    ("FRA", "France"),
    ("POR", "Portugal"),
    ("NED", "Netherlands"),
    ("BRA", "Brazil"),
    ("ARG", "Argentina"),
    ("BEL", "Belgium"),
]
COMPETITIONS = [
    ("GB1", "Premier League", "ENG"),
    ("ES1", "LaLiga", "ESP"),
    ("IT1", "Serie A", "ITA"),
    ("L1", "Bundesliga", "GER"),
    ("FR1", "Ligue 1", "FRA"),
    ("PO1", "Liga Portugal", "POR"),
    ("NL1", "Eredivisie", "NED"),
]
POSITIONS = [
    ("Goalkeeper", ["Goalkeeper"], 0.10),
    ("Defender", ["Centre-Back", "Left-Back", "Right-Back"], 0.33),
    ("Midfield", ["Central Midfield", "Defensive Midfield", "Attacking Midfield"], 0.33),
    ("Attack", ["Centre-Forward", "Left Winger", "Right Winger"], 0.24),
]
FIRST_NAMES = (
    "Lucas Gabriel Mateo Leo Hugo Noah Adam Enzo Tiago Diogo Joao Pedro Rui Andre Bruno Nuno Rafael Daniel "
    "Marco Luca Matteo Lorenzo Alessandro Federico Jonas Leon Finn Felix Lukas Jan Max Tom Harry Jack Oliver "
    "George James Kai Mohamed Youssef Ismael Karim Samuel Victor Pablo Sergio Alvaro Diego Carlos Javier Ivan"
).split()
LAST_NAMES = (
    "Martin Bernard Dubois Thomas Robert Richard Petit Durand Leroy Moreau Simon Laurent Lefebvre Michel Garcia "
    "Silva Santos Ferreira Pereira Oliveira Costa Rodrigues Almeida Nunes Gomes Rossi Russo Ferrari Esposito "
    "Bianchi Romano Colombo Ricci Marino Greco Muller Schmidt Schneider Fischer Weber Meyer Wagner Becker Hoffmann "
    "Smith Jones Taylor Brown Williams Wilson Johnson Davies Evans Walker Hughes de Jong Jansen de Vries van Dijk "
    "Bakker Visser Smit Meijer Mulder Fernandez Lopez Martinez Sanchez Perez Gonzalez Gomez Diaz Moreno Alonso"
).split()


@dataclass(frozen=True)
class Config:
    """Size of the synthetic graph."""

    players: int = 50_000
    played_with: int = 220_000
    clubs_per_competition: int = 20
    lower_clubs: int = 260
    transfers: int = 80_000
    valuations_per_player: int = 3
    seed: int = 42


Rows = list[dict[str, Any]]


def season_of(day: date) -> int:
    """Start year of the football season of `day` (seasons start on 1 July)."""
    return day.year if day.month >= 7 else day.year - 1


def random_day(rng: random.Random, first: date, last: date) -> date:
    """A uniformly random day between `first` and `last` (both included)."""
    return first + timedelta(days=rng.randint(0, (last - first).days))


def money(rng: random.Random, median: float, cap: int) -> int:
    """Log-normal amount in euros, rounded to 50,000 and capped: most players are cheap, a few are very dear."""
    return min(cap, max(50_000, round(rng.lognormvariate(0, 1.2) * median / 50_000) * 50_000))


def generate_clubs(config: Config) -> dict[str, Rows]:
    """Countries, competitions, clubs and their `PLAYS_IN` per season."""
    clubs, plays_in = [], []
    club_id = FIRST_LEAGUE_ID
    for code, name, _ in COMPETITIONS:
        for rank in range(config.clubs_per_competition):
            clubs.append({"id": club_id, "name": f"{name} Club {rank + 1:02d}"})
            relegated = rank >= config.clubs_per_competition - 2
            for season in SEASONS:
                if not relegated or season < 2020:
                    plays_in.append({"club": club_id, "comp": code, "season": season})
            club_id += 1
    for rank in range(config.lower_clubs):
        clubs.append({"id": club_id, "name": f"Lower Division Club {rank + 1:03d}"})
        club_id += 1
    return {
        "countries": [{"code": code, "name": name} for code, name in COUNTRIES],
        "competitions": [{"id": code, "name": name, "country": country} for code, name, country in COMPETITIONS],
        "clubs": clubs,
        "plays_in": plays_in,
    }


def generate_players(config: Config, rng: random.Random, league_clubs: int) -> Rows:
    """Players with a position, a birth date, a market value (or none) and a current club."""
    total_clubs = league_clubs + config.lower_clubs
    weights = [weight for _, _, weight in POSITIONS]
    players = []
    for player_id in range(1, config.players + 1):
        position, subs, _ = rng.choices(POSITIONS, weights)[0]
        in_league = rng.random() < 0.65
        club = rng.randint(1, league_clubs) if in_league else rng.randint(league_clubs + 1, total_clubs)
        players.append(
            {
                "id": player_id,
                "name": f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}",
                "position": position,
                "sub_position": rng.choice(subs),
                "dob": random_day(rng, date(1984, 1, 1), date(2008, 12, 31)),
                "value": None if rng.random() < 0.05 else money(rng, 2_000_000 if in_league else 300_000, 200_000_000),
                "club": club,
                "country": rng.choice(COUNTRIES)[0],
            }
        )
    return players


def teammate_of(club: int, rng: random.Random, members: dict[int, list[int]], config: Config) -> int:
    """A player likely to have shared a dressing room with a player of `club` (see the module docstring)."""
    league_clubs = len(COMPETITIONS) * config.clubs_per_competition
    roll = rng.random()
    if club <= league_clubs:
        league_start = (club - 1) // config.clubs_per_competition * config.clubs_per_competition + 1
        if roll < 0.80:
            other = club
        elif roll < 0.95:
            other = rng.randint(league_start, league_start + config.clubs_per_competition - 1)
        else:
            other = rng.randint(1, league_clubs)
    else:
        neighbour = club + rng.choice((-1, 1))
        other = club if roll < 0.85 or not league_clubs < neighbour <= league_clubs + config.lower_clubs else neighbour
    return rng.choice(members[other]) if members.get(other) else -1


def generate_played_with(config: Config, rng: random.Random, players: Rows) -> Rows:
    """`PLAYED_WITH` stored once, from the smaller to the larger player id."""
    members: dict[int, list[int]] = {}
    for player in players:
        members.setdefault(player["club"], []).append(player["id"])
    clubs_of = {player["id"]: player["club"] for player in players}
    ids = [player["id"] for player in players]
    pairs: set[tuple[int, int]] = set()
    attempts = 0
    while len(pairs) < config.played_with and attempts < config.played_with * 5:
        attempts += 1
        first = rng.choice(ids)
        second = teammate_of(clubs_of[first], rng, members, config)
        if second > 0 and second != first:
            pairs.add((min(first, second), max(first, second)))
    rows = []
    for low, high in sorted(pairs):
        first_season = rng.choice(SEASONS)
        rows.append(
            {
                "a": low,
                "b": high,
                "matches": rng.randint(1, 80),
                "first": first_season,
                "last": min(SEASONS[-1], first_season + rng.randint(0, 3)),
            }
        )
    return rows


def generate_transfers(config: Config, rng: random.Random, total_clubs: int) -> Rows:
    """Transfers between two different clubs, July 2015 to June 2025 (seasons 2015 to 2024)."""
    rows = []
    for index in range(config.transfers):
        source = rng.randint(1, total_clubs)
        target = rng.randint(1, total_clubs - 1)
        target += target >= source
        day = random_day(rng, date(2015, 7, 1), date(2025, 6, 30))
        loan = rng.random() < 0.15
        unknown = loan or rng.random() < 0.15
        rows.append(
            {
                "id": f"bench-t{index:06d}",
                "player": rng.randint(1, config.players),
                "src": source,
                "dst": target,
                "date": day,
                "season": season_of(day),
                "fee": None if unknown else money(rng, 1_500_000, 150_000_000),
                "loan": loan,
            }
        )
    return rows


def generate_valuations(config: Config, rng: random.Random, players: Rows) -> Rows:
    """A few market values per player at distinct dates (id `<player_id>-<date>` as in the ingestion)."""
    rows = []
    for player in players:
        count = rng.randint(1, 2 * config.valuations_per_player - 1)
        days = sorted({random_day(rng, date(2015, 1, 1), date(2025, 6, 30)) for _ in range(count)})
        base = player["value"] or 500_000
        for day in days:
            rows.append(
                {
                    "id": f"{player['id']}-{day.isoformat()}",
                    "player": player["id"],
                    "date": day,
                    "value": money(rng, base, 250_000_000),
                }
            )
    return rows


def generate(config: Config) -> dict[str, Rows]:
    """Every node and relationship of the synthetic graph, as rows ready for `UNWIND`."""
    rng = random.Random(config.seed)
    data = generate_clubs(config)
    league_clubs = len(COMPETITIONS) * config.clubs_per_competition
    data["players"] = generate_players(config, rng, league_clubs)
    data["played_with"] = generate_played_with(config, rng, data["players"])
    data["transfers"] = generate_transfers(config, rng, league_clubs + config.lower_clubs)
    data["valuations"] = generate_valuations(config, rng, data["players"])
    return data


WIPE = "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(*) AS deleted"
LOADS = [
    ("countries", "UNWIND $rows AS row CREATE (:Country {code: row.code, name: row.name})"),
    (
        "competitions",
        """
UNWIND $rows AS row MATCH (country:Country {code: row.country})
CREATE (:Competition {competition_id: row.id, name: row.name})-[:IN_COUNTRY]->(country)
""",
    ),
    ("clubs", "UNWIND $rows AS row CREATE (:Club {club_id: row.id, name: row.name})"),
    (
        "plays_in",
        """
UNWIND $rows AS row MATCH (club:Club {club_id: row.club}), (comp:Competition {competition_id: row.comp})
CREATE (club)-[:PLAYS_IN {season: row.season}]->(comp)
""",
    ),
    (
        "players",
        """
UNWIND $rows AS row MATCH (club:Club {club_id: row.club}), (country:Country {code: row.country})
CREATE (p:Player {player_id: row.id, name: row.name, position: row.position, sub_position: row.sub_position,
                  date_of_birth: row.dob, market_value_eur: row.value})
CREATE (p)-[:PLAYS_FOR]->(club)
CREATE (p)-[:CITIZEN_OF]->(country)
""",
    ),
    (
        "transfers",
        """
UNWIND $rows AS row
MATCH (p:Player {player_id: row.player}), (src:Club {club_id: row.src}), (dst:Club {club_id: row.dst})
CREATE (t:Transfer {transfer_id: row.id, date: row.date, season: row.season, fee_eur: row.fee, is_loan: row.loan})
CREATE (p)-[:MADE]->(t)
CREATE (t)-[:FROM]->(src)
CREATE (t)-[:TO]->(dst)
""",
    ),
    (
        "valuations",
        """
UNWIND $rows AS row MATCH (p:Player {player_id: row.player})
CREATE (p)-[:HAS_VALUATION]->(:Valuation {valuation_id: row.id, date: row.date, value_eur: row.value})
""",
    ),
    (
        "played_with",
        """
UNWIND $rows AS row MATCH (a:Player {player_id: row.a}), (b:Player {player_id: row.b})
CREATE (a)-[:PLAYED_WITH {matches: row.matches, first_season: row.first, last_season: row.last}]->(b)
""",
    ),
]
SENDS_TO = """
MATCH (src:Club)<-[:FROM]-(t:Transfer)-[:TO]->(dst:Club)
WITH src, dst, count(t) AS n, sum(coalesce(t.fee_eur, 0)) AS total
CREATE (src)-[:SENDS_TO {count: n, total_fee_eur: total}]->(dst)
"""
SUMMARY = """
CALL () { MATCH (n) RETURN count(n) AS nodes }
CALL () { MATCH ()-[r]->() RETURN count(r) AS relationships }
CALL () { MATCH (p:Player) RETURN count(p) AS players }
CALL () { MATCH ()-[r:PLAYED_WITH]->() RETURN count(r) AS played_with }
RETURN nodes, relationships, players, played_with
"""


def check_target(uri: str) -> None:
    """Refuse to wipe the application or the test database."""
    port = urlparse(uri).port or 7687
    if port in PROTECTED_PORTS:
        raise SystemExit(f"Refus : {uri} est {PROTECTED_PORTS[port]}. Lancez `make bench-db` (port 7689).")


def load(client: Neo4jClient, data: dict[str, Rows]) -> dict[str, float]:
    """Wipe the database, create constraints and indexes, then every node and relationship; return durations."""
    durations = {}
    start = time.perf_counter()
    while client.write(WIPE)[0]["deleted"]:
        pass
    run_file(client, CONSTRAINTS)
    durations["wipe_and_constraints"] = time.perf_counter() - start
    for name, query in LOADS:
        start = time.perf_counter()
        rows = data[name]
        for offset in range(0, len(rows), BATCH_SIZE):
            client.write(query, {"rows": rows[offset : offset + BATCH_SIZE]})
        durations[name] = time.perf_counter() - start
    start = time.perf_counter()
    client.write(SENDS_TO)
    client.write("CALL db.awaitIndexes(600)")
    durations["sends_to_and_indexes"] = time.perf_counter() - start
    return durations


def main() -> None:
    """Entry point: generate the synthetic graph and load it into the benchmark database."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--players", type=int, default=Config.players)
    parser.add_argument("--played-with", type=int, default=Config.played_with)
    parser.add_argument("--transfers", type=int, default=Config.transfers)
    parser.add_argument("--seed", type=int, default=Config.seed)
    args = parser.parse_args()
    check_target(args.uri)
    config = Config(players=args.players, played_with=args.played_with, transfers=args.transfers, seed=args.seed)
    start = time.perf_counter()
    data = generate(config)
    print(f"Génération : {time.perf_counter() - start:.1f} s, " + ", ".join(f"{k}={len(v)}" for k, v in data.items()))
    client = Neo4jClient(args.uri, ("neo4j", args.password), timeout_s=600)
    try:
        for step, seconds in load(client, data).items():
            print(f"  {step:<22} {seconds:6.1f} s")
        print(client.read(SUMMARY)[0])
    finally:
        client.close()


if __name__ == "__main__":
    main()
