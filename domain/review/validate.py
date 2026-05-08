from __future__ import annotations

import re

from domain.models import Anchor, Finding


class FindingValidator:
    RULE_ID_RE = re.compile(r'^CS-[A-Z]+-\d+$')
    ALLOWED_SEVERITIES = frozenset({'Critical', 'Major', 'Minor', 'Suggestion'})
    ALLOWED_CONFIDENCE = frozenset({'High', 'Medium', 'Low'})

    @classmethod
    def normalize_batch(cls, payload: dict, source_name: str) -> tuple[list[Finding], list[str]]:
        findings = payload.get('findings', [])
        anomalies = [str(item) for item in payload.get('anomalies', [])]
        pass_id = cls._pass_id_from_source(source_name)
        source_kind = 'cluster' if payload.get('cluster_id') else 'file'
        cluster_id = payload.get('cluster_id')
        normalized: list[Finding] = []
        for index, raw_finding in enumerate(findings):
            finding, error = cls._normalize_finding(raw_finding, pass_id, source_kind, cluster_id)
            if finding is None:
                anomalies.append(f'{source_name}#{index + 1}: {error}')
                continue
            normalized.append(finding)
        return normalized, anomalies

    @classmethod
    def _normalize_finding(
        cls,
        raw_finding: dict,
        pass_id: str,
        source_kind: str,
        cluster_id: str | None,
    ) -> tuple[Finding | None, str | None]:
        rule_ids = [
            str(rule_id) for rule_id in raw_finding.get('rule_ids', [])
            if isinstance(rule_id, str) and cls.RULE_ID_RE.match(rule_id)
        ]
        if not rule_ids:
            return None, 'missing valid CS-* rule IDs'

        anchor = cls._normalize_anchor(raw_finding.get('anchor', {}))
        if anchor is None:
            return None, 'missing anchor path or line'

        short_title = cls._clean_text(raw_finding.get('short_title'))
        body = cls._clean_text(raw_finding.get('body'))
        if not short_title or not body:
            return None, 'missing short_title or body'

        severity = cls._normalize_severity(raw_finding.get('severity'))
        confidence = cls._normalize_confidence(raw_finding.get('confidence'))
        evidence = cls._clean_text(raw_finding.get('evidence')) or cls._fallback_text(body)
        impact = cls._clean_text(raw_finding.get('impact')) or short_title
        focus_area = cls._clean_text(raw_finding.get('focus_area')) or pass_id.replace('_', '-')
        dedup_key = cls._clean_text(raw_finding.get('dedup_key')) or cls._dedup_key(
            anchor,
            rule_ids,
            focus_area,
            short_title,
        )

        finding = Finding(
            severity=severity,
            confidence=confidence,
            rule_ids=rule_ids,
            short_title=short_title,
            anchor=anchor,
            body=body,
            language=str(raw_finding.get('language', '')),
            focus_area=focus_area,
            evidence=evidence,
            impact=impact,
            dedup_key=dedup_key,
            source_pass=cls._clean_text(raw_finding.get('source_pass')) or pass_id,
            source_kind=cls._clean_text(raw_finding.get('source_kind')) or source_kind,
        )
        if cluster_id:
            finding['cluster_id'] = cluster_id
        return finding, None

    @classmethod
    def _normalize_anchor(cls, raw_anchor: dict) -> Anchor | None:
        new_path = cls._clean_text(raw_anchor.get('new_path'))
        old_path = cls._clean_text(raw_anchor.get('old_path')) or new_path
        new_line = cls._normalize_line(raw_anchor.get('new_line'))
        old_line = cls._normalize_line(raw_anchor.get('old_line'))
        if not new_path and not old_path:
            return None
        if new_line is None and old_line is None:
            return None
        return Anchor(
            old_path=old_path or new_path,
            new_path=new_path or old_path,
            new_line=new_line,
            old_line=old_line,
        )

    @classmethod
    def _normalize_line(cls, value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else None
        return None

    @classmethod
    def _normalize_severity(cls, value: object) -> str:
        text = cls._clean_text(value).title()
        return text if text in cls.ALLOWED_SEVERITIES else 'Suggestion'

    @classmethod
    def _normalize_confidence(cls, value: object) -> str:
        text = cls._clean_text(value).title()
        return text if text in cls.ALLOWED_CONFIDENCE else 'Medium'

    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or '').strip()

    @classmethod
    def _fallback_text(cls, body: str) -> str:
        return body.splitlines()[0][:240]

    @classmethod
    def _dedup_key(cls, anchor: Anchor, rule_ids: list[str], focus_area: str, short_title: str) -> str:
        anchor_line = anchor.get('new_line') or anchor.get('old_line') or 0
        normalized_title = re.sub(r'[^a-z0-9]+', '-', short_title.lower()).strip('-')
        return '|'.join([
            rule_ids[0],
            anchor['new_path'],
            str(anchor_line),
            focus_area,
            normalized_title[:48],
        ])

    @classmethod
    def _pass_id_from_source(cls, source_name: str) -> str:
        stem = source_name.removesuffix('.json')
        if '-' not in stem:
            return 'unknown'
        return stem.split('-', 1)[1]
