"""The Web Reader agent in a real worker, reading from a loopback web
server that holds fictional pages for Harbourline Studio. Loopback is
only reachable because the tests set the test-only variable; the
safety tests prove what stays refused with and without it.
"""

import asyncio

import pytest

from ai_runtime.execution.executor import FunctionExecutor
from sim.resources import InMemoryResourceProvider

from tests.web_stub import LOOPBACK_VARIABLE, WebStub, text_pdf, web  # noqa: F401

PRICING = """<!doctype html>
<html><head><title>Harbourline Studio — Pricing</title>
<script>window.tracking = "Starter plan costs nothing";</script>
<style>.plan { color: red }</style></head>
<body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<header><h1>Pricing</h1></header>
<main>
<p>Every plan includes unlimited projects and email support.</p>
<h2>Starter</h2>
<p>The Starter plan costs USD 12 a month
   for one designer.</p>
<ul><li>5 GB storage</li><li>Shared <a href="/templates">templates</a></li></ul>
<h2>Refunds</h2>
<p>Annual plans can be refunded within 30 days of purchase.</p>
<p>See the <a href="https://help.harbourline.example/refunds#annual">refund guide</a>.</p>
</main>
<footer>Copyright Harbourline Studio. <a href="/privacy">Privacy</a></footer>
</body></html>"""

REPORT_PAGES = [
    ["Harbourline Studio Annual Report 2026", "Revenue grew to USD 1.2 million."],
    ["Outlook", "The studio plans to open a second office in Lisbon."],
    ["Risks", "Currency movements may reduce margins."],
]


def run(awaitable):
    return asyncio.run(awaitable)


def make():
    provider = InMemoryResourceProvider()
    return FunctionExecutor(provider=provider), provider


def invoke(agents, ex, name, inputs, chat_level=2):
    return run(ex.invoke(agents["web_reader"], name, inputs, chat_level=chat_level))


class TestThePackage:
    def test_the_worker_verifies_every_declared_function(self, agents):
        from ai_runtime.agents.worker_handle import WorkerHandle

        agent = agents["web_reader"]
        assert WorkerHandle.probe(agent.environment.python, agent.folder,
                                  agent.manifest.document) == []

    def test_levels_are_reads_and_one_save(self, agents):
        levels = {name: agents["web_reader"].manifest.function(f"web_reader.pages.{name}")[1]["permission_level"]
                  for name in ("read", "find", "save")}
        assert levels == {"read": 0, "find": 0, "save": 2}


