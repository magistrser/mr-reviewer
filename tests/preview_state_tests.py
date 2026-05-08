from __future__ import annotations

import unittest

from domain.review.preview import PreviewItem, PreviewSessionState, PreviewValidationError


class PreviewSessionStateTests(unittest.TestCase):
    def test_navigation_stops_at_bounds(self) -> None:
        session = self._session()

        self.assertEqual(session.previous_item().cursor, 0)
        self.assertEqual(session.next_item().cursor, 1)
        self.assertEqual(session.next_item().next_item().cursor, 1)

    def test_scroll_is_clamped(self) -> None:
        session = self._session()

        session = session.scroll_down(lines=3, max_offset=2)
        self.assertEqual(session.scroll_offset, 2)
        session = session.scroll_up(lines=5)
        self.assertEqual(session.scroll_offset, 0)

    def test_toggle_publish_round_trips(self) -> None:
        session = self._session()

        hidden = session.toggle_publish()
        self.assertFalse(hidden.current_item.publish)
        restored = hidden.toggle_publish()
        self.assertTrue(restored.current_item.publish)

    def test_save_and_discard_edit_behavior(self) -> None:
        session = self._session().begin_edit()
        session = session.update_edit_buffer(short_title='Updated title', body='Updated body')
        saved = session.save_edit()

        self.assertEqual(saved.current_item.short_title, 'Updated title')
        self.assertEqual(saved.current_item.body, 'Updated body')
        self.assertFalse(saved.is_editing)

        discarded = saved.begin_edit().update_edit_buffer(short_title='Throwaway').discard_edit()
        self.assertEqual(discarded.current_item.short_title, 'Updated title')
        self.assertFalse(discarded.is_editing)

    def test_publishable_items_reject_blank_title_or_body(self) -> None:
        session = self._session().begin_edit()

        with self.assertRaises(PreviewValidationError):
            session.update_edit_buffer(short_title='   ', body='Still present').save_edit()

        with self.assertRaises(PreviewValidationError):
            session.update_edit_buffer(short_title='Still present', body='   ').save_edit()

    @staticmethod
    def _session() -> PreviewSessionState:
        return PreviewSessionState(
            items=(
                PreviewItem(
                    index=1,
                    target='inline',
                    severity='Major',
                    file_path='src/app.py',
                    line=10,
                    short_title='First title',
                    body='First body',
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
