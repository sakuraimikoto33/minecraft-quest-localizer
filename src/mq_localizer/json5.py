"""Strict parser for the small JSON5 subset used by FTB Quests language files.

FTB language documents are top-level objects whose values are strings or
lists of strings.  Keeping that restriction here prevents a permissive SNBT
parser from silently accepting malformed JSON5 and prevents unrelated quest
data from being mistaken for a translation table.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import TypeAlias
import unicodedata

__all__ = ["Json5LanguageValue", "Json5ParseError", "parse_json5_language"]


Json5LanguageValue: TypeAlias = str | list[str]


class Json5ParseError(ValueError):
    """A JSON5 syntax or language-file shape error with source location."""

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
            elif char in "\n\u2028\u2029":
                starts.append(index + 1)
            index += 1
        return starts

    def _line_column(self, offset: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def _error(self, message: str, offset: int | None = None) -> Json5ParseError:
        error_offset = self.pos if offset is None else offset
        line, column = self._line_column(error_offset)
        return Json5ParseError(
            message,
            offset=error_offset,
            line=line,
            column=column,
        )

    def parse_language(self) -> dict[str, Json5LanguageValue]:
        self._skip_trivia()
        if not self._consume("{"):
            raise self._error("JSON5 language file must be a top-level object")

        result: dict[str, Json5LanguageValue] = {}
        self._skip_trivia()
        if self._consume("}"):
            self._finish_document()
            return result

        while True:
            key_start = self.pos
            key = self._parse_key()
            if key in result:
                raise self._error(f"Duplicate language key {key!r}", key_start)

            self._skip_trivia()
            if not self._consume(":"):
                raise self._error("Expected ':' after object key")
            self._skip_trivia()
            result[key] = self._parse_language_value(key)

            self._skip_trivia()
            if self._consume(","):
                self._skip_trivia()
                if self._consume("}"):
                    break
                if self._peek(","):
                    raise self._error("Expected an object key after ','")
                continue
            if self._consume("}"):
                break
            if self.pos >= self.length:
                raise self._error("Unterminated object; expected '}'")
            raise self._error("Expected ',' or '}' after object value")

        self._finish_document()
        return result

    def _finish_document(self) -> None:
        self._skip_trivia()
        if self.pos != self.length:
            raise self._error("Unexpected trailing content")

    def _parse_key(self) -> str:
        if self.pos >= self.length:
            raise self._error("Expected an object key")
        if self.source[self.pos] in "\"'":
            return self._parse_string()
        return self._parse_identifier_name()

    def _parse_identifier_name(self) -> str:
        start = self.pos
        decoded: list[str] = []
        first = True

        while self.pos < self.length:
            char_start = self.pos
            if self.source.startswith("\\u", self.pos):
                self.pos += 2
                char = self._parse_fixed_hex_escape(4, "Unicode", char_start)
            else:
                char = self.source[self.pos]
                if not _is_identifier_part(char, first=first):
                    break
                self.pos += 1

            if not _is_identifier_part(char, first=first):
                position = "start" if first else "part"
                raise self._error(
                    f"Invalid character in IdentifierName {position}",
                    char_start,
                )
            decoded.append(char)
            first = False

        if first:
            raise self._error("Expected a quoted key or IdentifierName", start)
        return "".join(decoded)

    def _parse_language_value(self, key: str) -> Json5LanguageValue:
        if self.pos >= self.length:
            raise self._error(f"Expected a string or list of strings for {key!r}")
        if self.source[self.pos] in "\"'":
            return self._parse_string()
        if self._consume("["):
            return self._parse_string_list(key)
        raise self._error(f"Language value {key!r} must be a string or list of strings")

    def _parse_string_list(self, key: str) -> list[str]:
        values: list[str] = []
        self._skip_trivia()
        if self._consume("]"):
            return values

        while True:
            if self.pos >= self.length or self.source[self.pos] not in "\"'":
                raise self._error(f"Language list {key!r} may contain only strings")
            values.append(self._parse_string())

            self._skip_trivia()
            if self._consume(","):
                self._skip_trivia()
                if self._consume("]"):
                    return values
                if self._peek(","):
                    raise self._error("Expected a string after ','")
                continue
            if self._consume("]"):
                return values
            if self.pos >= self.length:
                raise self._error("Unterminated list; expected ']'")
            raise self._error("Expected ',' or ']' after list value")

    def _parse_string(self) -> str:
        start = self.pos
        quote = self.source[self.pos]
        self.pos += 1
        decoded: list[str] = []
        simple_escapes = {
            "'": "'",
            '"': '"',
            "\\": "\\",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "v": "\v",
        }

        while self.pos < self.length:
            char = self.source[self.pos]
            if char == quote:
                self.pos += 1
                return "".join(decoded)
            if char in "\r\n\u2028\u2029":
                raise self._error("Unescaped line terminator in string", self.pos)
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
            elif escape == "0":
                if self.pos < self.length and self.source[self.pos] in "0123456789":
                    raise self._error("Legacy octal escape is not valid JSON5", escape_start)
                decoded.append("\0")
            elif escape == "x":
                decoded.append(self._parse_fixed_hex_escape(2, "hex", escape_start))
            elif escape == "u":
                decoded.append(self._parse_unicode_escape(escape_start))
            elif escape == "\r":
                if self.pos < self.length and self.source[self.pos] == "\n":
                    self.pos += 1
            elif escape in "\n\u2028\u2029":
                pass
            elif escape in "0123456789":
                raise self._error("Numeric escape is not valid JSON5", escape_start)
            else:
                # JSON5 permits a NonEscapeCharacter (for example, ``\q``),
                # which evaluates to that character without the backslash.
                decoded.append(escape)

        raise self._error("Unterminated string", start)

    def _parse_unicode_escape(self, escape_start: int) -> str:
        high = self._parse_fixed_hex_code_unit(4, "Unicode", escape_start)
        if 0xD800 <= high <= 0xDBFF and self.source.startswith("\\u", self.pos):
            digits = self.source[self.pos + 2 : self.pos + 6]
            if len(digits) == 4 and all(char in _HEX_DIGITS for char in digits):
                low = int(digits, 16)
                if 0xDC00 <= low <= 0xDFFF:
                    self.pos += 6
                    codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
                    return chr(codepoint)
            # A following escape is parsed normally if it is not a low
            # surrogate; retaining the high code unit matches JSON semantics.
        return chr(high)

    def _parse_fixed_hex_escape(self, count: int, name: str, start: int) -> str:
        return chr(self._parse_fixed_hex_code_unit(count, name, start))

    def _parse_fixed_hex_code_unit(self, count: int, name: str, start: int) -> int:
        end = self.pos + count
        digits = self.source[self.pos : end]
        if len(digits) != count:
            raise self._error(f"Incomplete {name} escape", start)
        if any(char not in _HEX_DIGITS for char in digits):
            raise self._error(f"Invalid {name} escape", start)
        self.pos = end
        return int(digits, 16)

    def _skip_trivia(self) -> None:
        while self.pos < self.length:
            char = self.source[self.pos]
            if _is_json5_whitespace(char):
                self.pos += 1
                continue
            if char == "#":
                self._skip_line_comment(1)
                continue
            if self._peek("//"):
                self._skip_line_comment(2)
                continue
            if self._peek("/*"):
                comment_start = self.pos
                end = self.source.find("*/", self.pos + 2)
                if end < 0:
                    raise self._error("Unterminated block comment", comment_start)
                self.pos = end + 2
                continue
            break

    def _skip_line_comment(self, marker_length: int) -> None:
        self.pos += marker_length
        while self.pos < self.length and self.source[self.pos] not in "\r\n\u2028\u2029":
            self.pos += 1

    def _peek(self, expected: str) -> bool:
        return self.source.startswith(expected, self.pos)

    def _consume(self, expected: str) -> bool:
        if self._peek(expected):
            self.pos += len(expected)
            return True
        return False


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_IDENTIFIER_START_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Nl"})
_IDENTIFIER_PART_CATEGORIES = _IDENTIFIER_START_CATEGORIES | frozenset(
    {"Mn", "Mc", "Nd", "Pc"}
)


def _is_identifier_part(char: str, *, first: bool) -> bool:
    if char in "$_":
        return True
    category = unicodedata.category(char)
    if category in _IDENTIFIER_START_CATEGORIES:
        return True
    if not first and (category in _IDENTIFIER_PART_CATEGORIES or char in "\u200c\u200d"):
        return True
    return False


def _is_json5_whitespace(char: str) -> bool:
    return (
        char in "\t\v\f\r\n\u2028\u2029\u00a0\ufeff"
        or char == " "
        or unicodedata.category(char) == "Zs"
    )


def parse_json5_language(source: str) -> dict[str, Json5LanguageValue]:
    """Parse one FTB split-JSON5 language document.

    The accepted document is a top-level object with unique quoted or JSON5
    ``IdentifierName`` keys.  Each value must be a string or list of strings.
    Commas are mandatory, while trailing commas and ``//``, ``#`` and
    ``/* ... */`` comments are accepted.
    """

    if not isinstance(source, str):
        raise TypeError("source must be a string")
    return _Parser(source).parse_language()