class TestReading:
    def test_a_web_page_reads_as_sections_without_its_boilerplate(self, agents, web):
        web.page("/pricing", PRICING)
        start = web.redirect("/plans", "/pricing", status=301)
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": start})
        assert status == "success", result
        assert result["final_url"] == web.url + "/pricing"
        assert result["title"] == "Harbourline Studio — Pricing"
        assert (result["kind"], result["unit"]) == ("html", "section")
        headings = [p["heading"] for p in result["parts"]]
        assert headings == ["# Pricing", "## Starter", "## Refunds"]
        starter = result["parts"][1]["text"]
        # Whitespace collapsed, list items kept as lines.
        assert "The Starter plan costs USD 12 a month for one designer." in starter
        assert "- 5 GB storage" in starter
        everything = " ".join(p["text"] for p in result["parts"])
        assert "tracking" not in everything and "color: red" not in everything
        assert "Copyright" not in everything and "About" not in everything
        assert result["total_parts"] == 3 and result["truncated"] is False
        urls = [link["url"] for link in result["links"]]
        assert web.url + "/templates" in urls
        assert "https://help.harbourline.example/refunds" in urls
        assert web.url + "/about" not in urls        # the nav's links are boilerplate too
        # An honest User-Agent that names the platform.
        assert all("DecentAI" in agent for agent in web.user_agents)

    def test_a_long_page_is_read_in_parts_with_totals(self, agents, web):
        sections = "".join(
            f"<h2>Clause {n}</h2><p>{'The studio keeps records of every project. ' * 40}</p>"
            for n in range(1, 13))
        web.page("/terms", f"<html><head><title>Terms</title></head><body>{sections}</body></html>")
        ex, _ = make()
        first, status = invoke(agents, ex, "web_reader.pages.read",
                               {"url": web.url + "/terms", "max_chars": 4000})
        assert status == "success", first
        assert first["total_parts"] == 12
        assert first["truncated"] is True
        assert sum(len(p["text"]) for p in first["parts"]) <= 4000
        following, status = invoke(agents, ex, "web_reader.pages.read",
                                   {"url": web.url + "/terms", "from": first["next"],
                                    "max_chars": 4000})
        assert status == "success", following
        assert following["parts"][0]["number"] == first["next"]
        assert following["parts"][0]["heading"] == f"## Clause {first['next']}"
        beyond, status = invoke(agents, ex, "web_reader.pages.read",
                                {"url": web.url + "/terms", "from": 40})
        assert status == "error" and "12 sections" in beyond["error"]

    def test_a_pdf_reads_page_by_page(self, agents, web):
        url = web.page("/report.pdf", text_pdf(REPORT_PAGES, title="Annual Report 2026"),
                       "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "success", result
        assert (result["kind"], result["unit"], result["total_parts"]) == ("pdf", "page", 3)
        assert result["title"] == "Annual Report 2026"
        assert [p["number"] for p in result["parts"]] == [1, 2, 3]
        assert "second office in Lisbon" in result["parts"][1]["text"]
        assert result["unreadable_pages"] == []

    def test_plain_text_and_json_pass_through(self, agents, web):
        ex, _ = make()
        text_url = web.page("/notes.txt", "Opening hours\nMonday to Friday, 9 to 5.", "text/plain")
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": text_url})
        assert status == "success", result
        assert result["kind"] == "text"
        assert result["parts"][0]["text"] == "Opening hours\nMonday to Friday, 9 to 5."
        json_url = web.page("/status.json", '{"status": "operational"}', "application/json")
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": json_url})
        assert status == "success" and result["kind"] == "json"
        assert '"operational"' in result["parts"][0]["text"]

    def test_an_image_is_named_not_read(self, agents, web):
        url = web.page("/logo.png", b"\x89PNG\r\n\x1a\n" + bytes(200), "image/png")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error" and result["kind"] == "unsupported"
        assert "image/png" in result["error"]

    def test_an_error_status_is_reported(self, agents, web):
        url = web.fail("/down", 503)
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error"
        assert (result["kind"], result["status"]) == ("http", 503)


class TestFinding:
    def test_found_phrases_are_quoted_with_their_section_and_absent_ones_named(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.find", {
            "url": url, "phrases": ["costs USD 12 a month for", "refunded", "cancellation fee"]})
        assert status == "success", result
        found = {row["phrase"]: row for row in result["phrases"]}
        # A line break in the page does not hide a phrase.
        assert found["costs USD 12 a month for"]["found"] is True
        assert found["refunded"]["count"] == 1
        assert result["absent"] == ["cancellation fee"]
        assert "cancellation fee" in result["note"]
        refund = next(m for m in result["matches"] if m["phrase"] == "refunded")
        assert refund["heading"] == "## Refunds" and refund["number"] == 3
        assert "refunded within 30 days" in refund["quote"]

    def test_find_in_a_pdf_names_the_page(self, agents, web):
        url = web.page("/report.pdf", text_pdf(REPORT_PAGES), "application/pdf")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.find",
                                {"url": url, "phrase": "currency"})
        assert status == "success", result
        assert [m["number"] for m in result["matches"]] == [3]
        assert result["absent"] == []

    def test_no_phrase_is_refused(self, agents, web):
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.find", {"url": web.url + "/x"})
        assert status == "error" and result["kind"] == "invalid"


