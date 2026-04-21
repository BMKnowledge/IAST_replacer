#!/usr/bin/env python3
"""
Replace quotation marks with \\enquote{...} in all .tex files under the project root.

Put this script in the project root and run:

    python fix_quotes.py

Options:
  --dry-run  Preview changes without writing files
  --backup   Save a .bak copy of each changed file

Notes:
  - Existing \\enquote{...} blocks (including nested) are protected and left unchanged.
  - Verbatim environments (\\verb, verbatim, lstlisting, minted, etc.) are left unchanged.
  - Inline TeX comments starting with % are left unchanged.
  - Full-line TeX comments are left unchanged.
  - Single-quote conversion is conservative to reduce false positives.
  - Multiline quotations are not handled by this version.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".idea",
    ".vscode",
}

COMMENT_LINE = re.compile(r"^\s*%")

PATTERNS = [
    # ``LaTeX double quotes''
    re.compile(r"``([^`\n]|`(?!`))*?''"),
    # `LaTeX single quotes'
    re.compile(r"(?<!`)`([^'\n]|'(?!'))*?'"),
    # German low-high quotes  „ (U+201E) ... " (U+201D)
    re.compile(r"\u201e[^\u201d\n]+?\u201d"),
    # Guillemets
    re.compile(r"»[^»«\n]+?«"),
    re.compile(r"«[^»«\n]+?»"),
    # Smart double quotes
    re.compile(r"\u201c[^\u201c\n]+?\u201d"),
    # Smart single quotes
    re.compile(r"\u2018[^\u2018\n]+?\u2019"),
    # Straight double quotes
    re.compile(r'"[^"\n]+?"'),
    # Straight single quotes, conservatively matched to reduce false positives
    re.compile(r"(?<![\w\\])'[^'\n]+?'(?!\w)"),
]

# ---------------------------------------------------------------------------
# Verbatim environment detection
# ---------------------------------------------------------------------------

# \verb|...| or \verb*|...| with any delimiter character
VERB_INLINE = re.compile(r"\\verb\*?(.)(.*?)\1")

# Block verbatim environments that should not be touched.
# NOTE: protect_verbatim_blocks() must be called on the *whole text* before
# line-splitting, because these environments span multiple lines.
VERBATIM_ENV_NAMES = r"(?:verbatim\*?|lstlisting|minted|Verbatim|BVerbatim|LVerbatim|alltt)"
VERBATIM_BLOCK = re.compile(
    r"\\begin\{" + VERBATIM_ENV_NAMES + r"\}.*?\\end\{" + VERBATIM_ENV_NAMES + r"\}",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _make_token(registry: list[tuple[str, str]], value: str) -> str:
    """Store *value* in *registry* and return a collision-safe placeholder."""
    token = f"PROTECTEDBLOCK_{uuid.uuid4().hex}_END"
    registry.append((token, value))
    return token


def _tokenize_spans(
    text: str,
    spans: list[tuple[int, int, str]],
    registry: list[tuple[str, str]],
) -> str:
    """Replace each span in *text* with a token, updating *registry*."""
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end, original in spans:
        parts.append(text[cursor:start])
        parts.append(_make_token(registry, original))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def restore_regions(text: str, registry: list[tuple[str, str]]) -> str:
    for token, original in registry:
        text = text.replace(token, original)
    return text


# ---------------------------------------------------------------------------
# Multi-line protection — must run on the whole text before line splitting
# ---------------------------------------------------------------------------

def protect_verbatim_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Replace every verbatim block environment with a collision-safe token.
    Must be called on the complete document text *before* splitting into lines,
    because verbatim environments span multiple lines.
    """
    registry: list[tuple[str, str]] = []
    spans = [
        (m.start(), m.end(), m.group(0)) for m in VERBATIM_BLOCK.finditer(text)
    ]
    return _tokenize_spans(text, spans, registry), registry


# ---------------------------------------------------------------------------
# Balanced-brace \enquote{...} matching (handles nesting)
# ---------------------------------------------------------------------------

def find_enquote_spans(text: str) -> list[tuple[int, int]]:
    """
    Return (start, end) spans of every top-level \\enquote{...} in *text*,
    correctly handling nested braces.
    """
    spans: list[tuple[int, int]] = []
    search_from = 0
    marker = r"\enquote{"
    marker_len = len(marker)

    while True:
        start = text.find(marker, search_from)
        if start == -1:
            break

        depth = 0
        i = start + marker_len - 1  # position of the opening '{'
        end = -1
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1

        if end == -1:
            # Unmatched brace — skip past this marker and continue
            search_from = start + marker_len
        else:
            spans.append((start, end))
            search_from = end

    return spans


# ---------------------------------------------------------------------------
# Inline protection — safe to run per-line on the code segment
# ---------------------------------------------------------------------------

