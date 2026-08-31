"""A small, dependency-free parser for the SNBT used by FTB configuration.

The parser intentionally keeps bare scalar values as text.  That makes it
lossless for the parts of SNBT a localizer needs (numeric suffixes, booleans,
resource locations, and other unquoted values) without trying to duplicate
Minecraft's complete NBT type system.

Every syntax node has a :class:`SourceSpan`.  Offsets are zero-based and the
end offset is exclusive; line and column numbers are one-based.  Callers can
therefore recover the exact source spelling with ``source[node.span.start:
node.span.end]`` even though quoted string values are decoded.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

__all__ = [
    "LangValue",
    "SnbtCompound",
    "SnbtEntry",
    "SnbtList",
    "SnbtNode",
    "SnbtParseError",
    "SnbtScalar",
    "SnbtString",
    "SourceSpan",
    "dump_lang_snbt",
    "parse_lang_snbt",
    "parse_snbt",
]


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """The exact location of a syntax element in its source string."""

    start: int
    end: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @property
    def line(self) -> int:
        """Alias for ``start_line`` for concise diagnostic code."""

        return self.start_line

    @property
    def column(self) -> int:
        """Alias for ``start_column`` for concise diagnostic code."""

        return self.start_column

    def extract(self, source: str) -> str:
        """Return the exact source text covered by this span."""

        return source[self.start : self.end]


@dataclass(frozen=True, slots=True)
class SnbtNode:
    """Base class for parsed SNBT values."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SnbtString(SnbtNode):
    """A decoded single- or double-quoted string."""

    value: str
    quote: str


@dataclass(frozen=True, slots=True)
class SnbtScalar(SnbtNode):
    """An unquoted scalar, retained exactly as source text."""

    value: str


@dataclass(frozen=True, slots=True)
class SnbtList(SnbtNode):
    """An SNBT list."""

    items: tuple[SnbtNode, ...]

    def __iter__(self) -> Iterator[SnbtNode]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> SnbtNode:
        return self.items[index]


@dataclass(frozen=True, slots=True)
class SnbtEntry:
    """A compound key/value pair, including key and complete entry spans."""

    key: str
    key_span: SourceSpan
    value: SnbtNode
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SnbtCompound(SnbtNode):
    """An ordered SNBT compound.

    ``entries`` is a tuple instead of a dict so the generic parser does not
    silently discard duplicate keys.  Language-file validation rejects such
    duplicates explicitly.
    """

    entries: tuple[SnbtEntry, ...]

    def __iter__(self) -> Iterator[SnbtEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, key: str) -> SnbtNode:
        for entry in self.entries:
            if entry.key == key:
                return entry.value
        raise KeyError(key)

    def items(self) -> Iterator[tuple[str, SnbtNode]]:
        for entry in self.entries:
            yield entry.key, entry.value


LangValue: TypeAlias = str | list[str]


