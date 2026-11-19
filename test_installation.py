"""Launch smoke checks. Run with `python test_installation.py`."""

import asyncio
from importlib.metadata import PackageNotFoundError, packages_distributions, version

from camoufox.async_api import AsyncCamoufox

from camoufox_browseruse import CamoufoxSession, open_camoufox_session


async def check_native_camoufox():
    print("Testing native Camoufox launch...")
    async with AsyncCamoufox(headless=True) as browser:
        page = await browser.new_page()
        await page.goto("about:blank")
        print("  ✓ Native Camoufox launched and page opened successfully.")


async def check_camoufox_session():
    print("Testing native CamoufoxSession...")
    session = CamoufoxSession(headless=True, persistent_context=False)
    await session.start()
    print("  ✓ CamoufoxSession started successfully.")
    await session.stop()
    print("  ✓ CamoufoxSession stopped cleanly.")


async def check_native_package():
    print("Testing camoufox_browseruse open_camoufox_session helper...")
    async with open_camoufox_session(headless=True, persistent_context=False) as session:
        await session.navigate_to("about:blank")
        summary = await session.get_browser_state_summary(include_screenshot=False)
        assert summary.url == "about:blank"
        print("  ✓ camoufox_browseruse native session verified.")


def camoufox_version() -> str:
    try:
        return version('camoufox')
    except PackageNotFoundError:
        distributions = packages_distributions().get('camoufox', [])
        return version(distributions[0]) if distributions else 'editable / local'


async def main():
    print("========================================")
    print("Camoufox & Browser-Use Installation Test")
    print("========================================")
    print(f"browser-use:          {version('browser-use')}")
    print(f"camoufox:             {camoufox_version()}")
    try:
        print(f"camoufox-browser-use: {version('camoufox-browser-use')}")
    except PackageNotFoundError:
        print("camoufox-browser-use: editable / local")
    print("----------------------------------------")
    await check_native_camoufox()
    await check_camoufox_session()
    await check_native_package()
    print("========================================")
    print("Installation smoke checks passed for the tested launch and session paths.")
    print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
