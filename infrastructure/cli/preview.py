from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.review.preview import (
    PreviewSessionResult,
    PreviewSessionState,
    PreviewValidationError,
)

try:
    from prompt_toolkit.application import Application
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout
    from prompt_toolkit.layout.containers import ConditionalContainer, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.styles import Style
    from prompt_toolkit.widgets import Frame, TextArea

    HAS_PROMPT_TOOLKIT = True
except ModuleNotFoundError:
    Application = Condition = KeyBindings = Layout = None
    ConditionalContainer = Window = FormattedTextControl = Dimension = None
    Frame = Style = TextArea = None
    HAS_PROMPT_TOOLKIT = False


_PREVIEW_STYLE = (
    Style.from_dict(
        {
            'chrome': 'fg:#cbd5e1',
            'chrome.focused': 'fg:#7dd3fc bold',
            'panel.body': 'fg:#cbd5e1',
            'panel.header': 'fg:#93c5fd bold',
            'label': 'fg:#94a3b8',
            'value': 'fg:#e2e8f0',
            'title': 'fg:#f8fafc bold',
            'body': 'fg:#dbeafe',
            'message': 'fg:#e2e8f0',
            'hint': 'fg:#93c5fd',
            'warning': 'fg:#fbbf24 bold',
            'status.publish': 'bg:#166534 fg:#ecfdf5 bold',
            'status.skip': 'bg:#991b1b fg:#fee2e2 bold',
            'severity.critical': 'fg:#ef4444 bold',
            'severity.major': 'fg:#f97316 bold',
            'severity.minor': 'fg:#facc15 bold',
            'severity.suggestion': 'fg:#34d399 bold',
            'editor': 'bg:#0f172a fg:#dbeafe',
            'editor.focused': 'bg:#111827 fg:#f8fafc',
        }
    )
    if HAS_PROMPT_TOOLKIT else None
)


class PromptToolkitReviewPreview:
    def __init__(self, *, input_stream: Any = None, output_stream: Any = None) -> None:
        self._input_stream = input_stream
        self._output_stream = output_stream

    async def preview(self, session: PreviewSessionState) -> PreviewSessionResult:
        if not HAS_PROMPT_TOOLKIT:
            raise RuntimeError('prompt_toolkit is required for --preview-mode.')
        if not session.items:
            return PreviewSessionResult(items=session.items)
        controller = _PromptToolkitPreviewController(
            session=session,
            input_stream=self._input_stream,
            output_stream=self._output_stream,
        )
        return await controller.run()


