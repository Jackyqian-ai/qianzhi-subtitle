#!/usr/bin/env python3
"""Self-bootstrapping entry point for the subtitle aligner.

Run it with any Python 3.8+ on any platform:

    python3 scripts/align.py raw_wordlevel.srt script.txt out.srt

If `jieba` and `pypinyin` are missing from the interpreter you launched, this
creates a private virtual environment beside the package (`.venv/`), installs
them there once, and re-runs itself inside it. Nothing is installed into the
system Python and no path is hard-coded, so the package works as-is after
being copied to another machine.

Set SUBTITLE_ALIGN_NO_BOOTSTRAP=1 to disable that and fail loudly instead.
"""
import os
import subprocess
import sys
from pathlib import Path

DEPS = ['jieba', 'pypinyin']
PKG_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = PKG_ROOT / '.venv'
TARGET = Path(__file__).resolve().parent / 'align_subs.py'


def venv_python(venv: Path) -> Path:
    if os.name == 'nt':
        return venv / 'Scripts' / 'python.exe'
    return venv / 'bin' / 'python3'


def deps_present() -> bool:
    from importlib.util import find_spec
    return all(find_spec(mod) is not None for mod in DEPS)


def bootstrap() -> Path:
    """Create (once) and return the interpreter that has the dependencies."""
    py = venv_python(VENV_DIR)
    if not py.exists():
        print(f"[align] creating virtual environment at {VENV_DIR}", file=sys.stderr)
        try:
            subprocess.run([sys.executable, '-m', 'venv', str(VENV_DIR)], check=True)
        except subprocess.CalledProcessError:
            sys.exit(
                "[align] could not create a virtual environment.\n"
                "        On Debian/Ubuntu install it first: apt install python3-venv"
            )
    try:
        subprocess.run([str(py), '-c', 'import jieba, pypinyin'],
                       check=True, capture_output=True)
    except subprocess.CalledProcessError:
        print(f"[align] installing {', '.join(DEPS)} (one time)", file=sys.stderr)
        subprocess.run([str(py), '-m', 'pip', 'install', '--quiet',
                        '--disable-pip-version-check', *DEPS], check=True)
    return py


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(
            "usage: align.py <wordlevel.srt> <script.txt> <out.srt>\n\n"
            "  wordlevel.srt  ASR output transcribed WITH --word-timestamps\n"
            "  script.txt     the verbatim script, plain text\n"
            "  out.srt        subtitle file to write\n"
        )
    if deps_present():
        os.execv(sys.executable, [sys.executable, str(TARGET), *sys.argv[1:]])
    if os.environ.get('SUBTITLE_ALIGN_NO_BOOTSTRAP'):
        sys.exit(f"[align] missing dependencies: {', '.join(DEPS)}")
    py = bootstrap()
    raise SystemExit(subprocess.run([str(py), str(TARGET), *sys.argv[1:]]).returncode)


if __name__ == '__main__':
    main()