class TestSaving:
    def test_a_web_page_is_saved_as_markdown_with_its_source(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, provider = make()
        result, status = invoke(agents, ex, "web_reader.pages.save", {"url": url})
        assert status == "success", result
        # Named for its title, with characters unsafe in a filename replaced.
        assert result["filename"] == "Harbourline Studio _ Pricing.md"
        assert result["mime_type"] == "text/markdown"
        stored = provider.files["web_reader__page"][result["file_ref"]]["content"]
        text = stored.decode("utf-8")
        assert text.startswith("# Harbourline Studio — Pricing")
        assert f"Source: {url}" in text and f"Fetched: {result['fetched_at']}" in text
        assert "## Refunds" in text and "tracking" not in text
        assert result["size"] == len(stored)

    def test_a_pdf_is_saved_as_its_original_bytes(self, agents, web):
        raw = text_pdf(REPORT_PAGES)
        url = web.page("/files/annual-report.pdf", raw, "application/pdf")
        ex, provider = make()
        result, status = invoke(agents, ex, "web_reader.pages.save", {"url": url})
        assert status == "success", result
        assert result["filename"] == "annual-report.pdf"
        assert provider.files["web_reader__page"][result["file_ref"]]["content"] == raw

    def test_documents_reads_what_was_saved(self, agents, web):
        """The point of saving: another agent reads the file by its ref."""
        page_url = web.page("/pricing", PRICING)
        pdf_url = web.page("/report.pdf", text_pdf(REPORT_PAGES), "application/pdf")
        ex, provider = make()
        page, _ = invoke(agents, ex, "web_reader.pages.save", {"url": page_url})
        pdf, _ = invoke(agents, ex, "web_reader.pages.save", {"url": pdf_url})

        text, status = run(ex.invoke(agents["documents"], "documents.read.text",
                                     {"file_ref": page["file_ref"]}, chat_level=0))
        assert status == "success", text
        assert "Annual plans can be refunded within 30 days" in text["pages"][0]["text"]
        text, status = run(ex.invoke(agents["documents"], "documents.read.text",
                                     {"file_ref": pdf["file_ref"]}, chat_level=0))
        assert status == "success", text
        assert [p["page"] for p in text["pages"]] == [1, 2, 3]
        assert "Lisbon" in text["pages"][1]["text"]

    def test_a_page_too_large_to_save_is_refused_with_its_size(self, agents, web):
        """A PDF longer than one fetch reads is not saved cut short: the
        refusal says how much was read, and nothing is stored."""
        read = 5 * 1024 * 1024
        big = b"%PDF-1.4\n" + b"0" * (read + 1024)
        url = web.page("/big.pdf", big, "application/pdf")
        ex, provider = make()
        result, status = invoke(agents, ex, "web_reader.pages.save", {"url": url})
        assert status == "error", result
        assert result["kind"] == "too_large" and result["size"] == read
        assert f"more than {read:,} bytes" in result["error"]
        assert not provider.files.get("web_reader__page")

    def test_saving_needs_the_chats_trust(self, agents, web):
        url = web.page("/pricing", PRICING)
        ex, provider = make()
        result, status = invoke(agents, ex, "web_reader.pages.save", {"url": url}, chat_level=1)
        assert status == "error" and "approv" in result["error"].lower()
        assert not provider.files.get("web_reader__page")


class TestAddressSafety:
    @pytest.mark.parametrize("url, why", [
        ("http://10.0.0.1/admin", "private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://[::ffff:192.168.1.10]/", "private"),
        ("http://0.0.0.0:8080/", "unspecified"),
    ])
    def test_internal_addresses_are_refused_even_with_the_test_variable(self, agents, web, url, why):
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden" and why in result["error"]

    def test_loopback_is_refused_without_the_test_variable(self, agents, monkeypatch):
        monkeypatch.delenv(LOOPBACK_VARIABLE, raising=False)
        stub = WebStub().start()
        try:
            url = stub.page("/pricing", PRICING)
            ex, _ = make()
            result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
            assert status == "error", result
            assert result["kind"] == "forbidden" and "loopback" in result["error"]
            assert stub.hits == {}
        finally:
            stub.stop()

    def test_a_redirect_to_a_forbidden_address_is_refused(self, agents, web):
        url = web.redirect("/go", "http://169.254.169.254/latest/meta-data/iam")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden" and "169.254.169.254" in result["error"]

    def test_redirects_stop_after_five(self, agents, web):
        for n in range(7):
            web.redirect(f"/hop{n}", f"/hop{n + 1}")
        web.page("/hop7", "<p>arrived</p>")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": web.url + "/hop0"})
        assert status == "error" and result["kind"] == "too_many_redirects"
        assert web.hits.get("/hop6") is None

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://files.harbourline.example/report.pdf",
        "http://admin:secret@www.harbourline.example/",
    ])
    def test_other_schemes_and_credentials_are_refused(self, agents, url):
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error" and result["kind"] == "invalid_url", result

    def test_a_huge_body_is_read_only_up_to_the_cap(self, agents, web):
        url = web.page("/huge.txt", "line of text\n" * 500_000, "text/plain")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read",
                                {"url": url, "max_chars": 4000})
        assert status == "success", result
        assert result["body_truncated"] is True
        assert result["total_chars"] <= 5 * 1024 * 1024

    def test_the_connected_address_is_checked_too(self, web, monkeypatch):
        """A name that passed the check but connects somewhere internal
        (DNS rebinding) is refused at the socket. Exercised in-process,
        with the name check switched off, since no test resolver can
        rebind a name."""
        from web_reader.tools.fetch import FetchError, SafeFetcher

        url = web.page("/pricing", PRICING)
        monkeypatch.delenv(LOOPBACK_VARIABLE)
        fetcher = SafeFetcher("DecentAI-test")
        fetcher.check_url = lambda _url: None
        with pytest.raises(FetchError) as refused:
            fetcher.get(url)
        assert refused.value.kind == "forbidden"
        assert web.hits == {}


