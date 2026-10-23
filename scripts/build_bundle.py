"""Build a reproducible MCPB ZIP using only reviewed runtime files."""

import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent


def build_bundle(output, root=ROOT):
    files = [
        "manifest.json", "requirements.txt", "README.md",
        "LICENSE", "LICENSE-APACHE", "THIRD_PARTY_NOTICES", "server/main.py",
    ]
    files += [p.relative_to(root).as_posix() for p in (root / "arxiv_latex_mcp").glob("*.py")]
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            source = root / name
            if source.is_symlink():
                raise ValueError(f"Refusing to bundle symlink: {name}")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(ROOT / "bundle" / "arxiv-latex-mcp.mcpb"))
    args = parser.parse_args()
    print(build_bundle(args.output))
