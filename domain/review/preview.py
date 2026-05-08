from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal


PreviewItemTarget = Literal['inline', 'summary-only']


class PreviewValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreviewItem:
    index: int
    target: PreviewItemTarget
    severity: str
    file_path: str
    line: int | None
    short_title: str
    body: str
    publish: bool = True
    source_index: int = 0

    @property
    def location(self) -> str:
        if self.line is None:
            return self.file_path
        return f'{self.file_path}:{self.line}'

    def with_content(self, short_title: str, body: str) -> PreviewItem:
        self._validate(short_title, body, publish=self.publish)
        return replace(self, short_title=short_title, body=body)

    def with_publish(self, publish: bool) -> PreviewItem:
        self._validate(self.short_title, self.body, publish=publish)
        return replace(self, publish=publish)

    @staticmethod
    def _validate(short_title: str, body: str, *, publish: bool) -> None:
        if not publish:
            return
        if not short_title.strip():
            raise PreviewValidationError('Publishable findings must keep a non-empty short title.')
        if not body.strip():
            raise PreviewValidationError('Publishable findings must keep a non-empty body.')


@dataclass(frozen=True)
class PreviewEditBuffer:
    short_title: str
    body: str


@dataclass(frozen=True)
class PreviewSessionState:
    items: tuple[PreviewItem, ...]
    cursor: int = 0
    scroll_offset: int = 0
    edit_buffer: PreviewEditBuffer | None = None

    def __post_init__(self) -> None:
        if not self.items and self.cursor != 0:
            raise ValueError('Empty preview sessions must start at cursor 0.')
        if self.items and not 0 <= self.cursor < len(self.items):
            raise ValueError('Preview cursor is out of range.')
        if self.scroll_offset < 0:
            raise ValueError('Preview scroll offset must be >= 0.')

    @property
    def current_item(self) -> PreviewItem | None:
        if not self.items:
            return None
        return self.items[self.cursor]

    @property
    def is_editing(self) -> bool:
        return self.edit_buffer is not None

    def next_item(self) -> PreviewSessionState:
        if not self.items:
            return self
        cursor = min(self.cursor + 1, len(self.items) - 1)
        return self._move_to(cursor)

    def previous_item(self) -> PreviewSessionState:
        if not self.items:
            return self
        cursor = max(self.cursor - 1, 0)
        return self._move_to(cursor)

    def scroll_up(self, lines: int = 1) -> PreviewSessionState:
        return replace(self, scroll_offset=max(0, self.scroll_offset - max(1, lines)))

    def scroll_down(self, lines: int = 1, *, max_offset: int | None = None) -> PreviewSessionState:
        offset = self.scroll_offset + max(1, lines)
        if max_offset is not None:
            offset = min(offset, max(0, max_offset))
        return replace(self, scroll_offset=offset)

    def toggle_publish(self) -> PreviewSessionState:
        item = self._require_current_item()
        return self._replace_current_item(item.with_publish(not item.publish))

    def begin_edit(self) -> PreviewSessionState:
        item = self._require_current_item()
        return replace(
            self,
            edit_buffer=PreviewEditBuffer(short_title=item.short_title, body=item.body),
        )

    def update_edit_buffer(
        self,
        *,
        short_title: str | None = None,
        body: str | None = None,
    ) -> PreviewSessionState:
        if self.edit_buffer is None:
            raise ValueError('Preview session is not in edit mode.')
        buffer = PreviewEditBuffer(
            short_title=self.edit_buffer.short_title if short_title is None else short_title,
            body=self.edit_buffer.body if body is None else body,
        )
        return replace(self, edit_buffer=buffer)

    def save_edit(self) -> PreviewSessionState:
        if self.edit_buffer is None:
            raise ValueError('Preview session is not in edit mode.')
        item = self._require_current_item()
        updated = item.with_content(
            short_title=self.edit_buffer.short_title,
            body=self.edit_buffer.body,
        )
        return replace(self._replace_current_item(updated), edit_buffer=None)

    def discard_edit(self) -> PreviewSessionState:
        if self.edit_buffer is None:
            return self
        return replace(self, edit_buffer=None)

    def _move_to(self, cursor: int) -> PreviewSessionState:
        return replace(self, cursor=cursor, scroll_offset=0, edit_buffer=None)

    def _replace_current_item(self, item: PreviewItem) -> PreviewSessionState:
        items = list(self.items)
        items[self.cursor] = item
        return replace(self, items=tuple(items))

    def _require_current_item(self) -> PreviewItem:
        item = self.current_item
        if item is None:
            raise ValueError('Preview session has no current item.')
        return item


@dataclass(frozen=True)
class PreviewSessionResult:
    items: tuple[PreviewItem, ...]
