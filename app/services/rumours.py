"""Press rumours: import, extraction and reads. Owner: D.

Contract: keep these method names, parameters and return types. Agent tools call `for_player` directly.

Life cycle of a rumour: an article is fetched (RSS through `feedparser`, or the GDELT DOC API in JSON), the small
LLM extracts `{player, club, quote, confidence}`, both names are resolved through the full-text indexes (a name
that does not resolve to exactly one node is counted, never guessed), and a `Rumour {status: 'pending'}` is stored
with its `Article`. A's `scripts/confirm_rumours.py` later turns it `confirmed`, `contradicted` or `expired`.
"""

import hashlib
import json
import re
import unicodedata
from calendar import timegm
from datetime import date, datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import feedparser
import httpx
from pydantic import BaseModel, Field

from app.database.neo4j import Neo4jClient
from app.llm.client import get_chat_model, structured
from app.models.rumours import ImportReport, ImportRequest, RumourOut

DEFAULT_FEEDS = [
    "https://www.theguardian.com/football/transfers/rss",
    "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "https://api.gdeltproject.org/api/v2/doc/doc?query=%22transfer%22%20football%20sourcelang:english"
    + "&mode=artlist&format=json&maxrecords=50&sort=datedesc",
]
MAX_TEXT_CHARS = 4000
MAX_RUMOURS_PER_PLAYER = 100
# Weight of the prior in the source reliability: a source needs about this many decided rumours to speak for itself.
PRIOR_WEIGHT = 5
DEFAULT_PRIOR = 0.5
DECIDED = ["confirmed", "contradicted", "expired"]

_FOR_PLAYER = """
MATCH (r:Rumour)-[:ABOUT]->(p:Player {player_id: $player_id})
WHERE $status IS NULL OR r.status = $status
MATCH (r)-[:TARGET]->(c:Club)
OPTIONAL MATCH (a:Article)-[:REPORTS]->(r)
WITH r, p, c, a
ORDER BY a.published_at IS NULL, a.published_at DESC
WITH r, p, c, collect(a) AS articles
WITH r, p, c, articles, articles[0] AS a
WITH r, p, c, articles, a, coalesce(a.published_at, date(r.extracted_at)) AS published
RETURN r.rumour_id AS rumour_id, p.player_id AS player_id, c.club_id AS club_id, c.name AS club_name,
       r.status AS status, r.confidence AS confidence, coalesce(a.source, 'unknown') AS source,
       coalesce(a.url, '') AS url, published AS published_at, r.quote AS quote,
       [article IN articles | article.source] AS sources
ORDER BY published IS NULL, published DESC, rumour_id
LIMIT $limit
"""

# Per source: rumours it reported that are confirmed, and that are decided (no longer pending).
_SOURCE_STATS = """
UNWIND $sources AS source
OPTIONAL MATCH (a:Article {source: source})-[:REPORTS]->(r:Rumour)
WITH source, collect(DISTINCT r) AS rumours
RETURN source,
       size([r IN rumours WHERE r.status = 'confirmed']) AS confirmed,
       size([r IN rumours WHERE r.status IN $decided]) AS decided
LIMIT 1000
"""

# All sources together: the prior a source starts from before it has a track record.
_GLOBAL_STATS = """
MATCH (r:Rumour) WHERE r.status IN $decided
RETURN count(CASE WHEN r.status = 'confirmed' THEN 1 END) AS confirmed, count(r) AS decided
"""

_EXISTING_URLS = "UNWIND $urls AS url MATCH (a:Article {url: url}) RETURN url LIMIT 1000"

_RESOLVE_PLAYER = """
CALL db.index.fulltext.queryNodes('player_name_ft', $query) YIELD node, score
RETURN node.player_id AS id, node.name AS name, score
ORDER BY score DESC, id ASC
LIMIT 5
"""
_RESOLVE_CLUB = """
CALL db.index.fulltext.queryNodes('club_name_ft', $query) YIELD node, score
RETURN node.club_id AS id, node.name AS name, score
ORDER BY score DESC, id ASC
LIMIT 5
"""

