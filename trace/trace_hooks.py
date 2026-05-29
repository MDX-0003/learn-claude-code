"""Pluggable trace hooks — bridge the agent hook system to AgentLogger.

Usage in a step file's main block:

    from trace.trace_hooks import enable_trace

    trace_logger, trace_step = enable_trace(
        register_hook, trace_dir="logs/traces", enabled=True)

    # ... agent runs, hooks auto-record events ...

    trace_logger.finalize()   # writes SESSION_SUMMARY, closes files

Design:
    - All callbacks return None (pure observers — never block tool execution).
    - register_hook is received as a parameter (zero import dependency on step files).
    - AgentLogger is thread-safe; same instance shared across main + teammate threads.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .agent_logger import AgentLogger, Events, create_agent_logger
from .agent_utils import log_model_output, truncate_messages_for_log


class TraceHooks:
    """Wraps AgentLogger with hook-callback-shaped callables.

    Each method matches the signature of a hook event callback so it can be
    passed directly to register_hook().
    """

    def __init__(self, logger: AgentLogger, step_counter: dict[str, int]):
        self._log = logger
        self._step = step_counter

    # ── UserPromptSubmit(query: str) ──
    def on_user_prompt(self, query: str):
        self._log.log_event(Events.USER_INPUT, {"text": query})
        return None

    # ── PreModelCall(messages, context, tools) ──
    def on_pre_model_call(self, messages: list, context: dict, tools: list):
        self._step["count"] += 1
        system = ""
        if isinstance(context, dict):
            system = str(context.get("system_prompt", ""))
        truncated = truncate_messages_for_log(messages)
        self._log.log_event(Events.MODEL_CALL, {
            "system": system,
            "messages": truncated,
            "tools": [t.get("name", str(t)) for t in tools],
        }, step=self._step["count"])
        return None

    # ── PostModelCall(response, messages) ──
    def on_post_model_call(self, response, messages: list):
        if response is None:
            self._log.log_event(Events.ERROR, {
                "error": "Model call failed (exception)",
                "context": "post_model_call",
            }, step=self._step["count"])
        else:
            log_model_output(self._log, response, self._step["count"])
        return None

    # ── PreToolUse(block) ──
    def on_pre_tool_use(self, block):
        args = {}
        if hasattr(block, "input"):
            args = block.input
        elif isinstance(block, dict):
            args = block.get("input", {})
        self._log.log_event(Events.TOOL_CALL, {
            "tool": block.name if hasattr(block, "name") else block.get("name", "?"),
            "args": args,
        }, step=self._step["count"])
        return None  # never block

    # ── PostToolUse(block, output) ──
    def on_post_tool_use(self, block, output):
        name = block.name if hasattr(block, "name") else block.get("name", "?")
        self._log.log_event(Events.TOOL_RESULT, {
            "tool": name,
            "result": str(output)[:50000],
        }, step=self._step["count"])
        return None

    # ── Stop(messages) ──
    def on_stop(self, messages: list):
        # SESSION_SUMMARY is logged automatically by AgentLogger.finalize()
        return None


def enable_trace(
    register_hook_fn: Callable,
    trace_dir: str = "logs/traces",
    enabled: bool = True,
) -> tuple[AgentLogger | None, dict[str, int]]:
    """Create trace logger, register hook callbacks, return logger + step counter.

    Args:
        register_hook_fn: the step file's register_hook() function
        trace_dir: directory for trace output files
        enabled: if False, returns (None, {"count": 0}) — agent runs normally

    Returns:
        (logger, step_counter) — call logger.finalize() at exit
    """
    if not enabled:
        return None, {"count": 0}

    logger = create_agent_logger(trace_dir=trace_dir, enabled=True)
    step_counter = {"count": 0}
    hooks = TraceHooks(logger, step_counter)

    register_hook_fn("UserPromptSubmit", hooks.on_user_prompt)
    register_hook_fn("PreModelCall", hooks.on_pre_model_call)
    register_hook_fn("PostModelCall", hooks.on_post_model_call)
    register_hook_fn("PreToolUse", hooks.on_pre_tool_use)
    register_hook_fn("PostToolUse", hooks.on_post_tool_use)
    register_hook_fn("Stop", hooks.on_stop)

    return logger, step_counter
