"""Latency of the service methods and PROFILE of their queries, on the benchmark database. Owner: B.

Run with `python -m scripts.benchmark` (or `make bench`) after `make bench-db` and `make bench-data`.
Prints Markdown tables and writes every number to `docs/perf_results.json`; `docs/perf.md` comments them.

Measures, all on the synthetic graph of `scripts/bench_data.py`:

1. p50 / p95 latency of each service method (wall clock of the Python call: driver, Neo4j and Pydantic);
2. PROFILE before and after an index (the index is dropped, the query profiled, the index recreated from
   `scripts/constraints.cypher`): search by position and age, pipeline with `since`;
3. pagination: `SKIP` against the keyset cursor of `search`, at growing page depths;
4. shortest path bounded by `*..6` against unbounded, and the number of paths without `shortestPath`;
5. the GDS refresh step by step (against the 10 s limit of ordinary reads) and the GDS reads.

The script refuses the application and test databases (it drops indexes and writes GDS properties).
PROFILE is prefixed to the services' own constant queries, captured while the service runs.
"""

import argparse
import itertools
import json
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

import networkx as nx
from neo4j import Query
from neo4j.exceptions import ClientError

from app.database.neo4j import Neo4jClient, Record
from app.models.players import PlayerSearch
from app.services import gds
from app.services.clubs import ClubService
from app.services.players import _TEAMMATE_PATHS, PlayerService, encode_cursor
from app.services.rumours import RumourService
from scripts.bench_data import DEFAULT_PASSWORD, DEFAULT_URI, check_target
from scripts.cypher_files import split_statements

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "scripts" / "constraints.cypher"
RESULTS = ROOT / "docs" / "perf_results.json"
PAGE_SIZE = 20
PAGES = [1, 10, 100, 500, 2000]
LEAGUE_CLUBS = 140  # clubs 1..140 play in the competitions, the others form the lower-division chain
SEED = 7

# Index experiments: constant DROP statements (an index name cannot be a parameter).
DROP_INDEX = {
    "player_position_dob": "DROP INDEX player_position_dob IF EXISTS",
    "player_value": "DROP INDEX player_value IF EXISTS",
    "transfer_date": "DROP INDEX transfer_date IF EXISTS",
}
# The same filters without ORDER BY / LIMIT: what an index changes when every match must be read.
COUNT_YOUNG = """
MATCH (p:Player)
WHERE p.position = $position AND p.date_of_birth > date() - duration({years: $max_age + 1})
RETURN count(p) AS players
"""
COUNT_RECENT_TRANSFERS = "MATCH (t:Transfer) WHERE t.date >= $since RETURN count(t) AS transfers"
# Alternative to the brute-force cosine of `similar_players`, measured on the benchmark database only.
VECTOR_INDEX = """
CREATE VECTOR INDEX bench_player_embedding IF NOT EXISTS FOR (p:Player) ON p.embedding
OPTIONS {indexConfig: {`vector.dimensions`: 64, `vector.similarity_function`: 'cosine'}}
"""
DROP_VECTOR_INDEX = "DROP INDEX bench_player_embedding IF EXISTS"
SIMILAR_BY_VECTOR_INDEX = """
MATCH (p:Player {player_id: $player_id})
CALL db.index.vector.queryNodes('bench_player_embedding', $candidates, p.embedding) YIELD node, score
WITH p, node, score WHERE node <> p
RETURN node.player_id AS player_id, score AS similarity
ORDER BY similarity DESC, player_id
LIMIT $k
"""
WITH_TEAMMATES = (
    "MATCH (p:Player) WHERE p.player_id IN $ids AND EXISTS { (p)-[:PLAYED_WITH]-() } RETURN p.player_id AS id"
)
AWAIT_INDEXES = "CALL db.awaitIndexes(600)"

