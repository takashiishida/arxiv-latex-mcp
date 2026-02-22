#!/usr/bin/env python3
"""
ArXiv LaTeX MCP Server

This server provides tools to fetch and process arXiv papers' LaTeX source code
for better mathematical expression interpretation.
"""

import asyncio
import logging
from typing import Any

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.stdio import stdio_server

from arxiv_to_prompt import process_latex_source, list_sections, extract_section

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("arxiv-latex-mcp")

# Create server instance
server = Server("arxiv-latex-mcp")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="get_paper_prompt",
            description="Get a flattened LaTeX code of a paper from arXiv ID for precise interpretation of mathematical expressions",
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
            description="Get just the abstract of an arXiv paper (faster and cheaper than fetching the full paper)",
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
            description="List section headings of an arXiv paper to see its structure",
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
            description="Get a specific section of an arXiv paper by section path (use list_paper_sections first to find available sections)",
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
    ]


LATEX_RENDER_INSTRUCTIONS = """

IMPORTANT INSTRUCTIONS FOR RENDERING:
When discussing this paper, please use dollar sign notation ($...$) for inline equations and double dollar signs ($$...$$) for display equations when providing responses that include LaTeX mathematical expressions.
"""


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
            logger.info(f"Processing arXiv paper: {arxiv_id}")
            prompt = process_latex_source(arxiv_id)
            result = prompt + LATEX_RENDER_INSTRUCTIONS
            logger.info(f"Successfully processed arXiv paper: {arxiv_id}")

        elif name == "get_paper_abstract":
            logger.info(f"Getting abstract for arXiv paper: {arxiv_id}")
            result = process_latex_source(arxiv_id, abstract_only=True)
            logger.info(f"Successfully got abstract for: {arxiv_id}")

        elif name == "list_paper_sections":
            logger.info(f"Listing sections for arXiv paper: {arxiv_id}")
            text = process_latex_source(arxiv_id)
            sections = list_sections(text)
            result = "\n".join(sections)
            logger.info(f"Successfully listed sections for: {arxiv_id}")

        elif name == "get_paper_section":
            if "section_path" not in arguments:
                raise ValueError("Missing required argument: section_path")
            section_path = arguments["section_path"]
            logger.info(f"Getting section '{section_path}' for arXiv paper: {arxiv_id}")
            text = process_latex_source(arxiv_id)
            result = extract_section(text, section_path)
            if result is None:
                result = f"Section '{section_path}' not found. Use list_paper_sections to see available sections."
            else:
                result = result + LATEX_RENDER_INSTRUCTIONS
            logger.info(f"Successfully got section for: {arxiv_id}")

        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=result)]

    except Exception as e:
        error_msg = f"Error processing arXiv paper {arxiv_id}: {str(e)}"
        logger.error(error_msg)

        return [types.TextContent(type="text", text=error_msg)]


async def main():
    """Main entry point for the server."""
    # Run the server using stdio transport
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="arxiv-latex-mcp",
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
