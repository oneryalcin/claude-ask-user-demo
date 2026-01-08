#!/usr/bin/env python3
"""Minimal web-based AskUserQuestion via SSE + HTTP POST."""

import asyncio
import json
import logging
import uuid
from pathlib import Path
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
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

app = FastAPI()

# Shared state for async waiting
pending_events: dict[str, asyncio.Event] = {}
pending_answers: dict[str, dict] = {}
session_queues: dict[str, asyncio.Queue] = {}  # For SSE events
conversation_history: dict[str, list[dict]] = {}  # Store messages per session

# JSON Schema for ask_user tool
ASK_USER_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
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
                "required": ["question", "header", "options"],
            },
        }
    },
    "required": ["questions"],
}


def create_ask_user_tool(session_id: str):
    """Create ask_user tool that communicates via session queue."""

    @tool("ask_user", "Ask user questions with rich UI", ASK_USER_SCHEMA)
    async def ask_user_tool(args: dict[str, Any]) -> dict[str, Any]:
        questions = args.get("questions", [])
        if not questions:
            return {"content": [{"type": "text", "text": "No questions provided"}]}

        question_id = str(uuid.uuid4())
        event = asyncio.Event()
        pending_events[question_id] = event

        # Send questions to browser via SSE queue
        queue = session_queues.get(session_id)
        if queue:
            log.info(
                f"[{session_id[:8]}] Sending {len(questions)} questions (qid={question_id[:8]})"
            )
            await queue.put(
                {"type": "questions", "question_id": question_id, "questions": questions}
            )
        else:
            log.warning(f"[{session_id[:8]}] No queue found for session!")

        # Wait for answers (with timeout)
        log.info(f"[{session_id[:8]}] Waiting for user answers...")
        try:
            await asyncio.wait_for(event.wait(), timeout=300)  # 5 min timeout
        except asyncio.TimeoutError:
            log.warning(f"[{session_id[:8]}] Timeout waiting for answers")
            del pending_events[question_id]
            return {"content": [{"type": "text", "text": "User did not respond in time"}]}

        answers = pending_answers.pop(question_id, {})
        log.info(f"[{session_id[:8]}] Received answers: {answers}")
        del pending_events[question_id]

        # Format answers for Claude (handle arrays for multi-select)
        parts = ["User answers:"]
        for k, v in answers.items():
            if isinstance(v, list):
                v = ", ".join(v)
            parts.append(f"- {k}: {v}")
        answer_text = "\n".join(parts)

        # Store in conversation history so future turns remember
        if session_id in conversation_history:
            conversation_history[session_id].append(
                {"role": "assistant", "content": f"[Asked user questions, received: {answer_text}]"}
            )

        return {"content": [{"type": "text", "text": answer_text}]}

    return ask_user_tool


class ChatRequest(BaseModel):
    message: str
    session_id: str


class AnswerRequest(BaseModel):
    question_id: str
    answers: dict[str, Any]  # Can be str or list[str] for multi-select


@app.post("/chat")
async def chat(req: ChatRequest):
    """Start chat and return session for SSE streaming."""
    session_id = req.session_id or str(uuid.uuid4())
    log.info(f"[{session_id[:8]}] New chat: {req.message[:50]}...")

    # Create queue for this session
    queue = asyncio.Queue()
    session_queues[session_id] = queue

    # Run Claude in background task
    asyncio.create_task(run_claude(session_id, req.message, queue))

    return {"session_id": session_id}


async def run_claude(session_id: str, message: str, queue: asyncio.Queue):
    """Run Claude SDK and push events to queue."""
    log.info(f"[{session_id[:8]}] Starting Claude...")
    try:
        # Initialize or get conversation history
        if session_id not in conversation_history:
            conversation_history[session_id] = []

        # Add user message to history
        conversation_history[session_id].append({"role": "user", "content": message})

        # Build context from history
        history = conversation_history[session_id]
        if len(history) > 1:
            # Format previous messages as context
            context_parts = ["Previous conversation:"]
            for msg in history[:-1]:  # All except current message
                role = "User" if msg["role"] == "user" else "Assistant"
                context_parts.append(f"{role}: {msg['content']}")
            context = "\n".join(context_parts) + f"\n\nCurrent message: {message}"
        else:
            context = message

        ask_user_tool = create_ask_user_tool(session_id)
        server = create_sdk_mcp_server(name="user", version="1.0.0", tools=[ask_user_tool])

        options = ClaudeAgentOptions(
            mcp_servers={"user": server},
            allowed_tools=["mcp__user__ask_user"],
            disallowed_tools=["AskUserQuestion"],  # Block native tool
            model="claude-sonnet-4-5-20250929",
        )

        async with ClaudeSDKClient(options=options) as client:
            await client.query(context)

            assistant_response_parts = []

            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            await queue.put({"type": "assistant", "content": block.text})
                            assistant_response_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            await queue.put({"type": "tool_call", "name": block.name})

                elif isinstance(msg, ResultMessage):
                    # Save assistant response to history
                    if assistant_response_parts:
                        conversation_history[session_id].append(
                            {"role": "assistant", "content": " ".join(assistant_response_parts)}
                        )
                    cost = msg.total_cost_usd or 0
                    log.info(f"[{session_id[:8]}] Claude done. Cost: ${cost:.4f}")
                    await queue.put({"type": "done", "cost": cost})

    except Exception as e:
        log.error(f"[{session_id[:8]}] Error: {e}")
        await queue.put({"type": "error", "message": str(e)})
    finally:
        await queue.put(None)  # Signal end


@app.get("/stream/{session_id}")
async def stream(session_id: str):
    """SSE endpoint for streaming responses."""

    async def event_generator():
        queue = session_queues.get(session_id)
        if not queue:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Session not found'})}\n\n"
            return

        while True:
            event = await queue.get()
            if event is None:  # End signal
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/answers")
async def submit_answers(req: AnswerRequest):
    """Receive answers from browser, unblock tool handler."""
    question_id = req.question_id
    log.info(f"[qid={question_id[:8]}] Received answers: {req.answers}")

    if question_id not in pending_events:
        log.warning(f"[qid={question_id[:8]}] Question not found!")
        return {"error": "Question not found or already answered"}

    pending_answers[question_id] = req.answers
    pending_events[question_id].set()  # Unblock!
    log.info(f"[qid={question_id[:8]}] Unblocked tool handler")

    return {"ok": True}


# Serve static files
@app.get("/")
async def index():
    html_path = Path(__file__).parent / "index.html"
    return HTMLResponse(html_path.read_text())


def main():
    """Run the server."""
    import uvicorn

    print("Starting AskUserQuestion MCP Demo...")
    print("Open http://localhost:8000 in your browser")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
