"""Tests of the rumour service and of the article import (fake feeds, fake LLM, no network). Owner: D."""

from datetime import date

import pytest

from app.llm.client import FakeChatModel
from app.models.rumours import ImportRequest
from app.services.rumours import (
    ArticleExtraction,
    ExtractedRumour,
    RawArticle,
    RumourService,
    lucene_query,
    normalise,
    parse_feed,
    pick_unique,
    quote_in_text,
    rumour_id_for,
    source_of,
)
from scripts import import_articles

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test feed</title>
<item><title>Rovers chase striker</title><link>https://www.test-d.example/rss-1</link>
<description>&lt;p&gt;Quuxford Rovers are &lt;b&gt;monitoring&lt;/b&gt; a striker.&lt;/p&gt;</description>
<pubDate>Tue, 20 Oct 2026 09:30:00 GMT</pubDate></item>
<item><title>No link here</title></item>
</channel></rss>"""

GDELT = """{"articles": [{"url": "https://news.test-d.example/g1", "title": "Club eyes winger",
 "seendate": "20261019T101500Z", "domain": "news.test-d.example"}, {"title": "no url"}]}"""


# ---------- pure helpers ----------


def test_parse_rss_feed():
    articles = parse_feed("https://feeds.test-d.example/rss", RSS)
    assert len(articles) == 1
    article = articles[0]
    assert article.url == "https://www.test-d.example/rss-1"
    assert article.source == "test-d.example"
    assert article.published_at == date(2026, 10, 20)
    assert "<b>" not in article.text and "monitoring" in article.text


def test_parse_gdelt_json():
    articles = parse_feed("https://api.gdeltproject.org/api/v2/doc/doc?query=x&format=json", GDELT)
    assert [(a.url, a.source, a.published_at) for a in articles] == [
        ("https://news.test-d.example/g1", "news.test-d.example", date(2026, 10, 19))
    ]
    assert parse_feed("https://api.gdeltproject.org/api/v2/doc/doc", "")[:1] == []


def test_name_helpers():
    assert normalise("  Kylian MBAPPÉ-Lottin! ") == "kylian mbappe lottin"
    assert lucene_query('Gabriel "Rocha" OR *') == "gabriel~1 rocha~1 or"
    assert lucene_query("!!!") == ""
    gabriels = [{"id": 8, "name": "Gabriel Souza"}, {"id": 9, "name": "Gabriel Rocha"}]
    assert pick_unique("Gabriel", gabriels) is None
    assert pick_unique("gabriel rocha", gabriels) == 9
    assert pick_unique("Ronaldo", gabriels) is None and pick_unique("", gabriels) is None
    assert quote_in_text("Wexley are MONITORING Santos", "News: wexley are monitoring Santos, says a source.")
    assert not quote_in_text("Wexley signed Santos", "Wexley are monitoring Santos")
    assert rumour_id_for("u", 1, 2) == rumour_id_for("u", 1, 2) != rumour_id_for("u", 1, 3)
    assert source_of("not a url") == "unknown"


# ---------- import without a database ----------


class RecordingClient:
    """Fake Neo4j client: no URL is known, no name resolves, writes are recorded."""

    def __init__(self):
        self.writes = []

    def read(self, query, params=None):
        """Nothing exists."""
        return []

    def write(self, query, params=None):
        """Record the write."""
        self.writes.append(params)
        return []


def test_import_counts_failures_without_a_database():
    service = RumourService(RecordingClient())

    def fetcher(url):
        if "broken" in url:
            raise OSError("feed down")
        return [RawArticle(url="https://test-d.example/x1", text="Rovers want Zorblat Quenterio.")]

    def explode(_messages):
        raise ValueError("model failure")

    service.fetcher = fetcher
    service.model = FakeChatModel(script=[explode])
    report = service.import_articles(ImportRequest(feed_urls=["https://broken", "https://ok", "https://ok2"]))
    assert report.feeds_failed == 1
    assert report.articles_seen == 1  # the same URL from two feeds is read once
    assert report.extraction_errors == 1 and report.articles_new == 0
    assert service.client.writes == []  # a failed article is not stored, so it is retried next time


def test_unresolved_names_are_counted_never_invented():
    service = RumourService(RecordingClient())
    service.fetcher = lambda url: [RawArticle(url="https://test-d.example/x2", text="Rovers want Zorblat Quenterio.")]
    service.model = FakeChatModel(
        script=[{"rumours": [{"player": "Zorblat Quenterio", "club": "Rovers", "quote": "Rovers want Zorblat Quenterio",
                              "confidence": 0.5}]}]
    )
    report = service.import_articles(ImportRequest(feed_urls=["https://ok"]))
    assert report.articles_new == 1 and report.unresolved_names == 2 and report.rumours_created == 0
    assert len(service.client.writes) == 1  # the article only, no rumour


# ---------- against the test database ----------

SETUP = """
CREATE (:Player {player_id: -4101, name: 'Zorblat Quenterio'}),
       (:Player {player_id: -4102, name: 'Zorblat Mirandel'}),
       (:Club {club_id: -4201, name: 'Testd Quuxford Rovers'})
