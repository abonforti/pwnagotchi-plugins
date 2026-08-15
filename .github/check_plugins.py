#!/usr/bin/env python3
"""
Repository checks for pwnagotchi plugins.

Runs without pwnagotchi installed: everything is done on the syntax tree, so no
plugin is ever imported and no hardware is touched.

Lives under .github/ on purpose. Anything ending in .py in the repository root
is treated as a plugin by `pwnagotchi plugins`, and a checker is not one.
"""

import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REQUIRED_METADATA = ('__author__', '__version__', '__license__', '__description__')

# Deliberately narrow. The point is to catch a real key pasted into a config
# example, not to flag every string that looks like one.
SECRET_PATTERNS = (
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private key'),
    (re.compile(r'\bsk_[A-Za-z0-9]{16,}'), 'api key'),
    (re.compile(r'''(?i)\b(api_key|token|secret|password)\b\s*=\s*["'][A-Za-z0-9/+_-]{16,}["']'''),
     'credential assignment'),
)

PLACEHOLDERS = ('your', 'changeme', 'example', 'xxx', 'here', 'placeholder')

failures = []
notes = []


def fail(path, message):
    failures.append("%s: %s" % (os.path.relpath(path, ROOT), message))


def plugin_files():
    for name in sorted(os.listdir(ROOT)):
        if name.endswith('.py'):
            yield os.path.join(ROOT, name)


def check_flat_layout():
    """
    `pwnagotchi plugins update` unpacks with strip_dirs=1 and _get_available()
    globs a single level, so a plugin in a subdirectory is invisible to both
    `list` and `install`.
    """
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in ('.git', '.github', '__pycache__')]
        if dirpath == ROOT:
            continue
        for name in filenames:
            if name.endswith('.py'):
                fail(os.path.join(dirpath, name),
                     'plugins must live in the repository root to be installable')


def check_plugin(path, tree):
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    plugin_classes = []
    for node in classes:
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, 'id', '')
            if name == 'Plugin':
                plugin_classes.append(node)
                break

    if not plugin_classes:
        fail(path, 'no class deriving from plugins.Plugin')
        return

    if len(plugin_classes) > 1:
        fail(path, 'more than one plugin class in the same file')

    for node in plugin_classes:
        declared = set()
        for stmt in node.body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        declared.add(target.id)

        for key in REQUIRED_METADATA:
            if key not in declared:
                fail(path, 'missing %s' % key)

        if '__help__' not in declared:
            notes.append('%s: no __help__, so `plugins list -i` shows nothing useful'
                         % os.path.relpath(path, ROOT))


def check_secrets(path, source):
    for lineno, line in enumerate(source.splitlines(), 1):
        for pattern, what in SECRET_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            if any(word in line.lower() for word in PLACEHOLDERS):
                continue
            fail(path, 'line %d looks like a %s' % (lineno, what))


def main():
    check_flat_layout()

    found = False
    for path in plugin_files():
        found = True
        with open(path, encoding='utf-8') as handle:
            source = handle.read()

        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError as e:
            fail(path, 'does not parse: line %s, %s' % (e.lineno, e.msg))
            continue

        check_plugin(path, tree)
        check_secrets(path, source)

    if not found:
        fail(ROOT, 'no plugins in the repository root')

    for note in notes:
        print('note: %s' % note)

    if failures:
        print('\n%d problem(s):' % len(failures))
        for failure in failures:
            print('  %s' % failure)
        return 1

    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
