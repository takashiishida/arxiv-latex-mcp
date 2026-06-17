"""
ArXiv LaTeX MCP Server

This server provides tools to fetch and process arXiv papers' LaTeX source code
for better mathematical expression interpretation.
"""

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import arxiv
import httpx
from dateutil import parser as dateutil_parser
from arxiv_to_prompt import extract_section, list_sections, process_latex_source
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types

from arxiv_latex_mcp import __version__

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arxiv-latex-mcp")

# Create server instance
server = Server("arxiv-latex-mcp")

# MCP logging level (can be changed by client via logging/setLevel)
mcp_log_level: types.LoggingLevel = "info"

MAX_SEARCH_RESULTS = 50

# arXiv asks for >= 3s between requests
_last_request_time: float = 0.0
_request_lock = asyncio.Lock()
_MIN_REQUEST_INTERVAL = 3.0

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}
ARXIV_HEADERS = {
    "User-Agent": "arxiv-latex-mcp/1.0 (https://github.com/takashiishida/arxiv-latex-mcp; research tool)"
}

VALID_CATEGORIES = {
    "cs", "econ", "eess", "math", "physics", "q-bio", "q-fin", "stat",
    "astro-ph", "cond-mat", "gr-qc", "hep-ex", "hep-lat", "hep-ph",
    "hep-th", "math-ph", "nlin", "nucl-ex", "nucl-th", "quant-ph",
}

LATEX_RENDER_INSTRUCTIONS = """

IMPORTANT INSTRUCTIONS FOR RENDERING:
When discussing this paper, please use dollar sign notation ($...$) for inline equations and double dollar signs ($$...$$) for display equations when providing responses that include LaTeX mathematical expressions.
"""


@server.set_logging_level()
async def handle_set_logging_level(level: types.LoggingLevel) -> None:
    """Handle logging level changes from the client."""
    global mcp_log_level
    mcp_log_level = level
    logger.info(f"MCP logging level set to: {level}")


