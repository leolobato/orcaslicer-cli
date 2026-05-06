"""Extract TabPrint::build() page → optgroup → option layout from Tab.cpp.

Runs at vendor-bump time. Output goes to ``cpp/src/generated/process_pages.json``
which is checked into git so devs and CI don't re-run extraction unless
Tab.cpp itself changed.

Usage:
    python scripts/extract_tab_layout.py            # write generated JSON
    python scripts/extract_tab_layout.py --check    # exit non-zero if stale
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TAB_CPP = REPO_ROOT / "vendor" / "OrcaSlicer" / "src" / "slic3r" / "GUI" / "Tab.cpp"
OUT_PATH = REPO_ROOT / "cpp" / "src" / "generated" / "process_pages.json"

# Regexes target the call shape inside TabPrint::build(). Tab.cpp has
# been stable on these names for years; if upstream restructures the
# file, the call sites are the things to fix in this script.
RE_PAGE     = re.compile(r'add_options_page\s*\(\s*L\("([^"]+)"\)')
RE_OPTGROUP = re.compile(r'new_optgroup\s*\(\s*L\("([^"]+)"\)')
RE_APPEND   = re.compile(r'append_single_option_line\s*\(\s*"([^"]+)"')

# Identifies the start of TabPrint::build()'s body. We scope extraction
# to lines from this signature up to the matching closing brace at
# column 0. The extractor is deliberately strict about the signature
# shape — if it changes upstream we want to know.
RE_BUILD_START = re.compile(r'^void\s+TabPrint::build\s*\(\s*\)')


def _strip_line_comment(line: str) -> str:
    """Drop everything from `//` onwards. We don't strip block comments
    because Tab.cpp doesn't use them inside build(); add support if needed."""
    idx = line.find("//")
    return line if idx == -1 else line[:idx]


def _scope_to_build_function(src: str) -> str:
    """Return only the body of TabPrint::build(), or empty string if absent.

    Brace counting starts at the first `{` after the signature; the
    body ends when the depth returns to 0.
    """
    lines = src.splitlines()
    in_build = False
    depth = 0
    body: list[str] = []
    for line in lines:
        if not in_build:
            if RE_BUILD_START.search(line):
                in_build = True
                # If `{` is on the signature line, count it; otherwise
                # depth stays 0 until we see the opening brace.
                depth = line.count("{") - line.count("}")
                if depth > 0:
                    body.append(line)
            continue
        body.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break
    return "\n".join(body)


def extract_print_layout(src: str) -> list[dict]:
    """Parse a Tab.cpp source string and return the page layout.

    Output: a list of pages. Each page has ``label`` and ``optgroups``.
    Each optgroup has ``label`` and ``options`` (list of option keys in
    Tab.cpp order). Options before any page or optgroup are dropped.
    """
    body = _scope_to_build_function(src)
    pages: list[dict] = []
    cur_page: dict | None = None
    cur_optgroup: dict | None = None
    for raw in body.splitlines():
        line = _strip_line_comment(raw)
        m_page = RE_PAGE.search(line)
        if m_page:
            cur_page = {"label": m_page.group(1), "optgroups": []}
            cur_optgroup = None
            pages.append(cur_page)
            continue
        m_og = RE_OPTGROUP.search(line)
        if m_og and cur_page is not None:
            cur_optgroup = {"label": m_og.group(1), "options": []}
            cur_page["optgroups"].append(cur_optgroup)
            continue
        m_app = RE_APPEND.search(line)
        if m_app and cur_optgroup is not None:
            cur_optgroup["options"].append(m_app.group(1))
            continue
    return pages


def _git_blob_sha(path: Path) -> str:
    """Return the SHA git would record for this file's current content."""
    out = subprocess.run(
        ["git", "hash-object", str(path)],
        capture_output=True, check=True, text=True,
    )
    return out.stdout.strip()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true",
                   help="exit non-zero if generated JSON is stale")
    args = p.parse_args()

    if not TAB_CPP.exists():
        print(f"error: {TAB_CPP} not found", file=sys.stderr)
        return 2

    src = TAB_CPP.read_text(encoding="utf-8")
    layout = extract_print_layout(src)
    if not layout:
        print("error: extraction yielded zero pages — regex needs work",
              file=sys.stderr)
        return 2

    new_doc = {
        "extracted_from_path": str(TAB_CPP.relative_to(REPO_ROOT)),
        "extracted_from_sha":  _git_blob_sha(TAB_CPP),
        "pages": layout,
    }

    if args.check:
        if not OUT_PATH.exists():
            print(f"error: {OUT_PATH} missing; run without --check to generate",
                  file=sys.stderr)
            return 1
        existing = json.loads(OUT_PATH.read_text())
        # Compare on the actual layout + source SHA, ignoring extracted_at-
        # style fields (none today, but future-proof).
        for k in ("extracted_from_sha", "pages"):
            if existing.get(k) != new_doc[k]:
                print(f"error: {OUT_PATH} is stale (key {k!r} differs); "
                      "run scripts/extract_tab_layout.py", file=sys.stderr)
                return 1
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(new_doc, indent=2) + "\n")
    print(f"wrote {OUT_PATH} ({len(layout)} pages, "
          f"{sum(len(p['optgroups']) for p in layout)} optgroups, "
          f"{sum(len(og['options']) for p in layout for og in p['optgroups'])} options)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
