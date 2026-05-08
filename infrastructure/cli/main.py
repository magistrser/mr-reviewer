from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path
from typing import Any, TextIO

from application.dto import ReviewRunOptions
from application.ports import ReviewOutputPort
from infrastructure.cli.output import ConsoleReviewOutput
from infrastructure.cli.preview import PromptToolkitReviewPreview
from infrastructure.composition import ReviewApplicationFactory
from infrastructure.workspace.resume import WorkspaceResumeResolver


class CliApplication:
    def __init__(
        self,
        output: ReviewOutputPort,
        previewer: PromptToolkitReviewPreview | None = None,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
    ) -> None:
        self._output = output
        self._previewer = previewer
        self._stdin = stdin
        self._stdout = stdout

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description='Review a GitLab merge request with packaged MR review agents.'
        )
        parser.add_argument('mr_url', nargs='?', help='Full GitLab merge request URL')
        parser.add_argument(
            '--continue',
            dest='continue_workspace',
            nargs='?',
            const='',
            default=None,
            metavar='WORKSPACE',
            help='Continue the latest review workspace, or the named workspace when provided.',
        )
        parser.add_argument(
            '--model',
            default=None,
            help='Model ID to use. Defaults to the first model returned by the agent API.',
        )
        parser.add_argument(
            '--preview-mode',
            action='store_true',
            help='Preview translated review findings interactively before publish.',
        )
        parser.add_argument(
            '--review-root',
            type=Path,
            default=Path.cwd(),
            help='Project root used for .pr-review-workspaces and default root config paths.',
        )
        parser.add_argument(
            '--env-path',
            type=Path,
            default=None,
            help='Path to GitLab credentials file. Defaults to <review-root>/.env.',
        )
        parser.add_argument(
            '--settings-path',
            type=Path,
            default=None,
            help='Path to YAML settings file. Defaults to <review-root>/settings.yml.',
        )
        parser.add_argument(
            '--resources-dir',
            type=Path,
            default=None,
            help='Override packaged review resources directory.',
        )
        parser.add_argument('--agents-dir', type=Path, default=None, help='Override packaged agent prompt directory.')
        return parser

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = CliApplication.build_parser()
        args = parser.parse_args(argv)
        CliApplication.validate_args(parser, args)
        return args

    @staticmethod
    def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
        if args.continue_workspace is not None and args.mr_url:
            parser.error('mr_url cannot be used with --continue.')
        if args.continue_workspace is None and not args.mr_url:
            parser.error('mr_url is required unless --continue is used.')

    @staticmethod
    def _is_tty(stream: Any) -> bool:
        isatty = getattr(stream, 'isatty', None)
        if not callable(isatty):
            return False
        try:
            return bool(isatty())
        except Exception:
            return False

    @classmethod
    def validate_preview_mode(cls, preview_mode: bool, *, stdin: Any, stdout: Any) -> None:
        if not preview_mode:
            return
        if not cls._is_tty(stdin) or not cls._is_tty(stdout):
            raise RuntimeError('--preview-mode requires interactive stdin and stdout TTYs.')

    async def run(self) -> None:
        args = self.parse_args()

        review_root = args.review_root.resolve()
        env_path = args.env_path.resolve() if args.env_path else review_root / '.env'
        resume_workspace = None
        mr_url = args.mr_url
        preview_mode: bool | None = bool(args.preview_mode)

        if args.continue_workspace is not None:
            resumed = WorkspaceResumeResolver.resolve(
                review_root,
                args.continue_workspace or None,
            )
            if resumed.done:
                self._output.detail('', f'Review workspace already complete: {resumed.name}')
                self._output.detail('Workspace', str(resumed.paths.root))
                return
            mr_url = str(resumed.progress.get('mr_url', ''))
            if not mr_url:
                raise RuntimeError(f'{resumed.name} is missing mr_url in progress.json.')
            resume_workspace = resumed.paths.root
            completed_stages = resumed.progress.get('completed_stages', {})
            needs_saved_preview = (
                bool(resumed.progress.get('preview_mode', False))
                and isinstance(completed_stages, dict)
                and 'preview' not in completed_stages
            )
            preview_mode = True if args.preview_mode else None
            self.validate_preview_mode(
                bool(args.preview_mode) or needs_saved_preview,
                stdin=self._stdin,
                stdout=self._stdout,
            )
        else:
            self.validate_preview_mode(bool(args.preview_mode), stdin=self._stdin, stdout=self._stdout)

        use_case = await ReviewApplicationFactory.build_use_case(
            review_root=review_root,
            env_path=env_path,
            settings_path=args.settings_path,
            resources_dir=args.resources_dir,
            agents_dir=args.agents_dir,
            output=self._output,
            previewer=self._previewer,
        )

        assert mr_url is not None
        result = await use_case.execute(
            mr_url,
            options=ReviewRunOptions(
                model=args.model,
                preview_mode=preview_mode,
                resume_workspace=resume_workspace,
            ),
        )
        self._output.completed(result)

    @staticmethod
    def main() -> None:
        output: ReviewOutputPort = ConsoleReviewOutput()
        previewer = PromptToolkitReviewPreview()
        try:
            asyncio.run(CliApplication(output, previewer=previewer).run())
        except KeyboardInterrupt:
            output.cancelled()
            raise SystemExit(130)
        except Exception as exc:
            output.failed(exc)
            raise SystemExit(1)


main = CliApplication.main


if __name__ == '__main__':
    main()