async def mcp_log(level: types.LoggingLevel, message: str) -> None:
    """Send a log message to the MCP client."""
    mcp_level_order = [
        "debug",
        "info",
        "notice",
        "warning",
        "error",
        "critical",
        "alert",
        "emergency",
    ]
    if mcp_level_order.index(level) >= mcp_level_order.index(mcp_log_level):
        try:
            ctx = server.request_context
            await ctx.session.send_log_message(
                level=level, data=message, logger="arxiv-latex-mcp"
            )
        except Exception:
            pass


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="get_paper_prompt",
            description="Recommended default: fetch the full LaTeX source of an arXiv paper for precise interpretation of mathematical expressions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                    }
                },
                "required": ["arxiv_id"],
            },
        ),
        types.Tool(
            name="get_paper_abstract",
            description="Get just the abstract of an arXiv paper. Use this for a quick preview when the user hasn't read the paper yet, not when they provide an arXiv ID to discuss a paper.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                    }
                },
                "required": ["arxiv_id"],
            },
        ),
        types.Tool(
            name="list_paper_sections",
            description="List section headings of an arXiv paper. Useful when the full paper is too long for context and you need to identify which sections to fetch individually.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                    }
                },
                "required": ["arxiv_id"],
            },
        ),
        types.Tool(
            name="get_paper_section",
            description="Get a specific section of an arXiv paper by section path. Use when the full paper is too long for context or the user wants to focus on a particular section. Use list_paper_sections first to find available paths.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                    },
                    "section_path": {
                        "type": "string",
                        "description": "The section path to extract (e.g., '1', '2.1', 'Introduction'). Use list_paper_sections to find available paths.",
                    },
                },
                "required": ["arxiv_id", "section_path"],
            },
        ),
        types.Tool(
            name="search_papers",
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
            description="""Search for papers on arXiv with advanced filtering and query optimization.

QUERY CONSTRUCTION GUIDELINES:
- Use QUOTED PHRASES for exact matches: "multi-agent systems", "neural networks"
- Combine related concepts with OR: "AI agents" OR "software agents"
- Use field-specific searches: ti:"exact title phrase", au:"author name", abs:"keyword"
- Use ANDNOT to exclude: "machine learning" ANDNOT "survey"

CATEGORY FILTERING (recommended):
Computer Science: cs.AI, cs.LG, cs.CL (NLP), cs.CV, cs.MA (Multi-Agent), cs.RO, cs.NE, cs.IR, cs.HC, cs.CR, cs.DB
Statistics & Math: stat.ML, stat.AP, math.OC, math.ST
Physics & Other: quant-ph, eess.SP, eess.AS, physics.data-an

DATE FILTERING: Use YYYY-MM-DD format. Results sorted by relevance by default.
RATE LIMITING: arXiv enforces 3s between requests (handled automatically). On rate limit errors, wait 60s before retrying.""",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": 'Search query. Use quoted phrases for exact matches (e.g., \'"machine learning" OR "deep learning"\').',
                    },
                    "max_results": {
                        "type": "integer",
                        "description": f"Maximum results to return (default: 10, max: {MAX_SEARCH_RESULTS}).",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Start date filter (YYYY-MM-DD format).",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "End date filter (YYYY-MM-DD format).",
                    },
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "arXiv categories to filter by (e.g., ['cs.AI', 'cs.LG']).",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["relevance", "date"],
                        "description": "Sort by 'relevance' (default) or 'date' (newest first).",
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle tool calls."""
    if not arguments:
        raise ValueError("Missing arguments")

    if name == "search_papers":
        return await _handle_search(arguments)

    if "arxiv_id" not in arguments:
        raise ValueError("Missing required argument: arxiv_id")

    arxiv_id = arguments["arxiv_id"]

    try:
        if name == "get_paper_prompt":
            await mcp_log("info", f"Processing arXiv paper: {arxiv_id}")
            prompt = process_latex_source(arxiv_id)
            result = prompt + LATEX_RENDER_INSTRUCTIONS
            await mcp_log("info", f"Successfully processed arXiv paper: {arxiv_id}")

        elif name == "get_paper_abstract":
            await mcp_log("info", f"Getting abstract for arXiv paper: {arxiv_id}")
            result = process_latex_source(arxiv_id, abstract_only=True)
            await mcp_log("info", f"Successfully got abstract for: {arxiv_id}")

        elif name == "list_paper_sections":
            await mcp_log("info", f"Listing sections for arXiv paper: {arxiv_id}")
            text = process_latex_source(arxiv_id)
            sections = list_sections(text)
            result = "\n".join(sections)
            await mcp_log("info", f"Successfully listed sections for: {arxiv_id}")

        elif name == "get_paper_section":
            if "section_path" not in arguments:
                raise ValueError("Missing required argument: section_path")
            section_path = arguments["section_path"]
            await mcp_log(
                "info", f"Getting section '{section_path}' for arXiv paper: {arxiv_id}"
            )
            text = process_latex_source(arxiv_id)
            result = extract_section(text, section_path)
            if result is None:
                result = f"Section '{section_path}' not found. Use list_paper_sections to see available sections."
            else:
                result = result + LATEX_RENDER_INSTRUCTIONS
            await mcp_log("info", f"Successfully got section for: {arxiv_id}")

        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=result)]

    except Exception as exc:
        error_msg = f"Error processing arXiv paper {arxiv_id}: {str(exc)}"
        await mcp_log("error", error_msg)
        return [types.TextContent(type="text", text=error_msg)]


def _validate_categories(categories: List[str]) -> bool:
    for category in categories:
        prefix = category.split(".")[0] if "." in category else category
        if prefix not in VALID_CATEGORIES:
            return False
    return True


async def _rate_limited_get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """GET with arXiv's required 3s minimum interval between requests."""
    global _last_request_time
    async with _request_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        _last_request_time = time.monotonic()

    for attempt in range(2):
        try:
            response = await client.get(url, headers=ARXIV_HEADERS)
            if response.status_code in (429, 503):
                raise RuntimeError(
                    f"arXiv is rate limiting this IP (HTTP {response.status_code}). Wait 60s before retrying."
                )
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            if attempt == 0:
                logger.warning("arXiv request timed out, retrying once")
                await asyncio.sleep(5.0)
            else:
                raise
    raise RuntimeError("arXiv request timed out after retry")


def _parse_arxiv_atom(xml_text: str) -> List[Dict[str, Any]]:
    """Parse arXiv Atom XML feed into paper dicts."""
    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"Failed to parse arXiv API response: {e}")

    for entry in root.findall("atom:entry", ARXIV_NS):
        id_elem = entry.find("atom:id", ARXIV_NS)
        if id_elem is None or not id_elem.text:
            continue
        paper_id = id_elem.text.split("/abs/")[-1]
        short_id = paper_id.split("v")[0] if "v" in paper_id else paper_id

        title_elem = entry.find("atom:title", ARXIV_NS)
        title = title_elem.text.strip().replace("\n", " ") if title_elem is not None and title_elem.text else ""

        authors = [
            name_elem.text
            for author in entry.findall("atom:author", ARXIV_NS)
            if (name_elem := author.find("atom:name", ARXIV_NS)) is not None and name_elem.text
        ]

        summary_elem = entry.find("atom:summary", ARXIV_NS)
        abstract = summary_elem.text.strip().replace("\n", " ") if summary_elem is not None and summary_elem.text else ""

        categories: List[str] = []
        for cat in entry.findall("arxiv:primary_category", ARXIV_NS):
            if term := cat.get("term"):
                categories.append(term)
        for cat in entry.findall("atom:category", ARXIV_NS):
            if (term := cat.get("term")) and term not in categories:
                categories.append(term)

        published_elem = entry.find("atom:published", ARXIV_NS)
        published = published_elem.text if published_elem is not None and published_elem.text else ""

        pdf_url = next(
            (link.get("href") for link in entry.findall("atom:link", ARXIV_NS) if link.get("title") == "pdf"),
            f"https://arxiv.org/pdf/{paper_id}",
        )

        results.append({
            "id": short_id,
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "categories": categories,
            "published": published,
            "url": pdf_url,
            "resource_uri": f"arxiv://{short_id}",
        })

    return results