class SnbtParseError(ValueError):
    """An SNBT syntax or language-file shape error with source location."""

    def __init__(
        self,
        message: str,
        *,
        offset: int,
        line: int,
        column: int,
    ) -> None:
        self.message = message
        self.offset = offset
        self.line = line
        self.column = column
        super().__init__(f"{message} at line {line}, column {column}")


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.length = len(source)
        self.pos = 0
        self._line_starts = self._find_line_starts(source)

    @staticmethod
    def _find_line_starts(source: str) -> list[int]:
        starts = [0]
        index = 0
        while index < len(source):
            char = source[index]
            if char == "\r":
                if index + 1 < len(source) and source[index + 1] == "\n":
                    index += 1
                starts.append(index + 1)
            elif char == "\n":
                starts.append(index + 1)
            index += 1
        return starts

    def _line_column(self, offset: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def _span(self, start: int, end: int) -> SourceSpan:
        start_line, start_column = self._line_column(start)
        end_line, end_column = self._line_column(end)
        return SourceSpan(
            start=start,
            end=end,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
        )

    def _error(self, message: str, offset: int | None = None) -> SnbtParseError:
        error_offset = self.pos if offset is None else offset
        line, column = self._line_column(error_offset)
        return SnbtParseError(
            message,
            offset=error_offset,
            line=line,
            column=column,
        )

    def parse(self) -> SnbtNode:
        self._skip_trivia()
        if self.pos >= self.length:
            raise self._error("Expected an SNBT value")
        value = self._parse_value()
        self._skip_trivia()
        if self.pos != self.length:
            raise self._error("Unexpected trailing content")
        return value

    def _peek(self, expected: str) -> bool:
        return self.source.startswith(expected, self.pos)

    def _consume(self, expected: str) -> bool:
        if self._peek(expected):
            self.pos += len(expected)
            return True
        return False

    def _expect(self, expected: str) -> None:
        if not self._consume(expected):
            raise self._error(f"Expected {expected!r}")

    def _skip_trivia(self) -> bool:
        """Skip whitespace and FTB-style line comments.

        ``#`` and ``//`` are recognized while trivia is expected, which keeps
        comment markers inside quoted strings and bare values such as URLs
        intact.
        """

        start = self.pos
        while self.pos < self.length:
            if self.source[self.pos].isspace():
                self.pos += 1
                continue
            if self.source[self.pos] == "#":
                self._skip_line_comment(1)
                continue
            if self.source.startswith("//", self.pos):
                self._skip_line_comment(2)
                continue
            if self.source.startswith("/*", self.pos):
                comment_start = self.pos
                comment_end = self.source.find("*/", self.pos + 2)
                if comment_end < 0:
                    raise self._error("Unterminated block comment", comment_start)
                self.pos = comment_end + 2
                continue
            break
        return self.pos != start

    def _skip_line_comment(self, marker_length: int) -> None:
        self.pos += marker_length
        while self.pos < self.length and self.source[self.pos] not in "\r\n":
            self.pos += 1

    def _parse_value(self) -> SnbtNode:
        if self.pos >= self.length:
            raise self._error("Expected an SNBT value")
        char = self.source[self.pos]
        if char == "{":
            return self._parse_compound()
        if char == "[":
            return self._parse_list()
        if char in "\"'":
            return self._parse_quoted_string()
        return self._parse_scalar()

    def _parse_compound(self) -> SnbtCompound:
        start = self.pos
        self._expect("{")
        self._skip_trivia()
        entries: list[SnbtEntry] = []

        if self._consume("}"):
            return SnbtCompound(span=self._span(start, self.pos), entries=())

        while True:
            entry_start = self.pos
            key, key_span = self._parse_key()
            self._skip_trivia()
            self._expect(":")
            self._skip_trivia()
            value = self._parse_value()
            entries.append(
                SnbtEntry(
                    key=key,
                    key_span=key_span,
                    value=value,
                    span=self._span(entry_start, value.span.end),
                )
            )

            trivia_start = self.pos
            had_trivia = self._skip_trivia()
            if self._consume(","):
                self._skip_trivia()
                if self._consume("}"):
                    break
                if self._peek(","):
                    raise self._error("Expected a compound key after ','")
                continue
            if self._consume("}"):
                break
            if self.pos >= self.length:
                raise self._error("Unterminated compound; expected '}'")
            if not had_trivia and self.pos == trivia_start:
                raise self._error("Expected ',', whitespace, or '}' after value")

        return SnbtCompound(
            span=self._span(start, self.pos),
            entries=tuple(entries),
        )

    def _parse_key(self) -> tuple[str, SourceSpan]:
        if self.pos >= self.length:
            raise self._error("Expected a compound key")
        if self.source[self.pos] in "\"'":
            string = self._parse_quoted_string()
            return string.value, string.span

        start = self.pos
        while self.pos < self.length:
            char = self.source[self.pos]
            if char.isspace() or char in "{}[],:":
                break
            self.pos += 1
        if self.pos == start:
            raise self._error("Expected a compound key")
        return self.source[start : self.pos], self._span(start, self.pos)

    def _parse_list(self) -> SnbtList:
        start = self.pos
        self._expect("[")
        self._skip_trivia()
        items: list[SnbtNode] = []

        if self._consume("]"):
            return SnbtList(span=self._span(start, self.pos), items=())

        while True:
            items.append(self._parse_value())
            trivia_start = self.pos
            had_trivia = self._skip_trivia()
            if self._consume(","):
                self._skip_trivia()
                if self._consume("]"):
                    break
                if self._peek(","):
                    raise self._error("Expected a list value after ','")
                continue
            if self._consume("]"):
                break
            if self.pos >= self.length:
                raise self._error("Unterminated list; expected ']'")
            if not had_trivia and self.pos == trivia_start:
                raise self._error("Expected ',', whitespace, or ']' after value")

        return SnbtList(span=self._span(start, self.pos), items=tuple(items))

    def _parse_quoted_string(self) -> SnbtString:
        start = self.pos
        quote = self.source[self.pos]
        self.pos += 1
        decoded: list[str] = []

        simple_escapes = {
            "\"": "\"",
            "'": "'",
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }

        while self.pos < self.length:
            char = self.source[self.pos]
            if char == quote:
                self.pos += 1
                return SnbtString(
                    span=self._span(start, self.pos),
                    value="".join(decoded),
                    quote=quote,
                )
            if char != "\\":
                decoded.append(char)
                self.pos += 1
                continue

            escape_start = self.pos
            self.pos += 1
            if self.pos >= self.length:
                raise self._error("Unterminated escape sequence", escape_start)
            escape = self.source[self.pos]
            self.pos += 1
            if escape in simple_escapes:
                decoded.append(simple_escapes[escape])
            elif escape == "u":
                decoded.append(self._parse_unicode_escape(escape_start))
            else:
                raise self._error(f"Unsupported escape sequence \\{escape}", escape_start)

        raise self._error("Unterminated quoted string", start)

    def _parse_unicode_escape(self, escape_start: int) -> str:
        if self.pos + 4 > self.length:
            raise self._error("Incomplete Unicode escape", escape_start)
        digits = self.source[self.pos : self.pos + 4]
        if any(char not in "0123456789abcdefABCDEF" for char in digits):
            raise self._error("Invalid Unicode escape", escape_start)
        self.pos += 4
        codepoint = int(digits, 16)

        # Decode a JSON-style surrogate pair when present.  Lone surrogate
        # escapes are retained as their code unit so parsing remains lossless.
        if 0xD800 <= codepoint <= 0xDBFF and self.source.startswith("\\u", self.pos):
            low_digits = self.source[self.pos + 2 : self.pos + 6]
            if len(low_digits) == 4 and all(
                char in "0123456789abcdefABCDEF" for char in low_digits
            ):
                low = int(low_digits, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    self.pos += 6
                    codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
        return chr(codepoint)

    def _parse_scalar(self) -> SnbtScalar:
        start = self.pos
        while self.pos < self.length:
            char = self.source[self.pos]
            if char.isspace() or char in "{}[],,":
                break
            self.pos += 1
        if self.pos == start:
            raise self._error("Expected an SNBT value")
        return SnbtScalar(
            span=self._span(start, self.pos),
            value=self.source[start : self.pos],
        )


def parse_snbt(source: str) -> SnbtNode:
    """Parse one complete SNBT value and return its source-spanned AST."""

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    return _Parser(source).parse()


def parse_lang_snbt(source: str) -> dict[str, LangValue]:
    """Parse an FTB ``lang/<locale>.snbt`` document.

    The top level must be a compound.  Its values may be quoted or bare
    strings, or lists containing only those string forms.  Duplicate keys are
    rejected to avoid silently losing a translation.
    """

    root = parse_snbt(source)
    if not isinstance(root, SnbtCompound):
        raise _shape_error("Language SNBT must be a top-level compound", root.span)

    result: dict[str, LangValue] = {}
    for entry in root.entries:
        if entry.key in result:
            raise _shape_error(
                f"Duplicate language key {entry.key!r}",
                entry.key_span,
            )
        if isinstance(entry.value, (SnbtString, SnbtScalar)):
            result[entry.key] = entry.value.value
            continue
        if isinstance(entry.value, SnbtList):
            values: list[str] = []
            for item in entry.value.items:
                if not isinstance(item, (SnbtString, SnbtScalar)):
                    raise _shape_error(
                        f"Language list {entry.key!r} may contain only strings",
                        item.span,
                    )
                values.append(item.value)
            result[entry.key] = values
            continue
        raise _shape_error(
            f"Language value {entry.key!r} must be a string or list of strings",
            entry.value.span,
        )
    return result


def _shape_error(message: str, span: SourceSpan) -> SnbtParseError:
    return SnbtParseError(
        message,
        offset=span.start,
        line=span.start_line,
        column=span.start_column,
    )


def dump_lang_snbt(values: Mapping[str, str | Sequence[str]]) -> str:
    """Serialize language values as deterministic, canonical SNBT.

    Mapping iteration order is preserved.  Keys and values are always quoted,
    and commas are emitted for compatibility with strict SNBT readers.
    """

    if not isinstance(values, Mapping):
        raise TypeError("values must be a mapping")

    prepared: list[tuple[str, str | list[str]]] = []
    for key, value in values.items():
        if not isinstance(key, str):
            raise TypeError("language keys must be strings")
        if isinstance(value, str):
            prepared.append((key, value))
            continue
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            raise TypeError(f"language value for {key!r} must be a string or sequence of strings")
        items = list(value)
        if not all(isinstance(item, str) for item in items):
            raise TypeError(f"language list {key!r} may contain only strings")
        prepared.append((key, items))

    lines = ["{"]
    for entry_index, (key, value) in enumerate(prepared):
        entry_suffix = "," if entry_index + 1 < len(prepared) else ""
        quoted_key = _quote_string(key)
        if isinstance(value, str):
            lines.append(f"  {quoted_key}: {_quote_string(value)}{entry_suffix}")
        elif not value:
            lines.append(f"  {quoted_key}: []{entry_suffix}")
        else:
            lines.append(f"  {quoted_key}: [")
            for item_index, item in enumerate(value):
                item_suffix = "," if item_index + 1 < len(value) else ""
                lines.append(f"    {_quote_string(item)}{item_suffix}")
            lines.append(f"  ]{entry_suffix}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _quote_string(value: str) -> str:
    escaped: list[str] = ['"']
    replacements = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for char in value:
        replacement = replacements.get(char)
        if replacement is not None:
            escaped.append(replacement)
        elif ord(char) < 0x20:
            escaped.append(f"\\u{ord(char):04x}")
        elif 0xD800 <= ord(char) <= 0xDFFF:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    escaped.append('"')
    return "".join(escaped)
