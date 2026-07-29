import asyncio
import json

import pytest

from app.core import radar, scenarist


class ShortPostResult:
    def first(self):
        return ("   too short   ",)


class ShortPostSession:
    async def execute(self, *_args, **_kwargs):
        return ShortPostResult()


def test_voice_profile_serialization_is_compact_and_lossless():
    profile = {
        "tone": "direct",
        "taboo": ["water", "clickbait"],
        "sample_phrases": ["one", "two"],
    }

    serialized = scenarist._profile_str(profile)

    assert "\n" not in serialized
    assert json.loads(serialized) == profile


def test_radar_short_post_is_rejected_before_llm(monkeypatch):
    llm_called = False

    async def fake_ask_json(*_args, **_kwargs):
        nonlocal llm_called
        llm_called = True

    monkeypatch.setattr(radar, "ask_json", fake_ask_json)

    with pytest.raises(ValueError, match="too short"):
        asyncio.run(radar.razbor(ShortPostSession(), "post-id"))

    assert llm_called is False
