import asyncio

from arxiv_latex_mcp.server import main as async_main


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
