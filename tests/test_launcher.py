"""Browser-independent tests for executable selection and profile ownership."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import BrowserContext

from camoufox_browseruse import CamoufoxSession, launcher
from camoufox_browseruse.launcher import (
    ProfileLock,
    ProfileLockError,
    prepare_launch_environment,
    resolve_executable_path,
)
from camoufox_browseruse.session_manager import CamoufoxSessionManager


class TestResolveExecutablePath(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, clear=True))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.binary = self.directory / 'firefox'
        self.binary.touch(mode=0o700)

    def test_explicit_executable_wins_over_environment(self):
        os.environ['CAMOUFOX_EXECUTABLE_PATH'] = str(self.directory / 'missing')
        self.assertEqual(resolve_executable_path(self.binary), str(self.binary))

    def test_expands_user_home(self):
        home = str(self.directory)
        with patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}):
            self.assertEqual(resolve_executable_path('~/firefox'), str(self.binary))

    def test_invalid_explicit_path_fails_even_with_valid_environment(self):
        os.environ['CAMOUFOX_EXECUTABLE_PATH'] = str(self.binary)
        for invalid in (self.directory / 'missing', self.directory):
            with self.subTest(path=invalid), self.assertRaisesRegex(FileNotFoundError, 'executable_path'):
                resolve_executable_path(invalid)

    @unittest.skipUnless(os.name == 'posix', 'POSIX executable permissions')
    def test_non_executable_file_is_rejected(self):
        self.binary.chmod(0o600)
        with self.assertRaisesRegex(FileNotFoundError, 'executable file'):
            resolve_executable_path(self.binary)

    def test_falls_back_to_environment_override(self):
        os.environ['CAMOUFOX_EXECUTABLE_PATH'] = str(self.binary)
        self.assertEqual(resolve_executable_path(), str(self.binary))

    def test_invalid_environment_override_fails(self):
        for invalid in ('', str(self.directory / 'missing')):
            with (
                self.subTest(path=invalid),
                patch.dict(os.environ, {'CAMOUFOX_EXECUTABLE_PATH': invalid}),
                self.assertRaisesRegex(FileNotFoundError, 'CAMOUFOX_EXECUTABLE_PATH'),
            ):
                resolve_executable_path()

    def test_defaults_to_camoufox_binary_resolution(self):
        self.assertIsNone(resolve_executable_path())


class TestPrepareLaunchEnvironment(unittest.TestCase):
    def test_copies_supplied_mapping_and_overrides_wayland_settings(self):
        base = {'WAYLAND_DISPLAY': 'wayland-0', 'MOZ_ENABLE_WAYLAND': '1', 'CUSTOM': 'value'}
        result = prepare_launch_environment(MappingProxyType(base))
        self.assertEqual(result, {'MOZ_ENABLE_WAYLAND': '0', 'CUSTOM': 'value'})
        self.assertEqual(base, {'WAYLAND_DISPLAY': 'wayland-0', 'MOZ_ENABLE_WAYLAND': '1', 'CUSTOM': 'value'})

    def test_defaults_to_copy_of_process_environment(self):
        with patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-0', 'CUSTOM': 'process'}, clear=True):
            self.assertEqual(prepare_launch_environment(), {'MOZ_ENABLE_WAYLAND': '0', 'CUSTOM': 'process'})
            self.assertEqual(os.environ['WAYLAND_DISPLAY'], 'wayland-0')
            self.assertNotIn('MOZ_ENABLE_WAYLAND', os.environ)

    def test_empty_mapping_leaves_process_environment_out(self):
        with patch.dict(os.environ, {'CUSTOM': 'process'}):
            self.assertEqual(prepare_launch_environment({}), {'MOZ_ENABLE_WAYLAND': '0'})


class TestProfileLock(unittest.TestCase):
    def setUp(self):
        self.profile = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'profile'
        self.lock = ProfileLock(self.profile)
        self.addCleanup(self.lock.release)

    def test_acquire_records_pid_and_release_allows_reacquiring(self):
        self.lock.acquire()
        lockfile = self.profile / '.camoufox_browseruse.lock'
        self.assertEqual(lockfile.read_text(encoding='ascii'), f'{os.getpid()}\n')
        with self.assertRaises(ProfileLockError):
            ProfileLock(self.profile).acquire()
        self.lock.release()
        self.lock.release()
        self.assertEqual(lockfile.exists(), launcher.fcntl is not None)
        self.lock.acquire()

    def test_context_manager_releases_on_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'body failed'), self.lock as held:
            self.assertIs(held, self.lock)
            raise RuntimeError('body failed')
        with ProfileLock(self.profile):
            pass

    def test_cross_process_contention_and_reacquisition(self):
        cmd = [sys.executable, '-B', '-c', """
import sys
from camoufox_browseruse.launcher import ProfileLock, ProfileLockError

