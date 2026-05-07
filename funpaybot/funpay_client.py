from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import requests

from .config import FunPayConfig


log = logging.getLogger(__name__)


class FunPayClientError(RuntimeError):
    pass


@dataclass(slots=True)
class BumpResult:
    category_id: str
    ok: bool
    message: str
    raw: Any = None


@dataclass(slots=True)
class PublishPlanItem:
    tier_id: str
    title: str
    price: int
    category_id: str
    url: str
    description: str


class FunPayClient:
    def __init__(self, config: FunPayConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.refresh_session()

    def refresh_session(self) -> None:
        self.session.cookies.clear()
        self.session.proxies.clear()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": "https://funpay.com",
            }
        )
        if self.config.golden_key:
            self.session.cookies.set("golden_key", self.config.golden_key, domain="funpay.com")

        if self.config.proxy:
            self.session.proxies.update({"http": self.config.proxy, "https": self.config.proxy})

    @property
    def can_write(self) -> bool:
        return not self.config.dry_run and self.config.marketplace_writes_enabled

    async def check_session(self) -> str:
        if self.config.dry_run:
            return "DRY-RUN: проверка FunPay пропущена, реальных запросов нет."
        if not self.config.golden_key:
            raise FunPayClientError("Не заполнен funpay.golden_key.")

        return await asyncio.to_thread(self._check_session_sync)

    def _check_session_sync(self) -> str:
        response = self.session.get(
            "https://funpay.com/account/",
            timeout=self.config.requests_timeout,
            allow_redirects=False,
        )
        if response.status_code in {301, 302, 401, 403}:
            raise FunPayClientError(
                f"FunPay не принял сессию, status={response.status_code}. Проверь golden_key."
            )
        if response.status_code >= 400:
            raise FunPayClientError(f"FunPay вернул ошибку status={response.status_code}.")
        return "Сессия FunPay открывается. Если аккаунт авторизован в браузере, golden_key похож на рабочий."

    async def bump_all(self, category_ids: list[str]) -> list[BumpResult]:
        if not category_ids:
            return [BumpResult(category_id="-", ok=False, message="В config.json пустой funpay.category_ids.")]

        results: list[BumpResult] = []
        for category_id in category_ids:
            results.append(await self.bump_category(category_id))
            await asyncio.sleep(2.0)
        return results

    async def bump_category(self, category_id: str) -> BumpResult:
        category_id = str(category_id).strip()
        if not category_id:
            return BumpResult(category_id=category_id, ok=False, message="Пустой ID категории.")

        if self.config.dry_run:
            return BumpResult(
                category_id=category_id,
                ok=True,
                message="DRY-RUN: бот сделал бы поднятие этой категории, но реальный запрос не отправлен.",
            )

        if not self.config.marketplace_writes_enabled:
            return BumpResult(
                category_id=category_id,
                ok=False,
                message="Реальные действия выключены: marketplace_writes_enabled=false.",
            )

        if not self.config.golden_key:
            return BumpResult(category_id=category_id, ok=False, message="Не заполнен funpay.golden_key.")

        return await asyncio.to_thread(self._bump_category_sync, category_id)

    def build_publish_plan(self, config) -> list[PublishPlanItem]:
        from .catalog import render_funpay_text

        default_category_id = config.funpay.category_ids[0] if config.funpay.category_ids else ""
        items: list[PublishPlanItem] = []
        for tier in config.service.tiers:
            category_id = tier.funpay_category_id or default_category_id
            items.append(
                PublishPlanItem(
                    tier_id=tier.id,
                    title=tier.title,
                    price=tier.price,
                    category_id=category_id,
                    url=f"https://funpay.com/lots/{category_id}/trade" if category_id else "",
                    description=render_funpay_text(config, tier),
                )
            )
        return items

    def _bump_category_sync(self, category_id: str) -> BumpResult:
        api_result = self._try_funpay_api(category_id)
        if api_result is not None:
            return api_result

        return self._try_http_raise(category_id)

    def _try_funpay_api(self, category_id: str) -> BumpResult | None:
        try:
            from FunPayAPI import Account  # type: ignore
        except Exception:
            return None

        try:
            account = Account(self.config.golden_key)
            if hasattr(account, "get"):
                account.get()

            for method_name in ("raise_lots", "raise_offers", "raise_lots_by_category"):
                method = getattr(account, method_name, None)
                if method is None:
                    continue
                raw = method(category_id)
                return BumpResult(
                    category_id=category_id,
                    ok=True,
                    message=f"FunPayAPI: поднятие отправлено через {method_name}.",
                    raw=raw,
                )
        except Exception as exc:
            log.warning("FunPayAPI raise failed: %s", exc)
            return BumpResult(category_id=category_id, ok=False, message=f"FunPayAPI ошибка: {exc}")

        return None

    def _try_http_raise(self, category_id: str) -> BumpResult:
        url = "https://funpay.com/lots/raise"
        headers = {"Referer": f"https://funpay.com/lots/{category_id}/"}
        payloads = (
            {"node": category_id},
            {"node_id": category_id},
            {"category_id": category_id},
            {"game_id": category_id},
        )

        last_error = ""
        for payload in payloads:
            try:
                response = self.session.post(
                    url,
                    data=payload,
                    headers=headers,
                    timeout=self.config.requests_timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                continue

            raw_text = response.text[:500]
            if response.status_code == 429:
                return BumpResult(
                    category_id=category_id,
                    ok=False,
                    message="FunPay ограничил частоту запросов, нужно увеличить интервал.",
                    raw=raw_text,
                )
            if response.status_code in {401, 403}:
                return BumpResult(
                    category_id=category_id,
                    ok=False,
                    message="FunPay не принял сессию. Проверь golden_key/user_agent.",
                    raw=raw_text,
                )
            if response.status_code >= 500:
                last_error = f"status={response.status_code}: {raw_text}"
                continue

            try:
                raw_json = response.json()
            except ValueError:
                raw_json = {"status_code": response.status_code, "text": raw_text}

            ok = response.status_code < 400 and not raw_json.get("error")
            message = raw_json.get("msg") or raw_json.get("message") or "HTTP-запрос поднятия отправлен."
            return BumpResult(category_id=category_id, ok=ok, message=str(message), raw=raw_json)

        return BumpResult(
            category_id=category_id,
            ok=False,
            message=f"Не получилось отправить поднятие через HTTP. Последняя ошибка: {last_error}",
        )
