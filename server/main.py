#!/usr/bin/env python3
"""Bootstrap the desktop extension without reusing unverified dependencies."""

import hashlib
import importlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def prepare_dependencies(bundle_root):
    """Install wheels once per requirements/runtime combination, then reuse them.

    A private, complete installation takes precedence over global packages and
    the legacy server/lib folder. Failed installs never become usable caches.
    """
    bundle_root = Path(bundle_root)
    requirements = bundle_root / "requirements.txt"
    runtime = json.dumps([
        sys.implementation.name, sys.implementation.cache_tag,
        list(sys.version_info[:3]), sys.platform, platform.machine(),
    ]).encode()
    fingerprint = hashlib.sha256(requirements.read_bytes() + b"\0" + runtime).hexdigest()
    cache_root = bundle_root / "server" / ".deps"
    target = cache_root / fingerprint
    marker = target / ".complete"
    if marker.is_file() and marker.read_text() == fingerprint:
        return target

    cache_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="install-", dir=cache_root))
    try:
        subprocess.check_call([
            sys.executable, "-I", "-m", "pip", "install",
            "--disable-pip-version-check", "--only-binary=:all:",
            "--target", str(staging), "--no-user",
            "-r", str(requirements),
        ], stdout=sys.stderr)  # stdout is reserved for the MCP protocol.
        (staging / ".complete").write_text(fingerprint)
        try:
            staging.rename(target)
        except OSError:
            # Another simultaneous launch may have completed the same install.
            if not marker.is_file() or marker.read_text() != fingerprint:
                raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def run():
    bundle_root = Path(__file__).resolve().parent.parent
    dependencies = prepare_dependencies(bundle_root)
    sys.path[:0] = [str(dependencies), str(bundle_root)]
    importlib.invalidate_caches()

    import asyncio
    from arxiv_latex_mcp.server import main

    asyncio.run(main())


if __name__ == "__main__":
    run()
