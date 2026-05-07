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
        from urllib.parse import urljoin

        trade_url = f"https://funpay.com/lots/{category_id}/trade"

        # 1. Загружаем страницу trade, чтобы вытащить CSRF-токен и список серверов
        try:
            page = self.session.get(trade_url, timeout=self.config.requests_timeout)
        except requests.RequestException as exc:
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message=f"Не удалось загрузить страницу trade: {exc}",
            )

        if page.status_code in {401, 403} or "/account/login" in page.url:
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message="FunPay не принял сессию. Проверь golden_key: открылась страница входа.",
            )

        if "<form" not in page.text.lower():
            return CreateOfferResult(
                category_id=category_id, tier_id=tier_id, ok=False,
                message="FunPay не отдал форму создания предложения. Проверь golden_key и права аккаунта.",
            )

        form_match = _re.search(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", page.text, _re.I | _re.S)
        form_attrs = form_match.group("attrs") if form_match else ""
        form_body = form_match.group("body") if form_match else page.text
        action_match = _re.search(r'action=["\']([^"\']+)["\']', form_attrs, _re.I)
        post_url = urljoin(page.url, action_match.group(1)) if action_match else trade_url

        # Собираем реальные поля формы, чтобы не слать совсем произвольный payload.
        form_payload: dict[str, Any] = {}
        for input_match in _re.finditer(r"<input\b([^>]*)>", form_body, _re.I | _re.S):
            attrs = input_match.group(1)
            name_match = _re.search(r'name=["\']([^"\']+)["\']', attrs, _re.I)
            if not name_match:
                continue
            value_match = _re.search(r'value=["\']([^"\']*)["\']', attrs, _re.I)
            form_payload[name_match.group(1)] = value_match.group(1) if value_match else ""
        for select_match in _re.finditer(r"<select\b([^>]*)>(.*?)</select>", form_body, _re.I | _re.S):
            attrs, body = select_match.group(1), select_match.group(2)
            name_match = _re.search(r'name=["\']([^"\']+)["\']', attrs, _re.I)
            if not name_match:
                continue
            selected = _re.search(r'<option\b[^>]*value=["\']([^"\']+)["\'][^>]*selected', body, _re.I)
            first = _re.search(r'<option\b[^>]*value=["\']([^"\']+)["\']', body, _re.I)
            value = selected.group(1) if selected else first.group(1) if first else ""
            if value:
                form_payload[name_match.group(1)] = value

        textarea_names = [
            m.group(1)
            for m in _re.finditer(r"<textarea\b[^>]*name=[\"']([^\"']+)[\"']", form_body, _re.I)
        ]
        for name in textarea_names:
            form_payload[name] = text

        for key in list(form_payload):
            low = key.lower()
            if any(part in low for part in ("price", "cost", "amount")):
                form_payload[key] = str(price)
            if any(part in low for part in ("desc", "text", "summary", "details")):
                form_payload[key] = text

        server_id = ""
        server_match = _re.search(r'<option\b[^>]*value=["\'](\d+)["\']', form_body, _re.I)
        if server_match:
            server_id = server_match.group(1)

        payloads: list[dict[str, Any]] = []
        if form_payload:
            payloads.append(form_payload)
        payloads.extend([
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
        ])

        for payload in payloads:
            for token_name in ("_token", "csrf_token", "csrf"):
                token = _extract_form_value(page.text, token_name)
                if token and token_name not in payload:
                    payload[token_name] = token

        headers = {
            "Referer": trade_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        last_error = ""
        for payload in payloads:
            try:
                response = self.session.post(
                    post_url,
                    data=payload,
                    headers=headers,
                    timeout=self.config.requests_timeout,
                    allow_redirects=False,
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

            interpreted = _interpret_create_offer_response(response)
            if interpreted is not None:
                ok, message, raw = interpreted
                return CreateOfferResult(category_id=category_id, tier_id=tier_id, ok=ok, message=message, raw=raw)

            last_error = (
                f"FunPay не подтвердил создание: status={response.status_code}, "
                f"content-type={response.headers.get('content-type', '-')}. "
                f"Скорее всего вернулась страница формы/валидации, а не созданный лот."
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


def _extract_form_value(html: str, name: str) -> str:
    import re

    patterns = (
        rf'name=["\']{re.escape(name)}["\'][^>]*value=["\']([^"\']*)["\']',
        rf'value=["\']([^"\']*)["\'][^>]*name=["\']{re.escape(name)}["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            return match.group(1)
    return ""


def _interpret_create_offer_response(response: requests.Response) -> tuple[bool, str, Any] | None:
    status = response.status_code
    location = response.headers.get("Location", "")

    if 300 <= status < 400:
        if "/account/login" in location:
            return False, "FunPay отправил на страницу входа. golden_key не рабочий или устарел.", {"location": location}
        if "/trade" in location:
            return False, "FunPay вернул обратно на форму создания. Предложение не подтверждено.", {"location": location}
        return True, f"FunPay подтвердил создание редиректом: {location or 'redirect'}", {"location": location}

    if status == 204:
        return True, "FunPay вернул 204 No Content — запрос принят.", {"status_code": status}

    try:
        data = response.json()
    except ValueError:
        data = None

    if isinstance(data, dict):
        error = data.get("error") or data.get("errors")
        message = str(data.get("msg") or data.get("message") or data.get("error") or data.get("errors") or "")
        if error:
            return False, f"FunPay вернул ошибку: {message or error}", data
        status_value = str(data.get("status") or data.get("result") or "").lower()
        if data.get("success") is True or status_value in {"ok", "success", "done"}:
            return True, message or "FunPay JSON подтвердил создание.", data
        if any(key in data for key in ("url", "redirect", "lot_id", "offer_id")):
            return True, message or "FunPay JSON вернул данные созданного предложения.", data
        if message:
            positive = ("создан", "добавлен", "успеш")
            if any(word in message.lower() for word in positive):
                return True, message, data
            return False, f"FunPay не подтвердил создание: {message}", data
        return False, "FunPay вернул JSON без подтверждения создания.", data

    text = response.text[:2000]
    low = text.lower()
    if "/account/login" in response.url or "name=\"login\"" in low and "password" in low:
        return False, "FunPay вернул страницу входа. golden_key не рабочий или устарел.", {"status_code": status}
    if any(marker in low for marker in ("alert-danger", "has-error", "form-error", "ошибка", "error")):
        return False, f"FunPay вернул HTML с ошибкой/валидацией: {text[:250].strip()}", {"status_code": status}
    if any(marker in low for marker in ("предложение создан", "лот создан", "успешно создан", "alert-success")):
        return True, "FunPay HTML подтвердил создание предложения.", {"status_code": status}
    if "<form" in low:
        return False, (
            "FunPay вернул страницу формы, а не подтверждение создания. "
            "Предложение не создано или форма требует другие поля."
        ), {"status_code": status}
    return None
