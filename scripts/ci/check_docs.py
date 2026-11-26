"""Validate living Markdown docs; optionally render every Mermaid block offline."""

import argparse
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
MERMAID_IMAGE = (
    "ghcr.io/mermaid-js/mermaid-cli/mermaid-cli:11.12.0@"
    "sha256:bad64c9d9ad917c8dfbe9d9e9c162b96f6615ff019b37058638d16eb27ce7783"
)
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
LINK = re.compile(r"\[[^\]\n]+\]\((<[^>\n]+>|[^\s()]+)(?:\s+[^)]*)?\)")
INLINE_CODE = re.compile(r"(`+).*?\1")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")


@dataclass(frozen=True)
class Diagram:
    path: str
    line: int
    section: str
    code: str


def documentation(root):
    yield root / "README.md"
    yield from sorted((root / "docs").rglob("*.md"))


def check(root):
    errors, diagrams = [], []
    for file in documentation(root):
        name = file.relative_to(root).as_posix()
        opened = None
        section = name
        code = []
        for lineno, line in enumerate(file.read_text(encoding="utf-8-sig").splitlines(), 1):
            fence = FENCE.fullmatch(line)
            if opened is not None:
                marker, language, start = opened
                if (
                    fence and fence[1].startswith(marker) and not fence[2].strip()
                ):
                    if language == "mermaid":
                        if not any(item.strip() for item in code):
                            errors.append(f"{name}:{start}: empty Mermaid block")
                        else:
                            diagrams.append(Diagram(name, start, section, "\n".join(code)))
                    code.clear()
                    opened = None
                elif language == "mermaid":
                    code.append(line)
                continue
            if fence:
                info = fence[2].strip().split()
                opened = (fence[1], info[0].lower() if info else "", lineno)
                continue
            if heading := HEADING.match(line):
                section = heading[1]
            for link in LINK.finditer(INLINE_CODE.sub("", line)):
                target = urlsplit(link[1].strip("<>"))
                if target.scheme or target.netloc or not target.path:
                    continue
                path = (file.parent / unquote(target.path)).resolve()
                if not path.is_relative_to(root) or not path.exists():
                    errors.append(f"{name}:{lineno}: missing or out-of-repo link: {link[1]}")
        if opened is not None:
            errors.append(f"{name}:{opened[2]}: unclosed code fence")
    if not diagrams:
        errors.append("No Mermaid diagrams found in README.md or docs/.")
    if errors:
        raise ValueError("\n".join(errors))
    return diagrams


def render(diagrams, output):
    if output.exists():
        raise FileExistsError(f"Render output must not exist yet: {output}")
    with tempfile.TemporaryDirectory(prefix="roc-docs-") as temporary:
        staging = Path(temporary).resolve()
        # The unprivileged image user must write only within this disposable directory.
        staging.chmod(0o777)
        content = ["# Documentation diagram gallery\n"]
        for idx, diagram in enumerate(diagrams, 1):
            content.extend((
                f"## {idx}. {diagram.section}",
                f"\nSource: `{diagram.path}:{diagram.line}`\n",
                f"```mermaid\n{diagram.code}\n```\n",
            ))
        (staging / "source.md").write_text("\n".join(content), encoding="utf-8")
        print("Rendering diagrams in the order listed in source.md:", flush=True)
        for idx, diagram in enumerate(diagrams, 1):
            print(f"  {idx}: {diagram.path}:{diagram.line} {diagram.section}", flush=True)
        subprocess.run([
            "docker", "run", "--rm", "--pull=missing", "--network", "none", "--shm-size=256m",
            "--mount", f"type=bind,source={staging},target=/data", MERMAID_IMAGE,
            "-i", "/data/source.md", "-o", "/data/index.md",
        ], check=True, timeout=240)
        svg_files = tuple(sorted(staging.glob("*.svg")))
        if len(svg_files) != len(diagrams) or not (staging / "index.md").is_file():
            raise ValueError(f"Expected {len(diagrams)} rendered SVG files and an index")
        output.mkdir(parents=True, exist_ok=False)
        for file in (staging / "index.md", *svg_files):
            shutil.copyfile(file, output / file.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--render", type=Path, help="new directory for Mermaid SVGs and index")
    options = parser.parse_args()
    try:
        diagrams = check(options.root.resolve())
        print(f"Local links and code fences valid; {len(diagrams)} Mermaid diagrams.", flush=True)
        if options.render:
            render(diagrams, options.render.resolve())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Documentation check failed: {exc}\n")


if __name__ == "__main__":
    main()
