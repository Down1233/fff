"""Hero SMS API client — виртуальные номера для приёма SMS."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

# setStatus values
STATUS_READY = 1       # сообщить что номер готов
STATUS_USED = 3        # сообщить что код использован
STATUS_CANCEL = 8      # отменить активацию
STATUS_FINISHED = -1   # завершить активацию


class HeroSmsError(RuntimeError):
    pass


@dataclass(slots=True)
class BalanceInfo:
    balance: float
    currency: str = "RUB"

    def __str__(self) -> str:
        return f"{self.balance:.2f} {self.currency}"


@dataclass(slots=True)
class CountryInfo:
    id: int
    rus: str
    eng: str
    visible: bool
    retry: bool
    rent: bool
    multi_service: bool

    def __str__(self) -> str:
        return f"{self.id}: {self.rus} / {self.eng}"


@dataclass(slots=True)
class PriceInfo:
    country_id: int
    service: str
    price: float
    count: int = 0

    def __str__(self) -> str:
        return f"страна {self.country_id}, сервис {self.service}: {self.price:.2f} ₽ (доступно: {self.count})"


@dataclass(slots=True)
class NumberInfo:
    id: int
    number: str
    service: str
    country_id: int

    def __str__(self) -> str:
        return f"#{self.id}: +{self.number} (сервис={self.service}, страна={self.country_id})"


@dataclass(slots=True)
class SmsCode:
    code: str
    full_text: str

    def __str__(self) -> str:
        return self.code


@dataclass(slots=True)
class ActiveActivation:
    id: int
    number: str
    service: str
    country_id: int
    status: str

    def __str__(self) -> str:
        return f"#{self.id}: +{self.number} [{self.status}] (сервис={self.service}, страна={self.country_id})"


class HeroSmsClient:
    """Асинхронная обёртка над Hero SMS API."""

    def __init__(self, api_key: str, timeout: int = 30, proxy: str = "") -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.proxy = proxy
        self.session = requests.Session()
        self._refresh_session()

    def _refresh_session(self) -> None:
        self.session.cookies.clear()
        self.session.proxies.clear()
        self.session.headers.update({
            "User-Agent": "FunPayBot/1.0",
            "Accept": "application/json, text/plain, */*",
        })
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})

    def _get(self, params: dict[str, str | int]) -> requests.Response:
        params["api_key"] = self.api_key
        return self.session.get(BASE_URL, params=params, timeout=self.timeout)

    @staticmethod
    def _check_error(data: object) -> None:
        if isinstance(data, dict) and data.get("title") in (
            "BAD_KEY",
            "ERROR_ACCESS",
            "BAD_ACTION",
            "BAD_SERVICE",
            "BAD_COUNTRY",
            "NO_BALANCE",
            "NO_NUMBERS",
            "WRONG_ACTIVATION",
            "BANNED",
        ):
            raise HeroSmsError(f"{data.get('title')}: {data.get('details', '')}")

    # ── баланс ──────────────────────────────────────────────

    async def get_balance(self) -> BalanceInfo:
        data = await asyncio.to_thread(self._get_balance_sync)
        return data

    def _get_balance_sync(self) -> BalanceInfo:
        r = self._get({"action": "getBalance"})
        text = r.text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            balance = float(text.split(":", 1)[1])
            return BalanceInfo(balance=balance)
        try:
            data = r.json()
            self._check_error(data)
            balance = float(data.get("balance", data.get("ACCESS_BALANCE", 0)))
            return BalanceInfo(balance=balance)
        except (ValueError, KeyError):
            raise HeroSmsError(f"Неожиданный ответ баланса: {text[:200]}")

    # ── страны ──────────────────────────────────────────────

    async def get_countries(self) -> list[CountryInfo]:
        return await asyncio.to_thread(self._get_countries_sync)

    def _get_countries_sync(self) -> list[CountryInfo]:
        r = self._get({"action": "getCountries"})
        data = r.json()
        self._check_error(data)
        result: list[CountryInfo] = []
        for c in data:
            result.append(CountryInfo(
                id=int(c.get("id", 0)),
                rus=str(c.get("rus", "")),
                eng=str(c.get("eng", "")),
                visible=bool(c.get("visible", 0)),
                retry=bool(c.get("retry", 0)),
                rent=bool(c.get("rent", 0)),
                multi_service=bool(c.get("multiService", 0)),
            ))
        return result

    # ── цены ────────────────────────────────────────────────

    async def get_prices(self, country_id: int = 0, service: str = "") -> list[PriceInfo]:
        return await asyncio.to_thread(self._get_prices_sync, country_id, service)

    def _get_prices_sync(self, country_id: int, service: str) -> list[PriceInfo]:
        params: dict[str, str | int] = {"action": "getPrices"}
        if country_id:
            params["country"] = country_id
        if service:
            params["service"] = service
        r = self._get(params)
        data = r.json()
        self._check_error(data)
        result: list[PriceInfo] = []
        if isinstance(data, dict):
            for country_key, services in data.items():
                try:
                    cid = int(country_key)
                except (ValueError, TypeError):
                    continue
                if isinstance(services, dict):
                    for svc, info in services.items():
                        price = float(info.get("price", info) if isinstance(info, dict) else info)
                        count = int(info.get("count", 0)) if isinstance(info, dict) else 0
                        result.append(PriceInfo(country_id=cid, service=svc, price=price, count=count))
                elif isinstance(services, (int, float)):
                    result.append(PriceInfo(country_id=cid, service=service or "?", price=float(services)))
        return result

    # ── получить номер ──────────────────────────────────────

    async def get_number(self, service: str, country_id: int, max_price: float = 0) -> NumberInfo:
        return await asyncio.to_thread(self._get_number_sync, service, country_id, max_price)

    def _get_number_sync(self, service: str, country_id: int, max_price: float) -> NumberInfo:
        params: dict[str, str | int] = {
            "action": "getNumber",
            "service": service,
            "country": country_id,
        }
        if max_price > 0:
            params["maxPrice"] = max_price
        r = self._get(params)
        text = r.text.strip()
        # Формат: ACCESS_NUMBER:id:number
        if text.startswith("ACCESS_NUMBER:"):
            parts = text.split(":")
            return NumberInfo(
                id=int(parts[1]),
                number=parts[2],
                service=service,
                country_id=country_id,
            )
        try:
            data = r.json()
            self._check_error(data)
            # Если формат JSON
            return NumberInfo(
                id=int(data.get("id", data.get("activationId", 0))),
                number=str(data.get("number", data.get("phoneNumber", ""))),
                service=service,
                country_id=country_id,
            )
        except (ValueError, KeyError):
            raise HeroSmsError(f"Неожиданный ответ getNumber: {text[:200]}")

    # ── статус активации / получить код ─────────────────────

    async def get_status(self, activation_id: int) -> str | SmsCode:
        return await asyncio.to_thread(self._get_status_sync, activation_id)

    def _get_status_sync(self, activation_id: int) -> str | SmsCode:
        r = self._get({"action": "getStatus", "id": activation_id})
        text = r.text.strip()
        if text.startswith("STATUS_WAIT_CODE"):
            return "waiting"
        if text.startswith("STATUS_WAIT_RETRY:"):
            # Предыдущий код неверный, ожидание нового
            return "wait_retry"
        if text.startswith("STATUS_WAIT_RESEND:"):
            return "wait_resend"
        if text.startswith("STATUS_OK:"):
            code = text.split(":", 1)[1]
            full = code
            # Иногда приходит "код:полный_текст"
            if ":" in code:
                parts = code.split(":", 1)
                code = parts[0]
                full = parts[1]
            return SmsCode(code=code, full_text=full)
        try:
            data = r.json()
            self._check_error(data)
            status = str(data.get("status", data.get("getStatus", "")))
            code = str(data.get("code", data.get("smsCode", "")))
            if code:
                return SmsCode(code=code, full_text=str(data.get("text", code)))
            return status
        except ValueError:
            return text.lower()

    # ── установить статус ───────────────────────────────────

    async def set_status(self, activation_id: int, status: int) -> str:
        return await asyncio.to_thread(self._set_status_sync, activation_id, status)

    def _set_status_sync(self, activation_id: int, status: int) -> str:
        r = self._get({"action": "setStatus", "id": activation_id, "status": status})
        text = r.text.strip()
        return text

    # ── завершить активацию (код принят) ────────────────────

    async def finish(self, activation_id: int) -> str:
        return await self.set_status(activation_id, STATUS_USED)

    # ── отменить активацию ──────────────────────────────────

    async def cancel(self, activation_id: int) -> str:
        return await self.set_status(activation_id, STATUS_CANCEL)

    # ── активные активации ──────────────────────────────────

    async def get_active_activations(self) -> list[ActiveActivation]:
        return await asyncio.to_thread(self._get_active_sync)

    def _get_active_sync(self) -> list[ActiveActivation]:
        r = self._get({"action": "getActiveActivations"})
        data = r.json()
        self._check_error(data)
        result: list[ActiveActivation] = []
        items = data if isinstance(data, list) else data.get("data", data.get("activations", []))
        for a in items:
            if not isinstance(a, dict):
                continue
            result.append(ActiveActivation(
                id=int(a.get("id", a.get("activationId", 0))),
                number=str(a.get("number", a.get("phoneNumber", ""))),
                service=str(a.get("service", a.get("serviceCode", ""))),
                country_id=int(a.get("country", a.get("countryId", 0))),
                status=str(a.get("status", "")),
            ))
        return result

    # ── ждать код с поллингом ───────────────────────────────

    async def wait_for_code(
        self,
        activation_id: int,
        timeout_seconds: int = 300,
        poll_interval: float = 5.0,
    ) -> SmsCode:
        """Получить номер, затем поллить getStatus пока не придёт код."""
        elapsed = 0.0
        while elapsed < timeout_seconds:
            result = await self.get_status(activation_id)
            if isinstance(result, SmsCode):
                return result
            if result in ("cancel", "finished", "closed"):
                raise HeroSmsError(f"Активация {activation_id} завершена: {result}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        raise HeroSmsError(f"Таймаут ожидания кода для активации {activation_id} ({timeout_seconds} сек.)")

    # ── полный цикл: купить номер → дождаться кода ─────────

    async def request_code(
        self,
        service: str,
        country_id: int,
        max_price: float = 0,
        wait_timeout: int = 300,
        poll_interval: float = 5.0,
    ) -> tuple[NumberInfo, SmsCode]:
        """Купить номер и дождаться SMS-кода."""
        number = await self.get_number(service, country_id, max_price)
        # Сообщаем что готовы принять код
        await self.set_status(number.id, STATUS_READY)
        code = await self.wait_for_code(number.id, wait_timeout, poll_interval)
        # Код принят
        await self.finish(number.id)
        return number, code
