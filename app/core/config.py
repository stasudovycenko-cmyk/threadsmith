"""
Конфиг. Всё из env, валидация на старте — если чего-то нет, падаем сразу,
а не в проде посреди ночи.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str

    # База (Supabase -> Settings -> Database -> Connection string, режим session)
    DATABASE_URL: str  # postgresql+asyncpg://...

    # Threads API
    THREADS_APP_ID: str
    THREADS_APP_SECRET: str
    THREADS_REDIRECT_URI: str  # https://api.твойдомен.ru/oauth/threads/callback

    # Шифрование токенов: 32 байта в base64. Сгенерить:
    # python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    TOKEN_ENC_KEY: str

    # LLM
    ANTHROPIC_API_KEY: str = ""

    # Робокасса
    ROBOKASSA_LOGIN: str
    ROBOKASSA_PASS1: str  # для формирования ссылки на оплату
    ROBOKASSA_PASS2: str  # для проверки вебхука ResultURL
    ROBOKASSA_TEST_MODE: bool = True

    # API-процесс
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    PUBLIC_BASE_URL: str = ""  # https://api.твойдомен.ru

    class Config:
        env_file = ".env"


settings = Settings()

# Тарифы. Кредиты - месячная квота, зачисляется при оплате/продлении.
# Цены в рублях. Free выдаётся при регистрации разово.
PLANS = {
    "free":  {"price": 0,    "credits": 30,   "title": "Free"},
    "start": {"price": 990,  "credits": 500,  "title": "Старт"},
    "pro":   {"price": 2490, "credits": 2000, "title": "Про"},
}

# Прайс действий в кредитах (Модули 1-3 будут списывать по этой таблице)
CREDIT_COSTS = {
    "generate_post": 5,
    "rewrite": 4,
    "generate_thread": 12,
    "razbor": 3,
    "voice_onboarding": 10,
    "radar_search": 8,
}
