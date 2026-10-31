#!/usr/bin/env python3
"""
Edit single options in config.ini as text, so comments, blank lines and the
order of everything else survive.

ConfigParser.write() rebuilds the whole file and drops every comment, so code
that changes one or two values at runtime (scheduled-message send counts, web
viewer settings) uses these helpers instead.
"""

import os
import re
import tempfile

_SECTION_RE = re.compile(r'^\s*\[([^\]]+)\]')
_OPTION_RE = re.compile(r'^([^\s#;=:][^=:]*?)\s*[=:]')


def _norm(key: str) -> str:
    """Option names as ConfigParser compares them: lower case, spaces collapsed."""
    return ' '.join(key.split()).lower()


def _locate(lines: list[str], section: str) -> tuple[int, int] | None:
    """(header index, end index) of a section; end is the next header or EOF."""
    start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m:
            continue
        if start is not None:
            return start, i
        if m.group(1).strip() == section:
            start = i
    return (start, len(lines)) if start is not None else None


def _option_span(lines: list[str], start: int, end: int, key: str) -> tuple[int, int] | None:
    """Line range of an option within a section, including indented continuation lines."""
    want = _norm(key)
    for i in range(start + 1, end):
        m = _OPTION_RE.match(lines[i])
        if m and _norm(m.group(1)) == want:
            j = i + 1
            while j < end and lines[j].strip() and lines[j][:1] in (' ', '\t'):
                j += 1
            return i, j
    return None


def _read(path: str) -> list[str]:
    with open(path, encoding='utf-8') as f:
        return f.read().splitlines(keepends=True)


def _write(path: str, lines: list[str]) -> None:
    """Replace the file atomically, keeping its permissions."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.config-', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _newline(lines: list[str]) -> str:
    return '\r\n' if lines and lines[0].endswith('\r\n') else '\n'


def set_options(path: str, section: str, values: dict[str, str]) -> None:
    """Set options in a section, editing their lines in place.

    An existing option keeps its position (and the name as written); a new
    one goes after the section's last option; a missing section is appended.
    """
    lines = _read(path)
    nl = _newline(lines)
    for key, value in values.items():
        loc = _locate(lines, section)
        if loc is None:
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines[-1] += nl
            if lines and lines[-1].strip():
                lines.append(nl)
            lines += [f'[{section}]{nl}', f'{key} = {value}{nl}']
            continue
        start, end = loc
        span = _option_span(lines, start, end, key)
        if span is not None:
            i, j = span
            name = _OPTION_RE.match(lines[i]).group(1)
            lines[i:j] = [f'{name} = {value}{nl}']
            continue
        # After the last option (or its continuation lines), else right after the header.
        insert = start + 1
        for i in range(start + 1, end):
            if _OPTION_RE.match(lines[i]) or (lines[i].strip() and lines[i][:1] in (' ', '\t')):
                insert = i + 1
        if insert > 0 and not lines[insert - 1].endswith(('\n', '\r')):
            lines[insert - 1] += nl
        lines.insert(insert, f'{key} = {value}{nl}')
    _write(path, lines)


def remove_option(path: str, section: str, key: str) -> bool:
    """Delete one option's line(s). Returns False when it wasn't there."""
    lines = _read(path)
    loc = _locate(lines, section)
    if loc is None:
        return False
    span = _option_span(lines, *loc, key)
    if span is None:
        return False
    del lines[span[0]:span[1]]
    _write(path, lines)
    return True
