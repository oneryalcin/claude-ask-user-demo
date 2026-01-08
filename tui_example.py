#!/usr/bin/env python3
"""Complete AskUserQuestion implementation - TUI + SDK integration.

Features:
- Multiple questions in tabs (like Claude Code)
- Single-select (radio buttons) and multi-select (checkboxes)
- Tab/Arrow key navigation
- Review screen before submit
- Full integration with Claude Agent SDK via MCP
"""

import asyncio
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    RadioButton,
    RadioSet,
    Static,
    TabbedContent,
    TabPane,
)


class QuestionPane(TabPane):
    """A single question tab with options."""

    def __init__(self, question: dict, index: int, **kwargs):
        self.question_data = question
        self.question_index = index
        self.multi_select = question.get("multiSelect", False)
        super().__init__(question["header"], id=f"q_{index}", **kwargs)

    def compose(self) -> ComposeResult:
        yield Static(f"[bold]{self.question_data['question']}[/bold]\n", classes="question-text")
        options = self.question_data.get("options", [])

        with VerticalScroll(classes="options-container"):
            if self.multi_select:
                for i, opt in enumerate(options):
                    yield Checkbox(
                        f"{opt['label']}\n  [dim]{opt.get('description', '')}[/dim]",
                        id=f"opt_{self.question_index}_{i}",
                    )
                yield Checkbox(
                    "[italic]Type something...[/italic]\n  [dim]Custom response[/dim]",
                    id=f"opt_{self.question_index}_other",
                )
            else:
                with RadioSet(id=f"radio_{self.question_index}"):
                    for i, opt in enumerate(options):
                        label = opt["label"]
                        if i == 0:
                            label = f"{label} (Recommended)"
                        yield RadioButton(
                            f"{label}\n  [dim]{opt.get('description', '')}[/dim]",
                            id=f"opt_{self.question_index}_{i}",
                        )
                    yield RadioButton(
                        "[italic]Type something...[/italic]\n  [dim]Custom response[/dim]",
                        id=f"opt_{self.question_index}_other",
                    )

    def get_selection(self) -> str | list[str] | None:
        options = self.question_data.get("options", [])
        if self.multi_select:
            selected = []
            for i, opt in enumerate(options):
                cb = self.query_one(f"#opt_{self.question_index}_{i}", Checkbox)
                if cb.value:
                    selected.append(opt["label"])
            other = self.query_one(f"#opt_{self.question_index}_other", Checkbox)
            if other.value:
                selected.append("__OTHER__")
            return selected if selected else None
        else:
            rs = self.query_one(f"#radio_{self.question_index}", RadioSet)
            if rs.pressed_index is not None:
                idx = rs.pressed_index
                return options[idx]["label"] if idx < len(options) else "__OTHER__"
            return None


class ReviewPane(TabPane):
    """Review all answers before submit."""

    def __init__(self, questions: list[dict], **kwargs):
        self.questions = questions
        super().__init__("Submit", id="review", **kwargs)

    def compose(self) -> ComposeResult:
        yield Static("[bold]Review your answers[/bold]\n", classes="review-header")
        yield Static("", id="review-content", classes="review-content")
        yield Horizontal(
            Button("Submit answers", id="submit", variant="primary"),
            Button("Cancel", id="cancel", variant="default"),
            classes="button-row",
        )


