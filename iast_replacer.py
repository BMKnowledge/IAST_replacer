#!/usr/bin/env python3
"""
IAST Replacer for LaTeX Manuscripts
====================================
Pulls canonical IAST spellings from a Google Sheet (or local CSV fallback)
and applies them to .tex files, skipping LaTeX-sensitive contexts.

Built for the BM Knowledge IAST spelling guide. Handles:
  - Direct replacements from the guide ("Krishna" -> "Kṛṣṇa")
  - Parentheses-encoded optional suffixes ("avatar(s)" -> both forms)
  - Auto-generated dropped-a variants ("Duryodhan" -> "Duryodhana")
  - Case preservation (KRISHNA / Krishna / krishna each handled correctly)
  - LaTeX-aware: skips \\label{}, \\ref{}, comments, verbatim blocks, etc.

Usage:
    python iast_replacer.py --dry-run --csv guide.csv path/to/manuscript/
    python iast_replacer.py --csv guide.csv path/to/chapter.tex
    python iast_replacer.py --flag-unknown --csv guide.csv manuscript/
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SPREADSHEET_ID = "1Sdccj1QRyjtIZr1P_0J7934rqshP-hmH"
WORKSHEET_NAME = "BM_Knowledge_IAST_spelling_guide_22.08.24"
CREDENTIALS_FILE = "credentials.json"

# Column mapping — matches the BM Knowledge IAST guide
COL_IAST = "IAST"
COL_ROMANIZED = "Romanized Spelling"

# Minimum stem length for dropped-a variant generation. "Rama" -> "Ram"
# (3 chars) is too short and collides with English words; "Duryodhana"
# -> "Duryodhan" (9 chars) is safe.
DROPPED_A_MIN_STEM_LEN = 5

PROTECTED_COMMANDS = {
    r"\label", r"\ref", r"\pageref", r"\cite", r"\citep", r"\citet",
    r"\nocite", r"\url", r"\href", r"\includegraphics", r"\input",
    r"\include", r"\bibliography", r"\bibliographystyle",
    r"\usepackage", r"\documentclass", r"\newcommand", r"\renewcommand",
    r"\def", r"\let", r"\hypersetup",
}

PROTECTED_ENVIRONMENTS = {
    "verbatim", "lstlisting", "minted", "comment", "filecontents",
    "Verbatim", "BVerbatim", "LVerbatim",
}

# ASCII letters + Latin Extended-A + Latin Extended Additional
# (covers IAST diacritics like ā ī ū ṛ ṅ ñ ṭ ḍ ṇ ś ṣ ṃ ḥ)
LETTER_CHARS = r"A-Za-z\u0100-\u017F\u1E00-\u1EFF\u00C0-\u00FF"

# Title markers: if one of these appears in a capitalized term, treat
# the whole term as a book/text title and italicize it anyway.
# (Bhagavad-gītā, Śiva Purāṇa, Yoga-sūtra, etc.)
TITLE_MARKERS = {
    "gītā", "purāṇa", "upaniṣad", "saṃhitā", "saṁhitā",
    "rāmāyaṇa", "mahābhārata", "bhāgavatam", "bhāgavata",
    "sūtra", "śāstra", "smṛti", "brāhmaṇa",
}

# LaTeX commands that already apply italic — matches inside these
# should NOT be wrapped again.
ITALIC_COMMAND_RE = re.compile(
    r"\\(?:textit|emph|textsl|textsf)\{([^{}]*)\}"
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Replacement:
    wrong: str
    right: str
    origin: str       # "direct" | "paren-expansion" | "dropped-a"
    source_row: int
    pattern: re.Pattern = field(init=False, repr=False)
    case_sensitive: bool = field(init=False, repr=False)

    def __post_init__(self):
        # Always match case-insensitively. Output casing is preserved from
        # the original text via match_case(). This catches lowercase
        # "paramatma" when the guide lists "Paramatma" (capitalized),
        # and vice versa.
        self.case_sensitive = False

        pattern_str = (
            f"(?<![{LETTER_CHARS}])"
            f"{re.escape(self.wrong)}"
            f"(?![{LETTER_CHARS}])"
        )
        self.pattern = re.compile(pattern_str, re.IGNORECASE)


@dataclass
class ChangeRecord:
    file: str
    line_no: int
    matched_text: str
    replaced_with: str
    rule_wrong: str
    rule_right: str
    origin: str


# ---------------------------------------------------------------------------
# 1. Load replacement data
# ---------------------------------------------------------------------------

def load_from_google_sheet(spreadsheet_id, worksheet_name, credentials_file):
    try:
        import gspread
    except ImportError:
        print("ERROR: gspread not installed. Run:  pip install gspread")
        print("       Or pass --csv to supply a local CSV file.")
        sys.exit(1)
    gc = gspread.service_account(filename=credentials_file)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(worksheet_name)
    return ws.get_all_records()


def load_from_csv(csv_path):
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 2. Variant generation
# ---------------------------------------------------------------------------

PAREN_RE = re.compile(r"\(([^)]*)\)")


def expand_parentheses(iast: str, rom: str) -> list[tuple[str, str]]:
    """
    Expand parenthesized optional suffixes.
        "avatāra(s)", "avatar(s)"  ->  [("avatāra","avatar"), ("avatāras","avatars")]
        "Ambā(jī)",   "Amba(ji)"   ->  [("Ambā","Amba"), ("Ambājī","Ambaji")]
        "āḻvār(s)",   "alvar()"    ->  [("āḻvār","alvar"), ("āḻvārs","alvar")]
    """
    if "(" not in iast and "(" not in rom:
        return [(iast, rom)]

    iast_stem = PAREN_RE.sub("", iast)
    rom_stem = PAREN_RE.sub("", rom)
    iast_suffix = "".join(PAREN_RE.findall(iast))
    rom_suffix = "".join(PAREN_RE.findall(rom))

    iast_full = iast_stem + iast_suffix
    rom_full = rom_stem + rom_suffix

    pairs = [(iast_stem, rom_stem)]
    if iast_full != iast_stem or rom_full != rom_stem:
        pairs.append((iast_full, rom_full))

    seen = set()
    cleaned = []
    for i, r in pairs:
        i, r = i.strip(), r.strip()
        if not i or not r:
            continue
        if (i, r) in seen:
            continue
        seen.add((i, r))
        cleaned.append((i, r))
    return cleaned


def generate_dropped_a_variant(iast: str, rom: str):
    """
    If romanized ends in a plain 'a' and the stem is long enough,
    return (iast_canonical, rom_minus_a) so we also catch texts that
    dropped the final -a (e.g. "Duryodhan" for "Duryodhana").
    """
    if not rom or not rom.endswith("a"):
        return None
    if rom.endswith("ā"):
        return None

    rom_dropped = rom[:-1]
    # Judge the shortness of the LAST segment of a compound
    last_seg = re.split(r"[\s\-]", rom_dropped)[-1]
    if len(last_seg) < DROPPED_A_MIN_STEM_LEN:
        return None

    # Refuse to drop if that would leave a word ending in a vowel
    if rom_dropped[-1] in "aeiouāēīōūṛḷ":
        return None

    return (iast, rom_dropped)


# ---------------------------------------------------------------------------
# 3. Build replacement rules
# ---------------------------------------------------------------------------

def build_replacements(
    rows,
    col_iast=COL_IAST,
    col_rom=COL_ROMANIZED,
    enable_dropped_a=True,
    enable_paren_expansion=True,
    latex_mode=False,
):
    replacements: list[Replacement] = []
    seen_wrong: dict[str, Replacement] = {}
    stats = {
        "total_rows": 0, "direct": 0, "paren_expansion": 0, "dropped_a": 0,
        "italic_only": 0,
        "skipped_duplicate": 0, "skipped_same": 0, "skipped_empty": 0,
    }

    def try_add(wrong, right, origin, source_row):
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right:
            stats["skipped_empty"] += 1
            return
        if wrong == right:
            # When latex_mode is on, we keep these rules so they can still
            # be italicized (no spelling change, but \textit{} wrapping).
            # Otherwise they're no-ops and skipped.
            if not latex_mode:
                stats["skipped_same"] += 1
                return
            # Only keep identical entries that are italic-worthy; otherwise
            # they really are no-ops.
            if not should_italicize(right):
                stats["skipped_same"] += 1
                return
            origin = "italic-only"
        key = wrong.lower()
        if key in seen_wrong:
            stats["skipped_duplicate"] += 1
            return
        rule = Replacement(
            wrong=wrong, right=right, origin=origin, source_row=source_row
        )
        seen_wrong[key] = rule
        replacements.append(rule)
        stats[origin.replace("-", "_")] = stats.get(
            origin.replace("-", "_"), 0) + 1

    for i, row in enumerate(rows, start=2):
        stats["total_rows"] += 1
        iast = row.get(col_iast, "").strip()
        rom = row.get(col_rom, "").strip()
        if not iast or not rom:
            stats["skipped_empty"] += 1
            continue

        pairs = (expand_parentheses(iast, rom)
                 if enable_paren_expansion else [(iast, rom)])

        for iast_form, rom_form in pairs:
            origin = "direct" if len(pairs) == 1 else "paren-expansion"
            try_add(rom_form, iast_form, origin, i)

            if enable_dropped_a:
                dropped = generate_dropped_a_variant(iast_form, rom_form)
                if dropped is not None:
                    try_add(dropped[1], dropped[0], "dropped-a", i)

    replacements.sort(key=lambda r: (len(r.wrong), r.wrong), reverse=True)
    return replacements, stats


# ---------------------------------------------------------------------------
# 4. LaTeX-aware line replacement
# ---------------------------------------------------------------------------

def find_protected_spans(line: str):
    spans = []
    for m in re.finditer(r"(?<!\\)%", line):
        spans.append((m.start(), len(line)))
        break
    for cmd in PROTECTED_COMMANDS:
        pat = re.compile(re.escape(cmd) + r"(?:\[[^\]]*\])?\{[^}]*\}")
        for m in pat.finditer(line):
            spans.append((m.start(), m.end()))
    return spans


def is_inside_protected(pos, length, spans):
    end = pos + length
    for s, e in spans:
        if pos < e and end > s:
            return True
    return False


# Characters that indicate the end of a previous sentence (so the next
# word is sentence-initial and legitimately capitalized)
SENTENCE_END_RE = re.compile(r"[.!?][\"'”’)\]]*\s+$")

# LaTeX commands that introduce fresh text (arg starts a new "sentence"
# for capitalization purposes): \chapter{Foo}, \section{Foo}, \item Foo
SENTENCE_START_CONTEXT_RE = re.compile(
    r"\\(?:chapter|section|subsection|subsubsection|paragraph|"
    r"subparagraph|part|title|item|caption|footnote|textbf|textit|"
    r"emph)\*?\{?\s*$"
)


def is_at_sentence_start(line: str, pos: int) -> bool:
    """
    True if the character at `pos` is the first letter of a sentence:
    preceded only by whitespace from line start, or by sentence-ending
    punctuation + whitespace, or by a LaTeX command that opens fresh text.
    """
    before = line[:pos]
    if before.strip() == "":
        return True
    if SENTENCE_END_RE.search(before):
        return True
    if SENTENCE_START_CONTEXT_RE.search(before):
        return True
    return False


def match_case(original: str, replacement: str, sentence_start: bool) -> str:
    """
    The guide's canonical casing is authoritative, with two exceptions:
      - ALL CAPS in text (len > 1)  -> keep caps (headings etc.)
      - Sentence-initial capital    -> uppercase the first letter of the
                                       replacement, since capitalization
                                       there is grammatical, not editorial.
    """
    if not original or not replacement:
        return replacement
    if len(original) > 1 and original.isupper():
        return replacement.upper()
    if sentence_start and original[0].isupper():
        return replacement[0].upper() + replacement[1:]
    return replacement


def should_italicize(iast_form: str) -> bool:
    """
    Decide whether an IAST term should be wrapped in \\textit{}.

    Heuristic:
      - Lowercase start -> common noun / technical term -> italicize
        (karma, dharma, yoga, ātmā, bhakti, mokṣa, ...)
      - Uppercase start -> proper name -> don't italicize
        (Kṛṣṇa, Arjuna, Duryodhana, Kurukṣetra, ...)
      - EXCEPT: uppercase terms containing a title marker are treated
        as book/scripture titles and italicized anyway
        (Bhagavad-gītā, Śiva Purāṇa, Yoga-sūtra, ...)
    """
    if not iast_form or len(iast_form) < 2:
        return False
    if iast_form[0].islower():
        return True
    low = iast_form.lower()
    for marker in TITLE_MARKERS:
        if marker in low:
            return True
    return False


def find_italic_content_spans(line: str) -> list[tuple[int, int]]:
    """
    Find (start, end) positions of text *inside* \\textit{...} / \\emph{...}
    groups. A match at such a position should be spelling-corrected but
    NOT wrapped in another italic command.
    """
    spans = []
    for m in ITALIC_COMMAND_RE.finditer(line):
        spans.append((m.start(1), m.end(1)))
    return spans


def apply_replacements_to_line(line, replacements, latex_mode=False,
                               italic_cmd="textit"):
    protected = find_protected_spans(line)
    italic_spans = find_italic_content_spans(line) if latex_mode else []
    candidates = []

    for rule in replacements:
        for m in rule.pattern.finditer(line):
            start, end = m.start(), m.end()
            if is_inside_protected(start, end - start, protected):
                continue
            matched = m.group()
            new_spelling = match_case(matched, rule.right, is_at_sentence_start(line, start))

            # Decide whether to wrap in \textit{}:
            #   - LaTeX mode is on
            #   - Term is italic-worthy (common noun / technical term / title)
            #   - Not already inside an italic command
            inside_italic = is_inside_protected(
                start, end - start, italic_spans
            )
            wrap_italic = (
                latex_mode
                and not inside_italic
                and should_italicize(rule.right)
            )

            if wrap_italic:
                final = f"\\{italic_cmd}{{{new_spelling}}}"
            else:
                final = new_spelling

            if matched == final:
                continue  # no change

            candidates.append((start, end, matched, final, rule))

    candidates.sort(key=lambda x: (x[0], -(x[1] - x[0])))
    kept = []
    last_end = -1
    for c in candidates:
        if c[0] >= last_end:
            kept.append(c)
            last_end = c[1]

    new_line = line
    changes = []
    for start, end, matched, new, rule in reversed(kept):
        new_line = new_line[:start] + new + new_line[end:]
        changes.append((matched, new, rule))
    changes.reverse()
    return new_line, changes


# ---------------------------------------------------------------------------
# 5. File processing
# ---------------------------------------------------------------------------

def process_tex_content(lines, replacements, filename="<unknown>",
                        latex_mode=False, italic_cmd="textit"):
    new_lines = []
    all_changes = []
    inside = False
    env_name = ""

    begin_pats = {e: re.compile(r"\\begin\{" + re.escape(e) + r"\}")
                  for e in PROTECTED_ENVIRONMENTS}
    end_pats = {e: re.compile(r"\\end\{" + re.escape(e) + r"\}")
                for e in PROTECTED_ENVIRONMENTS}

    for line_no, line in enumerate(lines, start=1):
        if not inside:
            for e, pat in begin_pats.items():
                if pat.search(line):
                    inside = True
                    env_name = e
                    break
        if inside:
            new_lines.append(line)
            if env_name and end_pats[env_name].search(line):
                inside = False
                env_name = ""
            continue

        new_line, changes = apply_replacements_to_line(
            line, replacements,
            latex_mode=latex_mode,
            italic_cmd=italic_cmd,
        )
        new_lines.append(new_line)
        for matched, replaced, rule in changes:
            all_changes.append(ChangeRecord(
                file=filename, line_no=line_no,
                matched_text=matched, replaced_with=replaced,
                rule_wrong=rule.wrong, rule_right=rule.right,
                origin=rule.origin,
            ))
    return new_lines, all_changes


# ---------------------------------------------------------------------------
# 6. Unknown-term flagging
# ---------------------------------------------------------------------------

SANSKRIT_HINT_PATTERN = re.compile(
    r"\b[A-Z]?[a-z]*(?:yoga|dharma|karma|sutra|mantra|tantra|veda|"
    r"deva|guru|atma|brahma|purana|shastra|sastra|"
    r"bhakti|jnana|moksha|samsara|avatar|chakra|dhyana|prana|"
    r"siddhi|swami|acharya|upanishad|upanisad|"
    r"rishi|yogi|ananda|ishvara|ishwara)[a-z]*\b",
    re.IGNORECASE,
)


def flag_unknown_terms(lines, known_terms, filename):
    flagged = []
    for line_no, line in enumerate(lines, start=1):
        for m in SANSKRIT_HINT_PATTERN.finditer(line):
            term = m.group()
            if term.lower() not in known_terms:
                flagged.append((term, line_no))
    return flagged


# ---------------------------------------------------------------------------
# 7. Reporting
# ---------------------------------------------------------------------------

def print_load_stats(stats, total_rules):
    print(f"  Loaded {stats['total_rows']} rows from the guide.")
    print(f"  Built {total_rules} replacement rules:")
    print(f"    - direct:           {stats.get('direct', 0)}")
    print(f"    - paren-expansion:  {stats.get('paren_expansion', 0)}")
    print(f"    - dropped-a:        {stats.get('dropped_a', 0)}")
    if stats.get('italic_only', 0):
        print(f"    - italic-only:      {stats.get('italic_only', 0)}")
    print(f"  Skipped:")
    print(f"    - same in both cols: {stats['skipped_same']}")
    print(f"    - empty:             {stats['skipped_empty']}")
    print(f"    - duplicates:        {stats['skipped_duplicate']}")


def print_change_report(changes):
    if not changes:
        print("\n  No changes proposed.")
        return

    by_file = {}
    for c in changes:
        by_file.setdefault(c.file, []).append(c)

    by_origin: dict[str, int] = {}
    for c in changes:
        by_origin[c.origin] = by_origin.get(c.origin, 0) + 1

    print(f"\n  {'=' * 60}")
    print(f"  Total changes: {len(changes)}")
    for origin in ["direct", "paren-expansion", "dropped-a", "italic-only"]:
        n = by_origin.get(origin, 0)
        if n:
            print(f"    {origin}:{' ' * (16 - len(origin))}{n}")
    print(f"  {'=' * 60}")

    for fname, file_changes in by_file.items():
        print(f"\n  File: {fname}  ({len(file_changes)} changes)")
        print(f"  {'-' * 56}")
        for origin in ["direct", "paren-expansion", "dropped-a", "italic-only"]:
            group = [c for c in file_changes if c.origin == origin]
            if not group:
                continue
            print(f"    [{origin}]")
            for c in group:
                print(f"      Line {c.line_no:>5}: "
                      f"'{c.matched_text}' → '{c.replaced_with}'")


def print_unknown_report(flagged, filename):
    if not flagged:
        return
    unique = {}
    for term, ln in flagged:
        unique.setdefault(term.lower(), []).append(ln)
    print(f"\n  Possible Sanskrit terms not in the guide ({filename}):")
    print(f"  {'-' * 56}")
    for term in sorted(unique):
        lines = unique[term]
        ln_str = ", ".join(str(l) for l in lines[:8])
        if len(lines) > 8:
            ln_str += f" ... (+{len(lines) - 8} more)"
        print(f"    '{term}'  — lines: {ln_str}")


# ---------------------------------------------------------------------------
# 8. Main
# ---------------------------------------------------------------------------

def process_file(filepath, replacements, dry_run, flag_unknown,
                 known_terms, no_backup, latex_mode=False,
                 italic_cmd="textit"):
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines, changes = process_tex_content(
        lines, replacements, filename=str(filepath),
        latex_mode=latex_mode, italic_cmd=italic_cmd,
    )

    if flag_unknown:
        flagged = flag_unknown_terms(lines, known_terms, str(filepath))
        print_unknown_report(flagged, str(filepath))

    if not dry_run and changes:
        if not no_backup:
            backup = filepath.with_suffix(filepath.suffix + ".bak")
            with open(backup, "w", encoding="utf-8") as f:
                f.writelines(lines)
            note = f"  (backup: {backup.name})"
        else:
            note = ""
        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"  ✓ Updated {filepath}{note}")

    return changes


def main():
    parser = argparse.ArgumentParser(
        description="Apply IAST spellings from a Google Sheet to LaTeX files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("paths", nargs="+",
        help="One or more .tex files or directories.")
    parser.add_argument("--dry-run", action="store_true",
        help="Preview changes without modifying files.")
    parser.add_argument("--csv", metavar="FILE",
        help="Use local CSV instead of Google Sheets API.")
    parser.add_argument("--credentials", default=CREDENTIALS_FILE,
        help=f"Service account JSON path (default: {CREDENTIALS_FILE}).")
    parser.add_argument("--sheet-id", default=SPREADSHEET_ID,
        help="Google Spreadsheet ID.")
    parser.add_argument("--worksheet", default=WORKSHEET_NAME,
        help=f"Worksheet/tab name (default: '{WORKSHEET_NAME}').")
    parser.add_argument("--col-iast", default=COL_IAST,
        help=f"Column header for IAST (default: '{COL_IAST}').")
    parser.add_argument("--col-romanized", default=COL_ROMANIZED,
        help=f"Column header for romanized (default: '{COL_ROMANIZED}').")
    parser.add_argument("--no-dropped-a", action="store_true",
        help="Disable dropped-a variant generation.")
    parser.add_argument("--no-paren-expansion", action="store_true",
        help="Disable parenthesis expansion.")
    parser.add_argument("--flag-unknown", action="store_true",
        help="Report Sanskrit-looking terms not in the guide.")
    parser.add_argument("--no-backup", action="store_true",
        help="Skip creating .bak files.")
    parser.add_argument("--latex", action="store_true",
        help="Wrap italic-worthy terms in \\textit{} (or --italic-cmd). "
             "Lowercase terms and titles (Gītā, Purāṇa, etc.) are "
             "italicized; proper names stay roman.")
    parser.add_argument("--italic-cmd", choices=["textit", "emph"],
        default="textit",
        help="LaTeX command to use for italicizing (default: textit). "
             "Use 'emph' if you want the toggle-italic behavior in "
             "already-italicized contexts like section headings.")

    args = parser.parse_args()

    print("\n[1/3] Loading IAST spelling guide...")
    rows = (load_from_csv(args.csv) if args.csv
            else load_from_google_sheet(
                args.sheet_id, args.worksheet, args.credentials))

    replacements, stats = build_replacements(
        rows,
        col_iast=args.col_iast,
        col_rom=args.col_romanized,
        enable_dropped_a=not args.no_dropped_a,
        enable_paren_expansion=not args.no_paren_expansion,
        latex_mode=args.latex,
    )
    print_load_stats(stats, len(replacements))

    if not replacements:
        print("  ERROR: No rules built. Check column headers?")
        sys.exit(1)

    # Known-terms set for unknown-term flagging
    known_terms = set()
    for r in replacements:
        known_terms.add(r.wrong.lower())
        known_terms.add(r.right.lower())
    for row in rows:
        i = row.get(args.col_iast, "").strip().lower()
        r_ = row.get(args.col_romanized, "").strip().lower()
        if i:
            known_terms.add(i)
        if r_:
            known_terms.add(r_)

    print("\n[2/3] Scanning for .tex files...")
    tex_files = []
    for p in args.paths:
        path = Path(p)
        if path.is_file() and path.suffix == ".tex":
            tex_files.append(path)
        elif path.is_dir():
            tex_files.extend(sorted(path.rglob("*.tex")))
        else:
            print(f"  WARNING: skipping '{p}'.")
    if not tex_files:
        print("  ERROR: no .tex files found.")
        sys.exit(1)
    print(f"  Found {len(tex_files)} .tex file(s).")

    mode = "DRY RUN" if args.dry_run else "APPLYING CHANGES"
    if args.latex:
        mode += f" (LaTeX italic mode: \\{args.italic_cmd}{{}})"
    print(f"\n[3/3] Processing — {mode}...")

    all_changes = []
    for tex_file in tex_files:
        all_changes.extend(process_file(
            tex_file, replacements,
            dry_run=args.dry_run,
            flag_unknown=args.flag_unknown,
            known_terms=known_terms,
            no_backup=args.no_backup,
            latex_mode=args.latex,
            italic_cmd=args.italic_cmd,
        ))

    print_change_report(all_changes)

    if args.dry_run and all_changes:
        print("\n  Dry run complete. Re-run without --dry-run to apply.")
    print()


if __name__ == "__main__":
    main()
