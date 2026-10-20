"""Check tracked release files without reading or printing local user configuration."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_DIRS = {'.venv', 'venv', 'data', 'logs', 'wechatauto_logs', '.scratch',
                  '.ref', 'dist', '__pycache__', '.pytest_cache', 'installers', 'tools'}
SECRET_PATTERNS = [
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}'),
    re.compile(rb'github_pat_[A-Za-z0-9_]{40,}'),
    re.compile(rb'sk-[A-Za-z0-9]{24,}'),
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
]


def main() -> int:
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True)
    names = [name.decode('utf-8') for name in result.stdout.split(b'\0') if name]
    if not names:
        print('No tracked files. Stage the intended public files before checking.')
        return 1
    errors = []
    for name in names:
        path = PurePosixPath(name)
        if (FORBIDDEN_DIRS.intersection(path.parts) or path.name == 'config.yaml'
                or path.name.startswith('config.yaml.bak') or path.name == '.env'
                or (path.name.startswith('.env.') and path.name != '.env.example')
                or path.suffix.lower() in {'.db', '.sqlite', '.sqlite3', '.key', '.pem', '.p12', '.pfx'}
                or name.startswith('docs/archive/') or name.startswith('docs/screenshots/web-console-')):
            errors.append((name, 'private/runtime file'))
            continue
        content = (ROOT / name).read_bytes()
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            errors.append((name, 'possible credential; inspect locally'))
    for name, reason in errors:
        print(f'FAIL {name}: {reason}')
    print(f'Checked {len(names)} tracked files; {len(errors)} issue(s). This check is not a complete secret scanner.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
