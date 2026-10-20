"""Evidence checks of the verifier: do `Label:id` ids exist in the graph, which numbers a claim cites. Owner: D.

Queries are fixed strings chosen from a closed table (one per label), and the ids are passed as parameters.
"""

import re
from typing import Any, Iterable

from app.database.neo4j import Neo4jClient

# Label -> (Python type of the key, read query returning the keys that exist).
_NODE_QUERIES: dict[str, tuple[type, str]] = {
    "Player": (int, "UNWIND $keys AS key MATCH (n:Player {player_id: key}) RETURN key LIMIT 100"),
    "Club": (int, "UNWIND $keys AS key MATCH (n:Club {club_id: key}) RETURN key LIMIT 100"),
    "Competition": (str, "UNWIND $keys AS key MATCH (n:Competition {competition_id: key}) RETURN key LIMIT 100"),
    "Country": (str, "UNWIND $keys AS key MATCH (n:Country {code: key}) RETURN key LIMIT 100"),
    "Transfer": (str, "UNWIND $keys AS key MATCH (n:Transfer {transfer_id: key}) RETURN key LIMIT 100"),
    "Valuation": (str, "UNWIND $keys AS key MATCH (n:Valuation {valuation_id: key}) RETURN key LIMIT 100"),
    "Article": (str, "UNWIND $keys AS key MATCH (n:Article {url: key}) RETURN key LIMIT 100"),
    "Rumour": (str, "UNWIND $keys AS key MATCH (n:Rumour {rumour_id: key}) RETURN key LIMIT 100"),
}
_EDGE_QUERIES = {
    "PREDICTED_MOVE": "UNWIND $keys AS key MATCH (:Player {player_id: key[0]})-[:PREDICTED_MOVE]->"
    "(:Club {club_id: key[1]}) RETURN key LIMIT 100",
    "PREDICTED_LINK": "UNWIND $keys AS key MATCH (:Club {club_id: key[0]})-[:PREDICTED_LINK]->"
    "(:Club {club_id: key[1]}) RETURN key LIMIT 100",
}

_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:[ ,  ]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    + r"(%|bn|billion|milliards?|m\b|M€|M\b|million|millions|k\b|K\b)?",
    re.IGNORECASE,
)
_SCALES = {"bn": 1e9, "billion": 1e9, "milliard": 1e9, "milliards": 1e9, "m": 1e6, "m€": 1e6, "million": 1e6,
           "millions": 1e6, "k": 1e3}


class GraphEvidence:
    """Checks evidence ids against the graph with parameterised read queries."""

    def __init__(self, client: Neo4jClient):
        self.client = client

    def existing(self, evidence_ids: Iterable[str]) -> set[str]:
        """The subset of `evidence_ids` that exist in the graph (malformed ids never do)."""
        by_label: dict[str, dict[Any, str]] = {}
        for evidence in set(evidence_ids):
            parsed = parse_evidence(evidence)
            if parsed is not None:
                label, key = parsed
                by_label.setdefault(label, {})[_freeze(key)] = evidence
        found: set[str] = set()
        for label, keys in by_label.items():
            query = _NODE_QUERIES[label][1] if label in _NODE_QUERIES else _EDGE_QUERIES[label]
            rows = self.client.read(query, {"keys": [list(key) if isinstance(key, tuple) else key for key in keys]})
            found |= {keys[_freeze(row["key"])] for row in rows if _freeze(row["key"]) in keys}
        return found


def _freeze(key: Any) -> Any:
    """Make list keys hashable."""
    return tuple(key) if isinstance(key, list) else key


def parse_evidence(evidence: str) -> tuple[str, Any] | None:
    """Split `Label:id` (or `PREDICTED_MOVE:9->5`) into the label and a typed key; None when malformed."""
    label, sep, raw = evidence.partition(":")
    if not sep or not raw:
        return None
    if label in _EDGE_QUERIES:
        match = re.fullmatch(r"(-?\d+)->(-?\d+)", raw)
        return (label, (int(match.group(1)), int(match.group(2)))) if match else None
    if label not in _NODE_QUERIES:
        return None
    kind = _NODE_QUERIES[label][0]
    if kind is int:
        return (label, int(raw)) if re.fullmatch(r"-?\d+", raw) else None
    return label, raw


def numbers_in_text(text: str) -> list[float]:
    """Numbers cited in a sentence, with their scale applied: `50M` -> 5e7, `42%` -> 42 (checked as 0.42 too)."""
    values = []
    for raw, unit in _NUMBER.findall(text):
        compact = re.sub(r"[ ,  ](?=\d{3}\b)", "", raw).replace(",", ".")
        try:
            value = float(compact)
        except ValueError:
            continue
        scale = _SCALES.get((unit or "").lower(), 1.0)
        values.append(value * scale)
    return values


def numbers_in_data(data: Any) -> set[float]:
    """Every number found in tool data: numeric values and numbers inside strings (dates, ids)."""
    found: set[float] = set()
    if isinstance(data, bool):
        return found
    if isinstance(data, (int, float)):
        found.add(float(data))
    elif isinstance(data, str):
        found |= {float(raw) for raw in re.findall(r"\d+(?:\.\d+)?", data)}
    elif isinstance(data, dict):
        for value in data.values():
            found |= numbers_in_data(value)
    elif isinstance(data, (list, tuple, set)):
        for value in data:
            found |= numbers_in_data(value)
    elif data is not None:
        # Temporal values of the driver (`neo4j.time.Date`, `datetime.date`...) are read from their text form,
        # exactly as the LLM sees them in the tool message.
        found |= numbers_in_data(str(data))
    return found


def number_is_supported(value: float, known: set[float]) -> bool:
    """True when `value` matches a known number, allowing rounding (1 %) and percentages of probabilities."""
    candidates = (value, value / 100)
    for candidate in candidates:
        for number in known:
            if number == candidate or (number and abs(number - candidate) <= 0.01 * abs(number)):
                return True
    return False
