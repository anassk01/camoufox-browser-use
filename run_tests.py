import os
import signal
import subprocess
import sys
import time
import unittest


def preflight_check() -> bool:
    print("Running preflight browser launch check in isolated subprocess (60s timeout)...")
    cmd = [
        sys.executable,
        "-c",
        (
            "import asyncio\n"
            "from camoufox_browseruse import open_camoufox_session\n"
            "async def _run():\n"
            "    async with open_camoufox_session(headless=True, persistent_context=False) as s:\n"
            "        await s.navigate_to('about:blank')\n"
            "        await s.get_current_page_title()\n"
            "asyncio.run(_run())\n"
        ),
    ]
    env = dict(os.environ)
    env["MOZ_ENABLE_WAYLAND"] = "0"
    env.pop("WAYLAND_DISPLAY", None)

    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        print(f"❌ Failed to spawn preflight subprocess: {exc}")
        return False

    try:
        _, stderr = p.communicate(timeout=60)
        if p.returncode == 0:
            print("✓ Preflight browser launch succeeded.\n")
            return True
        else:
            print(f"❌ Preflight check failed with exit code {p.returncode}:")
            if stderr:
                print(stderr.decode(errors="replace"))
            return False
    except subprocess.TimeoutExpired:
        print("❌ Preflight check timed out! Camoufox failed to start within 60 seconds.")
        print("   Diagnostic: Terminating preflight process group...")
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass
        return False


def main():
    print("=" * 60)
    print("CAMOUFOX BROWSER-USE UNIFIED TEST RUNNER")
    print("=" * 60)

    # Ensure clean environment variables
    os.environ.setdefault("MOZ_ENABLE_WAYLAND", "0")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "False")
    os.environ.pop("WAYLAND_DISPLAY", None)

    print("Running browser-independent regression tests...")
    loader = unittest.TestLoader()
    unit_suite = loader.loadTestsFromNames([
        'tests.test_unit_core',
        'tests.test_evaluation_contracts',
        'tests.test_launcher',
        'tests.test_learning',
        'tests.test_runner',
    ])
    unit_result = unittest.TextTestRunner(verbosity=2).run(unit_suite)
    if not unit_result.wasSuccessful():
        sys.exit(1)

    if not preflight_check():
        print("Browser preflight failed; browser-independent regressions passed.")
        sys.exit(1)

    start_time = time.perf_counter()

    suite = loader.loadTestsFromNames([
        'tests.test_full_suite',
        'tests.test_stress_suite',
        'tests.test_reliability',
        'tests.test_iframe_parity',
        'tests.test_upstream_01313',
        'tests.test_runner_browser',
        'tests.test_legacy_dom',
        'tests.test_sensitive_input',
    ])

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    total_time = time.perf_counter() - start_time
    print("\n" + "=" * 60)
    print(f"Tests Run:  {unit_result.testsRun + result.testsRun}")
    print(f"Failures:   {len(unit_result.failures) + len(result.failures)}")
    print(f"Errors:     {len(unit_result.errors) + len(result.errors)}")
    print(f"Total Time: {total_time:.2f}s")
    print("=" * 60)

    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == "__main__":
    main()