# Pagination with SKIP, in the same order as the valued phase of `search` (so both can use the value index).
SKIP_PAGE = """
MATCH (p:Player) WHERE p.market_value_eur IS NOT NULL
WITH p ORDER BY p.market_value_eur DESC, p.player_id ASC SKIP $skip LIMIT $limit
RETURN p.player_id AS player_id, p.market_value_eur AS value_key
"""
# The first version of `search` (sort key computed with coalesce), kept to show why it was replaced.
COALESCE_CURSOR_PAGE = """
MATCH (p:Player)
WHERE coalesce(p.market_value_eur, -1) < $cursor_value
   OR (coalesce(p.market_value_eur, -1) = $cursor_value AND p.player_id > $cursor_id)
WITH p, coalesce(p.market_value_eur, -1) AS value_key
ORDER BY value_key DESC, p.player_id ASC
LIMIT $limit
RETURN p.player_id AS player_id
"""
UNBOUNDED_PATH = """
MATCH (start:Player {player_id: $from_id}), (goal:Player {player_id: $to_id})
MATCH path = shortestPath((start)-[:PLAYED_WITH*]-(goal))
RETURN length(path) AS hops
"""
# Every path up to k hops (no shortestPath): the cost grows like (average degree) ** k. One constant query per
# bound, built from integers as in `app/services/players.py` (a bound cannot be a parameter).
ALL_PATHS_TEMPLATE = (
    "MATCH (start:Player {player_id: $from_id}), (goal:Player {player_id: $to_id}) "
    "MATCH path = (start)-[:PLAYED_WITH*..__MAX_HOPS__]-(goal) RETURN count(path) AS paths"
)
ALL_PATHS = {hops: ALL_PATHS_TEMPLATE.replace("__MAX_HOPS__", str(hops)) for hops in range(2, 7)}  # *..6 at most

# Sampling reads: bounded like every list (the synthetic graph has 50,000 players and 220,000 PLAYED_WITH).
MAX_SAMPLED = 1_000_000
PLAYERS = (
    "MATCH (p:Player)-[:PLAYS_FOR]->(c:Club) RETURN p.player_id AS id, p.name AS name, c.club_id AS club LIMIT $limit"
)
EDGES = "MATCH (a:Player)-[:PLAYED_WITH]->(b:Player) RETURN a.player_id AS a, b.player_id AS b LIMIT $limit"
RUMOURED = "MATCH (:Rumour)-[:ABOUT]->(p:Player) RETURN DISTINCT p.player_id AS id ORDER BY id LIMIT 200"
SOURCES = "MATCH (a:Article) RETURN DISTINCT a.source AS source ORDER BY source LIMIT 50"
TOP_SELLER = (
    "MATCH (c:Club)<-[:FROM]-(t:Transfer) RETURN c.club_id AS club, count(t) AS transfers "
    "ORDER BY transfers DESC, club LIMIT 1"
)
SIZES = """
CALL () { MATCH (p:Player) RETURN count(p) AS players }
CALL () { MATCH (c:Club) RETURN count(c) AS clubs }
CALL () { MATCH (t:Transfer) RETURN count(t) AS transfers }
CALL () { MATCH (v:Valuation) RETURN count(v) AS valuations }
CALL () { MATCH ()-[r:PLAYED_WITH]->() RETURN count(r) AS played_with }
CALL () { MATCH ()-[r:SENDS_TO]->() RETURN count(r) AS sends_to }
CALL dbms.components() YIELD versions
RETURN players, clubs, transfers, valuations, played_with, sends_to, versions[0] AS neo4j
"""


@dataclass
class Plan:
    """What PROFILE says about one query, or the sum over the queries of one service call."""

    db_hits: int = 0
    rows: int = 0
    server_ms: int = 0
    leaves: list[str] = field(default_factory=list)

    def add(self, other: "Plan") -> "Plan":
        """Sum of two plans (a service call may run two queries)."""
        return Plan(
            self.db_hits + other.db_hits,
            self.rows + other.rows,
            self.server_ms + other.server_ms,
            self.leaves + [leaf for leaf in other.leaves if leaf not in self.leaves],
        )


