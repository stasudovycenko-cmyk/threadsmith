"""Typed operational contracts for Autocontent cost hardening."""

from enum import StrEnum


class RepairReason(StrEnum):
    TOO_LONG = "post_too_long"
    TOO_SHORT = "empty_or_too_short_body"
    CTA_MISSING = "missing_engagement_cta"
    BANNED_PHRASE = "banned_phrase"
    HOOK_DUPLICATE = "duplicate_hooks"
    REPETITIVE_OPENING_EXACT = "repeated_opening_exact"
    REPETITIVE_OPENING_SIMILAR = "repeated_opening_similar"
    REPETITIVE_STRATEGY = "repeated_content_strategy"
    QUALITY_SCORE_LOW = "weak_specificity"
    FORMAT_INVALID = "format_invalid"
    METADATA_INVALID = "metadata_invalid"
    JSON_INVALID = "json_invalid"
    HOOK_MISSING = "missing_hook"
    TOO_MANY_EMOJIS = "too_many_emojis"
    UNKNOWN = "unknown"


class CostGuardReason(StrEnum):
    REPAIR_RATE_HIGH = "REPAIR_RATE_HIGH"
