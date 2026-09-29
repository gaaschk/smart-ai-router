#!/usr/bin/env python3
"""
Add a GPL-2.0 source header to every Python file in the project
that doesn't already carry one.

Header format (PEP 263 style, UTF-8, first line comment):

    # This file is part of smart-ai-router.
    # smart-ai-router is free software; you can redistribute it and/or
    # modify it under the terms of the GNU General Public License as
    # published by the Free Software Foundation; either version 2 of the
    # License, or (at your option) any later version.

Files that already contain a recognizable project/heritage header
("smart-ai-router", "GPL-2", "GNU General Public", or "Copyright") are
skipped.  This check is tolerant: a comment block that mentions the
project name is enough to avoid double-injection.

Usage:
    python add_headers.py [--dry-run] [--exclude=file1,file2]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent  # project root
TRIPLE = '#'
HEADER_LINES: List[str] = [
    '# This file is part of smart-ai-router.',
    '# smart-ai-router is free software; you can redistribute it and/or',
    '# modify it under the terms of the GNU General Public License as',
    '# published by the Free Software Foundation; either version 2 of the',
    '# License, or (at your option) any later version.',
    '',
]

# tolerant pre-existing-header detection: any line mentioning the project
# name together with 'GPL', 'Copyright', or 'smart-ai-router' (already
# covered by the name match) is considered already-present.
_HEADER_PAT = re.compile(r'(smart-ai-router|GPL|GNU General Public|Copyright)')


def _file_has_header(path: Path) -> bool:
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return True  # un-readable → skip
    # Heuristic: if any non-blank line within the first ~12 lines
    # references the project or a license, assume it's already there.
    for idx, line in enumerate(text.splitlines()[:12]):
        if idx >= 0 and line.strip() and _HEADER_PAT.search(line):
            return True
    return False


def _add_header(path: Path) -> bool:
    """Prepend header if needed, return True if changed."""
    if _file_has_header(path):
        return False
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return False
    if not text:
        # empty file: just write the header
        path.write_text('\n'.join(HEADER_LINES), encoding='utf-8')
        return True
    # ensure single blank line between header and original first line
    if not text.startswith('\n'):
        # prepend header + one trailing blank line
        new_text = '\n'.join(HEADER_LINES) + '\n' + text
    else:
        new_text = '\n'.join(HEADER_LINES) + '\n' + text.lstrip('\n')
    # only write if something actually changed
    if new_text == text:
        return False
    path.write_text(new_text, encoding='utf-8')
    return True


def collect_files(root: Path, exclude: List[str]) -> List[Path]:
    exclude_set = {Path(e).resolve() for e in exclude}
    files: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        # skip hidden dirs and venv artifacts
        if any(part.startswith('.') for part in Path(dirpath).parts):
            continue
        if '.venv' in Path(dirpath).parts or '__pycache__' in Path(dirpath).parts:
            continue
        for fn in filenames:
            if fn.endswith('.py'):
                p = Path(dirpath) / fn
                if p.resolve() in exclude_set:
                    continue
                files.append(p)
    return files


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would change without writing')
    parser.add_argument('--exclude', default='',
                        help='comma-separated list of file paths/pats to skip')
    args = parser.parse_args(argv)

    exclude_paths: List[str] = [s.strip() for s in args.exclude.split(',') if s.strip()]
    files = collect_files(ROOT, exclude_paths)

    to_skip: List[Path] = []
    to_change: List[Path] = []
    errors: List[tuple[Path, str]] = []

    for p in sorted(files):
        try:
            had = _file_has_header(p)
            if had:
                to_skip.append(p)
                continue
            if args.dry_run:
                to_change.append(p)
                continue
            changed = _add_header(p)
            if changed:
                to_change.append(p)
        except Exception as exc:
            errors.append((p, str(exc)))

    if errors:
        print('ERRORS', file=sys.stderr)
        for p, msg in errors:
            print(f'{p}: {msg}', file=sys.stderr)
        return 2

    if args.dry_run:
        print(f'would add headers to {len(to_change)} file(s); '
              f'{len(to_skip)} already have a header')
        for p in to_change:
            print(p)
        return 0

    print(f'added headers to {len(to_change)} file(s); '
          f'{len(to_skip)} skipped (already present)')
    for p in to_change:
        print(f'  {p}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