def operators(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every operator of a PROFILE plan, depth first."""
    found = [plan]
    for child in plan.get("children", []):
        found += operators(child)
    return found


def summarise(plan: dict[str, Any], server_ms: int) -> Plan:
    """Total db hits, rows produced, and the operators that read the graph (the leaves of the plan)."""
    found = operators(plan)
    leaves = []
    for operator in found:
        name = operator["operatorType"].split("@")[0]
        if not operator.get("children") and name != "Argument" and name not in leaves:
            leaves.append(name)
    return Plan(sum(op.get("dbHits", 0) for op in found), plan.get("rows", 0), server_ms, leaves)


class ProfilingClient(Neo4jClient):
    """A `Neo4jClient` that can also PROFILE a query and capture the reads a service makes."""

    def __init__(self, uri: str, password: str, timeout_s: float = 60.0):
        super().__init__(uri, ("neo4j", password), timeout_s=timeout_s)
        self.captured: list[tuple[str, dict[str, Any]]] | None = None
        self.writes: list[tuple[str, float]] = []

    def read(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Run the read; remember it when a capture is active."""
        if self.captured is not None:
            self.captured.append((query, dict(params or {})))
        return super().read(query, params)

    def write(self, query: str, params: dict[str, Any] | None = None) -> list[Record]:
        """Run the write and record how long it took (used to time the GDS refresh step by step)."""
        start = time.perf_counter()
        try:
            return super().write(query, params)
        finally:
            self.writes.append((query, time.perf_counter() - start))

    def profile(self, query: str, params: dict[str, Any] | None = None) -> Plan:
        """PROFILE one constant query of a service (or of this module), within the client timeout."""
        with self._driver.session(database=self._database) as session:
            summary = session.run(Query("PROFILE " + query, timeout=self._timeout_s), params or {}).consume()
        return summarise(summary.profile, summary.result_available_after + summary.result_consumed_after)

    def profile_call(self, call: Callable[[], Any]) -> Plan:
        """PROFILE every read that `call` makes through this client: the service's own queries and parameters."""
        self.captured = []
        try:
            call()
            captured = self.captured
        finally:
            self.captured = None
        total = Plan()
        for query, params in captured:
            total = total.add(self.profile(query, params))
        return total


def latency(call: Callable[[], Any], runs: int, warmup: int = 3) -> dict[str, float]:
    """p50 and p95 in milliseconds of `call` (wall clock), after a few warm-up calls."""
    for _ in range(warmup):
        call()
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "p50_ms": round(statistics.median(samples), 1),
        "p95_ms": round(statistics.quantiles(samples, n=20)[18], 1),
        "runs": runs,
    }


def cycling(function: Callable[[Any], Any], inputs: list[Any]) -> Callable[[], Any]:
    """A call that feeds `function` the next input at each run (cold and warm entries mixed)."""
    pool = itertools.cycle(inputs)
    return lambda: function(next(pool))


@dataclass
class Samples:
    """Inputs drawn from the benchmark graph."""

    player_ids: list[int]
    names: list[str]
    clubs: list[int]
    top_seller: int
    pairs: dict[str, tuple[int, int, int | None]]  # case -> (from, to, true distance or None)
    rumoured: list[int] = field(default_factory=list)  # players with at least one rumour
    league_players: list[int] = field(default_factory=list)  # players with stored PREDICTED_MOVE
    sources: list[str] = field(default_factory=list)


def pick_pairs(graph: nx.Graph, club_of: dict[int, int], rng: random.Random) -> dict[str, tuple[int, int, int | None]]:
    """Player pairs at known distances: 2, 4, 6 hops in the leagues, far apart in the chain, without a path."""
    league = sorted(pid for pid in graph if club_of[pid] <= LEAGUE_CLUBS)
    chain = sorted(pid for pid in graph if club_of[pid] > LEAGUE_CLUBS)
    source = rng.choice(league)
    distances = nx.single_source_shortest_path_length(graph, source)
    pairs: dict[str, tuple[int, int, int | None]] = {}
    for hops in (2, 4, 6):
        targets = sorted(pid for pid, dist in distances.items() if dist == hops)
        if targets:
            pairs[f"{hops} sauts"] = (source, rng.choice(targets), hops)
    chain_start = min(chain, key=lambda pid: club_of[pid])
    chain_distances = nx.single_source_shortest_path_length(graph, chain_start)
    for wanted in (12, 30):
        target = min(chain_distances, key=lambda pid, goal=wanted: (abs(chain_distances[pid] - goal), pid))
        pairs[f"{chain_distances[target]} sauts (chaîne)"] = (chain_start, target, chain_distances[target])
    pairs["aucun chemin"] = (source, rng.choice(chain), None)
    return pairs


def gather(client: ProfilingClient) -> Samples:
    """Draw player ids, names, clubs and path pairs from the graph (fixed seed)."""
    rng = random.Random(SEED)
    players = client.read(PLAYERS, {"limit": MAX_SAMPLED})
    club_of = {row["id"]: row["club"] for row in players}
    graph = nx.Graph()
    graph.add_edges_from((row["a"], row["b"]) for row in client.read(EDGES, {"limit": MAX_SAMPLED}))
    picked = [players[index] for index in rng.sample(range(len(players)), 200)]
    return Samples(
        player_ids=[row["id"] for row in picked],
        names=[row["name"] for row in picked[:50]],
        clubs=sorted({row["club"] for row in picked if row["club"] <= LEAGUE_CLUBS}),
        top_seller=client.read(TOP_SELLER)[0]["club"],
        pairs=pick_pairs(graph, club_of, rng),
        rumoured=[row["id"] for row in client.read(RUMOURED)],
        league_players=[row["id"] for row in picked if row["club"] <= LEAGUE_CLUBS],
        sources=[row["source"] for row in client.read(SOURCES)],
    )


