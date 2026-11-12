"""Browser-independent public API and evaluation output contracts."""

import subprocess
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import camoufox_browseruse as package
from camoufox_browseruse.helpers import NoFakeCDPClient
from camoufox_browseruse.tools.overrides import (
    MAX_EVALUATE_OUTPUT_CHARS,
    _bounded_evaluate_output,
)


class TestPackageExports(unittest.TestCase):
    def test_default_paths_are_under_the_current_home(self):
        self.assertEqual(package.DEFAULT_DATA_DIR, Path.home() / '.camoufox_browseruse')
        self.assertEqual(package.DEFAULT_PROFILE_DIR, package.DEFAULT_DATA_DIR / 'profile')

    def test_public_api_lists_eager_and_lazy_exports(self):
        eager_exports = {
            'CamoufoxSession',
            'ProfileLock',
            'ProfileLockError',
            'apply_camoufox_mcp_compat',
            'open_camoufox_session',
            'register_camoufox_tools',
            'resolve_executable_path',
        }
        lazy_exports = {
            'BROWSER_OPERATION_INSTRUCTIONS',
            'BrowserTaskRunner',
            'ExperienceLearner',
            'GeminiChatModel',
            'GeminiKeyPoolChatModel',
            'RunLimits',
        }
        self.assertCountEqual(package.__all__, eager_exports | lazy_exports)
        for name in eager_exports:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(package, name)))

    def test_import_leaves_optional_modules_unloaded_until_accessed(self):
        result = subprocess.run(
            [sys.executable, '-B', '-c', """
import sys
import camoufox_browseruse as package

for name in ('runner', 'learning', 'llm'):
    assert f'camoufox_browseruse.{name}' not in sys.modules, name

from camoufox_browseruse import BROWSER_OPERATION_INSTRUCTIONS, BrowserTaskRunner, ExperienceLearner, RunLimits
from camoufox_browseruse import learning, runner

assert BrowserTaskRunner is runner.BrowserTaskRunner
assert RunLimits is runner.RunLimits
assert BROWSER_OPERATION_INSTRUCTIONS is runner.BROWSER_OPERATION_INSTRUCTIONS
assert ExperienceLearner is learning.ExperienceLearner
assert 'camoufox_browseruse.llm' not in sys.modules
"""],
            capture_output=True,
            check=False,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gemini_exports_resolve_to_the_provider_module(self):
        provider = ModuleType('camoufox_browseruse.llm')
        provider.GeminiChatModel = type('GeminiChatModel', (), {})
        provider.GeminiKeyPoolChatModel = type('GeminiKeyPoolChatModel', (), {})
        with patch.dict(sys.modules, {provider.__name__: provider}):
            self.assertIs(package.GeminiChatModel, provider.GeminiChatModel)
            self.assertIs(package.GeminiKeyPoolChatModel, provider.GeminiKeyPoolChatModel)

    def test_missing_gemini_dependency_has_install_guidance(self):
        with patch.dict(sys.modules, {'camoufox_browseruse.llm': None}):
            for name in ('GeminiChatModel', 'GeminiKeyPoolChatModel'):
                with self.subTest(name=name), self.assertRaisesRegex(
                    ImportError, r"pip install 'camoufox-browser-use\[gemini\]'"
                ):
                    getattr(package, name)

    def test_unknown_export_raises_attribute_error(self):
        with self.assertRaisesRegex(AttributeError, 'unknown_export'):
            _ = package.unknown_export


class TestNoFakeCDPClient(unittest.IsolatedAsyncioTestCase):
    def test_guard_is_false_and_rejects_attribute_access_and_calls(self):
        guard = NoFakeCDPClient()
        self.assertFalse(guard)
        with self.assertRaisesRegex(RuntimeError, r"'send'.*Camoufox.*Playwright"):
            guard.send('Page.enable')
        with self.assertRaisesRegex(RuntimeError, r"'__call__'.*native CamoufoxSession"):
            guard()

    async def test_async_context_returns_guard_and_propagates_errors(self):
        guard = NoFakeCDPClient()
        with self.assertRaisesRegex(RuntimeError, "'send'"):
            async with guard as entered:
                self.assertIs(entered, guard)
                entered.send('Page.enable')

    async def test_session_routes_cdp_access_through_guard(self):
        session = package.CamoufoxSession(headless=True)
        self.assertIsInstance(session.cdp_client, NoFakeCDPClient)
        with self.assertRaisesRegex(RuntimeError, "'get_or_create_cdp_session'"):
            await session.get_or_create_cdp_session()


class TestEvaluateOutputContract(unittest.TestCase):
    def test_small_output_is_unchanged(self):
        self.assertEqual(_bounded_evaluate_output({'value': 1}), ("{'value': 1}", False))

    def test_large_output_is_windowed_with_head_tail_and_guidance(self):
        raw = 'A' * MAX_EVALUATE_OUTPUT_CHARS + 'TAIL'
        output, truncated = _bounded_evaluate_output(raw)
        self.assertTrue(truncated)
        self.assertTrue(output.startswith('A'))
        self.assertTrue(output.endswith('TAIL'))
        self.assertIn('rerun a narrower query', output)
        self.assertLessEqual(len(output), MAX_EVALUATE_OUTPUT_CHARS)


if __name__ == '__main__':
    unittest.main()
