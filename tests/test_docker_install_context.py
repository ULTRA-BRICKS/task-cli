"""The Docker integration image must ship every file install.sh sources.

test/docker/Dockerfile copies a hand-picked subset of the repo into the image and then runs
install.sh the real way. When install.sh started sourcing scripts/lib/install_laya.sh
(task-cli#208), the Dockerfile kept copying only bin/, tasklib/ and a few root files, so the
image build died with "install_laya.sh: No such file or directory" and the deterministic
test-cli job went red on main. This pins the two files together.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "test" / "docker" / "Dockerfile"
INSTALL_SH = ROOT / "install.sh"


def _copied_sources() -> list[str]:
    sources: list[str] = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts or parts[0] != "COPY":
            continue
        args = [p for p in parts[1:] if not p.startswith("--")]
        sources.extend(a.rstrip("/") for a in args[:-1])
    return sources


def _sourced_repo_paths() -> list[str]:
    text = INSTALL_SH.read_text(encoding="utf-8")
    return re.findall(r'^\s*(?:source|\.)\s+"\$SRC/([^"]+)"', text, flags=re.MULTILINE)


def test_install_sh_sources_something_from_the_repo():
    assert _sourced_repo_paths(), "expected install.sh to source at least one repo helper"


def test_every_file_install_sh_sources_is_copied_into_the_image():
    copied = _copied_sources()
    missing = [
        path
        for path in _sourced_repo_paths()
        if not any(path == src or path.startswith(src + "/") for src in copied)
    ]
    assert not missing, f"Dockerfile does not COPY files install.sh sources: {missing}"
