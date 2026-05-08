from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path('/Users/franz/review')


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_domain_does_not_import_application_or_infrastructure(self) -> None:
        violations: list[str] = []
        for path in (PROJECT_ROOT / 'domain').rglob('*.py'):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or '']
                else:
                    continue
                for name in names:
                    if name == 'application' or name.startswith('application.'):
                        violations.append(f'{path}: imports {name}')
                    if name == 'infrastructure' or name.startswith('infrastructure.'):
                        violations.append(f'{path}: imports {name}')

        self.assertEqual(violations, [])

    def test_source_packages_do_not_contain_generated_python_artifacts(self) -> None:
        generated = []
        for package_name in ('application', 'domain', 'infrastructure', 'runtime', 'resources'):
            package_path = PROJECT_ROOT / package_name
            generated.extend(str(path.relative_to(PROJECT_ROOT)) for path in package_path.rglob('__pycache__'))
            generated.extend(str(path.relative_to(PROJECT_ROOT)) for path in package_path.rglob('*.pyc'))

        self.assertEqual(generated, [])


if __name__ == '__main__':
    unittest.main()
