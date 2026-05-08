from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from runtime.async_ops import AsyncCommandRunner, AsyncPathIO


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    system_prompt: str
    tool_names: tuple[str, ...]


class OpenAIAgentRunner:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        agents_dir: Path,
        retries: int = 2,
        backoff_seconds: float = 0.5,
    ) -> None:
        self._base_url = base_url.rstrip('/')
        self._timeout_seconds = timeout_seconds
        self._metadata_timeout_seconds = min(timeout_seconds, 30)
        self._agents_dir = agents_dir
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        self._tool_fns = {
            'read_file': self._tool_read_file,
            'write_file': self._tool_write_file,
            'run_shell_command': self._tool_shell,
        }

    async def default_model(self) -> str:
        data = await self._request_json('GET', 'models', timeout_seconds=self._metadata_timeout_seconds)
        models = data.get('data', [])
        if not models:
            raise RuntimeError('No models returned by API. Check the configured base URL.')
        return models[0]['id']

    async def load_agent(self, name: str) -> AgentDefinition:
        path = self._agents_dir / f'{name}.md'
        text = await AsyncPathIO.read_text(path)
        tool_names: list[str] = []
        body = text
        if text.startswith('---'):
            end = text.find('\n---', 3)
            if end != -1:
                frontmatter = text[3:end]
                body = text[end + 4:].lstrip()
                tool_names.extend(re.findall(r'^\s*-\s+(\S+)\s*$', frontmatter, re.MULTILINE))
        return AgentDefinition(name=name, system_prompt=body, tool_names=tuple(tool_names))

    async def run(self, agent_name: str, prompt: str, model: str) -> str:
        agent = await self.load_agent(agent_name)
        messages: list[dict] = [{'role': 'user', 'content': prompt}]
        tools = [
            tool_def for tool_def in self._tool_definitions()
            if not agent.tool_names or tool_def['function']['name'] in agent.tool_names
        ]

        while True:
            response = await self._request_json(
                'POST',
                'chat/completions',
                {
                    'model': model,
                    'messages': [{'role': 'system', 'content': agent.system_prompt}] + messages,
                    'tools': tools,
                    'max_tokens': 8192,
                },
            )
            choice = response['choices'][0]
            message = choice['message']

            assistant_message: dict[str, object] = {'role': 'assistant'}
            if message.get('content') is not None:
                assistant_message['content'] = message['content']
            if message.get('tool_calls'):
                assistant_message['tool_calls'] = message['tool_calls']
            messages.append(assistant_message)

            if not message.get('tool_calls'):
                return message.get('content') or ''

            for tool_call in message['tool_calls']:
                fn_name = tool_call['function']['name']
                try:
                    fn_args = json.loads(tool_call['function']['arguments'])
                    fn = self._tool_fns.get(fn_name)
                    result = await fn(fn_args) if fn else f'Error: unknown tool {fn_name}'
                except Exception as exc:
                    result = f'Error executing {fn_name}: {exc}'
                messages.append(
                    {
                        'role': 'tool',
                        'tool_call_id': tool_call['id'],
                        'content': str(result),
                    }
                )

    async def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        return await asyncio.to_thread(self._request_json_sync, method, endpoint, payload, timeout_seconds)

    def _request_json_sync(
        self,
        method: str,
        endpoint: str,
        payload: dict | None = None,
        timeout_seconds: int | None = None,
    ) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f'{self._base_url}/{endpoint}',
            data=data,
            headers=self._headers,
            method=method,
        )
        request_timeout = self._timeout_seconds if timeout_seconds is None else timeout_seconds
        for attempt in range(self._retries + 1):
            try:
                with urlopen(request, timeout=request_timeout) as response:
                    return json.loads(response.read())
            except HTTPError as exc:
                body = exc.read().decode()
                if exc.code >= 500 and attempt < self._retries:
                    time.sleep(self._backoff_seconds * (2 ** attempt))
                    continue
                raise RuntimeError(f'API error {exc.code}: {body}') from exc
            except URLError as exc:
                if attempt < self._retries:
                    time.sleep(self._backoff_seconds * (2 ** attempt))
                    continue
                raise RuntimeError(f'Network error: {exc}') from exc
        raise RuntimeError('Exhausted retries while calling agent API.')

    def _tool_definitions(self) -> list[dict]:
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'read_file',
                    'description': 'Read a file and return its contents with line numbers.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'path': {'type': 'string', 'description': 'Absolute path to file'},
                            'limit': {'type': 'integer', 'description': 'Max lines to read'},
                            'offset': {'type': 'integer', 'description': 'Line offset (0-based)'},
                        },
                        'required': ['path'],
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'write_file',
                    'description': 'Write content to a file, creating parent directories as needed.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'path': {'type': 'string', 'description': 'Absolute path to write'},
                            'content': {'type': 'string', 'description': 'Content to write'},
                        },
                        'required': ['path', 'content'],
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'run_shell_command',
                    'description': 'Run a shell command. Review agents use this for append-only logging.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'command': {'type': 'string', 'description': 'Shell command to run'},
                        },
                        'required': ['command'],
                    },
                },
            },
        ]

    @staticmethod
    async def _tool_read_file(args: dict) -> str:
        path = Path(args['path'])
        limit = args.get('limit')
        offset = args.get('offset', 0)
        try:
            lines = (await AsyncPathIO.read_text(path, errors='replace')).splitlines(keepends=True)
        except FileNotFoundError:
            return f'Error: file not found: {path}'
        except Exception as exc:
            return f'Error reading {path}: {exc}'
        if offset:
            lines = lines[offset:]
        if limit:
            lines = lines[:limit]
        numbered = ''.join(f'{offset + index + 1}\t{line}' for index, line in enumerate(lines))
        return numbered or '(empty file)'

    @staticmethod
    async def _tool_write_file(args: dict) -> str:
        path = Path(args['path'])
        content = args['content']
        await AsyncPathIO.mkdir(path.parent, parents=True, exist_ok=True)
        await AsyncPathIO.write_text(path, content)
        return f'Written {len(content)} bytes to {path}'

    @staticmethod
    async def _tool_shell(args: dict) -> str:
        command = args['command']
        result = await AsyncCommandRunner.run_shell(command)
        output = (result.stdout + result.stderr).strip()
        return output or '(no output)'


AgentRunner = OpenAIAgentRunner
