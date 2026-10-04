"""Spec conformance: the public interfaces a task says must exist.

Many real issues (SWE-bench Pro among them) end with a "New Interfaces" section that
names the files, functions and classes hidden tests will import. Checking that those
names exist before accepting a submission costs one sandbox run, uses only what the
agent was already told, and catches the most common way a near-correct change fails.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^#{1,6}\s*.*interface", re.IGNORECASE)
_FIELD = re.compile(r"^\s*[-*]\s*(Path|Name|Type)\s*:\s*(.+?)\s*$", re.IGNORECASE)
MAX_INTERFACES = 40


@dataclass(frozen=True)
class Interface:
    path: str
    name: str
    kind: str

    @property
    def symbol(self) -> str:
        """The identifier to look for: `User.getLocalCoverPath` -> `getLocalCoverPath`."""
        return re.split(r"[.:#]", self.name)[-1].strip("()")


def parse_interfaces(text: str) -> list[Interface]:
    """Interfaces listed as `- Path: ...` / `- Name: ...` / `- Type: ...` under a heading
    mentioning interfaces. Anything else is ignored rather than guessed."""
    found: list[Interface] = []
    in_section = False
    path = name = kind = ""

    def flush() -> None:
        nonlocal name, kind
        if path and name and len(found) < MAX_INTERFACES:
            found.append(Interface(path=path, name=name, kind=kind.lower() or "unknown"))
        name = kind = ""

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            in_section = bool(_HEADING.match(line.strip()))
            continue
        if not in_section:
            continue
        match = _FIELD.match(line)
        if not match:
            continue
        field, value = match.group(1).lower(), match.group(2).strip().strip("`").strip()
        if field == "path":
            flush()
            path = value.lstrip("./")
        elif field == "name":
            if name:
                flush()
            name = value
        else:
            kind = value
    flush()
    return [i for i in found if re.fullmatch(r"[\w./-]+", i.path) and i.symbol]