def typo(name: str) -> str:
    """The name with one letter missing in its last word (what a user or the press often writes)."""
    words = name.split()
    last = words[-1]
    words[-1] = last[:2] + last[3:] if len(last) > 4 else last
    return " ".join(words)


@dataclass
class Services:
    """Every service of `app/services`, bound to the benchmark client."""

    players: PlayerService
    clubs: ClubService
    network: gds.GdsService
    rumours: RumourService


def other_cases(services: Services, samples: Samples) -> list[tuple[str, str, Callable]]:
    """(method, case, call) for the read methods of the other roles' services (C and D) and B's helpers.

    Not here: `GdsService.refresh` (timed step by step in `gds_experiment`) and `RumourService.import_articles`
    (bound by the network and the LLM, not by the graph).
    """
    rumours = services.rumours
    return [
        ("ClubService.exists", "club au hasard", cycling(services.clubs.exists, samples.clubs)),
        ("PlayerService.latest_season", "saison la plus récente", services.players.latest_season),
        (
            "GdsService.predicted_moves (C)",
            "joueur de championnat",
            cycling(services.network.predicted_moves, samples.league_players),
        ),
        ("RumourService.for_player (D)", "joueur avec rumeurs", cycling(rumours.for_player, samples.rumoured)),
        (
            "RumourService.for_player (D)",
            "rumeurs en attente",
            cycling(lambda pid: rumours.for_player(pid, "pending"), samples.rumoured),
        ),
        ("RumourService.source_reliability (D)", "8 sources", lambda: rumours.source_reliability(samples.sources)),
    ]


def service_cases(players: PlayerService, clubs: ClubService, samples: Samples) -> list[tuple[str, str, Callable]]:
    """(method, case, call) for every method of B's services."""
    first_page = players.search(PlayerSearch(limit=PAGE_SIZE))
    close = samples.pairs["4 sauts"]
    far = samples.pairs["aucun chemin"]
    since = date(2024, 1, 1)
    return [
        (
            "search",
            "poste + âge (Defender, ≤ 23 ans)",
            lambda: players.search(PlayerSearch(position="Defender", max_age=23)),
        ),
        (
            "search",
            "sous-poste + budget (Left-Back, ≤ 5 M€)",
            lambda: players.search(PlayerSearch(sub_position="Left-Back", max_value_eur=5_000_000)),
        ),
        ("search", "compétition (FR1)", lambda: players.search(PlayerSearch(competition_id="FR1"))),
        ("search", "sans filtre, page 1", lambda: players.search(PlayerSearch(limit=PAGE_SIZE))),
        (
            "search",
            "sans filtre, page 2 (curseur)",
            lambda: players.search(PlayerSearch(limit=PAGE_SIZE, cursor=first_page.next_cursor)),
        ),
        ("get", "joueur au hasard", cycling(players.get, samples.player_ids)),
        ("value_trend", "joueur au hasard", cycling(players.value_trend, samples.player_ids)),
        ("resolve", "nom exact", cycling(players.resolve, samples.names)),
        ("resolve", "nom avec une faute", cycling(players.resolve, [typo(name) for name in samples.names])),
        ("teammate_path", "4 sauts", lambda: players.teammate_path(close[0], close[1])),
        ("teammate_path", "aucun chemin (borne 6)", lambda: players.teammate_path(far[0], far[1])),
        ("pipeline", "plus gros vendeur", lambda: clubs.pipeline(samples.top_seller)),
        ("pipeline", "plus gros vendeur, since 2024-01-01", lambda: clubs.pipeline(samples.top_seller, since)),
        ("pipeline", "club au hasard", cycling(clubs.pipeline, samples.clubs)),
        ("club resolve", "nom de club", lambda: clubs.resolve("Ligue 1 Club 07")),
    ]