# A rumour already pending on the same player and club is reinforced by the new article, not duplicated.
_PENDING_PAIR = """
MATCH (r:Rumour {status: 'pending'})-[:ABOUT]->(:Player {player_id: $player_id})
MATCH (r)-[:TARGET]->(:Club {club_id: $club_id})
RETURN r.rumour_id AS rumour_id
ORDER BY rumour_id
LIMIT 1
"""

_SAVE_ARTICLE = """
MERGE (a:Article {url: $url})
ON CREATE SET a.source = $source, a.title = $title, a.published_at = $published_at
RETURN a.url AS url
"""

_SAVE_RUMOURS = """
UNWIND $rows AS row
MATCH (a:Article {url: $url})
MATCH (p:Player {player_id: row.player_id}), (c:Club {club_id: row.club_id})
MERGE (r:Rumour {rumour_id: row.rumour_id})
ON CREATE SET r.status = 'pending', r.confidence = row.confidence, r.quote = row.quote,
              r.extracted_at = datetime()
// A firmer article on a pending rumour raises its confidence and brings its quote (every article stays linked).
ON MATCH SET r.quote = CASE WHEN row.confidence > r.confidence THEN row.quote ELSE r.quote END,
             r.confidence = CASE WHEN row.confidence > r.confidence THEN row.confidence ELSE r.confidence END
MERGE (a)-[:REPORTS]->(r)
MERGE (r)-[:ABOUT]->(p)
MERGE (r)-[:TARGET]->(c)
RETURN count(r) AS linked
"""

EXTRACTION_PROMPT = """You read football news and extract TRANSFER RUMOURS only: a named player linked with a move
to a named club. Ignore match reports, confirmed official signings described as done deals with no club named,
and opinions. For each rumour give:
- `player`: the player's full name as written,
- `club`: the destination club's full official name when the text makes it unambiguous ("Spurs" -> "Tottenham
  Hotspur", "PSG" -> "Paris Saint-Germain"), otherwise as written,
- `quote`: one sentence copied word for word from the text that states the rumour,
- `confidence`: 0 to 1, how firmly the text states the move (e.g. "monitoring" 0.3, "agreed personal terms" 0.8).
Return an empty list when the text contains no rumour. Never invent a name that is not in the text."""


class RawArticle(BaseModel):
    """An article as fetched from a feed, before extraction."""

    url: str
    title: str = ""
    text: str = ""
    source: str = "unknown"
    published_at: date | None = None


class ExtractedRumour(BaseModel):
    """One rumour extracted by the small LLM."""

    player: str = Field(description="Player full name as written in the text")
    club: str = Field(description="Destination club as written in the text")
    quote: str = Field(description="Sentence copied verbatim from the text")
    confidence: float = Field(ge=0, le=1)


class ArticleExtraction(BaseModel):
    """Structured output of the extraction: possibly no rumour at all."""

    rumours: list[ExtractedRumour] = Field(default_factory=list)


def source_of(url: str) -> str:
    """The domain of an article (`www.bbc.co.uk` -> `bbc.co.uk`): one source = one reliability score."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or "unknown"


def _parse_gdelt_date(raw: str) -> date | None:
    """GDELT `seendate` such as `20261020T101500Z`."""
    try:
        return datetime.strptime(raw[:8], "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def parse_feed(url: str, content: bytes | str) -> list[RawArticle]:
    """Parse a GDELT DOC JSON answer or an RSS/Atom feed into raw articles."""
    if "api.gdeltproject.org" in url:
        data = json.loads(content or "{}")
        return [
            RawArticle(
                url=item["url"],
                title=item.get("title", ""),
                text=item.get("title", ""),
                source=source_of(item["url"]),
                published_at=_parse_gdelt_date(item.get("seendate", "")),
            )
            for item in data.get("articles", [])
            if item.get("url")
        ]
    feed = feedparser.parse(content)
    articles = []
    for entry in feed.entries:
        link = entry.get("link")
        if not link:
            continue
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        published = datetime.fromtimestamp(timegm(parsed), tz=timezone.utc).date() if parsed else None
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
        title = entry.get("title", "")
        articles.append(
            RawArticle(url=link, title=title, text=f"{title}. {summary}".strip(), source=source_of(link),
                       published_at=published)
        )
    return articles


def fetch_feed(url: str) -> list[RawArticle]:
    """Download and parse one feed (network)."""
    response = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "football-scout-agent"})
    response.raise_for_status()
    return parse_feed(url, response.content)


def normalise(text: str) -> str:
    """Lower case, accents removed, punctuation turned into spaces."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"\w+", plain.lower()))


