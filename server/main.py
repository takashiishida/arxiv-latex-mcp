#!/usr/bin/env python3

import asyncio

from arxiv_latex_mcp.server import main


if __name__ == "__main__":
    asyncio.run(main())