def bench_services(client: ProfilingClient, samples: Samples, runs: int) -> list[dict[str, Any]]:
    """Latency and PROFILE of every case of `service_cases`."""
    services = Services(PlayerService(client), ClubService(client), gds.GdsService(client), RumourService(client))
    rows = []
    cases = service_cases(services.players, services.clubs, samples) + other_cases(services, samples)
    for method, case, call in cases:
        plan = client.profile_call(call)
        rows.append({"method": method, "case": case, **latency(call, runs), **asdict(plan)})
    return rows


def recreate_index(client: ProfilingClient, name: str) -> None:
    """Recreate an index from its own statement in `scripts/constraints.cypher`, then wait until it is online."""
    statements = split_statements(CONSTRAINTS.read_text(encoding="utf-8"))
    client.write(next(statement for statement in statements if f"INDEX {name} " in statement))
    client.write(AWAIT_INDEXES)


@dataclass
class Measured:
    """A query measured in several index states: its label, how to PROFILE it, how to run it."""

    label: str
    profile: Callable[[], Plan]
    call: Callable[[], Any]


def measure_all(measured: list[Measured], phase: str, runs: int) -> list[dict[str, Any]]:
    """PROFILE and latency of every query of `measured`, labelled with `phase`."""
    return [
        {"phase": phase, "query": item.label, **asdict(item.profile()), **latency(item.call, runs)} for item in measured
    ]


def index_experiment(
    client: ProfilingClient, measured: list[Measured], phases: list[tuple[str, list[str]]], runs: int
) -> list[dict[str, Any]]:
    """Every query in every phase (indexes dropped cumulatively), then again once every index is recreated
    from its own statement of `scripts/constraints.cypher`."""
    rows: list[dict[str, Any]] = []
    dropped: list[str] = []
    try:
        for phase, drops in phases:
            for name in drops:
                client.write(DROP_INDEX[name])
                dropped.append(name)
            rows += measure_all(measured, phase, runs)
    finally:
        for name in dropped:
            recreate_index(client, name)
    return rows + measure_all(measured, "index recréés", runs)


def search_index_experiment(client: ProfilingClient, runs: int) -> list[dict[str, Any]]:
    """Defenders of 23 or less: the page of `search`, and the count of every match (no ORDER BY / LIMIT)."""
    players = PlayerService(client)
    defenders = PlayerSearch(position="Defender", max_age=23)
    params = {"position": "Defender", "max_age": 23}
    measured = [
        Measured(
            "search, page 1",
            lambda: client.profile_call(lambda: players.search(defenders)),
            lambda: players.search(defenders),
        ),
        Measured(
            "count(*) des correspondances",
            lambda: client.profile(COUNT_YOUNG, params),
            lambda: client.read(COUNT_YOUNG, params),
        ),
    ]
    phases = [
        ("tous les index", []),
        ("sans (position, date_of_birth)", ["player_position_dob"]),
        ("sans aucun des deux", ["player_value"]),
    ]
    return index_experiment(client, measured, phases, runs)


def pipeline_index_experiment(client: ProfilingClient, top_seller: int, runs: int) -> list[dict[str, Any]]:
    """The pipeline of the biggest seller since a date, and every transfer since a date (no club anchor)."""
    clubs = ClubService(client)
    since = date(2024, 1, 1)
    params = {"since": date(2025, 1, 1)}
    measured = [
        Measured(
            "pipeline du plus gros vendeur depuis 2024-01-01",
            lambda: client.profile_call(lambda: clubs.pipeline(top_seller, since)),
            lambda: clubs.pipeline(top_seller, since),
        ),
        Measured(
            "count(*) des transferts depuis 2025-01-01",
            lambda: client.profile(COUNT_RECENT_TRANSFERS, params),
            lambda: client.read(COUNT_RECENT_TRANSFERS, params),
        ),
    ]
    return index_experiment(client, measured, [("avec index", []), ("sans Transfer(date)", ["transfer_date"])], runs)


