"""Управление заказами: флоу от покупки до подтверждения, защита от абуза."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

log = logging.getLogger(__name__)


class OrderState(str, Enum):
    PENDING = "pending"                     # заказ создан, ждём клиента
    WAITING_AUTH = "waiting_auth"            # просим клиента начать авторизацию
    WAITING_VERIFICATION = "waiting_verify"  # клиент дошёл до верификации, нужен номер
    NUMBER_RECEIVED = "number_received"      # номер получен, ждём SMS
    CODE_RECEIVED = "code_received"          # код получен, отправлен клиенту
    WAITING_CONFIRM = "waiting_confirm"      # ждём подтверждение заказа на FunPay
    COMPLETED = "completed"                 # заказ завершён
    CANCELLED = "cancelled"                  # заказ отменён


# Страны для верификации Claude.ai
COUNTRY_GERMANY = 43
COUNTRY_UK = 16

DEFAULT_COUNTRIES = {
    COUNTRY_GERMANY: "🇩🇪 Германия",
    COUNTRY_UK: "🇬🇧 Великобритания",
}

SERVICE_CLAUDE = "cl"


@dataclass(slots=True)
class Order:
    id: int
    user_id: int                          # Telegram user_id продавца
    country_id: int
    country_name: str
    service: str = SERVICE_CLAUDE
    state: OrderState = OrderState.PENDING
    hero_activation_id: int = 0
    hero_number: str = ""
    sms_code: str = ""
    sms_full_text: str = ""
    created_at: float = 0.0
    code_received_at: float = 0.0
    completed_at: float = 0.0
    remind_count: int = 0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def is_active(self) -> bool:
        return self.state not in (OrderState.COMPLETED, OrderState.CANCELLED)

    def __str__(self) -> str:
        return f"#{self.id} {self.country_name} [{self.state.value}]"


@dataclass(slots=True)
class AbuseConfig:
    max_concurrent_orders: int = 3
    cooldown_seconds: float = 30.0
    max_orders_per_day: int = 50
    max_hero_requests_per_minute: int = 12
    max_remind_count: int = 3
    remind_interval_seconds: float = 120.0
    confirm_timeout_seconds: float = 600.0


class AbuseProtection:
    """Защита от абуза: лимиты, кулдауны, рейт-лимиты."""

    def __init__(self, config: AbuseConfig) -> None:
        self.config = config
        self._hero_request_times: list[float] = []
        self._order_times: list[float] = []

    def check_can_create_order(self, active_count: int) -> str | None:
        """Вернёт None если можно, иначе строку с причиной отказа."""
        if active_count >= self.config.max_concurrent_orders:
            return f"Слишком много активных заказов (максимум {self.config.max_concurrent_orders})."
        now = time.time()
        if self._order_times:
            last = self._order_times[-1]
            if now - last < self.config.cooldown_seconds:
                remaining = int(self.config.cooldown_seconds - (now - last))
                return f"Подожди {remaining} сек. перед новым заказом."
        # Лимит в день
        day_ago = now - 86400
        self._order_times = [t for t in self._order_times if t > day_ago]
        if len(self._order_times) >= self.config.max_orders_per_day:
            return f"Дневной лимит заказов ({self.config.max_orders_per_day})."
        return None

    def record_order_created(self) -> None:
        self._order_times.append(time.time())

    def check_hero_rate(self) -> str | None:
        """Проверка рейт-лимита на запросы к Hero SMS API."""
        now = time.time()
        minute_ago = now - 60
        self._hero_request_times = [t for t in self._hero_request_times if t > minute_ago]
        if len(self._hero_request_times) >= self.config.max_hero_requests_per_minute:
            return f"Слишком много запросов к Hero SMS (максимум {self.config.max_hero_requests_per_minute} в минуту)."
        return None

    def record_hero_request(self) -> None:
        self._hero_request_times.append(time.time())

    def should_remind(self, order: Order) -> bool:
        """Нужно ли напомнить о подтверждении заказа."""
        if order.state != OrderState.WAITING_CONFIRM:
            return False
        if order.remind_count >= self.config.max_remind_count:
            return False
        if order.code_received_at <= 0:
            return False
        elapsed = time.time() - order.code_received_at
        interval = self.config.remind_interval_seconds * (order.remind_count + 1)
        return elapsed >= interval

    def is_confirm_timed_out(self, order: Order) -> bool:
        """Истёк ли таймаут подтверждения."""
        if order.state != OrderState.WAITING_CONFIRM:
            return False
        if order.code_received_at <= 0:
            return False
        return (time.time() - order.code_received_at) > self.config.confirm_timeout_seconds


class OrderManager:
    """Менеджер заказов с защитой от абуза и авто-уведомлениями."""

    def __init__(
        self,
        abuse_config: AbuseConfig | None = None,
        notify_callback: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        self._orders: dict[int, Order] = {}  # order_id -> Order
        self._next_id = 1
        self._user_orders: dict[int, list[int]] = {}  # user_id -> [order_ids]
        self.abuse = AbuseProtection(abuse_config or AbuseConfig())
        self._notify = notify_callback
        self._background_tasks: dict[int, asyncio.Task[None]] = {}  # order_id -> task

    @property
    def active_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.is_active]

    def active_orders_for_user(self, user_id: int) -> list[Order]:
        ids = self._user_orders.get(user_id, [])
        return [self._orders[i] for i in ids if i in self._orders and self._orders[i].is_active]

    def get_order(self, order_id: int) -> Order | None:
        return self._orders.get(order_id)

    def create_order(self, user_id: int, country_id: int, country_name: str) -> Order | str:
        """Создать заказ. Вернёт Order или строку с причиной отказа."""
        active = self.active_orders_for_user(user_id)
        err = self.abuse.check_can_create_order(len(active))
        if err:
            return err
        order = Order(
            id=self._next_id,
            user_id=user_id,
            country_id=country_id,
            country_name=country_name,
        )
        self._next_id += 1
        self._orders[order.id] = order
        self._user_orders.setdefault(user_id, []).append(order.id)
        self.abuse.record_order_created()
        log.info("Order #%d created: user=%d country=%s", order.id, user_id, country_name)
        return order

    def update_state(self, order_id: int, state: OrderState) -> None:
        order = self._orders.get(order_id)
        if not order:
            return
        order.state = state
        if state == OrderState.COMPLETED:
            order.completed_at = time.time()
            self._cancel_background_task(order_id)
        elif state == OrderState.CANCELLED:
            order.completed_at = time.time()
            self._cancel_background_task(order_id)
        log.info("Order #%d state -> %s", order_id, state.value)

    def set_hero_number(self, order_id: int, activation_id: int, number: str) -> None:
        order = self._orders.get(order_id)
        if not order:
            return
        order.hero_activation_id = activation_id
        order.hero_number = number
        order.state = OrderState.NUMBER_RECEIVED

    def set_sms_code(self, order_id: int, code: str, full_text: str) -> None:
        order = self._orders.get(order_id)
        if not order:
            return
        order.sms_code = code
        order.sms_full_text = full_text
        order.code_received_at = time.time()
        order.state = OrderState.CODE_RECEIVED

    def increment_remind(self, order_id: int) -> None:
        order = self._orders.get(order_id)
        if order:
            order.remind_count += 1

    # ── фоновые задачи ──────────────────────────────────────

    def _cancel_background_task(self, order_id: int) -> None:
        task = self._background_tasks.pop(order_id, None)
        if task and not task.done():
            task.cancel()

    def start_code_wait(
        self,
        order_id: int,
        hero_sms,
        poll_interval: float = 5.0,
        timeout: int = 300,
    ) -> asyncio.Task[None]:
        """Запустить фоновое ожидание SMS-кода."""
        self._cancel_background_task(order_id)

        async def _wait() -> None:
            order = self._orders.get(order_id)
            if not order or not order.hero_activation_id:
                return
            try:
                code = await hero_sms.wait_for_code(
                    order.hero_activation_id,
                    timeout_seconds=timeout,
                    poll_interval=poll_interval,
                )
                self.set_sms_code(order_id, code.code, code.full_text)
                # Завершаем активацию на Hero SMS
                try:
                    await hero_sms.finish(order.hero_activation_id)
                except Exception:
                    pass
                # Уведомляем продавца
                if self._notify:
                    await self._notify(
                        order.user_id,
                        f"Код для заказа #{order.id}: {code.code}\n"
                        f"Номер: +{order.hero_number}\n"
                        f"Страна: {order.country_name}\n\n"
                        f"Отправь код клиенту в чат FunPay и подтверди заказ.",
                    )
                # Переводим в статус ожидания подтверждения
                self.update_state(order_id, OrderState.WAITING_CONFIRM)
            except Exception as exc:
                log.error("Code wait failed for order #%d: %s", order_id, exc)
                if self._notify:
                    order = self._orders.get(order_id)
                    if order:
                        await self._notify(
                            order.user_id,
                            f"Ошибка ожидания кода для заказа #{order_id}: {exc}\n"
                            f"Проверь статус вручную через /hero_code",
                        )

        task = asyncio.create_task(_wait(), name=f"order-wait-{order_id}")
        self._background_tasks[order_id] = task
        return task

    def start_reminder_loop(
        self,
        order_id: int,
        bot_send: Callable[[int, str, Any], Awaitable[None]],
        keyboard_factory: Callable[[int], Any],
    ) -> asyncio.Task[None]:
        """Запустить цикл напоминаний о подтверждении заказа."""
        self._cancel_background_task(order_id)

        async def _remind() -> None:
            while True:
                await asyncio.sleep(30)
                order = self._orders.get(order_id)
                if not order or not order.is_active:
                    return
                # Проверяем таймаут
                if self.abuse.is_confirm_timed_out(order):
                    self.update_state(order_id, OrderState.COMPLETED)
                    await bot_send(
                        order.user_id,
                        f"Заказ #{order.id} автоматически завершён (таймаут подтверждения).",
                        keyboard_factory(order_id),
                    )
                    return
                # Проверяем нужно ли напомнить
                if self.abuse.should_remind(order):
                    self.increment_remind(order_id)
                    await bot_send(
                        order.user_id,
                        f"Напоминание: подтверди заказ #{order.id} на FunPay!\n"
                        f"Код: {order.sms_code} | Номер: +{order.hero_number}",
                        keyboard_factory(order_id),
                    )

        task = asyncio.create_task(_remind(), name=f"order-remind-{order_id}")
        self._background_tasks[order_id] = task
        return task

    # ── очистка ─────────────────────────────────────────────

    def cleanup_old_orders(self, max_age_hours: int = 24) -> int:
        """Удалить старые завершённые/отменённые заказы."""
        now = time.time()
        cutoff = now - max_age_hours * 3600
        to_remove = [
            oid for oid, o in self._orders.items()
            if not o.is_active and o.completed_at > 0 and o.completed_at < cutoff
        ]
        for oid in to_remove:
            self._cancel_background_task(oid)
            order = self._orders.pop(oid, None)
            if order:
                user_orders = self._user_orders.get(order.user_id, [])
                if oid in user_orders:
                    user_orders.remove(oid)
        return len(to_remove)
