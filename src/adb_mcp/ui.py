"""Parse `uiautomator dump` XML into a compact element list."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass
class Element:
    index: int
    cls: str
    text: str
    desc: str
    rid: str
    package: str
    bounds: tuple[int, int, int, int]
    flags: list[str]
    label: str = ""  # text gathered from children, for clickable containers with none of their own

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def short_id(self) -> str:
        return self.rid.split(":id/", 1)[-1]

    @property
    def interactive(self) -> bool:
        return any(f in self.flags for f in ("clickable", "long-clickable", "checkable", "scrollable", "editable"))

    def line(self) -> str:
        parts = [f"[{self.index}]", self.cls.rsplit(".", 1)[-1]]
        if self.text:
            parts.append(_quote(self.text))
        if self.desc:
            parts.append(f"desc={_quote(self.desc)}")
        if self.label:
            parts.append(f"label={_quote(self.label)}")
        if self.rid:
            parts.append(f"id={self.short_id}")
        parts.append("@(%d,%d)" % self.center)
        if self.flags:
            parts.append(",".join(self.flags))
        return " ".join(parts)


def _quote(s: str, limit: int = 120) -> str:
    s = s.replace("\n", "\\n")
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return '"' + s.replace('"', '\\"') + '"'


def extract_xml(output: str) -> str | None:
    """Cut the hierarchy document out of uiautomator's stdout noise."""
    start = output.find("<?xml")
    if start < 0:
        start = output.find("<hierarchy")
    end = output.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return None
    return output[start : end + len("</hierarchy>")]


def _flags(a: dict[str, str]) -> list[str]:
    flags = []
    if a.get("clickable") == "true":
        flags.append("clickable")
    if a.get("long-clickable") == "true":
        flags.append("long-clickable")
    if a.get("class", "").endswith("EditText"):
        flags.append("editable")
    if a.get("scrollable") == "true":
        flags.append("scrollable")
    if a.get("checkable") == "true":
        flags.append("checkable")
        flags.append("checked" if a.get("checked") == "true" else "unchecked")
    if a.get("selected") == "true":
        flags.append("selected")
    if a.get("focused") == "true":
        flags.append("focused")
    if a.get("enabled") == "false":
        flags.append("disabled")
    if a.get("password") == "true":
        flags.append("password")
    return flags


def _descendant_label(node: ET.Element) -> str:
    """Texts beneath a node, e.g. the title and summary inside a clickable list row."""
    seen: list[str] = []
    for child in node.iter("node"):
        if child is node:
            continue
        t = child.get("text") or child.get("content-desc") or ""
        if t and t not in seen:
            seen.append(t)
        if len(seen) == 3:
            break
    return " | ".join(seen)


def parse(xml: str) -> list[Element]:
    """Return the on-screen nodes worth showing: anything labelled or interactive.

    Containers that carry nothing but a resource id are dropped as noise.
    """
    root = ET.fromstring(xml)
    elements: list[Element] = []
    for node in root.iter("node"):
        a = node.attrib
        m = _BOUNDS.fullmatch(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        flags = _flags(a)
        text, desc, rid = a.get("text", ""), a.get("content-desc", ""), a.get("resource-id", "")
        has_children = len(node) > 0
        if not (text or desc or flags or (rid and not has_children)):
            continue
        label = ""
        if not (text or desc) and has_children and ("clickable" in flags or "checkable" in flags):
            label = _descendant_label(node)
        elements.append(
            Element(len(elements), a.get("class", ""), text, desc, rid, a.get("package", ""), (x1, y1, x2, y2), flags, label)
        )
    return elements


def find(
    elements: list[Element],
    text: str | None = None,
    resource_id: str | None = None,
    content_desc: str | None = None,
) -> list[Element]:
    """Match elements on all given criteria. Exact (case-insensitive) matches win over substring ones."""

    def matches(e: Element, exact: bool) -> bool:
        def cmp(needle: str | None, hay: str) -> bool:
            if needle is None:
                return True
            n, h = needle.casefold(), hay.casefold()
            return n == h if exact else n in h

        if resource_id is not None:
            if not (e.rid == resource_id or e.short_id == resource_id):
                return False
        return cmp(text, e.text) and cmp(content_desc, e.desc)

    exact = [e for e in elements if matches(e, True)]
    return exact or [e for e in elements if matches(e, False)]
