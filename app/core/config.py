"""
Конфиг. Всё из env, валидация на старте — если чего-то нет, падаем сразу,
а не в проде посреди ночи.
"""
from decimal import Decimal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    BOT_TOKEN: str

    # База (Supabase -> Settings -> Database -> Connection string, режим session)
    DATABASE_URL: str  # postgresql+asyncpg://...

    # Threads API
    THREADS_APP_ID: str
    THREADS_APP_SECRET: str
    THREADS_REDIRECT_URI: str

    # Шифрование токенов: 32 байта в base64
    TOKEN_ENC_KEY: str

    # LLM
    ANTHROPIC_API_KEY: str = ""
    AI_ENABLED: bool = True
    AI_AUTOCONTENT_ENABLED: bool = True
    AI_NEURO_ENABLED: bool = True
    AI_RADAR_ENABLED: bool = True
    AI_USER_DAILY_USD_LIMIT: Decimal = Decimal("3.00")
    AI_ACCOUNT_DAILY_USD_LIMIT: Decimal = Decimal("2.00")

    # Робокасса
    ROBOKASSA_LOGIN: str
    ROBOKASSA_PASS1: str
    ROBOKASSA_PASS2: str
    ROBOKASSA_TEST_MODE: bool = True

    # API-процесс
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    PUBLIC_BASE_URL: str = ""

    # Админы бота: telegram_id через запятую, например "123456,7891011"
    ADMIN_IDS: str = ""

    class Config:
        env_file = ".env"


settings = Settings()

# Тарифы. Кредиты - месячная квота, зачисляется при оплате/продлении.
PLANS = {
    "free":  {"price": 0,    "credits": 30,   "title": "Free"},
    "start": {"price": 990,  "credits": 500,  "title": "Старт"},
    "pro":   {"price": 2490, "credits": 2000, "title": "Про"},
}

# Прайс действий в кредитах
CREDIT_COSTS = {
    "generate_post": 5,
    "rewrite": 4,
    "generate_thread": 12,
    "razbor": 3,
    "voice_onboarding": 0,   # онбординг голоса бесплатный - это активация
    "radar_search": 8,
    "radar_semantic_score": 1,
    "neuro_comment": 2,
    "neuro_variant": 2,
}
