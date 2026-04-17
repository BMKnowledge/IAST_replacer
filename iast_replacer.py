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
    origin: str       # "direct" | "paren-expansion" | "dropped-a" | ...
    source_row: int
    pattern: re.Pattern = field(init=False, repr=False)
    case_sensitive: bool = field(init=False, repr=False)
    ambiguous: bool = field(default=False)       # True when rom → multiple distinct IAST
    casing_variant: bool = field(default=False)  # True when rom → same IAST, different case

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
    ambiguous: bool = False
    casing_variant: bool = False


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


# Matches ", Śrī" (IAST) at end of string, with optional whitespace
_IAST_SRI_SUFFIX_RE = re.compile(r",\s*Śrī\s*$")
# Matches ", Sri" or ", Shri" (romanized) at end of string, case-insensitive
_ROM_SRI_SUFFIX_RE = re.compile(r",\s*Sh?ri\s*$", re.IGNORECASE)


def generate_sri_variants(iast: str, rom: str) -> list[tuple[str, str]]:
    """
    For comma-inverted index entries like 'Madhvācārya, Śrī' / 'Madhvacharya, Sri',
    generate the name-first forms that appear in running text:
      - 'Madhvacharya'       -> 'Madhvācārya'
      - 'Sri Madhvacharya'   -> 'Śrī Madhvācārya'
      - 'Shri Madhvacharya'  -> 'Śrī Madhvācārya'
    Returns a list of (iast_form, rom_form) tuples.
    """
    iast_m = _IAST_SRI_SUFFIX_RE.search(iast)
    rom_m = _ROM_SRI_SUFFIX_RE.search(rom)
    if not iast_m or not rom_m:
        return []

    name_iast = iast[: iast_m.start()].strip()
    name_rom = rom[: rom_m.start()].strip()
    if not name_iast or not name_rom:
        return []

    return [
        (name_iast, name_rom),                          # bare name
        (f"Śrī {name_iast}", f"Sri {name_rom}"),        # Sri prefix
        (f"Śrī {name_iast}", f"Shri {name_rom}"),       # Shri prefix
    ]


# Separators used in compound romanized terms
_SEP_RE = re.compile(r"[-\s]")


def generate_compound_variants(iast: str, rom: str) -> list[tuple[str, str]]:
    """
    For compound terms written with hyphens or spaces, generate separator-
    normalised variants so that users who write them differently still get
    corrected.

    Given "Yoga-māyā" / "Yoga-maya":
      - "Yogamaya"   (separator removed, letters joined)
      - "Yoga maya"  (hyphen → space, or vice-versa)
      - "Yogmaya"    (junction dropped-a: first component's trailing 'a'
                      elided before the join, a common informal usage)

    Only applied when the romanized form contains at least one hyphen or
    internal space (i.e. is a genuine compound).
    """
    parts = _SEP_RE.split(rom)
    if len(parts) < 2:
        return []

    iast_parts = _SEP_RE.split(iast)
    if len(iast_parts) != len(parts):
        return []

    # joined (no separator) and space variants.
    # The IAST target is always the *canonical* iast (with separators),
    # so that e.g. "Yogamaya" → "Yoga-māyā" not the mangled "Yogamāyā".
    joined_rom = "".join(parts)
    spaced_rom = " ".join(parts)

    variants: list[tuple[str, str]] = []
    if joined_rom != rom:
        variants.append((iast, joined_rom))
    if spaced_rom != rom:
        variants.append((iast, spaced_rom))

    # junction dropped-a: if first component ends in plain 'a' (not ā)
    # and has a useful stem, also generate the elided form
    # e.g. "Yoga-maya" → wrong="Yogmaya", right="Yoga-māyā"
    first_rom = parts[0]
    if (first_rom.endswith("a")
            and not first_rom.endswith("ā")
            and len(first_rom) > 2):
        elided_rom = first_rom[:-1] + "".join(parts[1:])
        if elided_rom != joined_rom:
            variants.append((iast, elided_rom))

    return variants


# ---------------------------------------------------------------------------
# 3. Build replacement rules
# ---------------------------------------------------------------------------

