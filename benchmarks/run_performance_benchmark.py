"""Compare Camoufox and upstream Browser-Use on the same local page and model."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import gettempdir
from typing import TYPE_CHECKING

# Benchmarks own their timing and shutdown. Background analytics can otherwise
# add unrelated retry latency after the report has already been written.
os.environ.setdefault('ANONYMIZED_TELEMETRY', 'False')
os.environ.setdefault('BROWSER_USE_CLOUD_SYNC', 'False')

ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from browser_use.llm.base import BaseChatModel

FIXTURE_DIR = ROOT / "tests"
TASK = (
    "On the current test page, enter speed-test as the username, select Morocco "
    "as the country, submit the form, and finish only after the page says Submitted!."
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def fixture_server() -> Iterator[str]:
    handler = partial(QuietHandler, directory=str(FIXTURE_DIR))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/fixture.html"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def create_session(backend: str):
    common = {
        "headless": True,
        "viewport": {"width": 1280, "height": 900},
        "minimum_wait_page_load_time": 0,
        "wait_for_network_idle_page_load_time": 0,
        "wait_between_actions": 0,
        "keep_alive": True,
    }
    if backend == "camoufox":
        from camoufox_browseruse import CamoufoxSession

        return CamoufoxSession(
            humanize=False,
            persistent_context=False,
            block_ip_addresses=False,
            **common,
        )
    from browser_use import Browser

    return Browser(
        enable_default_extensions=False,
        **common,
    )


async def benchmark_backend(
    backend: str,
    url: str,
    iterations: int,
    model: BaseChatModel | None = None,
) -> dict:
    from benchmarks.performance_probe import PerformanceProbe, measure_observation_parts

    probe = PerformanceProbe()
    session = create_session(backend)
    await session.start()
    try:
        await session.navigate_to(url)
        await session.get_browser_state_summary(include_screenshot=True)
        await measure_observation_parts(session, probe, iterations)

        agent_result = None
        if model is not None:
            from browser_use import Agent, Tools

            from benchmarks.performance_probe import TimedChatModel, instrument_session

            tools = Tools()
            if backend == "camoufox":
                from camoufox_browseruse import register_camoufox_tools

                register_camoufox_tools(tools)
            needed_actions = {"input", "select_dropdown", "dropdown_options", "click", "done"}
            for action_name in list(tools.registry.registry.actions):
                if action_name not in needed_actions:
                    tools.exclude_action(action_name)
            instrument_session(session, probe)
            history = await Agent(
                task=TASK,
                llm=TimedChatModel(model, probe),
                browser_session=session,
                tools=tools,
                use_vision=False,
                use_judge=False,
                flash_mode=True,
                max_actions_per_step=5,
                llm_timeout=360,
                step_timeout=420,
            ).run(max_steps=12)
            agent_result = {
                "success": history.is_successful(),
                "steps": history.number_of_steps(),
                "duration_seconds": round(history.total_duration_seconds(), 4),
                "actions": history.action_names(),
                "errors": [error for error in history.errors() if error],
            }

        return {
            "backend": backend,
            "agent": agent_result,
            **probe.report(),
        }
    finally:
        await session.kill()


async def async_main(args: argparse.Namespace, api_key: str) -> dict:
    async with AsyncExitStack() as stack:
        model = None
        if args.agent:
            from camoufox_browseruse import GeminiChatModel

            model = GeminiChatModel(model=args.model, api_key=api_key)
            client = stack.enter_context(model.get_client())
            await stack.enter_async_context(client.aio)
        with fixture_server() as url:
            results = []
            for backend in args.backends:
                print(f"BENCHMARK backend={backend}", flush=True)
                result = await benchmark_backend(backend, url, args.iterations, model)
                results.append(result)
                print(json.dumps(result["summary"], indent=2), flush=True)
            return {"iterations": args.iterations, "url": url, "results": results}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backends", nargs="+", choices=("camoufox", "original"), default=["camoufox", "original"])
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--agent", action="store_true", help="Also run a short Gemini agent task using GEMINI_API_KEY.")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--output", type=Path, default=Path(gettempdir()) / "camoufox-performance.json")
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be at least 1")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() if args.agent else ""
    if args.agent and not api_key:
        parser.error("--agent requires GEMINI_API_KEY. Set it to your Gemini API key and rerun the benchmark.")
    try:
        report = asyncio.run(async_main(args, api_key))
    except ImportError as exc:
        parser.exit(1, f"{exc}\n")
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"REPORT={args.output}", flush=True)


if __name__ == "__main__":
    main()
