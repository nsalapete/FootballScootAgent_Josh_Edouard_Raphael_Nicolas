"""Data-quality rules applied to transfers before they reach Neo4j. Owner: A.

Pure functions on the rows shaped by `scripts/seed_data.py` (no database), in this order:

1. Loans. transfermarkt-datasets has no loan column. A loan shows up as a transfer X -> Y followed, for the same
   player, by the reverse move Y -> X within `LOAN_MAX_DAYS`, the return having no fee (0 or unknown). Both moves
   get `is_loan = true` (the loan and its end). The detection runs on every transfer of the player, including
   those dropped afterwards, so a loan whose return is planned in the future is still recognised.
2. Drops, first matching reason wins:
   - `before_scope`: no season, or a season before `SCOPE_FROM_SEASON` (outside the scope, not an error);
   - `future`: dated after the ingestion day (Transfermarkt lists planned ends of loan up to 2030);
   - `same_club`: origin and destination are the same club;
   - `pseudo_club`: one side is a Transfermarkt placeholder (Without Club, Unknown, Retired, Career break),
     which is not a club and would become a fake hub of the transfer network;
   - `duplicate`: same player, same origin, same destination within `DUPLICATE_MAX_DAYS` of each other; the move
     with the best-known fee is kept (then the latest).
3. Counts of what was kept: loans, unknown fees (`null`), zero fees.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

Row = dict

# Transfermarkt placeholder ids found in `transfers` (checked on the dataset: names in comments).
PSEUDO_CLUBS = {
    75: "Unknown",
    123: "Retired",
    515: "Without Club",
    2113: "Career break",
}
LOAN_MAX_DAYS = 400  # 13 months: a season-long loan plus the slack between the last game and June 30
DUPLICATE_MAX_DAYS = 30
DROP_REASONS = ("before_scope", "future", "same_club", "pseudo_club", "duplicate")


@dataclass
class CleanTransfers:
    """Transfers kept for the graph, ids of those dropped for a quality reason, and the counts of each case."""

    rows: list[Row] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def is_pseudo_club(club_id: int | None) -> bool:
    """True for a Transfermarkt placeholder such as 'Without Club'."""
    return club_id in PSEUDO_CLUBS


def mark_loans(rows: list[Row]) -> list[Row]:
    """Copies of `rows` with `is_loan` set on both legs of every out-and-back move detected as a loan."""
    marked = [{**row, "is_loan": False} for row in rows]
    by_player: dict[int, list[Row]] = defaultdict(list)
    for row in marked:
        by_player[row["player_id"]].append(row)
    for moves in by_player.values():
        moves.sort(key=lambda row: (row["date"], row["transfer_id"]))
        for out, back in zip(moves, moves[1:]):
            reverse = back["from_club_id"] == out["to_club_id"] and back["to_club_id"] == out["from_club_id"]
            days = (back["date"] - out["date"]).days
            if reverse and out["from_club_id"] != out["to_club_id"] and 0 <= days <= LOAN_MAX_DAYS:
                if back["fee_eur"] in (None, 0):
                    out["is_loan"] = back["is_loan"] = True
    return marked


def drop_reason(row: Row, from_season: int, today: date) -> str | None:
    """First reason to leave a transfer out of the graph (duplicates excepted), None to keep it."""
    if row["season"] is None or row["season"] < from_season:
        return "before_scope"
    if row["date"] > today:
        return "future"
    if row["from_club_id"] == row["to_club_id"]:
        return "same_club"
    if is_pseudo_club(row["from_club_id"]) or is_pseudo_club(row["to_club_id"]):
        return "pseudo_club"
    return None


def _fee_rank(row: Row) -> tuple:
    """Sort key of the best record among duplicates: known fee first, then the highest, then the latest."""
    return (row["fee_eur"] is not None, row["fee_eur"] or 0, row["date"])


def split_duplicates(rows: list[Row]) -> tuple[list[Row], list[Row]]:
    """(kept, duplicates): moves of one player on one route within `DUPLICATE_MAX_DAYS` count as one."""
    routes: dict[tuple, list[Row]] = defaultdict(list)
    for row in rows:
        routes[(row["player_id"], row["from_club_id"], row["to_club_id"])].append(row)
    kept, duplicates = [], []
    for moves in routes.values():
        moves.sort(key=lambda row: (row["date"], row["transfer_id"]))
        clusters: list[list[Row]] = []
        for row in moves:
            if clusters and (row["date"] - clusters[-1][0]["date"]).days <= DUPLICATE_MAX_DAYS:
                clusters[-1].append(row)
            else:
                clusters.append([row])
        for cluster in clusters:
            best = max(cluster, key=_fee_rank)
            kept.append(best)
            duplicates.extend(row for row in cluster if row is not best)
    return kept, duplicates


def clean_transfers(rows: list[Row], from_season: int, today: date) -> CleanTransfers:
    """Apply the loan rule then the drop rules to shaped transfer rows (see the module docstring)."""
    counts = {"raw": len(rows), **{reason: 0 for reason in DROP_REASONS}}
    dropped_ids: list[str] = []
    candidates = []
    for row in mark_loans(rows):
        reason = drop_reason(row, from_season, today)
        if reason is None:
            candidates.append(row)
            continue
        counts[reason] += 1
        if reason != "before_scope":
            dropped_ids.append(row["transfer_id"])
    kept, duplicates = split_duplicates(candidates)
    counts["duplicate"] = len(duplicates)
    dropped_ids.extend(row["transfer_id"] for row in duplicates)
    kept.sort(key=lambda row: (row["player_id"], row["date"], row["transfer_id"]))
    counts.update(
        {
            "kept": len(kept),
            "loans": sum(row["is_loan"] for row in kept),
            "unknown_fee": sum(row["fee_eur"] is None for row in kept),
            "zero_fee": sum(row["fee_eur"] == 0 for row in kept),
        }
    )
    return CleanTransfers(rows=kept, dropped_ids=sorted(dropped_ids), counts=counts)
