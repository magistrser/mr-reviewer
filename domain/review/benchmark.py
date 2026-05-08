from __future__ import annotations

from domain.models import BenchmarkCase, BenchmarkExpectedFinding, BenchmarkScore, Finding


class ReviewBenchmarkScorer:
    LINE_TOLERANCE = 3

    @classmethod
    def score_case(cls, case: BenchmarkCase) -> BenchmarkScore:
        expected = case.get('expected_findings', [])
        actual = case.get('actual_findings', [])
        matched_expected: set[int] = set()
        matched_actual: set[int] = set()

        for expected_index, expected_finding in enumerate(expected):
            for actual_index, actual_finding in enumerate(actual):
                if actual_index in matched_actual:
                    continue
                if cls._matches(actual_finding, expected_finding):
                    matched_expected.add(expected_index)
                    matched_actual.add(actual_index)
                    break

        matched = len(matched_expected)
        expected_count = len(expected)
        actual_count = len(actual)
        precision = matched / actual_count if actual_count else 1.0
        recall = matched / expected_count if expected_count else 1.0
        return BenchmarkScore(
            name=str(case.get('name', 'unnamed')),
            matched=matched,
            expected=expected_count,
            actual=actual_count,
            missed=max(expected_count - matched, 0),
            extra=max(actual_count - matched, 0),
            precision=round(precision, 4),
            recall=round(recall, 4),
        )

    @classmethod
    def score_cases(cls, cases: list[BenchmarkCase]) -> list[BenchmarkScore]:
        return [cls.score_case(case) for case in cases]

    @classmethod
    def summarize(cls, scores: list[BenchmarkScore]) -> BenchmarkScore:
        matched = sum(score.get('matched', 0) for score in scores)
        expected = sum(score.get('expected', 0) for score in scores)
        actual = sum(score.get('actual', 0) for score in scores)
        precision = matched / actual if actual else 1.0
        recall = matched / expected if expected else 1.0
        return BenchmarkScore(
            name='overall',
            matched=matched,
            expected=expected,
            actual=actual,
            missed=max(expected - matched, 0),
            extra=max(actual - matched, 0),
            precision=round(precision, 4),
            recall=round(recall, 4),
        )

    @classmethod
    def _matches(cls, actual: Finding, expected: BenchmarkExpectedFinding) -> bool:
        actual_path = actual.get('anchor', {}).get('new_path') or actual.get('anchor', {}).get('old_path')
        expected_path = expected.get('file_path')
        if actual_path != expected_path:
            return False

        expected_rule = expected.get('rule_id')
        if expected_rule and expected_rule not in actual.get('rule_ids', []):
            return False

        expected_severity = expected.get('severity')
        if expected_severity and actual.get('severity') != expected_severity:
            return False

        expected_line = expected.get('line')
        actual_line = actual.get('anchor', {}).get('new_line') or actual.get('anchor', {}).get('old_line')
        if expected_line is not None and actual_line is not None:
            if abs(actual_line - expected_line) > cls.LINE_TOLERANCE:
                return False

        expected_title = str(expected.get('short_title', '')).strip().lower()
        if expected_title and expected_title not in str(actual.get('short_title', '')).strip().lower():
            return False
        return True
