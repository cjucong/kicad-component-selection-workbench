"""KiCad S-expression parser retaining source offsets for targeted edits.

Extracted from the engineering FPGA utility; has no project or UI dependency.
"""
from __future__ import annotations

from dataclasses import dataclass, field


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


def parse_sexpr(source: str, expected_root: str = "kicad_sch") -> Node:
    length = len(source)
    index = 0

    def parse() -> Node:
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

    result = parse()
    if result.head != expected_root:
        raise ValueError(f"Not a KiCad {expected_root} expression")
    if source[index:].strip():
        raise ValueError("Unexpected data after KiCad expression")
    return result
