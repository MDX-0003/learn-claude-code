"""Trace system for learn-claude-code — pluggable agent execution recording.

Provides:
    - AgentLogger: session-scoped event recorder (JSONL + HTML output)
    - Events: 17 event type constants
    - LoggerConfig: truncation thresholds and timezone config
    - create_agent_logger(): factory function
    - log_model_output(): extract structured data from Anthropic response
    - truncate_messages_for_log(): content truncation helper
"""

from .agent_logger import AgentLogger, Events, LoggerConfig, create_agent_logger
from .agent_utils import log_model_output, truncate_messages_for_log
