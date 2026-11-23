"""Demonstration of an Agent filling out and submitting a form on native Camoufox."""

import asyncio
import os
from pathlib import Path
from browser_use.agent.service import Agent
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.tools.service import Tools
from camoufox_browseruse import open_camoufox_session, register_camoufox_tools

FORM_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>User Onboarding Form</title>
    <style>
        body { font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px; }
        .field { margin-bottom: 15px; }
        label { display: block; font-weight: bold; margin-bottom: 5px; }
        input[type="text"], input[type="email"], textarea, select {
            width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 4px;
        }
        button { background: #2563eb; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
        #success-card { display: none; background: #ecfdf5; border: 1px solid #10b981; padding: 20px; border-radius: 6px; }
    </style>
</head>
<body>
    <h1>Join the Developer Network</h1>
    <form id="signup-form" onsubmit="handleSubmit(event)">
        <div class="field">
            <label for="fullname">Full Name</label>
            <input type="text" id="fullname" name="fullname" placeholder="Enter full name" required />
        </div>
        <div class="field">
            <label for="email">Email Address</label>
            <input type="email" id="email" name="email" placeholder="Enter email address" required />
        </div>
        <div class="field">
            <label for="role">Primary Role</label>
            <select id="role" name="role">
                <option value="frontend">Frontend Developer</option>
                <option value="backend">Backend Developer</option>
                <option value="lead">Engineering Lead</option>
            </select>
        </div>
        <div class="field">
            <label for="bio">Bio / Notes</label>
            <textarea id="bio" name="bio" rows="3" placeholder="Tell us about yourself"></textarea>
        </div>
        <div class="field">
            <label>
                <input type="checkbox" id="newsletter" name="newsletter" /> Subscribe to newsletter
            </label>
        </div>
        <button type="submit" id="submit-btn">Submit Registration</button>
    </form>

    <div id="success-card">
        <h2>🎉 Registration Submitted!</h2>
        <p id="summary-text"></p>
    </div>

    <script>
        function handleSubmit(e) {
            e.preventDefault();
            const name = document.getElementById('fullname').value;
            const email = document.getElementById('email').value;
            const roleEl = document.getElementById('role');
            const role = roleEl.options[roleEl.selectedIndex].text;
            const bio = document.getElementById('bio').value;
            const newsletter = document.getElementById('newsletter').checked;

            document.getElementById('signup-form').style.display = 'none';
            document.getElementById('summary-text').innerText =
                `Welcome ${name} (${email}) - Role: ${role}. Newsletter: ${newsletter}. Notes: ${bio}`;
            document.getElementById('success-card').style.display = 'block';
        }
    </script>
</body>
</html>
"""


class FormFillingAgentLLM:
    """Step-by-step LLM plan filling every field of the form and submitting it."""
    model: str = "agent-form-fill"
    model_name: str = "agent-form-fill"
    provider: str = "camoufox"
    name: str = "form-filler"

    def __init__(self):
        self.step_count = 0

    async def ainvoke(self, messages, output_format=None, **kwargs):
        self.step_count += 1

        # Step 1: Fill full name
        if self.step_count == 1:
            data = {
                "thinking": "Step 1: Type full name into input field [3].",
                "evaluation_previous_goal": "Form opened",
                "memory": "Located form elements",
                "next_goal": "Type full name",
                "action": [{"input": {"index": 3, "text": "Alice Johnson"}}],
            }
        # Step 2: Fill email
        elif self.step_count == 2:
            data = {
                "thinking": "Step 2: Type email address into field [5].",
                "evaluation_previous_goal": "Full name entered",
                "memory": "Name entered",
                "next_goal": "Type email address",
                "action": [{"input": {"index": 5, "text": "alice@example.com"}}],
            }
        # Step 3: Select role dropdown
        elif self.step_count == 3:
            data = {
                "thinking": "Step 3: Select 'Engineering Lead' from dropdown [7].",
                "evaluation_previous_goal": "Email entered",
                "memory": "Email entered",
                "next_goal": "Select role",
                "action": [{"select_dropdown": {"index": 7, "text": "Engineering Lead"}}],
            }
        # Step 4: Fill bio textarea
        elif self.step_count == 4:
            data = {
                "thinking": "Step 4: Type bio into textarea [9].",
                "evaluation_previous_goal": "Role selected",
                "memory": "Role selected",
                "next_goal": "Type bio",
                "action": [{"input": {"index": 9, "text": "Passionate systems engineer."}}],
            }
        # Step 5: Click newsletter checkbox
        elif self.step_count == 5:
            data = {
                "thinking": "Step 5: Toggle newsletter checkbox [11].",
                "evaluation_previous_goal": "Bio entered",
                "memory": "Bio entered",
                "next_goal": "Click newsletter checkbox",
                "action": [{"click": {"index": 11}}],
            }
        # Step 6: Click submit button
        elif self.step_count == 6:
            data = {
                "thinking": "Step 6: Submit the form by clicking button [12].",
                "evaluation_previous_goal": "Checkbox clicked",
                "memory": "All form fields are populated",
                "next_goal": "Click submit button",
                "action": [{"click": {"index": 12}}],
            }
        # Step 7: Done
        else:
            data = {
                "thinking": "Registration confirmed on page. Task complete!",
                "evaluation_previous_goal": "Form submitted",
                "memory": "Confirmation visible",
                "next_goal": "Complete task",
                "action": [{"done": {"text": "Successfully submitted registration for Alice Johnson!"}}],
            }

        return ChatInvokeCompletion(completion=output_format.model_validate(data), usage=None)


async def main():
    print("=" * 60)
    print("CAMOUFOX BROWSER-USE FORM FILLING & SUBMISSION DEMO")
    print("=" * 60)

    fixture_path = Path(__file__).parent / "demo_form.html"
    fixture_path.write_text(FORM_HTML, encoding="utf-8")
    target_url = f"file://{fixture_path.resolve()}"

    tools = Tools()
    register_camoufox_tools(tools)

    headless = os.environ.get("CAMOUFOX_HEADLESS", "1").lower() not in {"0", "false", "no"}
    async with open_camoufox_session(headless=headless, persistent_context=False) as session:
        await session.navigate_to(target_url)
        print(f"Loaded Page: {await session.get_current_page_title()}")

        summary = await session.get_browser_state_summary()
        print("\n--- Form DOM Elements Detected by Camoufox ---")
        print(summary.dom_state.llm_representation())
        print("-----------------------------------------------\n")

        llm = FormFillingAgentLLM()
        agent = Agent(
            task="Fill out the registration form for Alice Johnson and submit it.",
            llm=llm,
            browser_session=session,
            tools=tools,
            use_vision=False,
            use_judge=False,
        )

        history = await agent.run(max_steps=8)

        page = await session.get_current_page()
        summary_text = await page.inner_text("#summary-text", timeout=5000)
        print("\n--- Live Page Verification ---")
        print(f"Confirmation on Page:\n  {summary_text}")
        print(f"Agent Completed: {history.is_successful()}")
        print(f"Agent Result:    {history.final_result()}")
        print("=" * 60)

        hold_seconds = max(0.0, float(os.environ.get("CAMOUFOX_DEMO_HOLD_SECONDS", "0")))
        if hold_seconds:
            print(f"Keeping the completed browser visible for {hold_seconds:g} seconds...")
            await asyncio.sleep(hold_seconds)


if __name__ == "__main__":
    asyncio.run(main())
