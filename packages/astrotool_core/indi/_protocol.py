"""Incremental parser for INDI's wire format — shared by the real client
(`client.py`) and the test double (`astrotool_core.testing.fake_indi_server`),
since both sides receive the same shape of message (a top-level element
with `device`/`name` attributes, containing named+valued child elements —
`defSwitchVector`/`setNumberVector`/... from a driver, `newSwitchVector`/
`newNumberVector`/`getProperties` from a client).

INDI's protocol is a raw stream of top-level XML elements with no
wrapping root — not well-formed XML as a whole, so a normal
`xml.etree.ElementTree`/`expat` parse of the whole stream in one pass is
not an option. The natural-looking alternative — feed a persistent
`expat` parser byte-by-byte across `recv()` calls — turns out not to
work either: `pyexpat.Parse(data, isfinal=False)` was found (empirically,
see the commit that added this file) to silently defer firing *any*
Start/EndElementHandler callback until its own internal buffer happens
to cross an implementation-specific size threshold, which for a single
short message fed in small increments can mean the callback never fires
at all before the *next* logically-separate element's bytes arrive and
corrupt the "one parser per top-level element" bookkeeping such an
approach would need.

So instead: this does its own light, hand-rolled scan to find each
complete top-level element's exact byte span (tracking `<`/`>` and quoted
attribute values only — no need to understand XML semantics beyond tag
boundaries), then hands *only* that self-contained, complete fragment to
a fresh `expat` parser in one call. A single complete `Parse(fragment,
True)` call was confirmed to fire reliably regardless of the fragment's
size, sidestepping the incremental-buffering behavior entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from xml.parsers import expat

_log = logging.getLogger(__name__)


def _byte_at(buf: bytearray, index: int) -> bytes:
    """A single byte at `index` as `bytes` (a `bytearray` slice is itself
    a `bytearray`, which compares equal to `bytes` at runtime but not
    under mypy's stricter type-overlap check)."""
    return bytes(buf[index : index + 1])


@dataclass
class ParsedElement:
    """One complete top-level INDI element, e.g. a `defNumberVector` or a
    client's `newSwitchVector` — attributes of the element itself, plus
    `{child_name: text}` for every child that had a `name` attribute
    (covers `oneSwitch`/`oneNumber`/`oneText`/`defSwitch`/`defNumber`/
    `defText` alike — the child *tag* name is never inspected, only its
    `name` attribute and text content)."""

    tag: str
    attrs: dict[str, str]
    children: dict[str, str] = field(default_factory=dict)


class IncrementalIndiParser:
    """Feed raw bytes in (as they arrive from `recv()`, any chunking);
    get one `ParsedElement` callback per complete top-level element found
    so far. Tolerant of a malformed/unparseable fragment: logs and drops
    just that one fragment rather than raising, since one garbled message
    should never take down the whole reader loop."""

    def __init__(self, on_element: Callable[[ParsedElement], None]) -> None:
        self._on_element = on_element
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        while True:
            fragment = self._extract_next_fragment()
            if fragment is None:
                return
            self._parse_fragment(fragment)

    def _extract_next_fragment(self) -> bytes | None:
        """Pop and return the next complete top-level element's exact
        bytes from the front of the buffer, or None if the buffer holds
        no complete element yet (wait for more data)."""
        buf = self._buffer
        start = buf.find(b"<")
        if start == -1:
            buf.clear()  # nothing but stray non-tag bytes buffered
            return None

        depth = 0
        i = start
        n = len(buf)
        while i < n:
            if _byte_at(buf, i) != b"<":
                i += 1
                continue
            scanned = self._scan_one_tag(buf, i + 1, n)
            if scanned is None:
                return None  # incomplete tag — wait for more data
            i, depth_delta = scanned
            depth += depth_delta
            if depth <= 0:
                fragment = bytes(buf[start:i])
                del buf[:i]
                return fragment
        return None  # incomplete element — wait for more data

    @staticmethod
    def _scan_one_tag(buf: bytearray, i: int, n: int) -> tuple[int, int] | None:
        """`i` points just past a tag's opening `<`. Returns (position
        just past the tag's `>`, net depth change: -1 close / 0
        self-close / +1 open), or None if the tag isn't complete yet."""
        if i < n and _byte_at(buf, i) in (b"?", b"!"):
            end = buf.find(b">", i)
            return None if end == -1 else (end + 1, 0)  # PI/comment/doctype

        is_close = i < n and _byte_at(buf, i) == b"/"
        if is_close:
            i += 1
        tag_end = IncrementalIndiParser._find_unquoted_gt(buf, i, n)
        if tag_end is None:
            return None
        is_self_close = _byte_at(buf, tag_end - 1) == b"/"
        depth_delta = -1 if is_close else (0 if is_self_close else 1)
        return tag_end + 1, depth_delta

    @staticmethod
    def _find_unquoted_gt(buf: bytearray, start: int, n: int) -> int | None:
        quote: bytes | None = None
        j = start
        while j < n:
            c = _byte_at(buf, j)
            if quote is not None:
                if c == quote:
                    quote = None
            elif c in (b'"', b"'"):
                quote = c
            elif c == b">":
                return j
            j += 1
        return None

    def _parse_fragment(self, fragment: bytes) -> None:
        current: ParsedElement | None = None
        current_child_name: str | None = None
        current_text: list[str] = []
        depth = 0

        def start_element(name: str, attrs: dict[str, str]) -> None:
            nonlocal current, current_child_name, current_text, depth
            depth += 1
            if depth == 1:
                current = ParsedElement(tag=name, attrs=dict(attrs))
            elif depth == 2 and current is not None:
                current_child_name = attrs.get("name")
                current_text = []

        def char_data(data: str) -> None:
            if current_child_name is not None:
                current_text.append(data)

        def end_element(_name: str) -> None:
            nonlocal current_child_name, current_text, depth
            depth -= 1
            if depth == 1 and current_child_name is not None:
                value = "".join(current_text).strip()
                if current is not None:
                    current.children[current_child_name] = value
                current_child_name = None
                current_text = []

        parser = expat.ParserCreate()
        parser.StartElementHandler = start_element
        parser.EndElementHandler = end_element
        parser.CharacterDataHandler = char_data
        try:
            parser.Parse(fragment, True)
        except expat.ExpatError:
            _log.warning("IncrementalIndiParser: dropping unparseable fragment", exc_info=True)
            return
        if current is not None:
            self._on_element(current)


def xml_escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