def build_replacements(
    rows,
    col_iast=COL_IAST,
    col_rom=COL_ROMANIZED,
    enable_dropped_a=True,
    enable_paren_expansion=True,
    enable_sri_variants=True,
    enable_compound_variants=True,
    latex_mode=False,
):
    replacements: list[Replacement] = []
    seen_wrong: dict[str, Replacement] = {}
    stats = {
        "total_rows": 0, "direct": 0, "paren_expansion": 0, "dropped_a": 0,
        "italic_only": 0, "sri_variant": 0, "compound_variant": 0,
        "skipped_duplicate": 0, "skipped_same": 0, "skipped_empty": 0,
    }

    def try_add(wrong, right, origin, source_row):
        wrong, right = wrong.strip(), right.strip()
        if not wrong or not right:
            stats["skipped_empty"] += 1
            return
        if wrong == right:
            if not latex_mode:
                stats["skipped_same"] += 1
                return
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

    # Pre-pass: build rom→IAST mapping from raw rows to detect ambiguities.
    # Done separately so that same-as-itself no-ops (e.g. "Vasudeva"→"Vasudeva")
    # are still counted, which try_add() would otherwise skip early.
    rom_to_iast: dict[str, set[str]] = {}
    for row in rows:
        iast_raw = row.get(col_iast, "").strip()
        rom_raw  = row.get(col_rom, "").strip()
        if not iast_raw or not rom_raw:
            continue
        pairs_raw = (expand_parentheses(iast_raw, rom_raw)
                     if enable_paren_expansion else [(iast_raw, rom_raw)])
        for iast_form, rom_form in pairs_raw:
            rom_to_iast.setdefault(rom_form.lower(), set()).add(iast_form)

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

            if enable_sri_variants:
                for var_iast, var_rom in generate_sri_variants(iast_form, rom_form):
                    try_add(var_rom, var_iast, "sri-variant", i)

            if enable_compound_variants:
                for var_iast, var_rom in generate_compound_variants(iast_form, rom_form):
                    try_add(var_rom, var_iast, "compound-variant", i)

    # Classify each conflicted romanized key into two categories:
    #
    # casing_variant_keys  — same IAST modulo capitalisation only
    #   e.g. "yoga-maya" → {"Yoga-māyā", "yoga-māyā"}
    #   match_case() already picks the right form; we annotate in the report
    #   so users can verify capitalisation was applied correctly.
    #
    # true_ambiguous_keys  — IAST forms differ even after lowercasing
    #   e.g. "vasudeva" → {"Vasudeva", "Vāsudeva"} — genuinely different words;
    #   flag for manual review and show both candidates.
    casing_variant_keys: set[str] = set()
    true_ambiguous_keys: set[str] = set()
    # rom_key -> sorted list of all IAST forms (for display in reports)
    ambiguity_map: dict[str, list[str]] = {}

    for key, iast_set in rom_to_iast.items():
        if len(iast_set) < 2:
            continue
        lowered = {i.lower() for i in iast_set}
        ambiguity_map[key] = sorted(iast_set)
        if len(lowered) == 1:
            casing_variant_keys.add(key)
        else:
            true_ambiguous_keys.add(key)

    for rule in replacements:
        k = rule.wrong.lower()
        rule.ambiguous = k in true_ambiguous_keys
        rule.casing_variant = k in casing_variant_keys

    replacements.sort(key=lambda r: (len(r.wrong), r.wrong), reverse=True)
    return replacements, stats, true_ambiguous_keys, casing_variant_keys, ambiguity_map


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
                ambiguous=getattr(rule, "ambiguous", False),
                casing_variant=getattr(rule, "casing_variant", False),
            ))
    return new_lines, all_changes


# ---------------------------------------------------------------------------
# 6. Unknown-term flagging
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 6. Unknown-term flagging
# ---------------------------------------------------------------------------

# This pattern is intentionally broad. It is not meant to "prove" that a word
# is Sanskrit; it is meant to catch likely Sanskrit / Indic technical terms
# written in romanized English-friendly spelling so they can be reviewed.
#
# Strategy:
# 1. Match exact common standalone terms.
# 2. Match common endings and stems frequently found in Sanskrit vocabulary.
# 3. Match compounds that contain recognizable Sanskrit components.
# 4. Keep the matching permissive, because false positives are acceptable for
#    a review/flagging pass, while false negatives are more costly.

