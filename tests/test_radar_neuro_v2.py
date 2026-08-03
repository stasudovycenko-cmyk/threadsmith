import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.bot.handlers import neuro as neuro_handler
from app.core import credits, neuro, radar, threads_api
from app.schemas.engagement import RadarSemanticScoreResponse


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.rowcount = len(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def mappings(self):
        return self


class ScriptedSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)

    async def commit(self):
        self.commits += 1


def public_post(**overrides):
    post = {
        "id": "post-1",
        "text": "AI помогает бизнесу. Какие задачи вы уже автоматизировали?",
        "username": "external",
        "owner": {"id": "external-id", "username": "external"},
        "timestamp": NOW.isoformat(),
        "permalink": "https://threads.net/@external/post/1",
        "has_replies": True,
        "is_reply": False,
    }
    post.update(overrides)
    return post


def test_threads_scopes_cover_discovery_publish_and_replies():
    assert set(threads_api.SCOPES.split(",")) == {
        "threads_basic",
        "threads_content_publish",
        "threads_manage_insights",
        "threads_read_replies",
        "threads_manage_replies",
        "threads_profile_discovery",
        "threads_keyword_search",
    }


def test_keyword_search_requests_safe_public_fields(monkeypatch):
    captured = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, *, params):
            captured.update({"url": url, "params": params})
            return httpx.Response(
                200,
                json={"data": [public_post()]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(threads_api.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(threads_api.keyword_search("secret", "AI"))
    assert result[0]["id"] == "post-1"
    assert captured["params"]["search_type"] == "RECENT"
    assert captured["params"]["limit"] == 25
    assert "owner" in captured["params"]["fields"]
    assert "timestamp" in captured["params"]["fields"]


def test_prefilter_excludes_own_post():
    reason = radar.prefilter_reason(
        public_post(owner={"id": "own-id"}),
        own_threads_user_id="own-id",
        own_username="creator",
        excluded_authors=set(),
        language="ru",
        max_age_hours=72,
        now=NOW,
    )
    assert reason == "own_post"


def test_prefilter_excludes_hidden_author():
    reason = radar.prefilter_reason(
        public_post(),
        own_threads_user_id="own-id",
        own_username="creator",
        excluded_authors={"external"},
        language="ru",
        max_age_hours=72,
        now=NOW,
    )
    assert reason == "excluded_author"


@pytest.mark.parametrize(
    ("post", "reason"),
    [
        (public_post(text=""), "empty_text"),
        (public_post(text="English content about AI"), "language"),
        (
            public_post(timestamp=(NOW - timedelta(hours=73)).isoformat()),
            "too_old",
        ),
        (public_post(text="Гарантированный доход и казино"), "spam"),
        (public_post(text="Здесь продажа оружия"), "prohibited"),
    ],
)
def test_prefilter_rejects_unsuitable_posts(post, reason):
    assert radar.prefilter_reason(
        post,
        own_threads_user_id="own-id",
        own_username="creator",
        excluded_authors=set(),
        language="ru",
        max_age_hours=72,
        now=NOW,
    ) == reason


def test_deterministic_scoring_is_explainable_and_penalizes_repetition():
    base = radar.deterministic_score(
        public_post(),
        niche="AI для бизнеса",
        keywords=["AI", "автоматизация"],
        now=NOW,
    )
    repeated = radar.deterministic_score(
        public_post(),
        niche="AI для бизнеса",
        keywords=["AI", "автоматизация"],
        duplicate_hits=2,
        author_comments=2,
        now=NOW,
    )
    assert base.safe is True
    assert base.topical_relevance > 0
    assert base.conversation_potential > 0
    assert repeated.total < base.total
    assert repeated.author_penalty == 20
    assert repeated.duplicate_penalty == 10


def test_keyword_results_are_deduplicated_per_account(monkeypatch):
    session = ScriptedSession([
        [("AI", ["AI", "автоматизация"], "ru", 72, [], "own-id", "creator", 0)],
        [(44,)],
        [],
        [],
        [],
        [],
        [(501,)],
        [],
        [],
    ])

    async def fake_search(_token, keyword, *, search_type):
        assert search_type == "RECENT"
        return [public_post()]

    monkeypatch.setattr(radar, "keyword_search", fake_search)
    monkeypatch.setattr(radar, "_utc_now", lambda: NOW)
    summary = asyncio.run(radar.discover_account_posts(
        session, user_id=7, account_id=11, token="secret"
    ))
    assert summary.results_seen == 2
    assert summary.candidates_saved == 1
    assert summary.duplicates == 1
    insert = next(
        (sql, params) for sql, params in session.calls
        if "INSERT INTO radar_candidates" in sql
    )
    assert set(insert[1]["keywords"]) == {"AI", "автоматизация"}
    assert insert[1]["duplicate_hits"] == 1
    assert all(
        params.get("account_id", 11) == 11
        for _, params in session.calls
        if "account_id" in params
    )


def test_rejected_candidate_is_not_reopened(monkeypatch):
    session = ScriptedSession([
        [("AI", ["AI"], "ru", 72, [], "own-id", "creator", 0)],
        [(45,)],
        [],
        [("rejected",)],
        [],
    ])

    async def fake_search(*_args, **_kwargs):
        return [public_post()]

    monkeypatch.setattr(radar, "keyword_search", fake_search)
    monkeypatch.setattr(radar, "_utc_now", lambda: NOW)
    summary = asyncio.run(radar.discover_account_posts(
        session, user_id=7, account_id=11, token="secret"
    ))
    assert summary.filtered == 1
    assert not any("INSERT INTO radar_candidates" in sql for sql, _ in session.calls)


@pytest.mark.parametrize(
    ("minimum_score", "expected_status"),
    [(75, "ready"), (90, "filtered")],
)
def test_semantic_score_respects_minimum_score(
    monkeypatch, minimum_score, expected_status
):
    session = ScriptedSession([
        [{
            "id": 501,
            "post_text": "AI помогает бизнесу",
            "author_username": "external",
            "deterministic_score": 80,
        }],
        [(minimum_score, "AI", ["AI"])],
        [],
    ])
    contexts = []

    async def fake_context(_session, **kwargs):
        contexts.append(kwargs)
        return SimpleNamespace(compact_dict=lambda: {"dna": {"tone": "direct"}})

    async def fake_spend(*_args, **_kwargs):
        return True

    async def fake_ask(*_args, **kwargs):
        assert kwargs["response_model"] is RadarSemanticScoreResponse
        assert kwargs["usage_context"].threads_account_id == 11
        return RadarSemanticScoreResponse(
            relevant=True,
            topical_relevance=80,
            conversation_potential=80,
            safe=True,
            reason="relevant and conversational",
        )

    monkeypatch.setattr(radar.social_brain, "build_account_context", fake_context)
    monkeypatch.setattr(radar.credits, "spend_once", fake_spend)
    monkeypatch.setattr(radar, "ask_json", fake_ask)
    assert asyncio.run(radar.semantic_score_candidates(
        session, user_id=7, account_id=11
    )) == 1
    update = next(
        params for sql, params in session.calls
        if "semantic_score = :semantic" in sql
    )
    assert update["status"] == expected_status
    assert contexts[0]["threads_account_id"] == 11
    assert contexts[0]["task"] == "radar"


def test_comment_strategies_rotate_and_avoid_last_strategy():
    first = neuro.choose_strategy([])
    second = neuro.choose_strategy([first], last_strategy=first)
    assert first in neuro.COMMENT_STRATEGIES
    assert second in neuro.COMMENT_STRATEGIES
    assert second != first
    assert len(neuro.COMMENT_STRATEGIES) == 8


@pytest.mark.parametrize(
    "comment",
    [
        "Полностью согласен!",
        "Читайте подробнее https://example.test",
        "@creator заходите ко мне",
        "x" * 281,
    ],
)
def test_comment_postfilter_rejects_empty_value_and_promotion(comment):
    assert neuro.safe_comment(comment) is False


def test_comment_postfilter_rejects_duplicate():
    assert neuro.safe_comment(
        "Практический эффект появляется после второй итерации.",
        ["Практический эффект появляется после второй итерации."],
    ) is False


def test_generation_claim_is_account_scoped_concurrent_and_restart_safe():
    session = ScriptedSession([[]])
    assert asyncio.run(neuro.claim_candidate_for_generation(
        session, user_id=7, account_id=11
    )) is None
    sql, params = session.calls[0]
    assert "FOR UPDATE OF candidate SKIP LOCKED" in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert "account_queue.status IN" in sql
    assert "candidate.threads_account_id = :account_id" in sql
    assert params == {"user_id": 7, "account_id": 11}


def test_worker_restart_recovery_never_retries_publish_claims():
    session = ScriptedSession([[None], [None, None], [None]])
    recovered = asyncio.run(neuro.recover_stale_claims(session))
    assert recovered == {
        "generation_recovered": 1,
        "publish_unknown": 2,
        "follow_up_unknown": 1,
    }
    source = "\n".join(sql for sql, _ in session.calls)
    assert "STALE_GENERATION_CLAIM" in source
    assert source.count("STALE_PUBLISH_CLAIM") == 2
    assert "SET status = 'unknown'" in source


@pytest.mark.parametrize("require_auto", [False, True])
def test_publish_claim_checks_ownership_cap_interval_and_mode(require_auto):
    session = ScriptedSession([[]])
    claim = asyncio.run(neuro.claim_comment_for_publish(
        session,
        user_id=7,
        account_id=11,
        comment_id=99,
        require_auto=require_auto,
    ))
    assert claim is None
    sql, params = session.calls[0]
    assert "FOR UPDATE" in sql
    assert "status IN ('publishing', 'posted', 'unknown')" in sql
    assert "setting.daily_cap" in sql
    assert "setting.minimum_interval_minutes" in sql
    assert "setting.mode = 'auto'" in sql
    assert params["account_id"] == 11
    assert params["require_auto"] is require_auto


def test_credits_are_charged_once_per_operation():
    session = ScriptedSession([
        [("operation",)],
        [(98,)],
        [],
        [],
    ])
    first = asyncio.run(credits.spend_once(
        session, 7, 11, 2, "neuro_comment", "neuro-comment:99:variant:0"
    ))
    second = asyncio.run(credits.spend_once(
        session, 7, 11, 2, "neuro_comment", "neuro-comment:99:variant:0"
    ))
    assert first is True
    assert second is False
    assert sum("UPDATE users SET credits_balance" in sql for sql, _ in session.calls) == 1
    event_sql, event_params = session.calls[0]
    assert "account.user_id = :user_id" in event_sql
    assert event_params["account_id"] == 11


class FakePostClient:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, params):
        self.calls += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return httpx.Response(
            self.outcome[0],
            json=self.outcome[1],
            request=httpx.Request("POST", url),
        )


def test_reply_container_temporary_error_is_known_before_publish(monkeypatch):
    request = httpx.Request("POST", "https://graph.threads.net/v1.0/me/threads")
    client = FakePostClient(httpx.ConnectError("offline", request=request))
    monkeypatch.setattr(threads_api.httpx, "AsyncClient", lambda **_kwargs: client)
    with pytest.raises(threads_api.ThreadsAPIError) as error:
        asyncio.run(threads_api.create_reply_container_once(
            "secret", "me", "comment", "post"
        ))
    assert not isinstance(error.value, threads_api.ThreadsPublishUnknownError)
    assert client.calls == 1


def test_permission_denied_is_controlled_and_not_retried(monkeypatch):
    client = FakePostClient((403, {"error": {"message": "permission denied"}}))
    monkeypatch.setattr(threads_api.httpx, "AsyncClient", lambda **_kwargs: client)
    with pytest.raises(threads_api.ThreadsAPIError) as error:
        asyncio.run(threads_api.create_reply_container_once(
            "secret", "me", "comment", "post"
        ))
    assert threads_api.is_permission_error(error.value)
    assert client.calls == 1


@pytest.mark.parametrize(
    "outcome",
    [
        httpx.ReadTimeout(
            "lost",
            request=httpx.Request("POST", "https://graph.threads.net/publish"),
        ),
        (503, {"error": {"message": "temporary"}}),
        (200, {}),
    ],
)
def test_publish_uncertainty_is_never_retried(monkeypatch, outcome):
    client = FakePostClient(outcome)
    monkeypatch.setattr(threads_api.httpx, "AsyncClient", lambda **_kwargs: client)
    with pytest.raises(threads_api.ThreadsPublishUnknownError):
        asyncio.run(threads_api.publish_reply_container_once(
            "secret", "me", "container"
        ))
    assert client.calls == 1


def test_publish_success_returns_threads_id(monkeypatch):
    client = FakePostClient((200, {"id": "published-1"}))
    monkeypatch.setattr(threads_api.httpx, "AsyncClient", lambda **_kwargs: client)
    result = asyncio.run(threads_api.publish_reply_container_once(
        "secret", "me", "container"
    ))
    assert result == "published-1"
    assert client.calls == 1


def test_reply_detection_updates_comment_and_relationship_memory(monkeypatch):
    session = ScriptedSession([
        [(99, "published-1", "external-id")],
        [],
        [],
    ])

    async def fake_replies(_token, post_id):
        assert post_id == "published-1"
        return [{"id": "reply-1", "username": "external", "text": "Спасибо"}]

    monkeypatch.setattr(neuro, "get_replies", fake_replies)
    replies = asyncio.run(neuro.poll_account_replies(
        session,
        user_id=7,
        account_id=11,
        token="secret",
        own_username="creator",
    ))
    assert replies[0]["reply_id"] == "reply-1"
    assert any("author_replied = true" in sql for sql, _ in session.calls)
    assert all(
        params.get("account_id", 11) == 11
        for _, params in session.calls
        if "account_id" in params
    )


def test_reply_permission_denied_stops_polling_without_retry(monkeypatch):
    session = ScriptedSession([
        [(99, "published-1", "external-id"), (100, "published-2", "other")],
        [],
    ])
    calls = []

    async def denied(_token, post_id):
        calls.append(post_id)
        raise threads_api.ThreadsAPIError("denied", status_code=403)

    monkeypatch.setattr(neuro, "get_replies", denied)
    replies = asyncio.run(neuro.poll_account_replies(
        session,
        user_id=7,
        account_id=11,
        token="secret",
        own_username="creator",
    ))
    assert replies == []
    assert calls == ["published-1"]
    assert "reply_poll_status = 'permission_denied'" in session.calls[1][0]


@pytest.mark.parametrize("connection_row", [None])
def test_disconnected_or_expired_account_stops_before_api(connection_row):
    session = ScriptedSession([[]])
    with pytest.raises(ValueError, match="owned connected account"):
        asyncio.run(radar.discover_account_posts(
            session, user_id=7, account_id=11, token="secret"
        ))
    sql, _ = session.calls[0]
    assert "connection_status = 'connected'" in sql
    assert "account.expires_at > now()" in sql


def test_threads_errors_redact_tokens_and_bound_response_body():
    request = httpx.Request("GET", "https://graph.threads.net/v1.0/test")
    response = httpx.Response(
        400,
        text='{"access_token":"very-secret-token","detail":"' + "x" * 900 + '"}',
        request=request,
    )
    error = threads_api._safe_response_error(response)
    assert "very-secret-token" not in str(error)
    assert "<redacted>" in str(error)
    assert len(str(error)) < 900


def test_telegram_mutations_recheck_selected_account_ownership():
    source = inspect.getsource(neuro_handler)
    assert "account.id" in source
    assert "threads_account_id = :account_id" in source
    assert "account.id != data.get(\"account_id\")" in source


def test_migration_has_safe_defaults_account_identity_and_uniqueness():
    migration = (ROOT / "migrations/011_radar_neurocommenting_v2.sql").read_text(
        encoding="utf-8"
    ).lower()
    assert "create table radar_settings" in migration
    assert "create table radar_candidates" in migration
    assert "create table neuro_author_memory" in migration
    assert "create table ai_credit_events" in migration
    assert "unique (threads_account_id, threads_post_id)" in migration
    assert "default 75" in migration
    assert "default 30" in migration
    assert "default false" in migration
    assert "alter column daily_cap set default 5" in migration
    assert "foreign key (threads_account_id, user_id)" in migration
    assert (
        "threads_account_id,\n    status,\n    final_score desc nulls last,"
        "\n    discovered_at desc"
    ) in migration
    assert "on delete no action" in migration


def test_migration_and_runtime_encode_cooldown_and_idempotent_claims():
    migration = (ROOT / "migrations/011_radar_neurocommenting_v2.sql").read_text(
        encoding="utf-8"
    ).lower()
    neuro_source = inspect.getsource(neuro)
    assert "cooldown_until" in migration
    assert "publish_claim_token" in migration
    assert "operation_key text primary key" in migration
    assert "generation_claimed_at" in migration
    assert "follow_up_threads_id" in migration
    assert "FOR UPDATE OF candidate SKIP LOCKED" in neuro_source
    assert "PUBLISH_RESULT_UNKNOWN" in neuro_source


def test_rollback_refuses_to_discard_v2_activity():
    rollback = (
        ROOT / "migrations/rollback/011_radar_neurocommenting_v2.sql"
    ).read_text(encoding="utf-8").lower()
    assert "rollback 011 blocked: v2 settings changed" in rollback
    assert "rollback 011 blocked: v2 activity data exists" in rollback
    assert "rollback 011 blocked: v2 comments exist" in rollback
    assert "drop table if exists radar_candidates" in rollback
