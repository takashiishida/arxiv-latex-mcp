from mcp.server.fastmcp import FastMCP
from arxiv_to_prompt import process_latex_source

# Initialize FastMCP server
mcp = FastMCP("arxiv")


@mcp.tool()
async def get_paper_prompt(arxiv_id: str) -> str:
    """Get a flattened LaTeX code of a paper from arXiv ID.

    Args:
        arxiv_id: The arXiv ID of the paper (e.g., '2403.12345')

    Returns:
        A flattened LaTeX code of the paper that can be used as a prompt for an LLM
    """
    try:
        prompt = process_latex_source(arxiv_id)

        # Append instructions for rendering LaTeX
        instructions = """
        
        IMPORTANT INSTRUCTIONS FOR RENDERING:
        Whenever I ask you a question about this paper, please use dollar sign notation ($...$) for inline equations and double dollar signs ($$...$$) for display equations when providing responses that include LaTeX.
        """

        return prompt + instructions
    except Exception as e:
        return f"Error processing paper: {str(e)}"


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
