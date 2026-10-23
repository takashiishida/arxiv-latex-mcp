"""Async arXiv discovery. See THIRD_PARTY_NOTICES for upstream attribution."""

import asyncio
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any

import httpx
import mcp.types as types

from arxiv_latex_mcp import __version__

logger = logging.getLogger("arxiv-latex-mcp")
MAX_SEARCH_RESULTS = 50
_MIN_REQUEST_INTERVAL = 3.0
_last_request_time = 0.0
_request_lock = asyncio.Lock()

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
ARXIV_HEADERS = {
    "User-Agent": f"arxiv-latex-mcp/{__version__} (https://github.com/takashiishida/arxiv-latex-mcp)"
}
VALID_CATEGORIES = {
    "cs", "econ", "eess", "math", "physics", "q-bio", "q-fin", "stat",
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph",
    "hep-th", "math-ph", "nlin", "nucl-ex", "nucl-th", "quant-ph",
}

SEARCH_TOOL = types.Tool(
    name="search_papers",
    annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
    description=(
        'Discover arXiv papers by topic, author, or title. Use quoted phrases for '
        'exact matches, ti:"title phrase", au:"author name", or abs:keyword. '
        'Combine terms with AND, OR, or ANDNOT. Optional categories such as '
        'cs.AI, cs.LG, stat.ML, and quant-ph are ORed together and restrict the '
        'query. Dates are inclusive submission dates in YYYY-MM-DD format (UTC). '
        'Pass a returned id to get_paper_prompt to read the LaTeX source. '
        'Requests are throttled; on rate-limit errors, wait 60s before retrying.'
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Topic, title, author, or arXiv search expression."},
            "max_results": {
                "type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS,
                "default": 10, "description": "Maximum number of papers to return.",
            },
            "date_from": {"type": "string", "description": "Inclusive start date (YYYY-MM-DD)."},
            "date_to": {"type": "string", "description": "Inclusive end date (YYYY-MM-DD)."},
            "categories": {
                "type": "array", "items": {"type": "string"},
                "description": "Categories to include, e.g. ['cs.LG', 'stat.ML'].",
            },
            "sort_by": {"type": "string", "enum": ["relevance", "date"], "default": "relevance"},
        },
        "required": ["query"],
    },
)


def _parse_date(value: str, name: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format") from exc


def _search_params(arguments: dict[str, Any]) -> dict[str, str | int]:
    query = arguments.get("query", "")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("No search query provided")
    maximum = arguments.get("max_results", 10)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("max_results must be a positive integer")
    sort_by = arguments.get("sort_by", "relevance")
    if sort_by not in ("relevance", "date"):
        raise ValueError("sort_by must be 'relevance' or 'date'")

    parts = [f"({query.strip()})"]
    categories = arguments.get("categories", [])
    if not isinstance(categories, list) or any(
        not isinstance(category, str)
        or not re.fullmatch(r"[a-z-]+(?:\.[A-Za-z-]+)?", category)
        or category.split(".")[0] not in VALID_CATEGORIES
        for category in categories
    ):
        raise ValueError("Invalid category. Check arXiv category names.")
    if categories:
        parts.append("(" + " OR ".join(f"cat:{category}" for category in categories) + ")")

    if "date_from" in arguments or "date_to" in arguments:
        start = _parse_date(arguments["date_from"], "date_from") if "date_from" in arguments else date(1991, 7, 1)
        end = _parse_date(arguments["date_to"], "date_to") if "date_to" in arguments else datetime.now(timezone.utc).date()
        if start > end:
            raise ValueError("date_from must be on or before date_to")
        # Use actual spaces here; httpx encodes the entire parameter exactly once.
        parts.append(f"submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]")
    return {
        "search_query": " AND ".join(parts),
        "max_results": min(maximum, MAX_SEARCH_RESULTS),
        "sortBy": "submittedDate" if sort_by == "date" else "relevance",
        "sortOrder": "descending",
    }


async def _rate_limited_get(client: httpx.AsyncClient, params: dict[str, str | int]) -> httpx.Response:
    """Serialize requests and throttle every attempt, including timeout retries."""
    global _last_request_time
    for attempt in range(2):
        try:
            async with _request_lock:
                elapsed = time.monotonic() - _last_request_time
                if elapsed < _MIN_REQUEST_INTERVAL:
                    await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
                _last_request_time = time.monotonic()
                response = await client.get(ARXIV_API_URL, params=params, headers=ARXIV_HEADERS)
            if response.status_code in (429, 503):
                raise RuntimeError(
                    f"arXiv is rate limiting this IP (HTTP {response.status_code}). Wait 60s before retrying."
                )
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            if attempt:
                raise
            logger.warning("arXiv request timed out, retrying once")
            await asyncio.sleep(5.0)
    raise RuntimeError("arXiv request timed out after retry")


def _parse_arxiv_atom(xml_text: str) -> list[dict[str, Any]]:
    """Parse metadata, preserving old-style IDs and rejecting API error feeds."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Failed to parse arXiv API response: {exc}") from exc
    if root.tag != f"{{{ARXIV_NS['atom']}}}feed":
        raise ValueError("arXiv returned an unexpected response instead of an Atom feed")
    results = []
    for entry in root.findall("atom:entry", ARXIV_NS):
        paper_url = entry.findtext("atom:id", "", ARXIV_NS).strip()
        if "/api/errors" in paper_url:
            raise ValueError(entry.findtext("atom:summary", "arXiv API error", ARXIV_NS).strip())
        if "/abs/" not in paper_url:
            continue
        paper_id = paper_url.split("/abs/", 1)[1]
        short_id = re.sub(r"v\d+$", "", paper_id)
        categories = []
        for element in entry.findall("arxiv:primary_category", ARXIV_NS) + entry.findall("atom:category", ARXIV_NS):
            category = element.get("term")
            if category and category not in categories:
                categories.append(category)
        pdf_url = next(
            (link.get("href") for link in entry.findall("atom:link", ARXIV_NS)
             if link.get("title") == "pdf" and link.get("href")),
            f"https://arxiv.org/pdf/{paper_id}",
        )
        results.append({
            "id": short_id,
            "title": " ".join(entry.findtext("atom:title", "", ARXIV_NS).split()),
            "authors": [name.text.strip() for name in entry.findall("atom:author/atom:name", ARXIV_NS) if name.text],
            "abstract": " ".join(entry.findtext("atom:summary", "", ARXIV_NS).split()),
            "categories": categories,
            "published": entry.findtext("atom:published", "", ARXIV_NS).strip(),
            "url": pdf_url,
        })
    return results


async def handle_search(arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        params = _search_params(arguments)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            response = await _rate_limited_get(client, params)
        results = _parse_arxiv_atom(response.text)
        result = json.dumps({"total_results": len(results), "papers": results}, indent=2)
        logger.info("Search returned %s results", len(results))
    except httpx.TimeoutException:
        result = "Error: arXiv search timed out. Try again later."
    except httpx.HTTPStatusError as exc:
        result = f"Error: arXiv answered HTTP {exc.response.status_code}. Try again later."
    except httpx.RequestError:
        result = "Error: arXiv could not be reached. Try again later."
    except (ValueError, RuntimeError) as exc:
        result = f"Error: {exc}"
    return [types.TextContent(type="text", text=result)]
