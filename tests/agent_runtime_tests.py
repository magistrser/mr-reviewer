from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from infrastructure.agents.openai_runner import AgentRunner


class AgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.agents_dir = Path('/Users/franz/review/resources/agents')
        self.runner = AgentRunner(
            base_url='https://example.invalid/v1',
            api_key='test-key',
            timeout_seconds=30,
            agents_dir=self.agents_dir,
        )

    async def test_load_agent_reads_frontmatter_tools(self) -> None:
        agent = await self.runner.load_agent('review-agent')
        self.assertEqual(agent.name, 'review-agent')
        self.assertIn('read_file', agent.tool_names)
        self.assertIn('write_file', agent.tool_names)
        self.assertIn('run_shell_command', agent.tool_names)
        self.assertIn('# review-agent', agent.system_prompt)

    async def test_load_dedup_agent_reads_expected_tools(self) -> None:
        agent = await self.runner.load_agent('deduplicate-comments-agent')
        self.assertEqual(agent.name, 'deduplicate-comments-agent')
        self.assertEqual(agent.tool_names, ('read_file', 'write_file'))
        self.assertIn('# deduplicate-comments-agent', agent.system_prompt)

    async def test_load_translation_agent_reads_expected_tools(self) -> None:
        agent = await self.runner.load_agent('translate-review-agent')
        self.assertEqual(agent.name, 'translate-review-agent')
        self.assertEqual(agent.tool_names, ('read_file', 'write_file'))
        self.assertIn('# translate-review-agent', agent.system_prompt)

    async def test_tool_read_file_adds_line_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'sample.txt'
            path.write_text('alpha\nbeta\n')
            output = await self.runner._tool_read_file({'path': str(path), 'limit': 1, 'offset': 1})
        self.assertEqual(output, '2\tbeta\n')

    async def test_default_model_uses_short_metadata_timeout(self) -> None:
        runner = AgentRunner(
            base_url='https://example.invalid/v1',
            api_key='test-key',
            timeout_seconds=600,
            agents_dir=self.agents_dir,
        )
        seen: dict[str, int | None] = {}

        def fake_request_sync(
            method: str,
            endpoint: str,
            payload: dict | None = None,
            timeout_seconds: int | None = None,
        ) -> dict:
            seen['timeout_seconds'] = timeout_seconds
            return {'data': [{'id': 'local-model'}]}

        runner._request_json_sync = fake_request_sync  # type: ignore[method-assign]

        self.assertEqual(await runner.default_model(), 'local-model')
        self.assertEqual(seen['timeout_seconds'], 30)

    async def test_run_executes_tool_calls_even_when_finish_reason_is_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / 'result.json'
            calls = 0

            def fake_request_sync(
                method: str,
                endpoint: str,
                payload: dict | None = None,
                timeout_seconds: int | None = None,
            ) -> dict:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return {
                        'choices': [
                            {
                                'finish_reason': 'stop',
                                'message': {
                                    'tool_calls': [
                                        {
                                            'id': 'call-1',
                                            'function': {
                                                'name': 'write_file',
                                                'arguments': json.dumps(
                                                    {
                                                        'path': str(result_path),
                                                        'content': '{"ok": true}',
                                                    }
                                                ),
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                return {
                    'choices': [
                        {
                            'finish_reason': 'stop',
                            'message': {'content': '{"status":"written"}'},
                        }
                    ]
                }

            self.runner._request_json_sync = fake_request_sync  # type: ignore[method-assign]

            result = await self.runner.run('translate-review-agent', '/tmp/input.json', 'local-model')

            self.assertEqual(result, '{"status":"written"}')
            self.assertEqual(json.loads(result_path.read_text()), {'ok': True})
            self.assertEqual(calls, 2)


if __name__ == '__main__':
    unittest.main()
