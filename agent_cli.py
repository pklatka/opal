#!/usr/bin/env python
"""
OPAL Symphony Agent CLI
========================
Interactive agent that uses LLM + MCP to test the Symphony extension levels
against the OPAL server API.

Prerequisites:
    1. OPAL server running       (uv run uvicorn opal_server.main:app --reload --timeout-keep-alive 300)
    2. LLM provider available    (Ollama, Anthropic, Gemini, or Copilot)

Usage:
    # Interactive mode
    uv run python agent_cli.py --level L0

    # Single-shot mode
    uv run python agent_cli.py --level L1 "Serve policy bundles excluding test files"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from symphony import SymphonyRunner


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OPAL Symphony Agent CLI - test L0/L1/L2/L3/L4 extensions",
    )
    parser.add_argument(
        "--level",
        choices=["L0", "L1", "L2", "L3", "L4"],
        default="L0",
        help="Extension level (default: L0)",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "anthropic", "gemini", "copilot"],
        default="ollama",
        help="LLM provider (default: ollama)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model name (default: haiku for anthropic, gemini-2.5-flash for gemini, "
            "qwen3.5:9b for ollama, claude-haiku-4.5 for copilot)"
        ),
    )
    parser.add_argument(
        "--no-reasoning",
        action="store_true",
        help="Disable reasoning",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show debug output",
    )
    parser.add_argument(
        "--export-stats",
        metavar="FILE",
        default=None,
        help="Append run stats to FILE in JSONL format (benchmark-compatible)",
    )
    parser.add_argument(
        "--check-contains",
        metavar="TEXT",
        default=None,
        help=(
            "Set passed=True in exported stats if the final response contains TEXT "
            "(case-insensitive). Use to automate correctness checks."
        ),
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Task to execute (omit for interactive mode)",
    )
    args = parser.parse_args()
    reasoning = not args.no_reasoning

    # Resolve default model per provider
    default_models = {
        "anthropic": "haiku",
        "gemini": "gemini-2.5-flash",
        "ollama": "qwen3.5:9b",
        "copilot": "claude-haiku-4.5",
    }
    model = args.model or default_models[args.provider]

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="  [DEBUG] %(message)s")

    mcp_server = str(Path(__file__).parent / "mcp_server.py")
    runner = SymphonyRunner(
        model=model,
        reasoning=reasoning,
        provider=args.provider,
        mcp_command=[sys.executable, mcp_server],
    )

    _codegen_side = {
        "L0": "n/a", "L1": "client",
        "L2": "server", "L3": "server", "L4": "server",
    }.get(args.level, "server")

    def _run_task(task: str) -> None:
        if args.export_stats:
            export_path = Path(args.export_stats)
            start_ts = datetime.now(timezone.utc).isoformat()

            # Accumulate last-turn content so final_response is ready when on_stats fires
            _content_parts: list[str] = []
            _thinking_parts: list[str] = []
            _transcript_parts: list[str] = []
            _turn_state = {"in_content": False, "in_thinking": False}

            def _on_stats(stats: dict) -> None:
                record = {
                    "test_id": "",
                    "app": "opal",
                    "level": args.level,
                    "provider": args.provider,
                    "model": model,
                    "reasoning": reasoning,
                    "codegen_side": _codegen_side,
                    "execution_mode": "agent",
                    "run_index": 0,
                    "timestamp": start_ts,
                    "latency_ms": round(stats["wall_time_s"] * 1000, 2),
                    "phase1_latency_ms": None,
                    "phase2_latency_ms": None,
                    "http_status": 0,
                    "response_json": {"task": task},
                    "extension_triggered": stats["extension_triggered"] > 0,
                    "generated_code": None,
                    "needs_extension": False,
                    "extension_error": None if stats["extension_errors"] == 0 else True,
                    "goex_record_id": None,
                    "goex_mode": False,
                    "goex_record_status": None,
                    "passed": (
                        args.check_contains.lower() in "".join(_content_parts).lower()
                        if args.check_contains else None
                    ),
                    "check_reason": (
                        f"contains '{args.check_contains}'"
                        if args.check_contains else ""
                    ),
                    "check_details": None,
                    "conversation_log_path": None,
                    "final_response": "".join(_content_parts),
                    # Full run transcript (turns, tool calls/results, thinking, answer).
                    "thinking_trace": "".join(_transcript_parts),
                    # Raw model thinking tokens only.
                    "thinking_only": "".join(_thinking_parts),
                    "total_tokens": stats["total_prompt_tokens"] + stats["total_completion_tokens"],
                    "total_cost_usd": stats["total_cost_usd"],
                    "turns_count": stats["turns"],
                    "tool_call_count": stats["tool_call_count"],
                }
                mode = "a" if export_path.exists() and export_path.stat().st_size > 0 else "w"
                with open(export_path, mode) as f:
                    f.write(json.dumps(record) + "\n")
                print(f"\n  [Export] Stats appended to {export_path}")

            cb = SymphonyRunner.terminal_callbacks(on_stats=_on_stats)

            # Wrap callbacks to capture the full run transcript and final answer
            _orig_on_thinking = cb["on_thinking"]
            _orig_on_content = cb["on_content"]
            _orig_on_tool_call = cb["on_tool_call"]
            _orig_on_tool_result = cb["on_tool_result"]
            _orig_on_turn_start = cb["on_turn_start"]
            _orig_on_meta = cb["on_meta"]

            def _on_thinking(delta: str) -> None:
                _thinking_parts.append(delta)
                if not _turn_state["in_thinking"]:
                    _transcript_parts.append("  [Thinking]\n")
                    _turn_state["in_thinking"] = True
                _transcript_parts.append(delta)
                _orig_on_thinking(delta)

            def _on_content(delta: str) -> None:
                if not _turn_state["in_content"]:
                    if _turn_state["in_thinking"]:
                        _transcript_parts.append("\n")
                    _transcript_parts.append("\nAgent: ")
                    _turn_state["in_content"] = True
                _content_parts.append(delta)
                _transcript_parts.append(delta)
                _orig_on_content(delta)

            def _on_tool_call(name: str, arguments: dict) -> None:
                payload = {"name": name, "arguments": arguments}
                _transcript_parts.append(f'\nAgent: TOOL_CALL: {json.dumps(payload)}\n')
                _orig_on_tool_call(name, arguments)

            def _on_tool_result(name: str, result: str) -> None:
                _transcript_parts.append(f"\n  [Tool Result: {name}]\n{result}\n")
                _orig_on_tool_result(name, result)

            def _on_turn_start(turn: int) -> None:
                _content_parts.clear()
                _turn_state["in_content"] = False
                _turn_state["in_thinking"] = False
                _transcript_parts.append(f"\n--- Turn {turn} ---\n")
                _orig_on_turn_start(turn)

            def _on_meta(raw: dict) -> None:
                parts: list[str] = []
                model_name = raw.get("model")
                if model_name:
                    parts.append(f"model={model_name}")
                prompt_tokens = raw.get("prompt_eval_count")
                if prompt_tokens:
                    parts.append(f"prompt_tokens={prompt_tokens}")
                completion_tokens = raw.get("eval_count")
                if completion_tokens:
                    parts.append(f"completion_tokens={completion_tokens}")
                total_ns = raw.get("total_duration")
                if total_ns:
                    parts.append(f"time={total_ns / 1e9:.1f}s")
                if parts:
                    _transcript_parts.append(f"  [Meta] {', '.join(parts)}\n")
                _orig_on_meta(raw)

            cb["on_thinking"] = _on_thinking
            cb["on_content"] = _on_content
            cb["on_tool_call"] = _on_tool_call
            cb["on_tool_result"] = _on_tool_result
            cb["on_turn_start"] = _on_turn_start
            cb["on_meta"] = _on_meta

            asyncio.run(runner.run(task, level=args.level, **cb))
        else:
            cb = SymphonyRunner.terminal_callbacks()
            asyncio.run(runner.run(task, level=args.level, **cb))

    if args.task:
        _run_task(args.task)
    else:
        print(f"OPAL Symphony Agent CLI ({args.level})")
        print(f"Provider: {args.provider} | Model: {model} | Reasoning: {'on' if reasoning else 'off'}")
        print("Type 'quit' to exit.\n")

        while True:
            try:
                task = input("> ").strip()
                if task.lower() in ("quit", "exit", "q"):
                    break
                if not task:
                    continue
                _run_task(task)
                print()
            except (KeyboardInterrupt, EOFError):
                break

        print("\nGoodbye!")


if __name__ == "__main__":
    main()
