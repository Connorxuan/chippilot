"""Conversation context management — summarization and compaction.

Prevents the agent from exceeding LLM context windows by periodically
summarizing old messages and rewriting the LangGraph checkpointer state.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain.chat_models.base import init_chat_model


def _msg_content(msg: Any) -> str:
    """Extract text content from a LangChain message."""
    content = getattr(msg, "content", "") or ""
    if isinstance(content, list):
        return "\n".join(
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in content
        )
    return str(content)


def estimate_tokens(messages: list[Any]) -> int:
    """Rough token estimate for a list of LangChain messages.

    Uses a conservative heuristic: 1 token ≈ 3 characters.
    This works for mixed CJK/English text, code, and JSON.
    """
    total_chars = 0
    for msg in messages:
        total_chars += len(_msg_content(msg))
    return total_chars // 3


def _truncate_old_messages(messages: list[Any], max_items: int = 80, max_chars_per_msg: int = 400) -> list[Any]:
    """Truncate a long message list for the summarizer.

    Keeps the first ``max_items // 4`` and the last ``max_items * 3 // 4``
    messages, dropping the middle.  Each message is capped to
    ``max_chars_per_msg`` characters so the summarizer prompt stays small.
    """
    if len(messages) <= max_items:
        trimmed = messages
    else:
        head = max_items // 4
        tail = max_items - head
        trimmed = messages[:head] + messages[-tail:]

    result: list[Any] = []
    for msg in trimmed:
        content = _msg_content(msg)
        if len(content) > max_chars_per_msg:
            content = content[:max_chars_per_msg] + "…"
        # Rebuild a lightweight dict so we don't mutate originals
        role = getattr(msg, "type", "unknown")
        result.append(f"[{role}] {content}")
    return result


def summarize_messages(
    messages: list[Any],
    model_name: str | None = None,
) -> str:
    """Use an LLM to summarize a slice of conversation history.

    Args:
        messages: List of LangChain messages to summarize.
        model_name: Provider-prefixed model string.

    Returns:
        A concise paragraph covering goals, actions, results, and status.
    """
    llm = init_chat_model(
        model_name or os.environ.get("OPENROAD_LLM_MODEL", "google_genai:gemini-2.5-flash")
    )

    trimmed = _truncate_old_messages(messages)

    prompt = (
        "Summarize the following conversation history into 2-4 concise paragraphs.\n"
        "Include:\n"
        "- The user's main goals and constraints\n"
        "- Key tool calls and actions performed\n"
        "- Important results, metrics, or decisions\n"
        "- Current status and any unresolved issues\n\n"
        "History:\n"
        + "\n".join(trimmed)
    )

    response = llm.invoke([HumanMessage(content=prompt)])
    return str(response.content) if response.content else "Previous conversation summarized."


class ContextManager:
    """Decides when to compact a message thread and performs the compaction."""

    def __init__(
        self,
        model_name: str | None = None,
        token_threshold: int = 200_000,
        keep_recent_messages: int = 6,
    ) -> None:
        self.model_name = model_name
        self.token_threshold = token_threshold
        self.keep_recent_messages = keep_recent_messages

    def should_compact(self, messages: list[Any]) -> bool:
        """Return True if the thread has grown beyond the token threshold."""
        return estimate_tokens(messages) >= self.token_threshold

    def compact(self, messages: list[Any]) -> list[Any]:
        """Summarize old messages and return a smaller list.

        The returned list starts with a ``SystemMessage`` containing the
        summary, followed by the most recent ``keep_recent_messages``
        messages from the original list.
        """
        keep = self.keep_recent_messages
        if len(messages) <= keep:
            return list(messages)

        old = messages[:-keep]
        recent = messages[-keep:]

        summary_text = summarize_messages(old, self.model_name)
        summary_msg = SystemMessage(
            content=f"[Earlier conversation summary]\n{summary_text}"
        )
        return [summary_msg, *recent]