class AskUserApp(App):
    """Main TUI application."""

    CSS = """
    .question-text { margin-bottom: 1; padding: 1; }
    .options-container { height: auto; max-height: 80%; }
    .review-content { margin: 1; padding: 1; border: solid green; }
    .button-row { margin-top: 2; height: auto; }
    """

    BINDINGS = [
        Binding("left", "prev_tab", "Previous"),
        Binding("right", "next_tab", "Next"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, questions: list[dict]):
        super().__init__()
        self.questions = questions
        self.answers: dict[str, str] = {}
        self.cancelled = False

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for i, q in enumerate(self.questions):
                yield QuestionPane(q, i)
            yield ReviewPane(self.questions)
        yield Footer()

    def action_prev_tab(self) -> None:
        self.query_one("#tabs", TabbedContent).action_previous_tab()

    def action_next_tab(self) -> None:
        self._update_review()
        self.query_one("#tabs", TabbedContent).action_next_tab()

    def action_cancel(self) -> None:
        self.cancelled = True
        self.exit()

    def _update_review(self) -> None:
        lines = []
        all_answered = True
        for i, q in enumerate(self.questions):
            pane = self.query_one(f"#q_{i}", QuestionPane)
            sel = pane.get_selection()
            if sel is None:
                all_answered = False
                lines.append(f"[dim]● {q['question']}[/dim]\n  [yellow]Not answered[/yellow]")
            else:
                if isinstance(sel, list):
                    sel_text = ", ".join(s for s in sel if s != "__OTHER__")
                else:
                    sel_text = sel
                if sel == "__OTHER__" or (isinstance(sel, list) and "__OTHER__" in sel):
                    sel_text = sel_text + " (+ custom)" if sel_text else "(custom)"
                lines.append(f"● {q['question']}\n  [green]→ {sel_text}[/green]")

        content = self.query_one("#review-content", Static)
        if not all_answered:
            header = "[yellow bold]⚠ Not all questions answered[/yellow bold]\n\n"
        else:
            header = "[green]Ready to submit?[/green]\n\n"
        content.update(header + "\n\n".join(lines))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self._collect_answers()
            self.exit()
        elif event.button.id == "cancel":
            self.cancelled = True
            self.exit()

    def on_tabbed_content_tab_activated(self, event) -> None:
        if "review" in str(event.tab.id):
            self._update_review()

    def _collect_answers(self) -> None:
        for i, q in enumerate(self.questions):
            pane = self.query_one(f"#q_{i}", QuestionPane)
            sel = pane.get_selection()
            if sel is not None:
                if isinstance(sel, list):
                    self.answers[q["header"]] = ", ".join(s for s in sel if s != "__OTHER__")
                else:
                    self.answers[q["header"]] = sel if sel != "__OTHER__" else ""


async def ask_user_interactive(questions: list[dict]) -> dict[str, str] | None:
    """Run TUI and return answers."""
    app = AskUserApp(questions)
    await app.run_async()
    return None if app.cancelled else app.answers


# === SDK INTEGRATION ===

ASK_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "Questions to ask (1-4)",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "header": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["label", "description"],
                        },
                    },
                    "multiSelect": {"type": "boolean", "default": False},
                },
                "required": ["question", "header", "options", "multiSelect"],
            },
        }
    },
    "required": ["questions"],
}


@tool(
    "ask_user",
    """Ask user questions with rich TUI. Supports multiple questions with tabs,
single/multi-select options, and review before submit. Use for gathering preferences.""",
    ASK_USER_SCHEMA,
)
async def ask_user_tool(args: dict[str, Any]) -> dict[str, Any]:
    """Handle ask_user tool by launching TUI."""
    questions = args.get("questions", [])
    if not questions:
        return {"content": [{"type": "text", "text": "Error: No questions"}], "is_error": True}

    try:
        answers = await ask_user_interactive(questions)
        if answers is None:
            return {"content": [{"type": "text", "text": "User cancelled."}]}

        parts = ["User answers:\n"] + [f"- {k}: {v}" for k, v in answers.items()]
        return {"content": [{"type": "text", "text": "\n".join(parts)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": f"Error: {e}"}], "is_error": True}


async def main():
    """Demo: Ask user about DCF model preferences."""
    server = create_sdk_mcp_server(
        name="user",
        version="1.0.0",
        tools=[ask_user_tool],
    )

    options = ClaudeAgentOptions(
        mcp_servers={"user": server},
        allowed_tools=["mcp__user__ask_user"],
        model="claude-sonnet-4-5-20250929",
    )

    prompt = """I'm building a DCF valuation model. Ask me about:
1. DCF structure type (two-stage/three-stage/single-stage)
2. Terminal value calculation method
3. Output format preference
4. Additional features needed (multi-select)

Use the ask_user tool to gather all this at once, then recommend based on my answers."""

    print(f"Prompt: {prompt}\n{'=' * 60}")

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        print(f"\nClaude: {block.text}")
                    elif isinstance(block, ToolUseBlock):
                        print(f"\n[Tool: {block.name}]")
            elif isinstance(msg, ResultMessage):
                print(f"\n{'=' * 60}\n✅ Done! Cost: ${msg.total_cost_usd:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