def pagination_experiment(client: ProfilingClient, runs: int) -> list[dict[str, Any]]:
    """For each page depth: SKIP, the keyset cursor of `search`, and the first coalesce-based cursor."""
    players = PlayerService(client)
    rows = []
    for page in PAGES:
        skip = (page - 1) * PAGE_SIZE
        params = {"skip": skip, "limit": PAGE_SIZE}
        cursor, boundary = None, {"cursor_value": 10**12, "cursor_id": 0}
        if skip:
            last = client.read(SKIP_PAGE, {"skip": skip - 1, "limit": 1})[0]
            cursor = encode_cursor(last["value_key"], last["player_id"])
            boundary = {"cursor_value": last["value_key"], "cursor_id": last["player_id"]}
        criteria = PlayerSearch(limit=PAGE_SIZE, cursor=cursor)
        variants = {
            "SKIP": (lambda p=params: client.read(SKIP_PAGE, p), lambda p=params: client.profile(SKIP_PAGE, p)),
            "curseur (search)": (
                lambda c=criteria: players.search(c),
                lambda c=criteria: client.profile_call(lambda: players.search(c)),
            ),
            "curseur coalesce (v1)": (
                lambda b=boundary: client.read(COALESCE_CURSOR_PAGE, {**b, "limit": PAGE_SIZE + 1}),
                lambda b=boundary: client.profile(COALESCE_CURSOR_PAGE, {**b, "limit": PAGE_SIZE + 1}),
            ),
        }
        for variant, (call, profile) in variants.items():
            rows.append({"page": page, "variant": variant, **asdict(profile()), **latency(call, runs)})
    return rows


def path_experiment(client: ProfilingClient, samples: Samples) -> list[dict[str, Any]]:
    """shortestPath bounded by 6 hops (the service query) against unbounded, for each pair of `samples`."""
    rows = []
    for case, (from_id, to_id, distance) in samples.pairs.items():
        params = {"from_id": from_id, "to_id": to_id}
        for variant, query in (("borne *..6", _TEAMMATE_PATHS[6]), ("non borné *", UNBOUNDED_PATH)):
            client.profile(query, params)  # warm-up
            plan = client.profile(query, params)
            found = bool(client.read(query, params))
            rows.append({"case": case, "distance": distance, "variant": variant, "found": found, **asdict(plan)})
    return rows


def all_paths_experiment(client: ProfilingClient, samples: Samples) -> list[dict[str, Any]]:
    """Number of paths up to k hops between two players 4 hops apart, without shortestPath."""
    from_id, to_id, _ = samples.pairs["4 sauts"]
    params = {"from_id": from_id, "to_id": to_id}
    rows = []
    for hops, query in ALL_PATHS.items():
        try:
            plan = client.profile(query, params)
            paths = client.read(query, params)[0]["paths"]
            rows.append({"max_hops": hops, "paths": paths, **asdict(plan)})
        except ClientError as error:  # transaction timeout: report it rather than stop the benchmark
            rows.append({"max_hops": hops, "paths": None, "error": error.code})
            break
    return rows


def gds_experiment(uri: str, password: str, samples: Samples, runs: int) -> dict[str, Any]:
    """GDS refresh step by step (with the long limit the API uses), then the latency of the GDS reads."""
    client = ProfilingClient(uri, password, timeout_s=gds.GDS_TIMEOUT_S)
    try:
        service = gds.GdsService(client)
        timings = []
        for attempt in ("1er recalcul", "2e recalcul"):
            client.writes = []
            refreshed = service.refresh()
            timings.append((attempt, refreshed.duration_s, client.writes))
        names = {
            gds.PROJECT_CLUBS: "projection clubs",
            gds.PAGERANK: "PageRank",
            gds.LOUVAIN: "Louvain",
            gds.BETWEENNESS: "Betweenness",
            gds.PROJECT_TEAMMATES: "projection coéquipiers",
            gds.FASTRP: "FastRP",
            gds.DROP: "suppression d'une projection",
        }
        steps = [
            {"refresh": attempt, "step": names.get(query, "autre"), "seconds": round(seconds, 2)}
            for attempt, _, writes in timings
            for query, seconds in writes
        ]
        reads = [
            ("hubs", lambda: service.hubs(20)),
            ("communities", lambda: service.communities(2)),
            ("gateways", lambda: service.gateways(20)),
            ("similar_players", cycling(lambda pid: service.similar_players(pid, 10), samples.player_ids)),
        ]
        latencies = [
            {"method": name, **latency(call, runs), **asdict(client.profile_call(call))} for name, call in reads
        ]
        return {
            "refresh_s": {attempt: duration for attempt, duration, _ in timings},
            "steps": steps,
            "reads": latencies + vector_index_experiment(client, service, samples, runs),
        }
    finally:
        client.close()


