#!/usr/bin/env python
"""
OPAL CAGE Agent CLI
========================
Interactive agent that uses LLM + MCP to test the CAGE extension levels
against the OPAL server API.

Prerequisites:
    1. OPAL server running       (uv run uvicorn opal_server.main:app --reload --timeout-keep-alive 300)
    2. LLM provider available    (Ollama, Anthropic, Gemini, Copilot, or Cursor Agent)

Usage:
    # Interactive mode
    uv run python agent_cli.py --level L0

    # Single-shot mode
    uv run python agent_cli.py --level L1 "Serve policy bundles excluding test files"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from cage import CAGERunner
from cage.stats_export import make_export_callbacks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="OPAL CAGE Agent CLI - test L0/L1/L2/L3/L4 extensions",
    )
    parser.add_argument(
        "--level",
        choices=["L0", "L1", "L2", "L3", "L4"],
        default="L0",
        help="Extension level (default: L0)",
    )
    parser.add_argument(
        "--provider",
        choices=["ollama", "anthropic", "gemini", "llmgateway", "copilot", "agent"],
        default="ollama",
        help="LLM provider (default: ollama)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model name (default: haiku for anthropic, gemini-2.5-flash for gemini, "
            "gpt-4o-mini for llmgateway, qwen3.5:9b for ollama, claude-haiku-4.5 for copilot, auto for agent)"
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
        "--api-url",
        default=os.getenv("OPAL_API_URL", os.getenv("API_URL", "http://127.0.0.1:8000")),
        help="Base URL for the OPAL API (default: %(default)s)",
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
        "llmgateway": "gpt-4o-mini",
        "ollama": "qwen3.5:9b",
        "copilot": "claude-haiku-4.5",
        "agent": "auto",
    }
    model = args.model or default_models[args.provider]

    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="  [DEBUG] %(message)s")

    os.environ["API_URL"] = args.api_url
    os.environ["OPAL_API_URL"] = args.api_url

    mcp_server = str(Path(__file__).parent / "mcp_server.py")
    runner = CAGERunner(
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
            cb = make_export_callbacks(
                app="opal",
                level=args.level,
                provider=args.provider,
                model=model,
                reasoning=reasoning,
                codegen_side=_codegen_side,
                task=task,
                export_path=Path(args.export_stats),
                check_contains=args.check_contains,
                extra_fields={"api_url": args.api_url},
            )
            asyncio.run(runner.run(task, level=args.level, **cb))
        else:
            cb = CAGERunner.terminal_callbacks()
            asyncio.run(runner.run(task, level=args.level, **cb))

    if args.task:
        _run_task(args.task)
    else:
        print(f"OPAL CAGE Agent CLI ({args.level})")
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
