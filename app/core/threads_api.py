"""
Threads API: OAuth-флоу + обёртка над graph.threads.net.

Флоу токенов:
1. authorize -> code (short-lived)
2. code -> short-lived token (1 час)
3. short-lived -> long-lived (60 дней)
4. long-lived рефрешится через /refresh_access_token, но ТОЛЬКО пока жив.
   Протух - юзер проходит OAuth заново. Поэтому token_refresher будет
   обновлять за 7 дней до expires_at.

Скоупы запрашиваем все четыре сразу - Meta ревьюит каждый отдельно,
но в dev mode на тестовых юзерах всё работает без ревью.
"""
import asyncio
import httpx
import re
from urllib.parse import urlencode

from app.core.config import settings

AUTH_URL = "https://threads.net/oauth/authorize"
BASE = "https://graph.threads.net"

SCOPES = "threads_basic,threads_content_publish,threads_manage_insights,threads_manage_replies,threads_keyword_search"
_SENSITIVE_RESPONSE_RE = re.compile(
    r"(?i)(access_token|refresh_token|client_secret|app_secret|bot_token)"
    r"([\"'\s:=]+)([^,\"'\s&}]+)"
)


class ThreadsAPIError(RuntimeError):
    """Threads API error without secrets from request query params."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code


def _safe_response_error(response: httpx.Response) -> ThreadsAPIError:
    method = response.request.method if response.request else "REQUEST"
    path = response.request.url.path if response.request else "unknown"

    try:
        body = response.text.strip()
    except Exception:
        body = ""
    body = _SENSITIVE_RESPONSE_RE.sub(
        r"\1\2<redacted>",
        body,
    )

    # Не тащим гигантский body в логи/БД.
    if len(body) > 800:
        body = body[:800] + "...<truncated>"

    detail = f"Threads API {method} {path} -> HTTP {response.status_code}"
    if body:
        detail += f": {body}"

    return ThreadsAPIError(detail, status_code=response.status_code)


def _raise_for_status_safe(response: httpx.Response) -> None:
    is_error = getattr(response, "is_error", None)

    if is_error is None:
        response.raise_for_status()
        return

    if is_error:
        raise _safe_response_error(response)

def auth_link(state: str) -> str:
    params = {
        "client_id": settings.THREADS_APP_ID,
        "redirect_uri": settings.THREADS_REDIRECT_URI,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """code -> short-lived token. Возвращает {access_token, user_id}."""
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{BASE}/oauth/access_token", data={
            "client_id": settings.THREADS_APP_ID,
            "client_secret": settings.THREADS_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": settings.THREADS_REDIRECT_URI,
            "code": code,
        })
        _raise_for_status_safe(r)
        return r.json()


async def to_long_lived(short_token: str) -> dict:
    """short -> long-lived (60 дней). Возвращает {access_token, expires_in}."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/access_token", params={
            "grant_type": "th_exchange_token",
            "client_secret": settings.THREADS_APP_SECRET,
            "access_token": short_token,
        })
        _raise_for_status_safe(r)
        return r.json()


async def refresh_long_lived(token: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/refresh_access_token", params={
            "grant_type": "th_refresh_token",
            "access_token": token,
        })
        _raise_for_status_safe(r)
        return r.json()


async def get_me(token: str) -> dict:
    """Профиль подключённого аккаунта - для отображения в боте."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{BASE}/v1.0/me", params={
            "fields": "id,username,threads_profile_picture_url",
            "access_token": token,
        })
        _raise_for_status_safe(r)
        return r.json()


# ---------- публикация (Модуль 3) ----------
# Двухшаговая схема Threads: создать контейнер -> опубликовать.
# Реплай - это тот же контейнер, но с reply_to_id.

async def create_container(token: str, threads_user_id: str, text: str,
                           reply_to_id: str | None = None,
                           image_url: str | None = None) -> str:
    params = {"access_token": token, "text": text}
    if image_url:
        params.update({"media_type": "IMAGE", "image_url": image_url})
    else:
        params["media_type"] = "TEXT"
    if reply_to_id:
        params["reply_to_id"] = reply_to_id
    async with httpx.AsyncClient(timeout=30) as c:
        last = None
        for attempt in range(6):
            r = await c.post(f"{BASE}/v1.0/{threads_user_id}/threads", params=params)
            if r.status_code == 200:
                return r.json()["id"]
            last = r
            if attempt < 5:
                await asyncio.sleep(5)
        _raise_for_status_safe(last)
        return last.json()["id"]


async def publish_container(token: str, threads_user_id: str,
                            container_id: str) -> str:
    """Publish container with retry (Threads flaky media-not-found)."""
    async with httpx.AsyncClient(timeout=30) as c:
        last = None
        for attempt in range(6):
            r = await c.post(f"{BASE}/v1.0/{threads_user_id}/threads_publish",
                             params={"access_token": token,
                                     "creation_id": container_id})
            if r.status_code == 200:
                return r.json()["id"]
            last = r
            if attempt < 5:
                await asyncio.sleep(5)
        _raise_for_status_safe(last)
        return last.json()["id"]


async def get_replies(token: str, post_id: str) -> list[dict]:
    """Комменты первого уровня к посту."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/v1.0/{post_id}/replies", params={
            "fields": "id,text,username,timestamp",
            "access_token": token,
        })
        _raise_for_status_safe(r)
        return r.json().get("data", [])


# ---------- Радар (Модуль 1) ----------

async def keyword_search(token: str, query: str,
                         search_type: str = "TOP") -> list[dict]:
    """Поиск публичных постов. ВАЖНО: метрик чужих постов API не отдаёт,
    только контент. Лимит: 2200 запросов/юзер/24ч (скользящие)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/v1.0/keyword_search", params={
            "q": query,
            "search_type": search_type,
            "fields": "id,text,username",
            "access_token": token,
        })
        _raise_for_status_safe(r)
        return r.json().get("data", [])


async def get_insights(token: str, post_id: str) -> dict:
    """Метрики СВОЕГО поста. Для чужих не работает - ограничение API."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{BASE}/v1.0/{post_id}/insights", params={
            "metric": "views,likes,replies,reposts,quotes,shares",
            "access_token": token,
        })
        _raise_for_status_safe(r)
        data = r.json().get("data", [])
        metrics = {}
        for item in data:
            name = item.get("name")
            values = item.get("values")
            if (
                not name
                or not isinstance(values, list)
                or not values
                or not isinstance(values[0], dict)
                or "value" not in values[0]
            ):
                continue
            metrics[name] = values[0]["value"]
        return metrics