def vector_index_experiment(
    client: ProfilingClient, service: gds.GdsService, samples: Samples, runs: int
) -> list[dict[str, Any]]:
    """`similar_players` through a vector index (benchmark database only), and its recall against brute force."""
    ids = [row["id"] for row in client.read(WITH_TEAMMATES, {"ids": samples.player_ids})][:40]
    client.write(VECTOR_INDEX)
    client.write(AWAIT_INDEXES)
    try:

        def nearest(pid: int) -> list[Record]:
            return client.read(SIMILAR_BY_VECTOR_INDEX, {"player_id": pid, "k": 10, "candidates": 50})

        overlaps = []
        for pid in ids[:20]:
            exact = {player.player_id for player in service.similar_players(pid, 10)}
            overlaps.append(len({row["player_id"] for row in nearest(pid)} & exact) / 10)
        plan = client.profile(SIMILAR_BY_VECTOR_INDEX, {"player_id": ids[0], "k": 10, "candidates": 50})
        return [
            {
                "method": "similar_players par index vectoriel",
                "recall_at_10": round(statistics.mean(overlaps), 2),
                **latency(cycling(nearest, ids), runs),
                **asdict(plan),
            }
        ]
    finally:
        client.write(DROP_VECTOR_INDEX)


def table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """Markdown table of `rows` restricted to `columns`."""
    lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(column, "")) for column in columns) + " |")
    return "\n".join(lines)


def report(results: dict[str, Any]) -> str:
    """Every result as Markdown tables (pasted and commented in `docs/perf.md`)."""
    plan = ["db_hits", "rows", "server_ms", "leaves"]
    parts = [
        f"Graphe : {results['graph']}",
        "## Services\n"
        + table(results["services"], ["method", "case", "p50_ms", "p95_ms", "db_hits", "rows", "leaves"]),
        "## Index Player(position, date_of_birth) et Player(market_value_eur) : Defender ≤ 23 ans\n"
        + table(results["index_search"], ["phase", "query", "p50_ms", *plan]),
        "## Index Transfer(date)\n" + table(results["index_pipeline"], ["phase", "query", "p50_ms", *plan]),
        "## Pagination\n" + table(results["pagination"], ["page", "variant", "p50_ms", "p95_ms", *plan]),
        "## Chemins\n" + table(results["paths"], ["case", "variant", "found", *plan]),
        "## Tous les chemins (sans shortestPath)\n"
        + table(results["all_paths"], ["max_hops", "paths", *plan, "error"]),
    ]
    if "gds" in results:
        parts.append(
            f"## GDS (refresh {results['gds']['refresh_s']} s)\n"
            + table(results["gds"]["steps"], ["refresh", "step", "seconds"])
        )
        columns = ["method", "p50_ms", "p95_ms", "db_hits", "rows", "leaves", "recall_at_10"]
        parts.append(table(results["gds"]["reads"], columns))
    return "\n\n".join(parts)


def measure_graph_queries(client: ProfilingClient, samples: Samples, runs: int) -> dict[str, Any]:
    """Every measure except GDS: services, index experiments, pagination and paths."""
    return {
        "graph": client.read(SIZES)[0],
        "pairs": {case: list(pair) for case, pair in samples.pairs.items()},
        "services": bench_services(client, samples, runs),
        "index_search": search_index_experiment(client, runs),
        "index_pipeline": pipeline_index_experiment(client, samples.top_seller, runs),
        "pagination": pagination_experiment(client, runs),
        "paths": path_experiment(client, samples),
        "all_paths": all_paths_experiment(client, samples),
    }


def main() -> None:
    """Entry point: run every measure on the benchmark database and save the results."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--skip-gds", action="store_true")
    parser.add_argument("--output", type=Path, default=RESULTS)
    parser.add_argument("--report-only", action="store_true", help="print the tables of an existing --output")
    parser.add_argument("--only-gds", action="store_true", help="re-measure GDS only, into an existing --output")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the Windows console encoding cannot print every character
    if args.report_only:
        print(report(json.loads(args.output.read_text(encoding="utf-8"))))
        return
    check_target(args.uri)
    client = ProfilingClient(args.uri, args.password)
    try:
        samples = gather(client)
        if args.only_gds:  # e.g. right after restarting the database, to time a cold refresh again
            results = json.loads(args.output.read_text(encoding="utf-8"))
        else:
            results = measure_graph_queries(client, samples, args.runs)
    finally:
        client.close()
    if not args.skip_gds:
        results["gds"] = gds_experiment(args.uri, args.password, samples, args.runs)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(report(results))


if __name__ == "__main__":
    main()
