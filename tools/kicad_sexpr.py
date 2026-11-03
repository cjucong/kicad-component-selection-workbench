"""KiCad S-expression parser retaining source offsets for targeted edits.

Extracted from the engineering FPGA utility; has no project or UI dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

_SKIP_TOKEN = re.compile(r'\s*(?:(\()|(\))|("(?:\\[\s\S]|[^"\\])*")|([^\s()]+))')


@dataclass
class Node:
    value: str | None
    start: int
    end: int
    children: list["Node"] = field(default_factory=list)

    @property
    def head(self) -> str:
        return self.children[0].value if self.children and self.children[0].value else ""

    def child(self, head: str) -> Node | None:
        return next((part for part in self.children if part.head == head), None)

    def children_named(self, head: str) -> list[Node]:
        return [part for part in self.children if part.head == head]

    def arg(self, index: int = 1) -> str:
        return (self.children[index].value or "") if index < len(self.children) else ""


def parse_sexpr(source: str, expected_root: str = "kicad_sch", *, root_children=None) -> Node:
    length = len(source)
    index = 0

    def skip_expression():
        # Validate discarded trees without allocating nodes for copper fills.
        nonlocal index
        depth = 0
        while True:
            token = _SKIP_TOKEN.match(source, index)
            if token is None:
                raise ValueError("Unclosed KiCad expression")
            index = token.end()
            if token[1]:
                depth += 1
            elif token[2]:
                depth -= 1
            elif token[4] and token[4].startswith('"'):
                raise ValueError("Unclosed KiCad string")
            if depth == 0:
                return

    def parse(*, root=False) -> Node:
        nonlocal index
        while index < length and source[index].isspace():
            index += 1
        if index >= length:
            raise ValueError("Unexpected end of schematic")
        start = index
        if source[index] == "(":
            index += 1
            children: list[Node] = []
            while True:
                while index < length and source[index].isspace():
                    index += 1
                if index >= length:
                    raise ValueError("Unclosed KiCad expression")
                if source[index] == ")":
                    index += 1
                    return Node(None, start, index, children)
                if root and root_children is not None and children and source[index] == '(':
                    head = re.match(r'\(\s*([^\s()"]+)', source[index:index + 128])
                    if head and head[1] not in root_children:
                        skip_expression()
                        continue
                children.append(parse())
        if source[index] == '"':
            index += 1
            chars: list[str] = []
            while index < length:
                char = source[index]
                if char == '"':
                    index += 1
                    return Node("".join(chars), start, index)
                if char == "\\" and index + 1 < length:
                    index += 1
                    escape = source[index]
                    chars.append({"n": "\n", "r": "\r", "t": "\t"}.get(escape, escape))
                else:
                    chars.append(char)
                index += 1
            raise ValueError("Unclosed KiCad string")
        while index < length and not source[index].isspace() and source[index] not in "()":
            index += 1
        if index == start:
            raise ValueError(f"Unexpected character at offset {index}")
        return Node(source[start:index], start, index)

    result = parse(root=True)
    if result.head != expected_root:
        raise ValueError(f"Not a KiCad {expected_root} expression")
    if source[index:].strip():
        raise ValueError("Unexpected data after KiCad expression")
    return result
