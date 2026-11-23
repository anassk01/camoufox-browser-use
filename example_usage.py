"""Example script demonstrating native Camoufox with Browser-Use 0.13.13."""

import asyncio
from pathlib import Path
from browser_use.agent.service import Agent
from browser_use.tools.service import Tools
from camoufox_browseruse import CamoufoxSession, open_camoufox_session, register_camoufox_tools

# Example mock model for offline execution
# (In production, replace with ChatOpenAI, ChatAnthropic, etc.)
class SimpleDemoModel:
    model: str = "demo-camoufox"
    model_name: str = "demo-camoufox"
    provider: str = "demo"
    name: str = "demo"

    async def ainvoke(self, messages, output_format=None, **kwargs):
        from browser_use.llm.views import ChatInvokeCompletion
        data = {
            "thinking": "Page is loaded, goal reached.",
            "evaluation_previous_goal": "Success",
            "memory": "Camoufox session is running smoothly",
            "next_goal": "Finish task",
            "action": [{"done": {"text": "Mission accomplished on stealth Camoufox!"}}],
        }
        return ChatInvokeCompletion(completion=output_format.model_validate(data), usage=None)


async def main():
    print("Launching Camoufox with Browser-Use...")

    # Keep Browser-Use's standard tools and replace only CDP-only operations
    tools = Tools()
    register_camoufox_tools(tools)

    # Use local fixture for 100% offline testing without consuming data
    fixture_path = Path(__file__).parent / "tests" / "fixture.html"
    target_url = f"file://{fixture_path.resolve()}" if fixture_path.exists() else "about:blank"

    # Use context manager for automatic browser startup and cleanup
    async with open_camoufox_session(headless=True) as session:
        # Navigate to target page
        await session.navigate_to(target_url)
        print(f"Page Title: {await session.get_current_page_title()}")
        print(f"Page URL:   {await session.get_current_page_url()}")

        # Extract DOM state and LLM representation
        summary = await session.get_browser_state_summary(include_screenshot=True)
        print("\n--- DOM Representation for Agent ---")
        print(summary.dom_state.llm_representation())
        print("------------------------------------\n")

        # Run official Browser-Use Agent
        agent = Agent(
            task="Explore the page and complete demo",
            llm=SimpleDemoModel(),
            browser_session=session,
            tools=tools,
            use_vision=False,
            use_judge=False,
        )

        history = await agent.run(max_steps=1)
        print(f"Agent Success: {history.is_successful()}")
        print(f"Agent Output:  {history.final_result()}")


if __name__ == "__main__":
    asyncio.run(main())
