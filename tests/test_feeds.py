"""The Feeds agent in a real worker, following fictional RSS, Atom and
RDF feeds served from a loopback web server that the tests add items
to between checks. Every new item must be handed on exactly once.
"""

import asyncio

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.web_stub import LOOPBACK_VARIABLE, WebStub, web  # noqa: F401


def rss(*items, title="Riverside Book Fair") -> str:
    body = ""
    for item in items:
        date = f"<pubDate>{item['date']}</pubDate>" if item.get("date") else ""
        guid = f"<guid isPermaLink=\"false\">{item['id']}</guid>" if item.get("id") else ""
        body += (f"<item><title>{item['title']}</title><link>{item.get('link', '')}</link>"
                 f"{guid}{date}<description><![CDATA[{item.get('summary', '')}]]></description>"
                 f"<dc:creator>{item.get('author', '')}</dc:creator></item>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<channel><title>{title}</title><link>https://fair.example/</link>"
            f"{body}</channel></rss>")


def atom(*entries, title="Harbourline Studio Releases") -> str:
    body = "".join(
        f"<entry><id>{e['id']}</id><title>{e['title']}</title>"
        f"<link rel=\"alternate\" href=\"{e['link']}\"/>"
        f"<link rel=\"enclosure\" href=\"https://cdn.harbourline.example/x.zip\"/>"
        f"<updated>{e['updated']}</updated><summary>{e.get('summary', '')}</summary>"
        f"<author><name>Harbourline Team</name></author></entry>"
        for e in entries)
    return (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<feed xmlns="http://www.w3.org/2005/Atom"><title>{title}</title>'
            f"<id>urn:harbourline:releases</id>{body}</feed>")


RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel rdf:about="https://notes.example/"><title>Field Notes</title></channel>
  <item rdf:about="https://notes.example/2">
    <title>Second note</title><link>https://notes.example/2</link>
    <dc:date>2026-09-10T08:30:00+02:00</dc:date><dc:creator>Mira</dc:creator>
  </item>
  <item rdf:about="https://notes.example/1">
    <title>First note</title><link>https://notes.example/1</link>
    <dc:date>2026-09-01</dc:date>
  </item>
