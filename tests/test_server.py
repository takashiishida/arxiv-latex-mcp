"""Offline tests for the tool handlers and fetch_source(). arXiv is never contacted."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from arxiv_latex_mcp import server as srv

PAPER = r"""\documentclass{article}
\begin{document}
\begin{abstract}A short abstract.\end{abstract}
\section{Introduction} Intro text.
\section{Method} Method text.
\subsection{Details} Detail text.
\end{document}
"""
FORMAT_WITH_SOURCE = httpx.Response(200, text="<a>Download source</a>")
FORMAT_PDF_ONLY = httpx.Response(200, text="<a>Download PDF</a>")


def call(name, **arguments):
    """Run one tool call and return its text."""
    return asyncio.run(srv.handle_call_tool(name, arguments))[0].text


class ServerTestCase(unittest.TestCase):
    """Isolates each test: temporary cache directory, caching on, 7-day TTL."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        for name, value in (("_CACHE_DIR", tmp.name), ("_CACHE_ENABLED", True), ("_CACHE_TTL_SECONDS", 7 * 86400)):
            patcher = mock.patch.object(srv, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def mark_cached(self, arxiv_id, age_days=0.0):
        """Create a complete cache entry for arxiv_id, age_days old."""
        entry = self.cache / arxiv_id.replace("/", "_")
        entry.mkdir(parents=True, exist_ok=True)
        marker = entry / srv._CACHE_MARKER
        marker.touch()
        stamp = time.time() - age_days * 86400
        os.utime(marker, (stamp, stamp))

    def patch_source(self, side_effect):
        patcher = mock.patch.object(srv, "process_latex_source", side_effect=side_effect)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def patch_format_page(self, response=None, error=None):
        patcher = mock.patch.object(srv.httpx, "get", return_value=response, side_effect=error)
        self.addCleanup(patcher.stop)
        return patcher.start()


class SectionTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.patch_source(lambda arxiv_id, **kw: PAPER)

    def test_section_by_title_and_subsection_path(self):
        self.assertIn("Method text.", call("get_paper_section", arxiv_id="2401.00001", section_path="Method"))
        self.assertIn("Detail text.", call("get_paper_section", arxiv_id="2401.00001", section_path="Method > Details"))

    def test_number_is_not_found_and_titles_are_listed(self):
        text = call("get_paper_section", arxiv_id="2401.00001", section_path="2.1")
        self.assertIn("not found", text)
        self.assertIn("Introduction; Method", text)

    def test_paper_without_sections(self):
        self.patch_source(lambda arxiv_id, **kw: "No headings here.")
        self.assertIn("No \\section headings", call("list_paper_sections", arxiv_id="2401.00001"))


class ErrorTests(ServerTestCase):
    def test_pdf_only_paper(self):
        self.patch_source(lambda arxiv_id, **kw: None)
        self.patch_format_page(FORMAT_PDF_ONLY)
        text = call("get_paper_prompt", arxiv_id="2401.00002")
        self.assertIn("submitted as a PDF only", text)
        self.assertIn("https://arxiv.org/pdf/2401.00002", text)

    def test_invalid_id(self):
        self.patch_source(lambda arxiv_id, **kw: None)
        self.patch_format_page(httpx.Response(400))
        self.assertIn("not a valid arXiv ID", call("list_paper_sections", arxiv_id="9999.99999"))

    def test_arxiv_unreachable(self):
        self.patch_source(lambda arxiv_id, **kw: None)
        self.patch_format_page(error=httpx.ConnectError("connection refused"))
        self.assertIn("could not be reached", call("get_paper_abstract", arxiv_id="2401.00003"))

    def test_source_without_abstract_block(self):
        self.patch_source(lambda arxiv_id, abstract_only=False, **kw: None if abstract_only else PAPER)
        self.patch_format_page(FORMAT_WITH_SOURCE)
        self.assertIn("no abstract block", call("get_paper_abstract", arxiv_id="2401.00004"))

    def test_cached_source_skips_the_format_page(self):
        self.mark_cached("2401.00005")
        self.patch_source(lambda arxiv_id, abstract_only=False, **kw: None if abstract_only else PAPER)
        format_page = self.patch_format_page(FORMAT_WITH_SOURCE)
        self.assertIn("no abstract block", call("get_paper_abstract", arxiv_id="2401.00005"))
        format_page.assert_not_called()

    def test_cached_source_that_cannot_be_processed(self):
        self.mark_cached("2401.00006")
        self.patch_source(lambda arxiv_id, **kw: None)
        self.assertIn("no usable main .tex file", call("get_paper_prompt", arxiv_id="2401.00006"))


class CacheTests(ServerTestCase):
    def use_cache_flags(self, source):
        return [c.kwargs.get("use_cache") for c in source.call_args_list]

    def test_fresh_copy_is_reused(self):
        self.mark_cached("2401.00007", age_days=1)
        source = self.patch_source(lambda arxiv_id, **kw: PAPER)
        srv.fetch_source("2401.00007")
        self.assertEqual(self.use_cache_flags(source), [True])

    def test_stale_copy_is_refreshed(self):
        self.mark_cached("2401.00008", age_days=8)
        source = self.patch_source(lambda arxiv_id, **kw: PAPER)
        srv.fetch_source("2401.00008")
        self.assertEqual(self.use_cache_flags(source), [False])

    def test_failed_refresh_falls_back_to_the_cached_copy(self):
        self.mark_cached("2401.00009", age_days=8)
        source = self.patch_source(lambda arxiv_id, use_cache=False, **kw: PAPER if use_cache else None)
        self.assertEqual(srv.fetch_source("2401.00009"), PAPER)
        self.assertEqual(self.use_cache_flags(source), [False, True])

    def test_versioned_id_never_expires(self):
        self.mark_cached("2401.00010v2", age_days=365)
        source = self.patch_source(lambda arxiv_id, **kw: PAPER)
        srv.fetch_source("2401.00010v2")
        self.assertEqual(self.use_cache_flags(source), [True])

    def test_old_style_id_maps_to_the_library_directory(self):
        self.assertEqual(srv._cache_entry("hep-th/9711200"), self.cache / "hep-th_9711200")

    def test_cache_can_be_turned_off(self):
        self.mark_cached("2401.00011", age_days=1)
        source = self.patch_source(lambda arxiv_id, **kw: PAPER)
        with mock.patch.object(srv, "_CACHE_ENABLED", False):
            srv.fetch_source("2401.00011")
        self.assertEqual(self.use_cache_flags(source), [None])


class InstructionsTests(unittest.TestCase):
    def test_instructions_cover_every_tool(self):
        for tool in ("get_paper_prompt", "get_paper_abstract", "list_paper_sections", "get_paper_section"):
            self.assertIn(tool, srv.SERVER_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