@dataclass
class _PromptToolkitPreviewController:
    session: PreviewSessionState
    input_stream: Any = None
    output_stream: Any = None

    def __post_init__(self) -> None:
        self._message = 'Preview translated findings before publish.'
        self._confirm_exit = False
        self._browse_mode = Condition(lambda: not self.session.is_editing)
        self._edit_mode = Condition(lambda: self.session.is_editing)
        self._browse_control = FormattedTextControl(self._render_browse_text)
        self._header_control = FormattedTextControl(self._render_header_text)
        self._footer_control = FormattedTextControl(self._render_footer_text)
        self._browse_window = Window(
            content=self._browse_control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self._title_area = TextArea(
            multiline=False,
            wrap_lines=False,
            style='class:editor',
        )
        self._body_area = TextArea(
            multiline=True,
            scrollbar=True,
            style='class:editor',
        )
        self._title_frame = Frame(
            self._title_area,
            title='Title',
            style='class:chrome',
        )
        self._body_frame = Frame(
            self._body_area,
            title='Body',
            style='class:chrome',
        )
        self._header_frame = Frame(
            Window(
                content=self._header_control,
                height=Dimension(min=3, max=3),
                wrap_lines=True,
            ),
            title='Review Preview',
            style='class:chrome.focused',
        )
        self._browse_frame = Frame(
            self._browse_window,
            title='Finding',
            style='class:chrome',
            height=Dimension(weight=1),
        )
        self._editor_frame = Frame(
            HSplit(
                [
                    self._title_frame,
                    self._body_frame,
                ],
                padding=1,
            ),
            title='Editor',
            style='class:chrome',
            height=Dimension(weight=1),
        )
        self._controls_frame = Frame(
            Window(
                content=self._footer_control,
                height=Dimension(min=3, max=3),
                wrap_lines=True,
            ),
            title='Controls',
            style='class:chrome',
        )
        self._application: Application | None = None
        self._layout: Layout | None = None

    async def run(self) -> PreviewSessionResult:
        key_bindings = self._build_key_bindings()
        root = HSplit(
            [
                self._header_frame,
                ConditionalContainer(
                    content=self._browse_frame,
                    filter=self._browse_mode,
                ),
                ConditionalContainer(
                    content=self._editor_frame,
                    filter=self._edit_mode,
                ),
                self._controls_frame,
            ]
        )
        self._layout = Layout(root, focused_element=self._browse_window)
        self._sync_editor_styles()
        self._application = Application(
            layout=self._layout,
            key_bindings=key_bindings,
            full_screen=True,
            mouse_support=False,
            input=self.input_stream,
            output=self.output_stream,
            style=_PREVIEW_STYLE,
        )
        result = await self._application.run_async()
        return result

    def _build_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add('left', filter=self._browse_mode)
        @bindings.add('p', filter=self._browse_mode)
        def _previous(event) -> None:
            self._reset_confirmation()
            self.session = self.session.previous_item()
            self._refresh(event)

        @bindings.add('right', filter=self._browse_mode)
        @bindings.add('n', filter=self._browse_mode)
        def _next(event) -> None:
            self._reset_confirmation()
            self.session = self.session.next_item()
            self._refresh(event)

        @bindings.add('up', filter=self._browse_mode)
        @bindings.add('k', filter=self._browse_mode)
        def _scroll_up(event) -> None:
            self._reset_confirmation()
            self.session = self.session.scroll_up()
            self._refresh(event)

        @bindings.add('down', filter=self._browse_mode)
        @bindings.add('j', filter=self._browse_mode)
        def _scroll_down(event) -> None:
            self._reset_confirmation()
            self.session = self.session.scroll_down(max_offset=self._max_scroll_offset())
            self._refresh(event)

        @bindings.add(' ', filter=self._browse_mode)
        def _toggle_publish(event) -> None:
            self._reset_confirmation()
            self.session = self.session.toggle_publish()
            state = 'publishable' if self.session.current_item and self.session.current_item.publish else 'hidden'
            self._message = f'Current finding marked {state}.'
            self._refresh(event)

        @bindings.add('e', filter=self._browse_mode)
        def _begin_edit(event) -> None:
            self._reset_confirmation()
            self.session = self.session.begin_edit()
            self._load_edit_buffers()
            self._message = 'Edit title and body, then press Ctrl-S to save.'
            self._focus_editor(self._title_area)
            self._refresh(event)

        @bindings.add('tab', filter=self._edit_mode, eager=True)
        def _switch_field(event) -> None:
            self._switch_editor_focus()
            self._refresh(event)

        @bindings.add('s-tab', filter=self._edit_mode, eager=True)
        def _switch_field_back(event) -> None:
            self._switch_editor_focus(reverse=True)
            self._refresh(event)

        @bindings.add('c-s', filter=self._edit_mode, eager=True)
        def _save_edit(event) -> None:
            try:
                self.session = self.session.update_edit_buffer(
                    short_title=self._title_area.text,
                    body=self._body_area.text,
                ).save_edit()
            except PreviewValidationError as exc:
                self._message = str(exc)
            else:
                self._message = 'Saved edits for the current finding.'
                self._focus_browse()
            self._refresh(event)

        @bindings.add('escape', filter=self._edit_mode, eager=True)
        def _discard_edit(event) -> None:
            self.session = self.session.discard_edit()
            self._message = 'Discarded unsaved edits.'
            self._focus_browse()
            self._refresh(event)

        @bindings.add('q', filter=self._browse_mode)
        def _quit(event) -> None:
            if not self._confirm_exit:
                self._confirm_exit = True
                self._message = 'Press q again to finish preview and publish the current selection.'
                self._refresh(event)
                return
            event.app.exit(result=PreviewSessionResult(items=self.session.items))

        @bindings.add('c-c')
        def _cancel(event) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        return bindings

    def _render_header_text(self):
        current = self.session.current_item
        if current is None:
            return [('class:warning', 'No findings available for preview.')]
        mode = 'EDIT' if self.session.is_editing else 'BROWSE'
        return [
            ('class:panel.header', f'Finding {current.index}/{len(self.session.items)}'),
            ('class:label', '  Mode: '),
            ('class:value', mode),
            ('class:label', '  Target: '),
            ('class:value', self._target_label(current.target)),
            ('', '\n'),
            ('class:label', 'Severity: '),
            (self._severity_style(current.severity), current.severity),
            ('class:label', '  Publish: '),
            *self._publish_badge_fragments(current.publish),
            ('', '\n'),
            ('class:label', 'Location: '),
            ('class:value', current.location),
            ('class:label', '  Scroll: '),
            ('class:value', str(self.session.scroll_offset)),
        ]

    def _render_browse_text(self):
        current = self.session.current_item
        if current is None:
            return [('class:warning', 'No findings available for preview.')]
        lines = self._browse_lines()
        page_lines = self._page_lines()
        visible = lines[self.session.scroll_offset:self.session.scroll_offset + page_lines]
        if not visible:
            visible = lines[-page_lines:] if lines else [('class:body', '')]

        fragments: list[tuple[str, str]] = []
        for index, (style, text) in enumerate(visible):
            fragments.append((style, text))
            if index < len(visible) - 1:
                fragments.append(('', '\n'))
        return fragments

    def _render_footer_text(self):
        return [
            ('class:message', self._message),
            ('', '\n'),
            ('class:hint', self._controls_hint()),
            ('', '\n'),
            ('class:label', 'Arrows edit text only inside the editor; browse navigation is disabled while editing.'),
        ]

    def _browse_lines(self) -> list[tuple[str, str]]:
        current = self.session.current_item
        if current is None:
            return [('class:body', '')]
        body_lines = current.body.splitlines() or ['']
        return [
            ('class:label', 'Short title'),
            ('class:title', current.short_title),
            ('', ''),
            ('class:label', 'Comment body'),
            *[('class:body', line) for line in body_lines],
        ]

    def _page_lines(self) -> int:
        return 16

    def _max_scroll_offset(self) -> int:
        lines = self._browse_lines()
        return max(len(lines) - self._page_lines(), 0)

    def _reset_confirmation(self) -> None:
        self._confirm_exit = False

    def _load_edit_buffers(self) -> None:
        buffer = self.session.edit_buffer
        assert buffer is not None
        self._title_area.text = buffer.short_title
        self._title_area.buffer.cursor_position = len(self._title_area.text)
        self._body_area.text = buffer.body
        self._body_area.buffer.cursor_position = len(self._body_area.text)

    def _focus_editor(self, target: TextArea) -> None:
        if self._layout is not None:
            self._layout.focus(target)
        self._sync_editor_styles()

    def _focus_browse(self) -> None:
        if self._layout is not None:
            self._layout.focus(self._browse_window)
        self._sync_editor_styles()

    def _switch_editor_focus(self, *, reverse: bool = False) -> None:
        if self._layout is None:
            return
        current = self._layout.current_control
        if reverse:
            target = self._title_area if current == self._body_area.control else self._body_area
        else:
            target = self._body_area if current == self._title_area.control else self._title_area
        self._layout.focus(target)
        self._sync_editor_styles()

    def _sync_editor_styles(self) -> None:
        title_focused = self._is_focused(self._title_area)
        body_focused = self._is_focused(self._body_area)
        self._title_area.style = 'class:editor.focused' if title_focused else 'class:editor'
        self._body_area.style = 'class:editor.focused' if body_focused else 'class:editor'
        self._title_frame.style = 'class:chrome.focused' if title_focused else 'class:chrome'
        self._body_frame.style = 'class:chrome.focused' if body_focused else 'class:chrome'

    def _is_focused(self, target: TextArea) -> bool:
        if self._layout is None:
            return False
        return self._layout.current_control == target.control

    def _target_label(self, target: str) -> str:
        return {
            'inline': 'Inline comment',
            'summary-only': 'Summary-only',
        }.get(target, target)

    def _severity_style(self, severity: str) -> str:
        return {
            'Critical': 'class:severity.critical',
            'Major': 'class:severity.major',
            'Minor': 'class:severity.minor',
            'Suggestion': 'class:severity.suggestion',
        }.get(severity, 'class:value')

    def _publish_badge_fragments(self, publish: bool) -> list[tuple[str, str]]:
        label = ' PUBLISH ' if publish else ' SKIP '
        style = 'class:status.publish' if publish else 'class:status.skip'
        return [(style, label)]

    def _controls_hint(self) -> str:
        if self.session.is_editing:
            return (
                'Edit: Arrow keys/Home/End move the caret | Tab/Shift-Tab switch fields | '
                'Ctrl-S save | Esc discard | Ctrl-C cancel run'
            )
        return (
            'Browse: Left/Right or p/n switch findings | Up/Down or j/k scroll | '
            'Space toggle publish | e edit | q finish | Ctrl-C cancel run'
        )

    def _refresh(self, event) -> None:
        self._sync_editor_styles()
        event.app.invalidate()
