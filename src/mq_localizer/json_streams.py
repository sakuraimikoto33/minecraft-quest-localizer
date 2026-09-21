"""Local layout plans for a conservative subset of raw JSON text arrays.

Only ordinary string gaps are translated together. Literal styled components
stay in their original slots and are represented by protected template tokens;
the provider never receives authority to create or edit JSON metadata.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .domain import TranslationError
from .protection import protected_syntax_ranges


_MARKER = re.compile(r"\{MQ_JSON_COMPONENT_[0-9A-F]{4}\}")
_STYLE_FIELDS = frozenset(
    {"text", "color", "bold", "italic", "underlined", "strikethrough", "obfuscated"}
)


@dataclass(frozen=True, slots=True)
class FlatJsonStreamPlan:
    skeleton: str
    markers: tuple[str, ...]
    node_indices: tuple[int, ...]
    node_groups: tuple[tuple[int, ...], ...]
    gap_indices: tuple[tuple[int, ...], ...]
    source_texts: tuple[str, ...]

    @classmethod
    def create(cls, value: Any) -> FlatJsonStreamPlan | None:
        # An initial string keeps the array's inherited root style ordinary.
        # Dynamic, nested and interactive components retain the existing,
        # conservative per-leaf path, including their independent surfaces.
        if not isinstance(value, list) or not value or not isinstance(value[0], str):
            return None
        texts: list[str] = []
        markers: list[str] = []
        nodes: list[int] = []
        groups: list[list[int]] = []
        gaps: list[list[int]] = [[]]
        chunks: list[str] = []
        for index, item in enumerate(value):
            if isinstance(item, str):
                text = item
                gaps[-1].append(index)
                chunks.append(text)
            elif (
                isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and set(item) <= _STYLE_FIELDS
                and all(
                    isinstance(setting, str) if field in {"text", "color"}
                    else isinstance(setting, bool)
                    for field, setting in item.items()
                )
            ):
                text = item["text"]
                # No string slot exists between adjacent objects. Expose them
                # as one indivisible marker, so the provider has no nonexistent
                # gap to fill. Individual node text and styles remain separate.
                if nodes and nodes[-1] == index - 1:
                    groups[-1].append(index)
                else:
                    if len(groups) > 0xFFFF:
                        return None
                    marker = f"{{MQ_JSON_COMPONENT_{len(groups):04X}}}"
                    groups.append([index])
                    markers.append(marker)
                    chunks.append(marker)
                    gaps.append([])
                nodes.append(index)
            else:
                return None
            # Legacy formatting, layout tokens and technical placeholders must
            # not acquire new boundaries as prose moves between JSON slots.
            if protected_syntax_ranges(text):
                return None
            texts.append(text)
        if not nodes or not any(
            character.isalnum() for item in value if isinstance(item, str)
            for character in item
        ):
            return None
        return cls(
            "".join(chunks), tuple(markers), tuple(nodes),
            tuple(tuple(group) for group in groups),
            tuple(tuple(gap) for gap in gaps), tuple(texts),
        )

    @property
    def node_markers(self) -> dict[int, str]:
        return {
            index: marker if offset == 0 else ""
            for marker, group in zip(self.markers, self.node_groups, strict=True)
            for offset, index in enumerate(group)
        }

    def validate_skeleton(self, candidate: str) -> tuple[str, ...]:
        matches = tuple(_MARKER.finditer(candidate))
        if tuple(match.group() for match in matches) != self.markers:
            raise TranslationError("JSON装飾コンポーネントの種類・個数・順序が変わりました")
        segments: list[str] = []
        position = 0
        for match in matches:
            segments.append(candidate[position:match.start()])
            position = match.end()
        segments.append(candidate[position:])
        if any(segment and not indices for segment, indices in zip(
            segments, self.gap_indices, strict=True,
        )):
            raise TranslationError("JSONの本文を書き込める位置がない装飾間に文字が追加されました")
        return tuple(segments)

    def render(
        self, template: list[Any], skeleton: str, node_texts: Mapping[int, str],
    ) -> list[Any]:
        segments = self.validate_skeleton(skeleton)
        rendered = copy.deepcopy(template)
        for segment, indices in zip(segments, self.gap_indices, strict=True):
            for offset, index in enumerate(indices):
                rendered[index] = segment if offset == 0 else ""
        for index in self.node_indices:
            rendered[index]["text"] = node_texts.get(index, self.source_texts[index])
        return rendered

    def candidate_skeleton(self, template: list[Any], candidate: Any) -> str | None:
        if not isinstance(candidate, list) or len(candidate) != len(template):
            return None
        chunks: list[str] = []
        markers = self.node_markers
        for index, (source_item, item) in enumerate(zip(template, candidate, strict=True)):
            if isinstance(source_item, str):
                if not isinstance(item, str):
                    return None
                chunks.append(item)
                continue
            if not isinstance(item, dict) or item.keys() != source_item.keys():
                return None
            if not isinstance(item.get("text"), str):
                return None
            if any(
                type(item[field]) is not type(value) or item[field] != value
                for field, value in source_item.items() if field != "text"
            ):
                return None
            chunks.append(markers[index])
        return "".join(chunks)

    def texts(self, candidate: list[Any]) -> list[str]:
        return [item if isinstance(item, str) else item["text"] for item in candidate]