SANSKRIT_HINT_PATTERN = re.compile(
    r"""
    \b
    (?:
        # -------------------------------------------------------------------
        # A. Very common standalone Sanskrit / Indic terms
        # -------------------------------------------------------------------
        atma|atman|antaratman|paramatma|jiva|jivatma|purusha|prakriti|
        brahma|brahman|parabrahman|
        ishvara|ishwara|maheshvara|maheshwara|bhagavan|bhagavat|
        deva|devi|devata|surya|indra|agni|vayu|varna|ashrama|
        dharma|adharma|karma|vikarma|akarma|moksha|mukti|samsara|
        maya|lila|guna|sattva|rajas|tamas|tattva|satya|rita|
        shanti|prema|bhava|rasa|ananda|kripa|daya|shraddha|bhakti|
        jnana|ajnana|vijnana|vairagya|viveka|tyaga|sankalpa|
        yoga|raja\s?yoga|karma\s?yoga|bhakti\s?yoga|jnana\s?yoga|
        dhyana|samadhi|asana|pranayama|mudra|bandha|mantra|yantra|tantra|
        japa|tapa|tapas|vrata|puja|arati|homa|yajna|darshana|seva|sadhana|
        satsanga|satsang|sadhu|sadguru|guru|acharya|acharya|swami|sannyasa|
        brahmacharya|brahmachari|brahmacharini|grihastha|vanaprastha|
        rishi|rsi|muni|yogi|yogini|siddha|siddhi|mahant|mahatma|
        veda|vedanta|vedanta|upanishad|upanishads|upanisad|upanisads|
        purana|puranas|itihasa|smriti|shruti|shastra|sastra|sutra|
        mahavakya|sloka|shloka|kirtan|kirtana|bhajan|nama|namasmarana|
        tilaka|murti|vigraha|prasada|prasad|mandir|tirtha|yatra|upavasa|
        chakra|nadi|kundalini|prana|apana|vyana|udana|samana|ojas|tejas|
        avatara|avatar|vibhuti|aishvarya|sankirtan|harinama|diksha|
        samskara|sanskara|nyasa|abhisheka|abhishek|utsava|mahotsava|
        grantha|stotra|stuti|kavacha|yantra|panchanga|agama|nigama|
        bhumi|loka|svarga|swarga|naraka|vaikuntha|goloka|kailasa|
        kali\s?yuga|dvapara\s?yuga|treta\s?yuga|satya\s?yuga|yuga|
        kala|desha|akasha|ahamkara|buddhi|manas|chitta|citta|indriya|
        prarabdha|sanchita|agami|iccha|kriya|shakti|shakta|shaktipat|
        shiva|shakti|gauri|lakshmi|sarasvati|durga|kali|radha|krishna|
        vishnu|narayana|rama|sita|hanuman|ganesha|ganapati|skanda|karttikeya|
        govinda|gopala|madhava|kesava|vasudeva|janardana|hari|haraa?|mahadeva|

        # -------------------------------------------------------------------
        # B. Words with characteristic Sanskrit endings
        # -------------------------------------------------------------------
        |
        [A-Z]?[a-z]+(?:a|am|an|ana|anam|atma|atman|maya|lila|rupa|rasa|bhava|
        deva|devi|natha|isha|eshvara|ishvara|ishwara|pati|putra|kanta|maya|
        yoga|yogi|yogini|veda|vedanta|vidya|avidya|siddhi|shakti|bhakti|
        mukti|smriti|shruti|mati|gati|pada|vada|vadin|tva|tvaṁ|tvam|taka|
        kara|dhara|dhari|nanda|ananda|jyoti|murti|pura|puri|giri|nidhi|
        ratna|maya|atma|jna|jnana|vijnana|darshana|darsana|sutra|tantra|
        mantra|yantra|shastra|sastra|purana|upanishad|upanisad|avatara|avatar|
        samskara|sanskara|acharya|arya|rishi|rsi|guru|swami|sadhu|vrata|tapas|
        yajna|homa|puja|arati|diksha|seva|loka|yuga|guna|tattva|prakasha|
        prajna|charya|carita|charita|katha|gita|gitā|sutram?)\b

        # -------------------------------------------------------------------
        # C. Prefix + Sanskrit stem combinations
        # -------------------------------------------------------------------
        |
        (?:maha|mahā|sri|shri|parama|sarva|sat|cit|ananda|jnana|vijnana|su|
        sva|swa|ati|adhi|upa|anu|ni|nir|nis|dur|dus|vi|sam|pra|pari|ava|ud|
        abhi|prati|ati|antar|bahir|eka|dvi|tri|catur|pancha|sapta|nava|dasha)
        [a-z]+

        # -------------------------------------------------------------------
        # D. Sanskrit compounds with separators
        # -------------------------------------------------------------------
        |
        [A-Z]?[a-z]+(?:[-_ ][A-Z]?[a-z]+){1,4}
    )
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Optional: a stricter "compound Sanskrit-ish" pattern that you can apply
# after the main pass if you want more aggressive review of compounds.
SANSKRIT_COMPOUND_PATTERN = re.compile(
    r"""
    \b
    [A-Z]?[a-z]+
    (?:
        [-_ ]
        (?:atma|atman|jiva|brahma|brahman|deva|devi|dharma|karma|yoga|bhakti|
         jnana|veda|vedanta|sutra|shastra|sastra|purana|upanishad|guru|swami|
         shakti|mantra|tantra|yantra|maya|lila|tattva|siddhi|ananda|rasa|bhava|
         puja|seva|yajna|homa|diksha|darshana|avatara|chakra|prana|murti|
         prasada|mandir|tirtha|yatra|loka|yuga|acharya|sadhu|satsanga)
    ){1,4}
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


# Common false positives you may want to suppress.
# Adjust freely based on your corpus.
SANSKRIT_FALSE_POSITIVES = {
    "data",
    "media",
    "area",
    "flora",
    "agenda",
    "extra",
    "opera",
    "formula",
    "camera",
    "yogae",   # typo-like junk examples can be added if needed
    "karmaed", # same idea
}


def normalize_flagged_term(term: str) -> str:
    """
    Normalize a candidate term before checking whether it is known.

    This is intentionally light-touch. It helps catch:
    - case differences
    - stray punctuation around tokens
    - simple ASCII variants
    """
    t = term.strip(" \t\n\r.,;:!?()[]{}<>\"'`")
    t = t.lower()

    # normalize whitespace / separators
    t = re.sub(r"[_\s]+", "-", t)

    # normalize a few common spelling variants
    t = t.replace("shree", "shri")
    t = t.replace("sree", "shri")
    t = t.replace("ishwar", "ishvar") if "ishwar" in t else t
    t = t.replace("sanskara", "samskara")
    t = t.replace("sastra", "shastra") if t == "sastra" else t

    return t


def looks_like_probable_sanskrit(term: str) -> bool:
    """
    Additional heuristic filter.

    A term is considered Sanskrit-ish if:
    - it matches the broad main pattern, OR
    - it matches the compound pattern, OR
    - it contains characteristic Sanskrit clusters.
    """
    if SANSKRIT_HINT_PATTERN.search(term):
        return True

    if SANSKRIT_COMPOUND_PATTERN.search(term):
        return True

    lower = term.lower()

    characteristic_clusters = (
        "sh", "ch", "dh", "bh", "th", "kh", "ph", "jn", "ksh", "tr", "shr"
    )
    characteristic_endings = (
        "a", "am", "ana", "atma", "yoga", "dharma", "karma", "tva",
        "maya", "lila", "veda", "sutra", "mantra", "tantra", "shakti",
        "bhakti", "jnana", "ananda", "rupa", "rasa", "pura", "natha",
        "deva", "devi", "guru", "swami", "acharya", "rishi"
    )

    if any(c in lower for c in characteristic_clusters) and any(
        lower.endswith(e) for e in characteristic_endings
    ):
        return True

    return False


def flag_unknown_terms(lines, known_terms, filename):
    """
    Flag possible Sanskrit / Indic technical terms that are not present in
    known_terms.

    known_terms is assumed to contain normalized lowercase keys.
    """
    flagged = []
    seen = set()

    for line_no, line in enumerate(lines, start=1):
        candidates = []

        # Main broad matcher
        candidates.extend(m.group() for m in SANSKRIT_HINT_PATTERN.finditer(line))

        # Optional second pass for compounds
        candidates.extend(m.group() for m in SANSKRIT_COMPOUND_PATTERN.finditer(line))

        for term in candidates:
            normalized = normalize_flagged_term(term)

            if not normalized:
                continue

            if normalized in SANSKRIT_FALSE_POSITIVES:
                continue

            if normalized in known_terms:
                continue

            if not looks_like_probable_sanskrit(term):
                continue

            key = (normalized, line_no)
            if key not in seen:
                flagged.append((term, line_no, filename))
                seen.add(key)

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
    print(f"    - sri-variant:      {stats.get('sri_variant', 0)}")
    print(f"    - compound-variant: {stats.get('compound_variant', 0)}")
    if stats.get('italic_only', 0):
        print(f"    - italic-only:      {stats.get('italic_only', 0)}")
    print(f"  Skipped:")
    print(f"    - same in both cols: {stats['skipped_same']}")
    print(f"    - empty:             {stats['skipped_empty']}")
    print(f"    - duplicates:        {stats['skipped_duplicate']}")


def print_ambiguity_report(
    true_ambiguous_keys: set[str],
    casing_variant_keys: set[str],
    ambiguity_map: dict[str, list[str]],
    replacements,
) -> None:
    """
    Report two categories of romanized terms that map to multiple IAST forms.

    True ambiguities: same romanization, genuinely different IAST
      e.g. "vasudeva" → Vasudeva (Krishna's father) OR Vāsudeva (Krishna).
      The first rule in the guide is applied; changes are flagged for review.

    Casing variants: same IAST word in uppercase and lowercase forms
      e.g. "yoga-maya" → Yoga-māyā (proper) or yoga-māyā (common noun).
      match_case() already selects the right capitalisation; changes are
      annotated so users can spot-check.
    """
    if true_ambiguous_keys:
        kept: dict[str, tuple[str, str]] = {
            r.wrong.lower(): (r.wrong, r.right)
            for r in replacements
            if r.wrong.lower() in true_ambiguous_keys
        }
        print(f"\n  {'!' * 60}")
        print(f"  TRUE IAST AMBIGUITIES ({len(true_ambiguous_keys)} term(s))")
        print(f"  These romanized forms map to distinct IAST spellings.")
        print(f"  The guide's first match is applied; flagged [!AMBIGUOUS] in report.")
        print(f"  {'!' * 60}")
        for key in sorted(true_ambiguous_keys):
            wrong, right = kept.get(key, (key, "?"))
            all_forms = " / ".join(ambiguity_map.get(key, [right]))
            print(f"    '{wrong}'  →  {all_forms}")

    if casing_variant_keys:
        print(f"\n  {'~' * 60}")
        print(f"  CASING VARIANTS ({len(casing_variant_keys)} term(s))")
        print(f"  These terms exist in both Capitalised and lowercase forms.")
        print(f"  Capitalisation is auto-selected; flagged [casing-variant] in report.")
        print(f"  {'~' * 60}")
        for key in sorted(casing_variant_keys):
            all_forms = " / ".join(ambiguity_map.get(key, [key]))
            print(f"    '{key}'  →  {all_forms}")


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

    n_ambiguous      = sum(1 for c in changes if c.ambiguous)
    n_casing_variant = sum(1 for c in changes if c.casing_variant)

    print(f"\n  {'=' * 60}")
    print(f"  Total changes: {len(changes)}")
    for origin in ["direct", "paren-expansion", "dropped-a", "sri-variant",
                   "compound-variant", "italic-only"]:
        n = by_origin.get(origin, 0)
        if n:
            print(f"    {origin}:{' ' * (17 - len(origin))}{n}")
    if n_ambiguous:
        print(f"  Needs manual review (!AMBIGUOUS):  {n_ambiguous}")
    if n_casing_variant:
        print(f"  Casing auto-selected (verify):     {n_casing_variant}")
    print(f"  {'=' * 60}")

    for fname, file_changes in by_file.items():
        print(f"\n  File: {fname}  ({len(file_changes)} changes)")
        print(f"  {'-' * 56}")
        for origin in ["direct", "paren-expansion", "dropped-a", "sri-variant",
                       "compound-variant", "italic-only"]:
            group = [c for c in file_changes if c.origin == origin]
            if not group:
                continue
            print(f"    [{origin}]")
            for c in group:
                if c.ambiguous:
                    flag = " [!AMBIGUOUS — review]"
                elif c.casing_variant:
                    flag = " [casing-variant — verify]"
                else:
                    flag = ""
                print(f"      Line {c.line_no:>5}: "
                      f"'{c.matched_text}' → '{c.replaced_with}'{flag}")


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
    parser.add_argument("--no-sri-variants", action="store_true",
        help="Disable generation of name-first / Sri-prefix variants for "
             "comma-inverted entries like 'Madhvacharya, Sri'.")
    parser.add_argument("--no-compound-variants", action="store_true",
        help="Disable generation of separator-normalised and junction-dropped-a "
             "variants for compound terms (e.g. 'Yogmaya' / 'Yogamaya' for "
             "'Yoga-maya').")
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

    replacements, stats, true_ambiguous_keys, casing_variant_keys, ambiguity_map = \
        build_replacements(
            rows,
            col_iast=args.col_iast,
            col_rom=args.col_romanized,
            enable_dropped_a=not args.no_dropped_a,
            enable_paren_expansion=not args.no_paren_expansion,
            enable_sri_variants=not args.no_sri_variants,
            enable_compound_variants=not args.no_compound_variants,
            latex_mode=args.latex,
        )
    print_load_stats(stats, len(replacements))
    print_ambiguity_report(
        true_ambiguous_keys, casing_variant_keys, ambiguity_map, replacements
    )

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
