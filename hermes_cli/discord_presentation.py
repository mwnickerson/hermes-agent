"""Discord-only outbound presentation boundary.

The gateway keeps raw diagnostics in logs and internal state. This module owns
the narrower contract for human-facing Discord text: preserve normal prose and
trusted interactive code replies, but fail closed on machine-shaped output.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

DISCORD_PRESENTATION_FALLBACK = (
    "Hermes has an internal update, but the raw details were withheld from Discord."
)

_JSONISH_RE = re.compile(r"^\s*(?:[{\[]|\\[{\[])[\s\S]*(?:[}\]]|[}\]]\\)\s*$")
_ESCAPED_JSONISH_RE = re.compile(r'^\s*"{\\?"(?:[A-Za-z0-9_ -]+)\\?":')
_TRACEBACK_RE = re.compile(r"\bTraceback \(most recent call last\):|\bFile \"[^\"]+\", line \d+", re.I)
_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:/Users/|/private/|/tmp/|/var/|/home/|[A-Za-z]:\\)[^\s`)]*")
_INTERNAL_ID_RE = re.compile(
    r"\b(?:task|run|event|thread|session|message|guild|channel)_?id\b\s*[:=]"
    r"|\bt_[a-f0-9]{6,}\b"
    r"|\b(?:session|run|event|thread)[:#][A-Za-z0-9_.:-]{6,}\b",
    re.I,
)
_COMMAND_RE = re.compile(r"(?m)^\s*(?:\$|#)?\s*(?:python|pytest|git|curl|npm|node|uv|hermes|bash|sh)\s+\S+")
_TERMINAL_RE = re.compile(r"(?im)^\s*(?:stdout|stderr|exit code|returncode|command|cwd|pid)\s*[:=]")
_METADATA_RE = re.compile(r"(?im)^\s*(?:metadata|raw_response|payload|kwargs|args|headers|env|config)\s*[:=]\s*[{[]")
_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_SAFE_FIELD_LABELS = {
    "summary",
    "status",
    "reason",
    "next step",
    "action needed",
    "current state",
    "what changed",
    "why it matters",
}


@dataclass(frozen=True)
class DiscordPresentationResult:
    text: str
    allowed: bool
    reason: str = ""


def metadata_allows_interactive_code(metadata: Mapping[str, Any] | None) -> bool:
    """Return True only for explicitly trusted interactive response metadata."""
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        metadata.get("discord_allow_code")
        or metadata.get("discord_interactive_response")
        or metadata.get("interactive_response")
    )


def _loads_jsonish(text: str) -> bool:
    candidate = text.strip()
    if not candidate:
        return False
    if candidate.startswith('"') and candidate.endswith('"'):
        try:
            candidate = json.loads(candidate)
        except Exception:
            pass
        if not isinstance(candidate, str):
            return True
    if not (candidate.startswith("{") or candidate.startswith("[")):
        return False
    try:
        json.loads(candidate)
        return True
    except Exception:
        return False


def _strip_code_for_policy(text: str, *, allow_code: bool) -> str:
    if not allow_code:
        return text
    text = _FENCED_CODE_RE.sub("", text)
    return _INLINE_CODE_RE.sub("", text)


def _looks_like_machine_field_dump(text: str) -> bool:
    field_lines = 0
    total_lines = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        total_lines += 1
        if re.match(r"^[A-Za-z_][A-Za-z0-9_ -]{1,40}\s*[:=]\s*\S", stripped):
            label = stripped.split(":", 1)[0].split("=", 1)[0].strip().lower()
            if label not in _SAFE_FIELD_LABELS:
                field_lines += 1
    return total_lines >= 3 and field_lines >= max(2, total_lines // 2)


def render_discord_human_text(
    text: Any,
    *,
    metadata: Mapping[str, Any] | None = None,
    allow_code: bool | None = None,
    fallback: str = DISCORD_PRESENTATION_FALLBACK,
) -> DiscordPresentationResult:
    """Return Discord-safe human text or a generic fallback.

    The rejected text is intentionally absent from the fallback so user-visible
    surfaces cannot leak the unsafe payload by accident.
    """
    raw = "" if text is None else str(text)
    if not raw.strip():
        return DiscordPresentationResult(raw, True, "")

    trusted_code = metadata_allows_interactive_code(metadata) if allow_code is None else bool(allow_code)
    policy_text = _strip_code_for_policy(raw, allow_code=trusted_code)

    reason = ""
    if _loads_jsonish(raw) or _JSONISH_RE.match(raw) or _ESCAPED_JSONISH_RE.match(raw):
        reason = "jsonish"
    elif _TRACEBACK_RE.search(policy_text):
        reason = "traceback"
    elif _TERMINAL_RE.search(policy_text):
        reason = "terminal-output"
    elif _PATH_RE.search(policy_text):
        reason = "absolute-path"
    elif _COMMAND_RE.search(policy_text):
        reason = "command"
    elif _INTERNAL_ID_RE.search(policy_text):
        reason = "internal-id"
    elif _METADATA_RE.search(policy_text):
        reason = "metadata-dump"
    elif _looks_like_machine_field_dump(policy_text):
        reason = "machine-field-dump"
    elif not trusted_code and _FENCED_CODE_RE.search(raw):
        reason = "untrusted-code"

    if reason:
        return DiscordPresentationResult(fallback, False, reason)
    return DiscordPresentationResult(raw, True, "")


def audit_discord_human_texts(texts: list[str]) -> list[dict[str, Any]]:
    """Dry-run audit helper for recent bot-message fixtures."""
    rows = []
    for index, text in enumerate(texts):
        result = render_discord_human_text(text, metadata={})
        rows.append({
            "index": index,
            "allowed": result.allowed,
            "reason": result.reason,
            "rendered": result.text,
        })
    return rows
