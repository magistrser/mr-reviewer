from __future__ import annotations

import unittest

from domain.review.benchmark import ReviewBenchmarkScorer


class ReviewBenchmarkScorerTests(unittest.TestCase):
    def test_score_case_matches_by_path_rule_and_nearby_line(self) -> None:
        score = ReviewBenchmarkScorer.score_case(
            {
                'name': 'api-mismatch',
                'actual_findings': [
                    {
                        'rule_ids': ['CS-CORE-001'],
                        'severity': 'Major',
                        'short_title': 'Response field renamed in one layer only',
                        'anchor': {
                            'old_path': 'src/api/handler.py',
                            'new_path': 'src/api/handler.py',
                            'new_line': 42,
                            'old_line': None,
                        },
                    }
                ],
                'expected_findings': [
                    {
                        'file_path': 'src/api/handler.py',
                        'line': 44,
                        'rule_id': 'CS-CORE-001',
                        'severity': 'Major',
                        'short_title': 'response field renamed',
                    }
                ],
            }
        )

        self.assertEqual(score['matched'], 1)
        self.assertEqual(score['missed'], 0)
        self.assertEqual(score['extra'], 0)
        self.assertEqual(score['precision'], 1.0)
        self.assertEqual(score['recall'], 1.0)

    def test_summarize_accumulates_precision_and_recall(self) -> None:
        summary = ReviewBenchmarkScorer.summarize(
            [
                {
                    'name': 'case-a',
                    'matched': 2,
                    'expected': 3,
                    'actual': 4,
                    'missed': 1,
                    'extra': 2,
                    'precision': 0.5,
                    'recall': 0.6667,
                },
                {
                    'name': 'case-b',
                    'matched': 1,
                    'expected': 1,
                    'actual': 2,
                    'missed': 0,
                    'extra': 1,
                    'precision': 0.5,
                    'recall': 1.0,
                },
            ]
        )

        self.assertEqual(summary['name'], 'overall')
        self.assertEqual(summary['matched'], 3)
        self.assertEqual(summary['expected'], 4)
        self.assertEqual(summary['actual'], 6)
        self.assertEqual(summary['precision'], 0.5)
        self.assertEqual(summary['recall'], 0.75)


if __name__ == '__main__':
    unittest.main()
