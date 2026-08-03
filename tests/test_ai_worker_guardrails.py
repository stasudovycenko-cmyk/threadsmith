import asyncio

from app.core.config import settings
from app.worker import autocontent, m3_jobs, m4_jobs


class EmptyResult:
    def first(self):
        return None


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, *_args, **_kwargs):
        return EmptyResult()

    async def commit(self):
        return None


def test_autocontent_generation_and_pending_caps():
    assert autocontent._bounded_generation_count(
        need=55,
        pending_count=0,
        available_slots=55,
        max_generations=55,
    ) == autocontent.AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN
    assert autocontent._bounded_generation_count(
        need=3,
        pending_count=autocontent.AUTOCONTENT_MAX_PENDING_POSTS,
        available_slots=3,
        max_generations=3,
    ) == 0


def test_autocontent_absurd_daily_need_stops_before_database(monkeypatch):
    def forbidden_session():
        raise AssertionError("database must not be opened for absurd plan")

    monkeypatch.setattr(autocontent, "Session", forbidden_session)
    result = asyncio.run(autocontent._plan_for_user(
        1,
        55,
        "niche",
        ["keyword"],
        10,
    ))
    assert result == 0


def test_neuro_worker_claims_one_candidate_per_account_run(monkeypatch):
    calls = []

    async def claim_candidate(_session, *, user_id, account_id):
        calls.append(("claim", user_id, account_id))
        return {
            "comment_id": 99,
            "author_username": "author",
            "post_text": "post",
            "final_score": 88,
            "score_reason": "relevant",
        }

    async def generate_claimed(
        _session, *, user_id, account_id, comment_id
    ):
        calls.append(("generate", user_id, account_id, comment_id))
        return None

    monkeypatch.setattr(m4_jobs, "Session", FakeSession)
    monkeypatch.setattr(
        m4_jobs.neuro,
        "claim_candidate_for_generation",
        claim_candidate,
    )
    monkeypatch.setattr(
        m4_jobs.neuro,
        "generate_claimed_comment",
        generate_claimed,
    )

    asyncio.run(m4_jobs._hunt_for_user(
        1,
        "approve",
        10,
        "niche",
        {"tone": "direct"},
        "threads-user",
        b"encrypted-token",
        123456,
        acc_id=10,
    ))

    assert calls == [
        ("claim", 1, 10),
        ("generate", 1, 10, 99),
    ]


def test_publishing_continues_when_global_ai_is_disabled(monkeypatch):
    published = []

    async def claim_due_posts(_session):
        return [(99, 7)]

    async def publish_one(_session, row):
        published.append(row)
        return True, "published"

    monkeypatch.setattr(settings, "AI_ENABLED", False)
    monkeypatch.setattr(m3_jobs, "Session", FakeSession)
    monkeypatch.setattr(
        m3_jobs.autopilot,
        "claim_due_posts",
        claim_due_posts,
    )
    monkeypatch.setattr(m3_jobs.autopilot, "publish_one", publish_one)

    asyncio.run(m3_jobs.publisher())

    assert published == [(99, 7)]
