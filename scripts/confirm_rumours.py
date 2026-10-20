"""Match pending rumours with real transfers and update their status. Owner: A.

Run with `python -m scripts.confirm_rumours` (after an ingestion, so that new transfers are in the graph).

A `Rumour` is `(:Article)-[:REPORTS]->(:Rumour)-[:ABOUT]->(:Player)` and `(:Rumour)-[:TARGET]->(:Club)`. Its date is
the earliest `published_at` of the articles reporting it (or the day it was extracted when no article has a date).
For each `pending` rumour, the transfers of the player in the `CONFIRM_DAYS` days from that date decide:

- `confirmed` + `(:Rumour)-[:CONFIRMED_BY]->(:Transfer)`: the player moves to the target club in the window
  (even if he moved elsewhere first: the rumour came true);
- `contradicted`: the player moves to another club in the window, and never to the target;
- `expired`: `EXPIRE_DAYS` days have passed without any of the above;
- otherwise it stays `pending`.

A rumour is never turned into a fact: the fact is the Transfermarkt `Transfer`, the rumour only points to it.
Rumours that are not `pending` are never touched, so the job can run as often as needed.
"""

from datetime import date, timedelta
from typing import Any

from app.config import get_settings
from app.database.neo4j import Neo4jClient

CONFIRM_DAYS = 180
EXPIRE_DAYS = 365
MAX_RUMOURS = 10_000  # one run handles at most this many pending rumours (LIMIT on every list)
STATUSES = ("confirmed", "contradicted", "expired", "pending")

# Pending rumours with their date and the player's transfers inside the confirmation window.
_PENDING = """
MATCH (r:Rumour {status: 'pending'})
WHERE __IDS__
MATCH (r)-[:ABOUT]->(p:Player), (r)-[:TARGET]->(target:Club)
OPTIONAL MATCH (a:Article)-[:REPORTS]->(r)
WITH r, p, target, coalesce(min(a.published_at), date(r.extracted_at)) AS published
OPTIONAL MATCH (p)-[:MADE]->(t:Transfer)-[:TO]->(dst:Club)
WHERE published IS NOT NULL AND published <= t.date <= published + duration({days: $confirm_days})
WITH r, target, published, t, dst
ORDER BY t.date, t.transfer_id
WITH r, target, published,
     collect(CASE WHEN t IS NULL THEN null
                  ELSE {transfer_id: t.transfer_id, date: t.date, club_id: dst.club_id} END) AS transfers
RETURN r.rumour_id AS rumour_id, target.club_id AS target_id, published, transfers
ORDER BY rumour_id
LIMIT $limit
"""
# Two constant variants instead of `$rumour_ids IS NULL OR ...`: the id list always travels in `params`.
_PENDING_QUERIES = {
    False: _PENDING.replace("__IDS__", "true"),
    True: _PENDING.replace("__IDS__", "r.rumour_id IN $rumour_ids"),
}

# Only a rumour still pending changes: two concurrent runs cannot overwrite each other's decision.
_APPLY = """
UNWIND $rows AS row
MATCH (r:Rumour {rumour_id: row.rumour_id})
WHERE r.status = 'pending'
SET r.status = row.status, r.resolved_on = row.resolved_on
WITH r, row WHERE row.transfer_id IS NOT NULL
MATCH (t:Transfer {transfer_id: row.transfer_id})
MERGE (r)-[:CONFIRMED_BY]->(t)
"""


def _native(value: Any) -> Any:
    """Neo4j temporal value to its Python equivalent (`neo4j.time.Date` -> `datetime.date`)."""
    return value.to_native() if hasattr(value, "to_native") else value


def decide(published: date | None, today: date, transfers: list[dict], target_id: int) -> tuple[str, str | None]:
    """New status of a pending rumour and the id of the confirming transfer (None unless confirmed).

    `transfers` are the player's moves (`transfer_id`, `date`, `club_id` of the destination); only those between
    the rumour date and `min(rumour date + CONFIRM_DAYS, today)` count.
    """
    if published is None:
        return "pending", None
    end = min(published + timedelta(days=CONFIRM_DAYS), today)
    window = sorted(
        (move for move in transfers if published <= move["date"] <= end),
        key=lambda move: (move["date"], move["transfer_id"]),
    )
    for move in window:
        if move["club_id"] == target_id:
            return "confirmed", move["transfer_id"]
    if window:
        return "contradicted", None
    if today > published + timedelta(days=EXPIRE_DAYS):
        return "expired", None
    return "pending", None


def confirm_rumours(db: Neo4jClient, today: date, rumour_ids: list[str] | None = None) -> dict:
    """Update every pending rumour (or only `rumour_ids`) and return how many ended in each status."""
    params: dict[str, Any] = {"confirm_days": CONFIRM_DAYS, "limit": MAX_RUMOURS}
    if rumour_ids is not None:
        params["rumour_ids"] = list(rumour_ids)
    rows = db.read(_PENDING_QUERIES[rumour_ids is not None], params)
    counts = {"checked": len(rows), **{status: 0 for status in STATUSES}}
    updates = []
    for row in rows:
        transfers = [{**move, "date": _native(move["date"])} for move in row["transfers"]]
        status, transfer = decide(_native(row["published"]), today, transfers, row["target_id"])
        counts[status] += 1
        if status != "pending":
            updates.append(
                {"rumour_id": row["rumour_id"], "status": status, "transfer_id": transfer, "resolved_on": today}
            )
    if updates:
        db.write(_APPLY, {"rows": updates})
    return counts


def main() -> None:
    """Entry point of the confirmation job: every pending rumour, as of today."""
    settings = get_settings()
    db = Neo4jClient(
        settings.neo4j_uri,
        (settings.neo4j_user, settings.neo4j_password),
        settings.neo4j_database,
        settings.neo4j_query_timeout_s,
    )
    try:
        if not db.ping():
            raise SystemExit(f"Neo4j unreachable at {settings.neo4j_uri}.")
        counts = confirm_rumours(db, date.today())
        print(", ".join(f"{name}: {count}" for name, count in counts.items()))
    finally:
        db.close()


if __name__ == "__main__":
    main()
