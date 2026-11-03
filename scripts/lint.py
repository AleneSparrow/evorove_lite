"""Dependency-free syntax and whitespace checks."""
import ast
from pathlib import Path

for folder in ('app', 'tests', 'scripts'):
    for file in Path(folder).rglob('*.py'):
        text = file.read_text()
        ast.parse(text, filename=str(file))
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip() != line or '\t' in line:
                raise SystemExit(f'{file}:{number}: whitespace error')
print('Syntax and whitespace checks passed')
