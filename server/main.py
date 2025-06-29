#!/usr/bin/env python3
"""
ArXiv LaTeX MCP Server

This server provides tools to fetch and process arXiv papers' LaTeX source code
for better mathematical expression interpretation.
"""

import asyncio
import logging
import sys
from typing import Any, Sequence

from mcp.server import Server, NotificationOptions
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server.stdio import stdio_server

from arxiv_to_prompt import process_latex_source

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
        )
    ]


@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict[str, Any] | None
) -> list[types.TextContent]:
    """Handle tool calls."""
    if name != "get_paper_prompt":
        raise ValueError(f"Unknown tool: {name}")

    if not arguments or "arxiv_id" not in arguments:
        raise ValueError("Missing required argument: arxiv_id")

    arxiv_id = arguments["arxiv_id"]

    try:
        logger.info(f"Processing arXiv paper: {arxiv_id}")

        # Process the LaTeX source using arxiv-to-prompt
        prompt = process_latex_source(arxiv_id)

        # Append instructions for rendering LaTeX
        instructions = """

IMPORTANT INSTRUCTIONS FOR RENDERING:
When discussing this paper, please use dollar sign notation ($...$) for inline equations and double dollar signs ($$...$$) for display equations when providing responses that include LaTeX mathematical expressions.
"""

        result = prompt + instructions

        logger.info(f"Successfully processed arXiv paper: {arxiv_id}")

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
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