async def _raw_arxiv_search(
    query: str,
    max_results: int,
    sort_by: str = "relevance",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    categories: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Search arXiv via raw HTTP to preserve '+TO+' in date filter (arxiv package encodes it as %2B)."""
    query_parts = []
    if query.strip():
        query_parts.append(f"({query})")
    if categories:
        query_parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
    if date_from or date_to:
        start = dateutil_parser.parse(date_from).strftime("%Y%m%d0000") if date_from else "199107010000"
        end = dateutil_parser.parse(date_to).strftime("%Y%m%d2359") if date_to else datetime.now().strftime("%Y%m%d2359")
        query_parts.append(f"submittedDate:[{start}+TO+{end}]")

    if not query_parts:
        raise ValueError("No search criteria provided")

    final_query = " AND ".join(query_parts)
    sort_map = {"relevance": "relevance", "date": "submittedDate"}
    encoded = final_query.replace(" AND ", "+AND+").replace(" OR ", "+OR+").replace(" ", "+")
    url = f"{ARXIV_API_URL}?search_query={encoded}&max_results={max_results}&sortBy={sort_map.get(sort_by, 'relevance')}&sortOrder=descending"
    logger.debug(f"Raw arXiv API URL: {url}")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await _rate_limited_get(client, url)
    return _parse_arxiv_atom(response.text)


async def _handle_search(arguments: Dict[str, Any]) -> list[types.TextContent]:
    try:
        max_results = min(int(arguments.get("max_results", 10)), MAX_SEARCH_RESULTS)
        base_query = arguments.get("query", "").strip()
        categories: Optional[List[str]] = arguments.get("categories")
        date_from_arg: Optional[str] = arguments.get("date_from")
        date_to_arg: Optional[str] = arguments.get("date_to")
        sort_by_arg: str = arguments.get("sort_by", "relevance")

        if not base_query:
            return [types.TextContent(type="text", text="Error: No search query provided")]

        if categories and not _validate_categories(categories):
            return [types.TextContent(type="text", text="Error: Invalid category. Check arXiv category names.")]

        # Use raw HTTP when date filtering is requested: the arxiv package URL-encodes
        # '+TO+' as '%2B', which breaks the submittedDate range syntax server-side.
        if date_from_arg or date_to_arg:
            try:
                results = await _raw_arxiv_search(
                    query=base_query,
                    max_results=max_results,
                    sort_by=sort_by_arg,
                    date_from=date_from_arg,
                    date_to=date_to_arg,
                    categories=categories,
                )
            except ValueError as e:
                return [types.TextContent(type="text", text=f"Error: {e}")]
            except httpx.HTTPStatusError as e:
                return [types.TextContent(type="text", text=f"Error: arXiv HTTP error - {e}")]
        else:
            query_parts = [f"({base_query})"]
            if categories:
                query_parts.append("(" + " OR ".join(f"cat:{c}" for c in categories) + ")")
            final_query = " ".join(query_parts)

            sort_criterion = (
                arxiv.SortCriterion.SubmittedDate
                if sort_by_arg == "date"
                else arxiv.SortCriterion.Relevance
            )
            search = arxiv.Search(query=final_query, max_results=max_results, sort_by=sort_criterion)

            global _last_request_time
            elapsed = time.monotonic() - _last_request_time
            if elapsed < _MIN_REQUEST_INTERVAL:
                await asyncio.sleep(_MIN_REQUEST_INTERVAL - elapsed)

            client = arxiv.Client()
            results = []
            try:
                for paper in client.results(search):
                    if len(results) >= max_results:
                        break
                    results.append({
                        "id": paper.get_short_id(),
                        "title": paper.title,
                        "authors": [a.name for a in paper.authors],
                        "abstract": paper.summary,
                        "categories": paper.categories,
                        "published": paper.published.isoformat(),
                        "url": paper.pdf_url,
                        "resource_uri": f"arxiv://{paper.get_short_id()}",
                    })
            except arxiv.ArxivError as e:
                if any(code in str(e) for code in ("429", "503")):
                    raise RuntimeError("arXiv is rate limiting this IP. Wait 60s before retrying.")
                raise
            finally:
                _last_request_time = time.monotonic()

        logger.info(f"Search returned {len(results)} results")
        return [types.TextContent(
            type="text",
            text=json.dumps({"total_results": len(results), "papers": results}, indent=2),
        )]

    except RuntimeError as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]
    except arxiv.ArxivError as e:
        return [types.TextContent(type="text", text=f"Error: ArXiv API error - {e}")]
    except Exception as e:
        logger.error(f"Search error: {e}")
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def main() -> None:
    """Main entry point for the server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="arxiv-latex-mcp",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
