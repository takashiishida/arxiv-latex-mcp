#!/usr/bin/env python3

import importlib
import os
import subprocess
import sys

_server_dir = os.path.dirname(os.path.abspath(__file__))
_bundle_root = os.path.dirname(_server_dir)
_lib_dir = os.path.join(_server_dir, "lib")

# Install dependencies into server/lib on first run so that compiled
# extensions match the user's Python version and platform.
# Skip if deps are already available (e.g. installed via pip/uv).
def _needs_runtime_install():
    if os.path.isdir(_lib_dir):
        return False
    try:
        import mcp  # noqa: F401
        return False
    except ImportError:
        return True

if _needs_runtime_install():
    subprocess.check_call(
        [
            sys.executable, "-m", "pip", "install",
            "--target", _lib_dir,
            "--no-user",
            "-r", os.path.join(_bundle_root, "requirements.txt"),
        ],
        stdout=sys.stderr,  # keep stdout clean for MCP protocol
    )
    # Invalidate caches so Python discovers the newly-created lib dir.
    importlib.invalidate_caches()

# Add bundle root (for arxiv_latex_mcp package) and lib dir to sys.path.
for _path in [_bundle_root, _lib_dir]:
    if _path not in sys.path:
        sys.path.insert(0, _path)

import asyncio

from arxiv_latex_mcp.server import main


if __name__ == "__main__":
    asyncio.run(main())
