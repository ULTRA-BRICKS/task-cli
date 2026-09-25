from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

from tasklib import stdio


def _stream(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="\n")


def test_non_utf8_streams_are_reencoded(monkeypatch):
    out, err = _stream("cp1251"), _stream("ascii")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    stdio.ensure_utf8_stdio()

    assert out.encoding == "utf-8" and err.encoding == "utf-8"
    print("task → ✗", file=out)  # the glyphs that used to raise UnicodeEncodeError
    out.flush()
    assert out.buffer.getvalue().decode("utf-8") == "task → ✗\n"


def test_utf8_and_unreconfigurable_streams_are_left_alone(monkeypatch):
    utf8 = _stream("utf-8")
    monkeypatch.setattr(sys, "stdout", utf8)
    monkeypatch.setattr(sys, "stderr", io.StringIO())  # no reconfigure(): a test double

    stdio.ensure_utf8_stdio()

    assert sys.stdout is utf8 and utf8.encoding == "utf-8"


def test_task_new_help_survives_a_legacy_codepage_pipe():
    # A cp1251 pipe is exactly what a mintty/Cygwin terminal hands a native Windows python.
    env = {**os.environ, "PYTHONIOENCODING": "cp1251", "PYTHONUTF8": "0"}
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from tasklib.cli import main; sys.exit(main(['new', '--help']))"],
        capture_output=True,
        env=env,
        cwd=Path(__file__).resolve().parent.parent,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "→".encode("utf-8") in result.stdout
