"""Fail closed when a release migration upgrade is not additive-only."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

MIGRATIONS_DIRECTORY = Path('migrations/versions')
MINIMUM_CREATE_INDEX_ARGUMENTS = 2
EXPECTED_ALEMBIC_CHAIN = (
    ('20260827_049', '20260826_048'),
    ('20260827_050', '20260827_049'),
)
DISALLOWED_OPERATIONS = {
    'alter_column',
    'drop_column',
    'drop_constraint',
    'drop_index',
    'drop_table',
    'rename_table',
}
DESTRUCTIVE_SQL = re.compile(r'\b(?:DELETE|DROP|TRUNCATE)\b', re.IGNORECASE)


def migration_findings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    upgrades = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == 'upgrade'
    ]
    if len(upgrades) != 1:
        return ['upgrade-function-count']
    findings = []
    for node in ast.walk(upgrades[0]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in DISALLOWED_OPERATIONS:
                findings.append(f'op.{node.func.attr}')
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and DESTRUCTIVE_SQL.search(node.value)
        ):
            findings.append('destructive-sql')
    return sorted(set(findings))


def _assignment_value(tree: ast.Module, name: str) -> str | None:
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        if isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _origin_scope_findings(path: Path) -> list[str]:
    source = path.read_text(encoding='utf-8')
    if 'DBAMV' in source or 'glosa' in source.lower():
        return ['origin-scope-forbidden-reference']
    tree = ast.parse(source, filename=str(path))
    created_tables = {
        call.args[0].value
        for call in ast.walk(tree)
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == 'create_table'
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        )
    }
    created_indexes = {
        call.args[1].value
        for call in ast.walk(tree)
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == 'create_index'
            and len(call.args) >= MINIMUM_CREATE_INDEX_ARGUMENTS
            and isinstance(call.args[1], ast.Constant)
            and isinstance(call.args[1].value, str)
        )
    }
    findings = []
    if created_tables != {
        'auditoria_agendamentos',
        'agendamento_origens',
        'agendamento_origem_eventos',
    }:
        findings.append('origin-scope-tables')
    if created_indexes != {'agendamento_origens'}:
        findings.append('origin-scope-indexes')
    return findings


def release_migration_findings() -> list[str]:
    findings = []
    for revision, expected_down_revision in EXPECTED_ALEMBIC_CHAIN:
        paths = list(MIGRATIONS_DIRECTORY.glob(f'{revision}_*.py'))
        if len(paths) != 1:
            findings.append(f'{revision}: migration-count')
            continue
        path = paths[0]
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        if _assignment_value(tree, 'revision') != revision:
            findings.append(f'{revision}: revision')
        if _assignment_value(tree, 'down_revision') != expected_down_revision:
            findings.append(f'{revision}: down-revision')
        findings.extend(
            f'{revision}: {finding}' for finding in migration_findings(path)
        )
        if revision == '20260827_049':
            findings.extend(
                f'{revision}: {finding}'
                for finding in _origin_scope_findings(path)
            )
    return sorted(findings)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('paths', nargs='*', type=Path)
    args = parser.parse_args()
    if not args.paths:
        findings = release_migration_findings()
        if findings:
            print('\n'.join(findings))
            return 1
        print('additive-migrations-ok: linear-head=20260827_050')
        return 0
    rejected = {
        str(path): findings
        for path in args.paths
        if (findings := migration_findings(path))
    }
    if rejected:
        for path, path_findings in rejected.items():
            print(f'{path}: {",".join(path_findings)}')
        return 1
    print('additive-migrations-ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