def lucene_query(name: str) -> str:
    """Safe full-text query: word characters only, one typo allowed on words of 4 letters or more."""
    return " ".join(f"{word}~1" if len(word) >= 4 else word for word in normalise(name).split())


def pick_unique(name: str, candidates: list[dict[str, Any]]) -> int | None:
    """The id of the only candidate whose name contains every word of `name`; None when zero or several do."""
    words = set(normalise(name).split())
    if not words:
        return None
    fitting = [row["id"] for row in candidates if words <= set(normalise(row["name"]).split())]
    return fitting[0] if len(fitting) == 1 else None


def quote_in_text(quote: str, text: str) -> bool:
    """The quote must really come from the article (compared without case, accents or punctuation)."""
    needle = normalise(quote)
    return bool(needle) and needle in normalise(text)


def smoothed_reliability(confirmed: int, decided: int, prior: float, weight: float = PRIOR_WEIGHT) -> float:
    """Share of confirmed rumours among decided ones, pulled towards `prior` for sources with few decided rumours.

    `(confirmed + weight * prior) / (decided + weight)`: a new source gets `prior`, a source with 1 confirmed out of 1
    is not trusted blindly, and a large source converges to its own confirmation rate.
    """
    return (confirmed + weight * prior) / (decided + weight)


def rumour_id_for(url: str, player_id: int, club_id: int) -> str:
    """Stable id, so that importing the same article twice cannot duplicate a rumour."""
    digest = hashlib.sha1(f"{url}|{player_id}|{club_id}".encode()).hexdigest()[:16]
    return f"rm-{digest}"


