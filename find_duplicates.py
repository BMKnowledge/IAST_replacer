#!/usr/bin/env python3
"""
Find duplicate entries in a CSV file.

This script checks for duplicates in:
1. the IAST column
2. the Romanized Spelling column
3. exact full row duplicates

It ignores leading/trailing spaces, and can optionally ignore case.

Usage:
    python find_duplicates.py path/to/file.csv
    python find_duplicates.py path/to/file.csv --ignore-case
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


def normalize(value: str, ignore_case: bool = False) -> str:
    """Normalize a CSV field for duplicate comparison."""
    value = value.strip()
    if ignore_case:
        value = value.casefold()
    return value


def collect_duplicates(rows: list[dict], column_name: str, ignore_case: bool) -> dict[str, list[int]]:
    """
    Return a mapping of normalized field value -> list of row numbers
    for entries that appear more than once.
    """
    seen: dict[str, list[int]] = defaultdict(list)

    for row_number, row in enumerate(rows, start=2):  # row 1 is header
        raw_value = row.get(column_name, "")
        value = normalize(raw_value, ignore_case=ignore_case)
        if value:
            seen[value].append(row_number)

    return {value: line_numbers for value, line_numbers in seen.items() if len(line_numbers) > 1}


def collect_full_row_duplicates(rows: list[dict], ignore_case: bool) -> dict[tuple[str, ...], list[int]]:
    """
    Return a mapping of normalized full row tuples -> list of row numbers
    for rows that appear more than once.
    """
    seen: dict[tuple[str, ...], list[int]] = defaultdict(list)

    for row_number, row in enumerate(rows, start=2):
        normalized_row = tuple(
            normalize(str(value), ignore_case=ignore_case)
            for value in row.values()
        )
        seen[normalized_row].append(row_number)

    return {row_key: line_numbers for row_key, line_numbers in seen.items() if len(line_numbers) > 1}


def print_duplicates(title: str, duplicates: dict, original_rows: list[dict] | None = None, column_name: str | None = None) -> None:
    """Print duplicates in a readable format."""
    print(f"\n{title}")
    print("-" * len(title))

    if not duplicates:
        print("No duplicates found.")
        return

    for duplicated_value, line_numbers in duplicates.items():
        print(f"Value: {duplicated_value!r}")
        print(f"Rows:  {line_numbers}")
        if original_rows is not None and column_name is not None:
            examples = [original_rows[line - 2].get(column_name, "") for line in line_numbers]
            print(f"Original values: {examples}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Find duplicates in a CSV file.")
    parser.add_argument("csv_file", type=Path, help="Path to the CSV file")
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help="Treat uppercase/lowercase as the same"
    )
    args = parser.parse_args()

    if not args.csv_file.exists():
        raise FileNotFoundError(f"File not found: {args.csv_file}")

    with args.csv_file.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not reader.fieldnames:
        raise ValueError("The CSV file appears to have no header row.")

    required_columns = ["IAST", "Romanized Spelling"]
    for col in required_columns:
        if col not in reader.fieldnames:
            raise ValueError(
                f"Missing required column: {col!r}\n"
                f"Found columns: {reader.fieldnames}"
            )

    iast_duplicates = collect_duplicates(rows, "IAST", args.ignore_case)
    romanized_duplicates = collect_duplicates(rows, "Romanized Spelling", args.ignore_case)
    full_row_duplicates = collect_full_row_duplicates(rows, args.ignore_case)

    print_duplicates("Duplicate IAST entries", iast_duplicates, rows, "IAST")
    print_duplicates("Duplicate Romanized Spelling entries", romanized_duplicates, rows, "Romanized Spelling")
    print_duplicates("Exact duplicate full rows", full_row_duplicates)

    total_iast = len(iast_duplicates)
    total_romanized = len(romanized_duplicates)
    total_full_rows = len(full_row_duplicates)

    print("\nSummary")
    print("-------")
    print(f"Duplicate IAST values: {total_iast}")
    print(f"Duplicate Romanized values: {total_romanized}")
    print(f"Exact duplicate rows: {total_full_rows}")


if __name__ == "__main__":
    main()