"""
CLEANUP = """
MATCH (n) WHERE (n:Article AND n.url STARTS WITH 'https://test-d.example/')
   OR (n:Rumour AND EXISTS { (n)-[:ABOUT]->(:Player {player_id: -4101}) })
   OR (n:Player AND n.player_id IN [-4101, -4102]) OR (n:Club AND n.club_id = -4201)
DETACH DELETE n
"""


@pytest.fixture()
def test_nodes(neo4j_db):
    """Two homonymous test players and one test club, removed afterwards."""
    neo4j_db.write(CLEANUP)
    neo4j_db.write(SETUP)
    yield neo4j_db
    neo4j_db.write(CLEANUP)


def article(number: int, text: str, published: date | None = date(2026, 10, 1)) -> RawArticle:
    """A test article with a test URL."""
    return RawArticle(url=f"https://test-d.example/{number}", title=f"Test {number}", text=text,
                      source="test-d.example", published_at=published)


@pytest.mark.neo4j
def test_import_creates_pending_rumours(test_nodes):
    first = article(1, "Quuxford Rovers are monitoring Zorblat Quenterio. Zorblat is wanted by everyone.")
    second = article(2, "Testd Quuxford Rovers have agreed terms with Zorblat Quenterio.", date(2026, 10, 15))
    third = article(3, "A match report with no transfer in it.")
    feeds = {"https://feed-a": [first, second], "https://feed-b": [third, first]}
    service = RumourService(test_nodes)
    service.fetcher = feeds.__getitem__
    service.model = FakeChatModel(script=[
        ArticleExtraction(rumours=[
            ExtractedRumour(player="Zorblat Quenterio", club="Quuxford Rovers",
                            quote="Quuxford Rovers are monitoring Zorblat Quenterio", confidence=0.4),
            ExtractedRumour(player="Zorblat", club="Quuxford Rovers",
                            quote="Zorblat is wanted by everyone", confidence=0.2),
            ExtractedRumour(player="Zorblat Quenterio", club="Quuxford Rovers",
                            quote="Zorblat Quenterio has signed a contract", confidence=0.9),
        ]),
        {"rumours": [{"player": "zorblat quenterio", "club": "Testd Quuxford Rovers",
                      "quote": "have agreed terms with Zorblat Quenterio", "confidence": 0.8}]},
        ArticleExtraction(),
    ])

    report = service.import_articles(ImportRequest(feed_urls=list(feeds)))
    assert report.articles_seen == 3 and report.articles_new == 3
    assert report.rumours_created == 1  # the second article reinforces the pending rumour
    assert report.unresolved_names == 1  # "Zorblat" is ambiguous: counted, not guessed
    assert report.rejected_quotes == 1  # the quote is not in the article

    rumours = service.for_player(-4101)
    assert len(rumours) == 1
    rumour = rumours[0]
    assert rumour.status == "pending" and rumour.club_id == -4201 and rumour.extraction_confidence == 0.4
    assert rumour.url == "https://test-d.example/2" and rumour.published_at == date(2026, 10, 15)
    assert rumour.nature == "rumour"
    reports = test_nodes.read(
        "MATCH (a:Article)-[:REPORTS]->(:Rumour {rumour_id: $id}) RETURN count(a) AS n", {"id": rumour.rumour_id}
    )
    assert reports == [{"n": 2}]

    again = service.import_articles(ImportRequest(feed_urls=list(feeds)))
    assert again.articles_seen == 3 and again.articles_new == 0 and again.rumours_created == 0


@pytest.mark.neo4j
def test_for_player_on_the_mini_graph(neo4j_db):
    service = RumourService(neo4j_db)
    [pending] = service.for_player(5)
    assert (pending.rumour_id, pending.status, pending.club_name) == ("r1", "pending", "Wexley Athletic")
    assert pending.source == "example-sport" and pending.published_at == date(2024, 7, 20)
    assert pending.quote.startswith("Wexley Athletic")
    assert [r.rumour_id for r in service.for_player(3, "confirmed")] == ["r2"]
    assert service.for_player(3, "pending") == [] and service.for_player(1) == []


@pytest.mark.neo4j
def test_rumours_endpoint(api):
    response = api.get("/api/players/5/rumours")
    assert response.status_code == 200
    assert [row["rumour_id"] for row in response.json()] == ["r1"]
    assert api.get("/api/players/5/rumours", params={"status": "confirmed"}).json() == []


def test_script_parses_arguments():
    args = import_articles.parse_args(["--feed", "https://a", "--feed", "https://b", "--max", "5"])
    assert args.feed == ["https://a", "https://b"] and args.max == 5
