from __future__ import annotations

import asyncio
import unittest

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from domain.review.preview import PreviewItem, PreviewSessionState
from infrastructure.cli.preview import PromptToolkitReviewPreview


class PromptToolkitReviewPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_mode_left_arrow_moves_caret_inside_body(self) -> None:
        result = await self._run_preview(
            self._session(first_body='ABCD'),
            [
                ('text', 'e'),
                ('text', '\t'),
                ('bytes', b'\x1b[D'),
                ('text', 'X'),
                ('bytes', b'\x13'),
                ('text', 'q'),
                ('text', 'q'),
            ],
        )

        self.assertEqual(result.items[0].body, 'ABCXD')

    async def test_edit_mode_up_arrow_moves_between_body_lines(self) -> None:
        result = await self._run_preview(
            self._session(first_body='line1\nline2'),
            [
                ('text', 'e'),
                ('text', '\t'),
                ('bytes', b'\x1b[A'),
                ('text', 'X'),
                ('bytes', b'\x13'),
                ('text', 'q'),
                ('text', 'q'),
            ],
        )

        self.assertEqual(result.items[0].body, 'line1X\nline2')

    async def test_tab_and_shift_tab_switch_between_title_and_body(self) -> None:
        result = await self._run_preview(
            self._session(first_title='Title', first_body='Body'),
            [
                ('text', 'e'),
                ('text', '1'),
                ('text', '\t'),
                ('text', 'B'),
                ('bytes', b'\x1b[Z'),
                ('text', '2'),
                ('bytes', b'\x13'),
                ('text', 'q'),
                ('text', 'q'),
            ],
        )

        self.assertEqual(result.items[0].short_title, 'Title12')
        self.assertEqual(result.items[0].body, 'BodyB')

    async def test_escape_discards_unsaved_edits(self) -> None:
        result = await self._run_preview(
            self._session(first_body='Body'),
            [
                ('text', 'e'),
                ('text', '\t'),
                ('text', 'X'),
                ('bytes', b'\x1b'),
                ('text', 'q'),
                ('text', 'q'),
            ],
        )

        self.assertEqual(result.items[0].body, 'Body')

    async def test_browse_mode_right_arrow_still_switches_findings(self) -> None:
        result = await self._run_preview(
            self._session(),
            [
                ('bytes', b'\x1b[C'),
                ('text', ' '),
                ('text', 'q'),
                ('text', 'q'),
            ],
        )

        self.assertTrue(result.items[0].publish)
        self.assertFalse(result.items[1].publish)

    async def _run_preview(
        self,
        session: PreviewSessionState,
        actions: list[tuple[str, str | bytes]],
    ):
        with create_pipe_input() as pipe_input:
            preview = PromptToolkitReviewPreview(
                input_stream=pipe_input,
                output_stream=DummyOutput(),
            )
            task = asyncio.create_task(preview.preview(session))
            await asyncio.sleep(0.05)
            for kind, payload in actions:
                if kind == 'text':
                    assert isinstance(payload, str)
                    pipe_input.send_text(payload)
                else:
                    assert isinstance(payload, bytes)
                    pipe_input.send_bytes(payload)
                await asyncio.sleep(0.05)
            return await asyncio.wait_for(task, timeout=2.0)

    @staticmethod
    def _session(
        *,
        first_title: str = 'First title',
        first_body: str = 'First body',
    ) -> PreviewSessionState:
        return PreviewSessionState(
            items=(
                PreviewItem(
                    index=1,
                    target='inline',
                    severity='Major',
                    file_path='src/app.py',
                    line=10,
                    short_title=first_title,
                    body=first_body,
                    publish=True,
                    source_index=0,
                ),
                PreviewItem(
                    index=2,
                    target='summary-only',
                    severity='Minor',
                    file_path='src/app.py',
                    line=20,
                    short_title='Second title',
                    body='Second body',
                    publish=True,
                    source_index=0,
                ),
            ),
        )


if __name__ == '__main__':
    unittest.main()
