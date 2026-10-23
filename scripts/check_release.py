"""Check release versions without importing application code (Python 3.11+)."""

import argparse
import ast
import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent


def check_release(root=ROOT, tag=None):
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((root / "uv.lock").read_text())
    versions = {
        "pyproject.toml": project["version"],
        "manifest.json": json.loads((root / "manifest.json").read_text())["version"],
        "uv.lock": next(p["version"] for p in lock["package"] if p["name"] == project["name"]),
    }
    module = ast.parse((root / "arxiv_latex_mcp" / "__init__.py").read_text())
    versions["__init__.py"] = next(
        ast.literal_eval(node.value) for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    )
    version = project["version"]
    if any(value != version for value in versions.values()):
        raise ValueError(f"Release versions disagree: {versions}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} must match v{version}")
    requirements = [
        line.strip() for line in (root / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if sorted(requirements) != sorted(project["dependencies"]):
        raise ValueError("requirements.txt and pyproject.toml dependencies disagree")
    if not (root / "docs" / "releases" / f"v{version}.md").is_file():
        raise ValueError(f"Missing release notes for v{version}")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    args = parser.parse_args()
    print(f"Release metadata verified: v{check_release(tag=args.tag)}")