try:
    with ProfileLock(sys.argv[1]):
        pass
except ProfileLockError:
    sys.exit(3)
""", str(self.profile)]
        with self.lock:
            contender = subprocess.run(cmd, capture_output=True, check=False, text=True, timeout=20)
            self.assertEqual(contender.returncode, 3, contender.stderr)
            self.assertEqual(self.lock.path.read_text(encoding='ascii'), f'{os.getpid()}\n')
        successor = subprocess.run(cmd, capture_output=True, check=False, text=True, timeout=20)
        self.assertEqual(successor.returncode, 0, successor.stderr)

    def test_exclusive_create_fallback_removes_only_its_own_file(self):
        with patch.object(launcher, 'fcntl', None):
            with self.lock:
                self.assertEqual(self.lock.path.read_text(encoding='ascii'), f'{os.getpid()}\n')
                with self.assertRaises(ProfileLockError):
                    ProfileLock(self.profile).acquire()
                self.assertTrue(self.lock.path.is_file())
            self.assertFalse(self.lock.path.exists())
            with ProfileLock(self.profile):
                pass

    def test_failed_pid_write_closes_descriptor_and_allows_retry(self):
        with (
            patch.object(launcher.os, 'write', side_effect=OSError('write failed')) as write,
            self.assertRaisesRegex(OSError, 'write failed'),
        ):
            self.lock.acquire()
        fd = write.call_args.args[0]
        with self.assertRaises(OSError):
            os.fstat(fd)
        with ProfileLock(self.profile):
            pass


class TestSessionLaunchLock(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.profile = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'profile'
        self.manager = CamoufoxSessionManager(SimpleNamespace(
            browser_profile=SimpleNamespace(),
            _launch_options={'executable_path': None, 'env': {}},
            _context_options={'user_data_dir': self.profile},
            event_bus=SimpleNamespace(dispatch=AsyncMock()),
            security_watchdog=SimpleNamespace(route_request=Mock()),
        ))
        self.module = 'camoufox_browseruse.session_manager'
        self.enterContext(patch.dict(os.environ, clear=True))

    async def test_releases_profile_if_option_builder_fails(self):
        with (
            patch(f'{self.module}.build_camoufox_launch_options', side_effect=RuntimeError('builder failed')),
            self.assertRaisesRegex(RuntimeError, 'builder failed'),
        ):
            await self.manager.launch()
        with ProfileLock(self.profile):
            pass

    async def test_releases_profile_if_browser_entry_fails(self):
        cm = AsyncMock()
        cm.__aenter__.side_effect = RuntimeError('browser failed')
        with (
            patch(f'{self.module}.build_camoufox_launch_options', return_value={}),
            patch(f'{self.module}.AsyncCamoufox', return_value=cm),
            self.assertRaisesRegex(RuntimeError, 'browser failed'),
        ):
            await self.manager.launch()
        cm.__aexit__.assert_awaited_once()
        with ProfileLock(self.profile):
            pass

    async def test_holds_profile_until_browser_shutdown_even_if_exit_fails(self):
        cm = AsyncMock()
        context = Mock(spec=BrowserContext)
        context.pages = [Mock()]
        cm.__aenter__.return_value = context
        held_during_exit = False

        async def fail_exit(*_args):
            nonlocal held_during_exit
            try:
                with ProfileLock(self.profile):
                    pass
            except ProfileLockError:
                held_during_exit = True
            raise RuntimeError('exit failed')

        cm.__aexit__.side_effect = fail_exit
        with (
            patch(f'{self.module}.build_camoufox_launch_options', return_value={}) as build,
            patch(f'{self.module}.AsyncCamoufox', return_value=cm),
            patch.object(self.manager, '_register_page'),
        ):
            await self.manager.launch()
            self.assertNotIn('executable_path', build.call_args.kwargs)
            self.assertEqual(build.call_args.kwargs['env'], {'MOZ_ENABLE_WAYLAND': '0'})
            with self.assertRaises(ProfileLockError):
                ProfileLock(self.profile).acquire()
            await self.manager.close()

        cm.__aexit__.assert_awaited_once()
        self.assertTrue(held_during_exit)
        with ProfileLock(self.profile):
            pass

    async def test_kill_releases_profile_if_storage_export_fails(self):
        session = CamoufoxSession(headless=True, storage_state=str(self.profile / 'state.json'))
        manager = session.session_manager
        lock = ProfileLock(self.profile)
        lock.acquire()
        self.addCleanup(lock.release)
        manager._profile_lock = lock
        manager.camoufox = AsyncMock()
        manager.browser = manager.context = Mock(spec=BrowserContext)

        with (
            patch.object(session.storage_watchdog, 'export', side_effect=OSError('export failed')),
            self.assertRaisesRegex(OSError, 'export failed'),
        ):
            await session.kill()

        self.assertIsNone(manager.context)
        with ProfileLock(self.profile):
            pass
