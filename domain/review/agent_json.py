from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any


@dataclass(frozen=True)
class ParsedAgentJson:
    payload: Any
    repair_notes: tuple[str, ...] = ()


class AgentJsonArtifactParser:
    @classmethod
    def parse(cls, text: str) -> ParsedAgentJson:
        candidates = cls._candidates(text)
        errors: list[str] = []
        for candidate, notes in candidates:
            try:
                return ParsedAgentJson(json.loads(candidate), tuple(notes))
            except JSONDecodeError as exc:
                errors.append(str(exc))

            repaired = cls._escape_raw_control_chars_in_strings(candidate)
            if repaired == candidate:
                continue
            try:
                return ParsedAgentJson(
                    json.loads(repaired),
                    tuple([*notes, 'escaped raw control characters inside JSON strings']),
                )
            except JSONDecodeError as exc:
                errors.append(str(exc))

        detail = errors[-1] if errors else 'empty artifact'
        raise ValueError(f'unreadable agent JSON artifact: {detail}')

    @classmethod
    def _candidates(cls, text: str) -> list[tuple[str, list[str]]]:
        candidates: list[tuple[str, list[str]]] = []
        seen: set[str] = set()

        def add(candidate: str, notes: list[str]) -> None:
            if candidate in seen:
                return
            seen.add(candidate)
            candidates.append((candidate, notes))

        add(text, [])
        stripped = text.strip()
        if stripped != text:
            add(stripped, ['trimmed surrounding whitespace'])

        unfenced = cls._strip_markdown_fence(stripped)
        if unfenced != stripped:
            add(unfenced, ['removed markdown code fence'])

        for candidate, notes in list(candidates):
            extracted = cls._extract_json_value(candidate)
            if extracted is not None and extracted != candidate:
                add(extracted, [*notes, 'extracted JSON value from surrounding text'])

        return candidates

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        lines = text.splitlines()
        if len(lines) < 3:
            return text
        if not lines[0].strip().startswith('```'):
            return text
        if lines[-1].strip() != '```':
            return text
        return '\n'.join(lines[1:-1]).strip()

    @staticmethod
    def _extract_json_value(text: str) -> str | None:
        start = -1
        for index, char in enumerate(text):
            if char in '{[':
                start = index
                break
        if start == -1:
            return None

        stack: list[str] = []
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
                continue
            if char in '{[':
                stack.append('}' if char == '{' else ']')
                continue
            if char in '}]':
                if not stack or stack[-1] != char:
                    return None
                stack.pop()
                if not stack:
                    return text[start:index + 1].strip()
        return None

    @staticmethod
    def _escape_raw_control_chars_in_strings(text: str) -> str:
        result: list[str] = []
        in_string = False
        escaped = False
        for char in text:
            if in_string:
                if escaped:
                    result.append(char)
                    escaped = False
                    continue
                if char == '\\':
                    result.append(char)
                    escaped = True
                    continue
                if char == '"':
                    result.append(char)
                    in_string = False
                    continue
                if char == '\n':
                    result.append('\\n')
                    continue
                if char == '\r':
                    result.append('\\r')
                    continue
                if char == '\t':
                    result.append('\\t')
                    continue
            elif char == '"':
                in_string = True
            result.append(char)
        return ''.join(result)
