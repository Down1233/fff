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


@dataclass(slots=True)
class CreateOfferResult:
    category_id: str
    tier_id: str
    ok: bool
    message: str
    raw: Any = None


class FunPayClient:
    def __init__(self, config: FunPayConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self._seen_order_ids: set[str] = set()
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
        return bool(self.config.golden_key)

    async def check_session(self) -> str:
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

    # ── Заказы FunPay ──────────────────────────────────────

    @dataclass(slots=True)
    class FunPayOrder:
        order_id: str
        title: str
        price: float
        buyer_name: str
        status: str
        raw: Any = None

        def __str__(self) -> str:
            return f"#{self.order_id}: {self.title} ({self.price}₽) [{self.status}]"

    async def get_new_orders(self) -> list[FunPayOrder]:
        """Получить новые оплаченные заказы, которые ещё не обрабатывались."""
        if not self.config.golden_key:
            return []
        orders = await asyncio.to_thread(self._fetch_orders_sync)
        new_orders = [o for o in orders if o.order_id not in self._seen_order_ids]
        for o in new_orders:
            self._seen_order_ids.add(o.order_id)
        return new_orders

    def _fetch_orders_sync(self) -> list[FunPayOrder]:
        """Получить список оплаченных заказов через FunPayAPI или HTTP."""
        # Сначала пробуем FunPayAPI
        api_orders = self._try_funpay_api_orders()
        if api_orders is not None:
            return api_orders
        # Фолбэк на HTTP
        return self._try_http_orders()

    def _try_funpay_api_orders(self) -> list[FunPayOrder] | None:
        try:
            from FunPayAPI import Account  # type: ignore
        except Exception:
            return None

        try:
            account = Account(self.config.golden_key)
            if hasattr(account, "get"):
                account.get()

            orders: list[FunPayClient.FunPayOrder] = []

            # Пробуем разные методы получения заказов
            for method_name in ("get_orders", "orders", "get_trades", "trades"):
                method = getattr(account, method_name, None)
                if method is None:
                    continue
                raw = method()
                if raw is None:
                    continue
                items = raw if isinstance(raw, list) else []
                for item in items:
                    if not isinstance(item, dict):
                        # Возможно это объект FunPayAPI
                        oid = getattr(item, "id", None) or getattr(item, "order_id", None)
                        if oid is None:
                            continue
                        title = getattr(item, "title", "") or getattr(item, "description", "") or ""
                        price = getattr(item, "price", 0) or 0
                        buyer = getattr(item, "buyer", "") or getattr(item, "buyer_name", "") or ""
                        status = getattr(item, "status", "") or ""
                        orders.append(FunPayClient.FunPayOrder(
                            order_id=str(oid),
                            title=str(title),
                            price=float(price),
                            buyer_name=str(buyer),
                            status=str(status),
                            raw=item,
                        ))
                    else:
                        oid = item.get("id") or item.get("order_id") or item.get("orderId")
                        if not oid:
                            continue
                        orders.append(FunPayClient.FunPayOrder(
                            order_id=str(oid),
                            title=str(item.get("title", item.get("description", ""))),
                            price=float(item.get("price", item.get("amount", 0))),
                            buyer_name=str(item.get("buyer", item.get("buyer_name", ""))),
                            status=str(item.get("status", item.get("state", ""))),
                            raw=item,
                        ))
                if orders:
                    return orders
        except Exception as exc:
            log.warning("FunPayAPI orders failed: %s", exc)
            return None

        return None

    def _try_http_orders(self) -> list[FunPayOrder]:
        """Получить заказы через HTTP-запрос к странице аккаунта."""
        orders: list[FunPayClient.FunPayOrder] = []
        try:
            response = self.session.get(
                "https://funpay.com/account/trades",
                timeout=self.config.requests_timeout,
            )
            if response.status_code >= 400:
                log.warning("HTTP orders: status=%d", response.status_code)
                return orders

            # Парсим HTML/JSON ответ
            text = response.text
            try:
                data = response.json()
                # JSON формат
                items = data if isinstance(data, list) else data.get("orders", data.get("trades", []))
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    oid = item.get("id") or item.get("order_id")
                    if not oid:
                        continue
                    orders.append(FunPayClient.FunPayOrder(
                        order_id=str(oid),
                        title=str(item.get("title", item.get("description", ""))),
                        price=float(item.get("price", item.get("amount", 0))),
                        buyer_name=str(item.get("buyer", item.get("buyer_name", ""))),
                        status=str(item.get("status", item.get("state", ""))),
                        raw=item,
                    ))
            except ValueError:
                # HTML формат — простой парсинг
                import re
                # Ищем ID заказов и их статусы
                order_pattern = re.compile(r'data-order[^"]*?(\d+)', re.IGNORECASE)
                for match in order_pattern.finditer(text):
                    oid = match.group(1)
                    orders.append(FunPayClient.FunPayOrder(
                        order_id=oid,
                        title="",
                        price=0,
                        buyer_name="",
                        status="paid",
                        raw=None,
                    ))
        except Exception as exc:
            log.warning("HTTP orders fetch failed: %s", exc)

        return orders

    async def send_chat_message(self, order_id: str, message: str) -> bool:
        """Отправить сообщение в чат заказа FunPay."""
        if not self.config.golden_key:
            return False
        return await asyncio.to_thread(self._send_chat_sync, order_id, message)

    def _send_chat_sync(self, order_id: str, message: str) -> bool:
        """Отправить сообщение через FunPayAPI или HTTP."""
        # Сначала FunPayAPI
        try:
            from FunPayAPI import Account  # type: ignore
            account = Account(self.config.golden_key)
            if hasattr(account, "get"):
                account.get()
            for method_name in ("send_message", "chat_send", "message_send"):
                method = getattr(account, method_name, None)
                if method:
                    method(order_id, message)
                    return True
        except Exception as exc:
            log.debug("FunPayAPI chat send failed: %s", exc)

        # HTTP фолбэк
        try:
            response = self.session.post(
                f"https://funpay.com/account/trades/{order_id}/chat",
                data={"message": message},
                headers={"Referer": f"https://funpay.com/account/trades/{order_id}/"},
                timeout=self.config.requests_timeout,
            )
            return response.status_code < 400
        except Exception as exc:
            log.warning("HTTP chat send failed: %s", exc)
            return False

    async def confirm_order(self, order_id: str) -> bool:
        """Подтвердить выполнение заказа на FunPay."""
        if not self.config.golden_key:
            return False
        return await asyncio.to_thread(self._confirm_order_sync, order_id)

    def _confirm_order_sync(self, order_id: str) -> bool:
        try:
            from FunPayAPI import Account  # type: ignore
            account = Account(self.config.golden_key)
            if hasattr(account, "get"):
                account.get()
            for method_name in ("confirm_order", "complete_order", "finish_order"):
                method = getattr(account, method_name, None)
                if method:
                    method(order_id)
                    return True
        except Exception as exc:
            log.debug("FunPayAPI confirm failed: %s", exc)

        try:
            response = self.session.post(
                f"https://funpay.com/account/trades/{order_id}/confirm",
                headers={"Referer": f"https://funpay.com/account/trades/{order_id}/"},
                timeout=self.config.requests_timeout,
            )
            return response.status_code < 400
        except Exception as exc:
            log.warning("HTTP confirm failed: %s", exc)
            return False

    # ── Создание предложений на FunPay ──────────────────────

    async def create_offer(
        self, category_id: str, tier_id: str, text: str, price: int,
    ) -> CreateOfferResult:
        if not self.config.golden_key:
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id,
                ok=False, message="Не заполнен funpay.golden_key.",
            )
        return await asyncio.to_thread(
            self._create_offer_sync, category_id, tier_id, text, price,
        )

    def _create_offer_sync(
        self, category_id: str, tier_id: str, text: str, price: int,
    ) -> CreateOfferResult:
        api_result = self._try_funpay_api_create_offer(category_id, tier_id, text, price)
        if api_result is not None:
            return api_result
        return self._try_http_create_offer(category_id, tier_id, text, price)

    def _try_funpay_api_create_offer(
        self, category_id: str, tier_id: str, text: str, price: int,
    ) -> CreateOfferResult | None:
        try:
            from FunPayAPI import Account  # type: ignore
        except Exception:
            return None

        try:
            account = Account(self.config.golden_key)
            if hasattr(account, "get"):
                account.get()

            for method_name in ("create_offer", "add_offer", "create_lot"):
                method = getattr(account, method_name, None)
                if method is None:
                    continue
                raw = method(category_id, text, price)
                return CreateOfferResult(
                    category_id=category_id, tier_id=tier_id, ok=True,
                    message=f"FunPayAPI: предложение создано через {method_name}.",
                    raw=raw,
                )
        except Exception as exc:
            log.warning("FunPayAPI create offer failed: %s", exc)
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message=f"FunPayAPI ошибка: {exc}",
            )
        return None

    def _try_http_create_offer(
        self, category_id: str, tier_id: str, text: str, price: int,
    ) -> CreateOfferResult:
        import re as _re

        trade_url = f"https://funpay.com/lots/{category_id}/trade"

        # 1. Загружаем страницу trade, чтобы вытащить CSRF-токен и список серверов
        try:
            page = self.session.get(trade_url, timeout=self.config.requests_timeout)
        except requests.RequestException as exc:
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message=f"Не удалось загрузить страницу trade: {exc}",
            )

        if page.status_code in {401, 403}:
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message="FunPay не принял сессию. Проверь golden_key.",
            )

        # CSRF-токен
        csrf_token = ""
        for pattern in (
            r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']_token["\']',
            r'name=["\']csrf[_-]?token["\'][^>]*value=["\']([^"\']+)["\']',
        ):
            m = _re.search(pattern, page.text)
            if m:
                csrf_token = m.group(1)
                break

        # Список серверов (subcategory) — берём первый
        server_id = ""
        server_match = _re.search(
            r'<option[^>]*value=["\'](\d+)["\'][^>]*>', page.text,
        )
        if server_match:
            server_id = server_match.group(1)

        # 2. Пробуем создать предложение разными форматами payload
        payloads: list[dict[str, Any]] = [
            {
                "node": category_id,
                "text": text,
                "price": str(price),
                **({"server": server_id} if server_id else {}),
            },
            {
                "category_id": category_id,
                "description": text,
                "price": str(price),
                **({"server_id": server_id} if server_id else {}),
            },
            {
                "offer[text]": text,
                "offer[price]": str(price),
                "offer[node]": category_id,
                **({"offer[server]": server_id} if server_id else {}),
            },
        ]

        for payload in payloads:
            if csrf_token:
                payload["_token"] = csrf_token

        headers = {
            "Referer": trade_url,
            "X-Requested-With": "XMLHttpRequest",
        }

        last_error = ""
        for payload in payloads:
            try:
                response = self.session.post(
                    trade_url,
                    data=payload,
                    headers=headers,
                    timeout=self.config.requests_timeout,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                continue

            if response.status_code in {401, 403}:
                return CreateOfferResult(
                    category_id=category_id, tier_id=tier_id, ok=False,
                    message="FunPay не принял сессию.",
                )
            if response.status_code == 429:
                return CreateOfferResult(
                    category_id=category_id, tier_id=tier_id, ok=False,
                    message="FunPay ограничил частоту запросов.",
                )
            if response.status_code >= 500:
                last_error = f"status={response.status_code}"
                continue

            try:
                raw_json = response.json()
            except ValueError:
                raw_json = {"status_code": response.status_code, "text": response.text[:500]}

            ok = response.status_code < 400 and not raw_json.get("error")
            message = (
                raw_json.get("msg") or raw_json.get("message")
                or ("Предложение создано" if ok else "Неизвестная ошибка")
            )
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id,
                ok=ok, message=str(message), raw=raw_json,
            )

        return CreateOfferResult(
            category_id=category_id, tier_id=tier_id, ok=False,
            message=f"Не удалось создать предложение. Последняя ошибка: {last_error}",
        )

    async def create_all_offers(self, config) -> list[CreateOfferResult]:
        from .catalog import render_funpay_text

        results: list[CreateOfferResult] = []
        default_category = (
            config.funpay.category_ids[0] if config.funpay.category_ids else ""
        )

        for tier in config.service.tiers:
            category_id = tier.funpay_category_id or default_category
            if not category_id:
                results.append(CreateOfferResult(
                    category_id="-", tier_id=tier.id,
                    ok=False, message="Нет категории для товара.",
                ))
                continue

            text = render_funpay_text(config, tier)
            result = await self.create_offer(category_id, tier.id, text, tier.price)
            results.append(result)
            await asyncio.sleep(3.0)

        return results
