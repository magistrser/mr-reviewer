from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from domain.models import Finding, SEVERITY_ORDER
from domain.review.agent_json import AgentJsonArtifactParser
from domain.review.validate import FindingValidator
from runtime.async_ops import AsyncPathIO


class FindingsConsolidator:
    FINDINGS_RE = re.compile(r'^(\d+)-([a-z0-9_]+)\.json$')

    @classmethod
    async def consolidate(
        cls,
        findings_dirs: Path | Iterable[Path],
        output_file: Path,
    ) -> dict[str, object]:
        all_findings: list[Finding] = []
        anomalies: list[str] = []
        invalid = 0
        sources = [findings_dirs] if isinstance(findings_dirs, Path) else list(findings_dirs)
        for findings_dir in sources:
            if not await AsyncPathIO.exists(findings_dir):
                continue
            entries = sorted(
                path for path in await AsyncPathIO.iterdir(findings_dir)
                if path.suffix == '.json' and cls.FINDINGS_RE.match(path.name)
            )
            for path in entries:
                try:
                    parsed = AgentJsonArtifactParser.parse(await AsyncPathIO.read_text(path))
                    data = parsed.payload
                    if not isinstance(data, dict):
                        raise ValueError('findings payload must be a JSON object')
                    if parsed.repair_notes:
                        anomalies.append(
                            f'{path.name}: recovered malformed JSON ({", ".join(parsed.repair_notes)})'
                        )
                    normalized, file_anomalies = FindingValidator.normalize_batch(data, path.name)
                    all_findings.extend(normalized)
                    invalid += max(len(data.get('findings', [])) - len(normalized), 0)
                    anomalies.extend(file_anomalies)
                except Exception as exc:
                    anomalies.append(f'{path.name}: unreadable findings payload ({exc})')
        all_findings.sort(
            key=lambda finding: SEVERITY_ORDER.get(finding.get('severity', ''), 0),
            reverse=True,
        )
        await AsyncPathIO.write_text(output_file, json.dumps(all_findings, indent=2))
        return {
            'count': len(all_findings),
            'invalid': invalid,
            'anomalies': anomalies,
        }

    @classmethod
    async def fix_malformed_findings(cls, findings_dir: Path) -> None:
        for name in [path.name for path in await AsyncPathIO.iterdir(findings_dir)]:
            if not name.endswith('.json') or cls.FINDINGS_RE.match(name):
                continue
            match = re.match(r'^(\d+-[a-z0-9_]+)\.', name)
            if not match:
                continue
            correct = findings_dir / f'{match.group(1)}.json'
            if not await AsyncPathIO.exists(correct):
                await AsyncPathIO.rename(findings_dir / name, correct)

    @classmethod
    async def count_done_findings(cls, findings_dir: Path) -> int:
        return sum(1 for path in await AsyncPathIO.iterdir(findings_dir) if cls.FINDINGS_RE.match(path.name))
