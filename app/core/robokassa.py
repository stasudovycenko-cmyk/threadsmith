"""
Робокасса. Голый HTTP, без SDK.

Схема:
1. Бот создаёт запись в payments -> получает inv_id (bigserial, Робокассе
   нужен уникальный int).
2. Формируем ссылку с подписью SHA256(login:sum:inv_id:pass1[:shp_...]).
3. Юзер платит -> Робокасса дёргает наш ResultURL (пароль #2 в подписи).
4. Проверяем подпись, помечаем payment paid, активируем подписку,
   начисляем кредиты. Отвечаем "OK<inv_id>" - иначе Робокасса будет
   ретраить вебхук.

Кастомные параметры (shp_) участвуют в подписи В АЛФАВИТНОМ ПОРЯДКЕ -
это самые частые грабли интеграции.
"""
import hashlib
from urllib.parse import urlencode

from app.core.config import settings

PAY_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def payment_link(inv_id: int, amount: float, description: str,
                 user_id: int, plan: str) -> str:
    shp = {"shp_plan": plan, "shp_uid": str(user_id)}
    shp_sorted = sorted(shp.items())  # алфавитный порядок обязателен
    sig_base = f"{settings.ROBOKASSA_LOGIN}:{amount:.2f}:{inv_id}:{settings.ROBOKASSA_PASS1}"
    sig_base += "".join(f":{k}={v}" for k, v in shp_sorted)
    params = {
        "MerchantLogin": settings.ROBOKASSA_LOGIN,
        "OutSum": f"{amount:.2f}",
        "InvId": inv_id,
        "Description": description,
        "SignatureValue": _sha256(sig_base),
        **dict(shp_sorted),
    }
    if settings.ROBOKASSA_TEST_MODE:
        params["IsTest"] = 1
    return f"{PAY_URL}?{urlencode(params)}"


def verify_result(out_sum: str, inv_id: str, signature: str,
                  shp_params: dict) -> bool:
    """Проверка подписи вебхука ResultURL (пароль #2)."""
    shp_sorted = sorted(shp_params.items())
    sig_base = f"{out_sum}:{inv_id}:{settings.ROBOKASSA_PASS2}"
    sig_base += "".join(f":{k}={v}" for k, v in shp_sorted)
    return _sha256(sig_base).lower() == signature.lower()
