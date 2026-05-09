from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from domain.review.agent_json import AgentJsonArtifactParser
from domain.review.consolidate import FindingsConsolidator


class AgentJsonArtifactTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_extracts_json_from_surrounding_text(self) -> None:
        parsed = AgentJsonArtifactParser.parse('Result follows:\n{"ok": true}\nDone.')

        self.assertEqual(parsed.payload, {'ok': True})
        self.assertIn('extracted JSON value from surrounding text', parsed.repair_notes)

    async def test_consolidate_recovers_agent_findings_json_with_fence_and_raw_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            findings_dir = root / 'findings'
            findings_dir.mkdir()
            output_file = root / 'raw-findings.json'
            (findings_dir / '001-correctness.json').write_text(
                '''```json
{
  "file": "src/app.py",
  "findings": [
    {
      "severity": "Major",
      "confidence": "High",
      "rule_ids": ["CS-CORE-001"],
      "short_title": "Missing validation",
      "anchor": {
        "old_path": "src/app.py",
        "new_path": "src/app.py",
        "new_line": 10,
        "old_line": null
      },
      "body": "Line one
Line two",
      "language": "python",
      "focus_area": "correctness",
      "evidence": "Changed line 10 accepts invalid input.",
      "impact": "Invalid input can be persisted.",
      "dedup_key": "CS-CORE-001|src/app.py|10|correctness|missing-validation",
      "source_pass": "correctness",
      "source_kind": "file"
    }
  ],
  "anomalies": []
}
```'''
            )

            result = await FindingsConsolidator.consolidate(findings_dir, output_file)

            findings = json.loads(output_file.read_text())

            self.assertEqual(result['count'], 1)
            self.assertEqual(result['invalid'], 0)
            self.assertTrue(any('recovered malformed JSON' in item for item in result['anomalies']))
            self.assertEqual(findings[0]['body'], 'Line one\nLine two')


if __name__ == '__main__':
    unittest.main()