</rdf:RDF>"""

FAIR_ITEMS = [
    {"id": "fair-2", "title": "Programme published", "link": "https://fair.example/programme",
     "date": "Wed, 09 Sep 2026 10:00:00 +0400",
     "summary": "<p>The <b>full programme</b> is out.</p><script>x()</script>",
     "author": "Fair Office"},
    {"id": "fair-1", "title": "Stall bookings open", "link": "https://fair.example/stalls",
     "date": "Tue, 01 Sep 2026 09:00:00 GMT"},
]

RELEASES = [
    {"id": "urn:harbourline:release:2.1", "title": "Studio 2.1", "link": "https://harbourline.example/2.1",
     "updated": "2026-09-08T12:00:00Z", "summary": "Faster exports."},
    {"id": "urn:harbourline:release:2.0", "title": "Studio 2.0", "link": "https://harbourline.example/2.0",
     "updated": "2026-08-20T09:15:00+01:00"},
]


def run(awaitable):
    return asyncio.run(awaitable)


def make():
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=1):
    return run(ex.invoke(agents["feeds"], name, inputs, chat_level=chat_level))


def page_advertising(href: str, kind: str = "application/rss+xml") -> str:
    return (f'<html><head><title>Riverside Book Fair</title>'
            f'<link rel="stylesheet" href="/site.css">'
            f'<link rel="alternate" type="{kind}" title="News" href="{href}">'
            f"</head><body><h1>Welcome</h1></body></html>")


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["feeds"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_new_items_is_declared_schedulable_at_level_one(self, agents):
        _, function = agents["feeds"].manifest.function("feeds.feeds.new_items")
        assert function["schedulable"] is True and function["permission_level"] == 1
        assert not function.get("llm")
        levels = {name: agents["feeds"].manifest.function(f"feeds.feeds.{name}")[1]["permission_level"]
                  for name in ("add", "list", "remove", "read")}
        assert levels == {"add": 1, "list": 0, "remove": 1, "read": 0}


class TestReading:
    def test_an_rss_feed_is_read_with_dates_in_utc_and_plain_summaries(self, agents, web):
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, provider = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
        assert status == "success", result
        assert (result["title"], result["format"], result["total"]) == ("Riverside Book Fair", "rss", 2)
        assert result["discovered_from"] == ""
        first = result["items"][0]
        assert first["published"] == "2026-09-09T06:00:00Z"
        assert first["summary"] == "The full programme is out."
        assert first["author"] == "Fair Office" and first["id"] == "fair-2"
        assert result["items"][1]["published"] == "2026-09-01T09:00:00Z"
        assert provider.data == {}

    def test_an_atom_feed_is_discovered_from_its_web_page(self, agents, web):
        web.page("/releases.atom", atom(*RELEASES), "application/atom+xml")
        page = web.page("/", page_advertising("/releases.atom", "application/atom+xml"))
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": page, "max_results": 1})
        assert status == "success", result
        assert result["feed_url"] == web.url + "/releases.atom"
        assert result["discovered_from"] == page
        assert (result["format"], result["total"], len(result["items"])) == ("atom", 2, 1)
        item = result["items"][0]
        assert item["link"] == "https://harbourline.example/2.1"
        assert item["author"] == "Harbourline Team"

    def test_an_rdf_feed_is_read_with_dublin_core_dates(self, agents, web):
        url = web.page("/notes.rdf", RDF, "application/rdf+xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
        assert status == "success", result
        assert (result["format"], result["title"]) == ("rdf", "Field Notes")
        assert [i["published"] for i in result["items"]] == ["2026-09-10T06:30:00Z",
                                                            "2026-09-01T00:00:00Z"]
        assert result["items"][0]["id"] == "https://notes.example/2"

    def test_a_page_without_a_feed_says_so(self, agents, web):
        url = web.page("/about", "<html><head><title>About</title></head><body>Hi</body></html>")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
        assert status == "error" and result["kind"] == "not_a_feed"
        assert "advertises no RSS or Atom feed" in result["error"]

    def test_a_feed_declaring_entities_is_not_parsed(self, agents, web):
        bomb = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaa">'
                '<!ENTITY b "&a;&a;&a;&a;">]><rss version="2.0"><channel>'
                '<title>&b;</title></channel></rss>')
        url = web.page("/bomb.xml", bomb, "application/rss+xml")
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
        assert status == "error" and "entities" in result["error"]


class TestSubscribing:
    def test_rss_items_are_handed_on_exactly_once_oldest_first(self, agents, web):
        web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        page = web.page("/", page_advertising("/feed.xml"))
        ex, provider = make()
        added, status = invoke(agents, ex, "feeds.feeds.add", {"url": page})
        assert status == "success", added
        assert added["feed_url"] == web.url + "/feed.xml"
        assert added["discovered_from"] == page and added["already"] is False
        assert added["newest"] == "2026-09-09T06:00:00Z"

        again, _ = invoke(agents, ex, "feeds.feeds.add", {"url": web.url + "/feed.xml"})
        assert again["already"] is True and again["feed_ref"] == added["feed_ref"]
        assert len(provider.data["feeds__feed"]) == 1

        # From now: what the feed held already is not news.
        quiet, status = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert status == "success", quiet
        assert quiet == {"checked": 1, "items": [], "failed": [], "more": False}

        # Two new items, one in the same second as the other.
        newer = [
            {"id": "fair-4", "title": "Parking update", "link": "https://fair.example/parking",
             "date": "Fri, 11 Sep 2026 08:00:00 GMT"},
            {"id": "fair-3", "title": "Author talks", "link": "https://fair.example/talks",
             "date": "Fri, 11 Sep 2026 08:00:00 GMT"},
            {"id": "fair-2b", "title": "Map", "link": "https://fair.example/map",
             "date": "Thu, 10 Sep 2026 07:00:00 GMT"},
        ]
        web.page("/feed.xml", rss(*newer, *FAIR_ITEMS), "application/rss+xml")
        news_, _ = invoke(agents, ex, "feeds.feeds.new_items", {"max_results": 2})
        # Same second: the one listed further down the feed came first.
        assert [i["id"] for i in news_["items"]] == ["fair-2b", "fair-3"]
        assert news_["more"] is True
        assert news_["items"][0]["feed_ref"] == added["feed_ref"]
        assert news_["items"][0]["feed_title"] == "Riverside Book Fair"

        rest, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        # The twin in the cursor's second is neither lost nor repeated.
        assert [i["id"] for i in rest["items"]] == ["fair-4"]
        assert rest["more"] is False

        done, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert done["items"] == []

    def test_atom_items_across_two_feeds_are_merged_oldest_first(self, agents, web):
        fair = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        releases = web.page("/releases.atom", atom(*RELEASES), "application/atom+xml")
        ex, provider = make()
        a, _ = invoke(agents, ex, "feeds.feeds.add", {"url": fair})
        b, _ = invoke(agents, ex, "feeds.feeds.add", {"url": releases})
        assert b["format"] == "atom"

        web.page("/releases.atom", atom(
            {"id": "urn:harbourline:release:2.2", "title": "Studio 2.2",
             "link": "https://harbourline.example/2.2", "updated": "2026-09-12T10:00:00+02:00"},
            *RELEASES), "application/atom+xml")
        web.page("/feed.xml", rss(
            {"id": "fair-5", "title": "Volunteers wanted", "link": "https://fair.example/volunteer",
             "date": "Sat, 12 Sep 2026 09:00:00 GMT"}, *FAIR_ITEMS), "application/rss+xml")
        news_, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert [(i["title"], i["feed_ref"]) for i in news_["items"]] == [
            ("Studio 2.2", b["feed_ref"]), ("Volunteers wanted", a["feed_ref"])]
        assert news_["items"][0]["published"] == "2026-09-12T08:00:00Z"
        again, _ = invoke(agents, ex, "feeds.feeds.new_items", {"feed_ref": b["feed_ref"]})
        assert again["items"] == [] and again["checked"] == 1

    def test_undated_items_are_new_only_when_their_id_was_not_seen(self, agents, web):
        undated = [{"id": f"memo-{n}", "title": f"Memo {n}", "link": f"https://fair.example/memo/{n}"}
                   for n in (2, 1)]
        url = web.page("/memos.xml", rss(*undated, title="Fair memos"), "application/rss+xml")
        ex, provider = make()
        added, status = invoke(agents, ex, "feeds.feeds.add", {"url": url})
        assert status == "success" and added["newest"] == ""
        quiet, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert quiet["items"] == []

        web.page("/memos.xml", rss({"id": "memo-3", "title": "Memo 3",
                                    "link": "https://fair.example/memo/3"},
                                   *undated, title="Fair memos"), "application/rss+xml")
        news_, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert [i["id"] for i in news_["items"]] == ["memo-3"]
        assert news_["items"][0]["published"] == ""
        again, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert again["items"] == []

    def test_a_failing_feed_is_listed_apart_and_wakes_no_one(self, agents, web):
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, provider = make()
        added, _ = invoke(agents, ex, "feeds.feeds.add", {"url": url})
        web.fail("/feed.xml", 500)
        first, status = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert status == "success", first
        assert first["items"] == []
        assert [(f["feed_ref"], f["failures"]) for f in first["failed"]] == [(added["feed_ref"], 1)]
        second, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert second["failed"][0]["failures"] == 2
        listed, _ = invoke(agents, ex, "feeds.feeds.list", {})
        assert listed["feeds"][0]["failures"] == 2

        web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        back, _ = invoke(agents, ex, "feeds.feeds.new_items", {})
        assert back == {"checked": 1, "items": [], "failed": [], "more": False}

    def test_a_subscription_is_listed_and_removed(self, agents, web):
        url = web.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
        ex, provider = make()
        added, _ = invoke(agents, ex, "feeds.feeds.add", {"url": url})
        listed, _ = invoke(agents, ex, "feeds.feeds.list", {})
        assert listed["total"] == 1
        assert listed["feeds"][0]["read_up_to"] == "2026-09-09T06:00:00Z"
        removed, status = invoke(agents, ex, "feeds.feeds.remove", {"feed_ref": added["feed_ref"]})
        assert status == "success" and removed["removed"] is True
        assert provider.data["feeds__feed"] == {}


class TestAddressSafety:
    def test_a_private_feed_address_is_refused_even_with_the_test_variable(self, agents, web):
        ex, provider = make()
        result, status = invoke(agents, ex, "feeds.feeds.add", {"url": "http://192.168.0.10/feed.xml"})
        assert status == "error" and result["kind"] == "forbidden"
        assert "private" in result["error"] and provider.data == {}

    def test_a_page_advertising_a_private_feed_is_refused(self, agents, web):
        page = web.page("/", page_advertising("http://10.0.0.1/feed.xml"))
        ex, provider = make()
        result, status = invoke(agents, ex, "feeds.feeds.add", {"url": page})
        assert status == "error" and result["kind"] == "forbidden"
        assert "10.0.0.1" in result["error"]

    def test_a_redirect_to_link_local_is_refused(self, agents, web):
        url = web.redirect("/feed.xml", "http://[fe80::1]/feed.xml", status=307)
        ex, _ = make()
        result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
        assert status == "error" and result["kind"] == "forbidden"

    def test_loopback_is_refused_without_the_test_variable(self, agents, monkeypatch):
        monkeypatch.delenv(LOOPBACK_VARIABLE, raising=False)
        stub = WebStub().start()
        try:
            url = stub.page("/feed.xml", rss(*FAIR_ITEMS), "application/rss+xml")
            ex, _ = make()
            result, status = invoke(agents, ex, "feeds.feeds.read", {"url": url})
            assert status == "error" and result["kind"] == "forbidden"
            assert "loopback" in result["error"] and stub.hits == {}
        finally:
            stub.stop()
