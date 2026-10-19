#!/usr/bin/env python3
"""Optional search credentials and engine policy, using isolated state only."""
import contextlib
import io
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'src'))
from ailocal import runtime

SOURCE = REPO / 'src/ailocal/resources/deploy/searxng/settings.yml'


class SearchSettings(unittest.TestCase):
    def render(self, keys, source=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'deploy/searxng/settings.yml'
            src.parent.mkdir(parents=True)
            src.write_text(SOURCE.read_text() if source is None else source)
            output = root / 'state/settings.yml'
            captured = io.StringIO()
            with patch.object(runtime.policy, 'data_root', return_value=root), \
                 patch.object(runtime, 'searxng_settings', return_value=output), \
                 patch.object(runtime, 'env_value', side_effect=lambda name: keys.get(name, '')), \
                 contextlib.redirect_stdout(captured):
                runtime.render_searxng_settings()
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            return output.read_text(), captured.getvalue()

    def test_set_credentials(self):
        keys = {'BRAVE_API': 'fixture-brave-value', 'GITHUB_SEARCH_TOKEN': 'fixture-github-value'}
        text, log = self.render(keys)
        self.assertIn(keys['GITHUB_SEARCH_TOKEN'], text)
        for key in keys.values():
            self.assertNotIn(key, log)
        self.assertNotIn('__GITHUB_SEARCH_TOKEN__', text)
        block = re.search(r'  - name: github code\n((?:    .*\n)+)', text)[0]
        self.assertIn('inactive: false', block)
        self.assertRegex(text, r'(?m)^      - github code$')

    def test_brave_is_never_a_searxng_engine(self):
        # Brave is spent only through the research ledger (ADR 014); an engine
        # here would be a second, uncounted path to the same allowance.
        text, _ = self.render({'BRAVE_API': 'fixture-brave-value'})
        self.assertNotIn('fixture-brave-value', text)
        self.assertNotRegex(text, r'(?m)^\s+- (name: )?braveapi\s*$')
        self.assertNotIn('__BRAVE_API_KEY__', SOURCE.read_text())

    def test_unset_credentials(self):
        text, log = self.render({})
        block = re.search(r'  - name: github code\n((?:    .*\n)+)', text)[0]
        self.assertIn('inactive: true', block)
        self.assertNotRegex(text, r'(?m)^      - github code$')
        self.assertIn('GITHUB_SEARCH_TOKEN is unset', log)
        self.assertNotIn('__GITHUB_SEARCH_TOKEN__', text)

    def test_leaks_fail_without_exposing_value(self):
        for variable in ('BRAVE_API', 'GITHUB_SEARCH_TOKEN'):
            with self.subTest(variable=variable):
                value = 'fixture-secret-already-in-source'
                with self.assertRaises(SystemExit) as error:
                    self.render({variable: value}, SOURCE.read_text() + '\n# ' + value)
                self.assertIn('SETTINGS_SECRET_LEAK', str(error.exception))
                self.assertNotIn(value, str(error.exception))

    def test_registry_and_research_categories(self):
        text = SOURCE.read_text()
        for engine in ('github', 'stackoverflow', 'askubuntu', 'superuser', 'mdn', 'pypi', 'npm', 'docker hub'):
            block = re.search(rf'  - name: {engine}\n((?:    .*\n)+)', text)[0]
            self.assertIn('categories: [it]', block)
            self.assertNotIn('general', block)
        for engine in ('arxiv', 'crossref', 'openalex'):
            block = re.search(rf'  - name: {engine}\n((?:    .*\n)+)', text)[0]
            self.assertIn('categories: [science]', block)


if __name__ == '__main__':
    unittest.main()