def protect_regions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """
    Replace inline \\verb commands and \\enquote{...} blocks (including nested)
    with collision-safe tokens so they are not modified by quote conversion.

    Verbatim block environments are handled separately by protect_verbatim_blocks().
    """
    registry: list[tuple[str, str]] = []

    protected_spans: list[tuple[int, int, str]] = []

    for m in VERB_INLINE.finditer(text):
        protected_spans.append((m.start(), m.end(), m.group(0)))

    for start, end in find_enquote_spans(text):
        protected_spans.append((start, end, text[start:end]))

    if not protected_spans:
        return text, registry

    # Sort and remove overlapping spans (keep the first / outermost).
    protected_spans.sort(key=lambda t: t[0])
    merged: list[tuple[int, int, str]] = []
    last_end = -1
    for start, end, original in protected_spans:
        if start < last_end:
            continue
        merged.append((start, end, original))
        last_end = end

    return _tokenize_spans(text, merged, registry), registry


# ---------------------------------------------------------------------------
# Quote conversion
# ---------------------------------------------------------------------------

def convert_match(match: re.Match[str]) -> str:
    text = match.group(0)

    pairs = [
        ("``", "''"),
        ("`", "'"),
        ("\u201e", "\u201d"),   # German „ ... "
        ("»", "«"),
        ("«", "»"),
        ("\u201c", "\u201d"),   # Smart double " ... "
        ("\u2018", "\u2019"),   # Smart single ' ... '
        ('"', '"'),
        ("'", "'"),
    ]

    for left, right in pairs:
        if (
            text.startswith(left)
            and text.endswith(right)
            and len(text) >= len(left) + len(right)
        ):
            inner = text[len(left): len(text) - len(right)]
            return f"\\enquote{{{inner}}}"

    # Should never reach here if PATTERNS and pairs stay in sync.
    logger.warning(
        "convert_match: no matching quote pair found for %r — left unchanged", text
    )
    return text


def process_code_part(code: str) -> str:
    """Apply quote conversion to a single non-comment code segment."""
    code, registry = protect_regions(code)
    for pattern in PATTERNS:
        code = pattern.sub(convert_match, code)
    code = restore_regions(code, registry)
    return code


# ---------------------------------------------------------------------------
# Line-level processing (respects TeX comments)
# ---------------------------------------------------------------------------

def split_tex_comment(line: str) -> tuple[str, str]:
    """Split a TeX line into code and comment parts at the first unescaped %."""
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "%":
            return line[:i], line[i:]
    return line, ""


def process_line(line: str) -> str:
    # Preserve line ending characters.
    stripped = line.rstrip("\r\n")
    ending = line[len(stripped):]

    if COMMENT_LINE.match(stripped):
        return line  # full-line comment — leave entirely untouched

    code, comment = split_tex_comment(stripped)
    code = process_code_part(code)
    return code + comment + ending


def detect_line_ending(text: str) -> str:
    """Return the dominant line ending found in *text* (default: LF)."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    if crlf > 0 and crlf >= lf and crlf >= cr:
        return "\r\n"
    if cr > 0 and cr > lf:
        return "\r"
    return "\n"


def process_text(text: str) -> str:
    # Protect multi-line verbatim blocks across the whole text FIRST, before
    # splitting into lines — the DOTALL regex cannot work line-by-line.
    text, block_registry = protect_verbatim_blocks(text)

    # Process each line individually (handles inline comments, single-line verbs,
    # existing \enquote blocks, and quote conversion).
    result = "".join(process_line(line) for line in text.splitlines(keepends=True))

    # Restore the verbatim blocks that were held aside.
    return restore_regions(result, block_registry)


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def process_file(path: Path, root: Path, dry_run: bool, backup: bool) -> bool:
    raw = path.read_bytes()
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError:
        print(f"Skipping {path.relative_to(root)}: not valid UTF-8.")
        return False

    modified = process_text(original)

    if modified == original:
        return False

    rel = path.relative_to(root)
    if dry_run:
        print(f"[dry-run] Would modify: {rel}")
    else:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        # Write back as bytes to avoid platform line-ending translation.
        path.write_bytes(modified.encode("utf-8"))
        print(f"Modified: {rel}")

    return True


def should_skip(path: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in path.parts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=r"Replace quotation marks with \enquote{} in all .tex files."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without writing files"
    )
    parser.add_argument(
        "--backup", action="store_true", help="Keep .bak copies of changed files"
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    tex_files = sorted(path for path in root.rglob("*.tex") if not should_skip(path))

    print(f"Script location: {root}")
    print(f"Found {len(tex_files)} .tex file(s)")

    if not tex_files:
        return

    changed = 0
    for path in tex_files:
        if process_file(path, root=root, dry_run=args.dry_run, backup=args.backup):
            changed += 1

    action = "Would change" if args.dry_run else "Changed"
    print(f"Done. {action} {changed}/{len(tex_files)} file(s).")


if __name__ == "__main__":
    main()
