"""Assemble the deployment-free, reproducible demo release assets."""

import argparse
import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

VERSION = re.compile(r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}")
IMAGE = re.compile(r"(ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+)@sha256:[a-f0-9]{64}")
ROOT = Path(__file__).resolve().parents[2]


def checksum(data, name):
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


def env_example(image):
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "DEMO_PASSWORD"):
        if re.findall(rf"^{name}=(.*)$", template, re.MULTILINE) != [""]:
            raise ValueError(f".env.example must contain one blank {name} assignment")
    content, count = re.subn(r"^APP_IMAGE=.*$", f"APP_IMAGE={image}", template, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(".env.example must contain exactly one APP_IMAGE assignment")
    return content.encode("utf-8")


def package(version, image, output):
    match = IMAGE.fullmatch(image)
    if not match or len(version) > 128 or not VERSION.fullmatch(version):
        raise ValueError("expected vMAJOR.MINOR.PATCH and a full GHCR sha256 image reference")
    versioned_image = f"{match[1]}:{version}"
    files = {
        name: (ROOT / name).read_bytes()
        for name in ("compose.yaml", "docs/operations/containers.md", "docs/operations/ci.md",
                     "docs/operations/demo.md")
    }
    files.update({
        ".env.example": env_example(image),
        "image-references.txt": (
            f"VERSION={version}\nPLATFORM=linux/amd64\nIMAGE_TAG={versioned_image}\nAPP_IMAGE={image}\n"
        ).encode(),
        "README.md": (
            f"# 学术成果智能归集与认领系统 · {version}\n\n"
            "这是仅含合成数据的本地演示包（Linux amd64 容器），不含源码、私密配置或部署凭据。\n\n"
            "从发布页获取外部 `.sha256` 文件，在解压前核对 ZIP。"
            "运行说明见 [CI 与发布手册](docs/operations/ci.md#运行发布演示包)。"
            "`.env.example` 已将 `APP_IMAGE` 指向不可变摘要。\n"
        ).encode(),
    })
    files["SHA256SUMS"] = "".join(checksum(data, name) for name, data in sorted(files.items())).encode()

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"research-output-claims-demo-{version}.zip"
    checksum_file = archive.with_suffix(".zip.sha256")
    if archive.exists() or checksum_file.exists():
        raise FileExistsError(f"refusing to overwrite release assets in {output}")
    with ZipFile(archive, "x") as bundle:
        for name, data in sorted(files.items()):
            entry = ZipInfo(name)  # Fixed timestamps and modes, independent of checkout times.
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    with checksum_file.open("x", encoding="ascii", newline="\n") as target:
        target.write(checksum(archive.read_bytes(), archive.name))
    print(f"Wrote {archive} and {checksum_file}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--image", required=True, help="ghcr.io/owner/repo@sha256:<64 hex digits>")
    parser.add_argument("--output", required=True, type=Path, help="directory for new release assets")
    args = parser.parse_args()
    try:
        package(args.version, args.image, args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Packaging failed: {exc}\n")


if __name__ == "__main__":
    main()