class RumourService:
    """Reads and writes `Article` and `Rumour` nodes."""

    def __init__(self, db: Neo4jClient):
        self.client = db
        # Replaceable in tests (no network): feed URL -> raw articles, and the extraction model.
        self.fetcher: Callable[[str], list[RawArticle]] = fetch_feed
        self.model = None

    def for_player(self, player_id: int, status: str | None = None) -> list[RumourOut]:
        """Rumours about a player, newest first, optionally filtered by status."""
        rows = self.client.read(
            _FOR_PLAYER, {"player_id": player_id, "status": status, "limit": MAX_RUMOURS_PER_PLAYER}
        )
        cited = sorted({source for row in rows for source in row["sources"] or [row["source"]] if source})
        reliability = self.source_reliability(cited) if rows else {}
        rumours = []
        for row in rows:
            extraction = row["confidence"]
            sources = [source for source in row.pop("sources") or [row["source"]] if source]
            # The rumour is as credible as its most reliable source (several articles can report it).
            trust = max((reliability.get(source, DEFAULT_PRIOR) for source in sources), default=DEFAULT_PRIOR)
            fields = {
                **row,
                "sources_count": len(set(sources)),
                "published_at": _native(row["published_at"]),
                "confidence": round(extraction * trust, 3),
                "extraction_confidence": extraction,
                "source_reliability": round(trust, 3),
            }
            rumours.append(RumourOut(**fields))
        return rumours

    def source_reliability(self, sources: list[str]) -> dict[str, float]:
        """Smoothed reliability of each source (see `smoothed_reliability`), with the global rate as prior."""
        overall = self.client.read(_GLOBAL_STATS, {"decided": DECIDED})
        decided = overall[0]["decided"] if overall else 0
        prior = overall[0]["confirmed"] / decided if decided else DEFAULT_PRIOR
        rows = self.client.read(_SOURCE_STATS, {"sources": sources, "decided": DECIDED})
        return {row["source"]: smoothed_reliability(row["confirmed"], row["decided"], prior) for row in rows}

    def import_articles(self, request: ImportRequest) -> ImportReport:
        """Fetch articles, extract rumours with the small LLM, resolve names and store them as `pending`."""
        report = ImportReport(articles_seen=0, articles_new=0, rumours_created=0, unresolved_names=0)
        articles = self._fetch(request.feed_urls or DEFAULT_FEEDS, report)[: request.max_articles]
        report.articles_seen = len(articles)
        existing = {row["url"] for row in self.client.read(_EXISTING_URLS, {"urls": [a.url for a in articles]})}
        for article in articles:
            if article.url in existing:
                continue
            existing.add(article.url)
            try:
                extraction = self._extract(article)
            except Exception:  # pylint: disable=broad-exception-caught
                # The article is not stored, so a later import retries it.
                report.extraction_errors += 1
                continue
            report.articles_new += 1
            report.rumours_created += self._store(article, extraction, report)
        return report

    # ---------- steps ----------

    def _fetch(self, feed_urls: list[str], report: ImportReport) -> list[RawArticle]:
        """Every article of every feed, without duplicate URLs; a failing feed is counted and skipped."""
        seen: dict[str, RawArticle] = {}
        for url in feed_urls:
            try:
                for article in self.fetcher(url):
                    seen.setdefault(article.url, article)
            except Exception:  # pylint: disable=broad-exception-caught
                report.feeds_failed += 1
        return list(seen.values())

    def _extract(self, article: RawArticle) -> ArticleExtraction:
        """Ask the small model for the rumours of one article."""
        if self.model is None:
            self.model = get_chat_model("small")
        text = f"Title: {article.title}\n\n{article.text}"[:MAX_TEXT_CHARS]
        return structured(self.model, ArticleExtraction).invoke(
            [("system", EXTRACTION_PROMPT), ("human", text)]
        )

    def _resolve(self, query: str, name: str) -> int | None:
        """Resolve one name through a full-text index; None unless exactly one node fits."""
        lucene = lucene_query(name)
        if not lucene:
            return None
        return pick_unique(name, self.client.read(query, {"query": lucene}))

    def _store(self, article: RawArticle, extraction: ArticleExtraction, report: ImportReport) -> int:
        """Save the article and its resolved rumours; return how many rumours are new."""
        rows: dict[str, dict[str, Any]] = {}
        created = 0
        for rumour in extraction.rumours:
            if not quote_in_text(rumour.quote, f"{article.title}. {article.text}"):
                report.rejected_quotes += 1
                continue
            player_id = self._resolve(_RESOLVE_PLAYER, rumour.player)
            club_id = self._resolve(_RESOLVE_CLUB, rumour.club)
            report.unresolved_names += (player_id is None) + (club_id is None)
            for name, found in ((rumour.player, player_id), (rumour.club, club_id)):
                if found is None and name not in report.unresolved_examples and len(report.unresolved_examples) < 20:
                    report.unresolved_examples.append(name)
            if player_id is None or club_id is None:
                continue
            pending = self.client.read(_PENDING_PAIR, {"player_id": player_id, "club_id": club_id})
            rumour_id = pending[0]["rumour_id"] if pending else rumour_id_for(article.url, player_id, club_id)
            if rumour_id not in rows:
                created += not pending
                rows[rumour_id] = {"rumour_id": rumour_id, "player_id": player_id, "club_id": club_id,
                                   "confidence": rumour.confidence, "quote": rumour.quote}
        params = {"url": article.url, "source": article.source, "title": article.title,
                  "published_at": article.published_at}
        self.client.write(_SAVE_ARTICLE, params)
        if rows:
            self.client.write(_SAVE_RUMOURS, {"url": article.url, "rows": list(rows.values())})
        return created


def _native(value: Any) -> Any:
    """Neo4j temporal value to its Python equivalent (`neo4j.time.Date` -> `datetime.date`)."""
    return value.to_native() if hasattr(value, "to_native") else value