class TestBehindThePlatformsProxy:
    """Where the platform confines an agent, its one way out is the
    platform's proxy. A fetch goes through it and leaves the name to
    it; what this agent refuses by itself, it still refuses."""

    @pytest.fixture
    def proxy(self, web, monkeypatch):
        from ai_runtime.agents.egress import EgressProxy

        names = {"inside.harbourline.example": ["10.0.0.5"],
                 "nowhere.harbourline.example": []}
        monkeypatch.setattr(
            EgressProxy, "resolver",
            staticmethod(lambda host, port: names.get(host, ["127.0.0.1"])))
        found = EgressProxy(0, allow_loopback=True)
        assert found.start() == []
        found.admitted = lambda hosts: monkeypatch.setenv(
            "DECENTAI_PROXY", found.address(found.admit("Web Reader", {
                "declared": True, "any": hosts == "any",
                "hosts": [] if hosts == "any" else hosts, "from_secrets": []})))
        yield found
        found.stop()

    def port(self, web):
        return web.url.rsplit(":", 1)[1]

    def test_a_page_is_read_through_it_by_name(self, agents, web, proxy):
        web.page("/pricing", PRICING)
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": f"http://www.harbourline.example:{self.port(web)}/pricing"})
        assert status == "success", result
        assert result["title"] == "Harbourline Studio — Pricing"
        assert web.hits == {"/pricing": 1}
        assert proxy.refusals == 0

    def test_it_is_the_way_out_and_not_a_way_beside_one(self, agents, web, proxy):
        """The same page, and a pass that opens nothing: had the fetch
        gone around the proxy, it would have arrived."""
        url = web.page("/pricing", PRICING)
        proxy.admitted([])
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden"
        assert web.hits == {}

    def test_a_name_that_resolves_inside_is_refused_by_it(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": "http://inside.harbourline.example/admin"})
        assert status == "error", result
        assert result["kind"] == "forbidden"
        assert "private network address" in result["error"]
        assert proxy.refusals == 1

    def test_an_encrypted_page_refused_says_why_too(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": "https://inside.harbourline.example/admin"})
        assert status == "error", result
        assert result["kind"] == "forbidden"
        assert "private network address" in result["error"]

    def test_an_address_written_as_one_is_refused_before_it_is_asked(
            self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": "http://169.254.169.254/latest/meta-data/"})
        assert status == "error", result
        assert result["kind"] == "forbidden" and "link-local" in result["error"]
        assert proxy.refusals == 0

    def test_a_redirect_inside_is_refused_at_the_hop(self, agents, web, proxy):
        url = web.redirect("/go", "http://inside.harbourline.example/admin")
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {"url": url})
        assert status == "error", result
        assert result["kind"] == "forbidden"
        assert web.hits == {"/go": 1}

    def test_a_name_that_does_not_resolve_is_the_networks(self, agents, web, proxy):
        proxy.admitted("any")
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": "http://nowhere.harbourline.example/"})
        assert status == "error", result
        assert result["kind"] == "network"
        assert "does not resolve" in result["error"]

    def test_the_pass_is_in_no_error(self, agents, web, proxy, monkeypatch):
        import os

        proxy.admitted("any")
        address = os.environ["DECENTAI_PROXY"]
        proxy.stop()                  # nothing listens where the agent was sent
        ex, _ = make()
        result, status = invoke(agents, ex, "web_reader.pages.read", {
            "url": "http://www.harbourline.example/"})
        assert status == "error", result
        assert address not in result["error"]
        assert address.split("@")[0].rsplit(":", 1)[1] not in result["error"]
