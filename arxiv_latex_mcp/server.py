"""
ArXiv LaTeX MCP Server

This server provides tools to fetch and process arXiv papers' LaTeX source code
for better mathematical expression interpretation.
"""

import logging
from typing import Any

import httpx
from arxiv_to_prompt import (
    extract_arxiv_id,
    extract_section,
    list_sections,
    process_latex_source,
)
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

# Sent to the client at initialization (MCP "instructions"); clients such as Claude Code show it to
# the model at session start, so the agent knows how to use the tools before it calls them.
SERVER_INSTRUCTIONS = """Read arXiv papers from their LaTeX source, so equations, theorems and notation are exact (better than PDF text or web-page summaries).
- Whole paper: get_paper_prompt (the default; one call returns the full flattened LaTeX).
- Long paper: list_paper_sections (top-level section titles only), then get_paper_section with section_path set to one of those titles copied verbatim, including any LaTeX. A section includes its subsections; for one subsection use "Section > Subsection" with exact titles. Numbers such as "2.1" do not work.
- Quick preview only: get_paper_abstract.
- IDs: "2403.12345" is the latest version, "2403.12345v2" pins a version, and old-style IDs such as "hep-th/9711200" work.
- An error message says why a paper failed: no LaTeX source (PDF-only paper), no abstract block, a bad ID, or arXiv unreachable. For PDF-only papers read https://arxiv.org/pdf/<id> and say that the equations came from the PDF."""

LATEX_RENDER_INSTRUCTIONS = """

IMPORTANT INSTRUCTIONS FOR RENDERING:
When discussing this paper, please use dollar sign notation ($...$) for inline equations and double dollar signs ($$...$$) for display equations when providing responses that include LaTeX mathematical expressions.
"""


class SourceUnavailable(Exception):
    """No usable LaTeX could be produced; the message says why and what to do instead."""


def _source_problem(arxiv_id: str) -> str | None:
    """Why arXiv cannot provide a LaTeX source, or None if its format page lists one.

    This is the page arxiv-to-prompt checks before downloading.
    """
    paper = extract_arxiv_id(arxiv_id)
    pdf = f"https://arxiv.org/pdf/{paper}"
    try:
        page = httpx.get(
            f"https://arxiv.org/format/{paper}",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=True,
        )
    except httpx.HTTPError:
        return f"arXiv could not be reached. Try again later, or read the PDF: {pdf}"
    if page.status_code in (400, 404):
        return f"'{paper}' is not a valid arXiv ID. Check the ID."
    if not page.is_success:
        return f"arXiv answered HTTP {page.status_code}. Try again later, or read the PDF: {pdf}"
    if "Download source" not in page.text:
        return f"arXiv has no LaTeX source for this paper (it was submitted as a PDF only). Read the PDF instead: {pdf}"
    return None


def fetch_source(arxiv_id: str, **kwargs: Any) -> str:
    """Like process_latex_source(), but raises SourceUnavailable with the reason instead of returning None."""
    result = process_latex_source(arxiv_id, **kwargs)
    if result is not None:
        return result
    problem = _source_problem(arxiv_id)
    if problem:
        raise SourceUnavailable(problem)
    paper = extract_arxiv_id(arxiv_id)
    if kwargs.get("abstract_only") and process_latex_source(arxiv_id) is not None:
        raise SourceUnavailable(
            "the paper's LaTeX source has no abstract block. Use get_paper_prompt, get_paper_section with a "
            f"title from list_paper_sections, or the abstract page https://arxiv.org/abs/{paper}"
        )
    raise SourceUnavailable(
        "the LaTeX source could not be downloaded or processed. "
        f"Try again later, or read the PDF: https://arxiv.org/pdf/{paper}"
    )


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
            description="List section headings of an arXiv paper. Useful when the full paper is too long for context and you need to identify which sections to fetch individually. Returns the top-level section titles; pass one verbatim to get_paper_section.",
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
            description="Get a specific section of an arXiv paper by its title. Use when the full paper is too long for context or the user wants to focus on a particular section. Use list_paper_sections first to get the titles.",
            inputSchema={
                "type": "object",
                "properties": {
                    "arxiv_id": {
                        "type": "string",
                        "description": "The arXiv ID of the paper (e.g., '2403.12345')",
                    },
                    "section_path": {
                        "type": "string",
                        "description": "A section title copied verbatim from list_paper_sections (e.g., 'Introduction'), including any LaTeX in it. For a subsection use 'Section > Subsection' with exact titles. Numbers such as '2.1' are not supported.",
                    },
                },
                "required": ["arxiv_id", "section_path"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle tool calls."""
    if not arguments or "arxiv_id" not in arguments:
        raise ValueError("Missing required argument: arxiv_id")

    arxiv_id = arguments["arxiv_id"]

    try:
        if name == "get_paper_prompt":
            await mcp_log("info", f"Processing arXiv paper: {arxiv_id}")
            prompt = fetch_source(arxiv_id)
            result = prompt + LATEX_RENDER_INSTRUCTIONS
            await mcp_log("info", f"Successfully processed arXiv paper: {arxiv_id}")

        elif name == "get_paper_abstract":
            await mcp_log("info", f"Getting abstract for arXiv paper: {arxiv_id}")
            result = fetch_source(arxiv_id, abstract_only=True)
            await mcp_log("info", f"Successfully got abstract for: {arxiv_id}")

        elif name == "list_paper_sections":
            await mcp_log("info", f"Listing sections for arXiv paper: {arxiv_id}")
            text = fetch_source(arxiv_id)
            sections = list_sections(text)
            result = "\n".join(sections) if sections else (
                "No \\section headings found in this paper. Read it whole with get_paper_prompt."
            )
            await mcp_log("info", f"Successfully listed sections for: {arxiv_id}")

        elif name == "get_paper_section":
            if "section_path" not in arguments:
                raise ValueError("Missing required argument: section_path")
            section_path = arguments["section_path"]
            await mcp_log(
                "info", f"Getting section '{section_path}' for arXiv paper: {arxiv_id}"
            )
            text = fetch_source(arxiv_id)
            result = extract_section(text, section_path)
            if result is None:
                available = "; ".join(list_sections(text)) or "(none)"
                result = (
                    f"Section '{section_path}' not found. Use an exact section title, not a number. "
                    f"Top-level sections: {available}. For a subsection use 'Section > Subsection'."
                )
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


async def main() -> None:
    """Main entry point for the server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="arxiv-latex-mcp",
                server_version=__version__,
                instructions=SERVER_INSTRUCTIONS,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )
