"""Автоматическая верификация: FunPay заказ → Hero SMS номер → код → чат FunPay."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import AppConfig
from .funpay_client import FunPayClient
from .hero_sms import HeroSmsClient, HeroSmsError
from .orders import OrderManager, OrderState

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AutoVerifyConfig:
    poll_interval: float = 15.0       # интервал проверки заказов FunPay (сек)
    enabled: bool = True


class AutoVerifier:
    """Полностью автоматический обработчик верификации.

    Флоу:
    1. Поллит FunPay на новые оплаченные заказы
    2. Определяет страну по товару (hero_sms_country_id в тарифе)
    3. Покупает номер на Hero SMS
    4. Ждёт SMS-код
    5. Отправляет код в чат FunPay
    6. Подтверждает заказ на FunPay
    7. Уведомляет продавца через Telegram
    """

    def __init__(
        self,
        config: AppConfig,
        funpay: FunPayClient,
        hero_sms: HeroSmsClient,
        order_mgr: OrderManager,
        tg_notify: _TgNotifyCallback | None = None,
    ) -> None:
        self.config = config
        self.funpay = funpay
        self.hero_sms = hero_sms
        self.order_mgr = order_mgr
        self.tg_notify = tg_notify
        self.av_config = AutoVerifyConfig()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._processed_funpay_ids: set[str] = set()
        self.stats = {"processed": 0, "failed": 0, "codes_sent": 0}

    # ── тип колбэка ────────────────────────────────────────

    async def _notify(self, user_id: int, text: str) -> None:
        if self.tg_notify:
            await self.tg_notify(user_id, text)

    # ── запуск/остановка ───────────────────────────────────

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop(), name="auto-verify")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    def set_enabled(self, enabled: bool) -> None:
        self.av_config.enabled = enabled

    @property
    def is_enabled(self) -> bool:
        return self.av_config.enabled

    def status_text(self) -> str:
        active = len(self.order_mgr.active_orders)
        return (
            f"Авто-верификация: {'включена' if self.av_config.enabled else 'выключена'}\n"
            f"Активных заказов: {active}\n"
            f"Обработано: {self.stats['processed']}\n"
            f"Кодов отправлено: {self.stats['codes_sent']}\n"
            f"Ошибок: {self.stats['failed']}\n"
            f"Интервал проверки: {self.av_config.poll_interval} сек."
        )

    # ── главный цикл ───────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.av_config.enabled:
                await self._sleep(10)
                continue

            try:
                await self._poll_and_process()
            except Exception:
                log.exception("Auto-verify poll failed")

            await self._sleep(self.av_config.poll_interval)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(1, seconds))
        except TimeoutError:
            pass

    # ── поллинг и обработка ────────────────────────────────

    async def _poll_and_process(self) -> None:
        # 1. Проверяем новые заказы на FunPay
        try:
            new_orders = await self.funpay.get_new_orders()
        except Exception as exc:
            log.warning("FunPay order poll failed: %s", exc)
            return

        if not new_orders:
            return

        # 2. Обрабатываем каждый новый заказ
        for fp_order in new_orders:
            if fp_order.order_id in self._processed_funpay_ids:
                continue
            self._processed_funpay_ids.add(fp_order.order_id)
            await self._process_funpay_order(fp_order)

    async def _process_funpay_order(self, fp_order) -> None:
        """Обработать один FunPay заказ автоматически."""
        log.info("New FunPay order: %s", fp_order)

        # Определяем страну по товару
        country_id, country_name, tier = self._match_order_to_tier(fp_order)
        if not country_id:
            log.warning("Cannot match order %s to a tier with country", fp_order.order_id)
            await self._notify_admin(f"Заказ {fp_order}: не удалось определить страну. Обработай вручную.")
            return

        # Создаём заказ в менеджере
        result = self.order_mgr.create_order(0, country_id, country_name)
        if isinstance(result, str):
            log.warning("Cannot create order: %s", result)
            await self._notify_admin(f"Заказ {fp_order}: отказ — {result}")
            return

        order = result
        order_mgr.update_state(order.id, OrderState.WAITING_VERIFICATION)

        # Уведомляем продавца
        await self._notify_admin(
            f"Новый заказ #{order.id} ({country_name})\n"
            f"FunPay: {fp_order}\n"
            f"Начинаю автоматическую верификацию..."
        )

        # Покупаем номер
        try:
            number = await self.hero_sms.get_number(
                self.config.hero_sms.default_service,
                country_id,
                self.config.hero_sms.max_price,
            )
        except HeroSmsError as exc:
            log.error("Hero SMS get_number failed: %s", exc)
            order_mgr.update_state(order.id, OrderState.CANCELLED)
            self.stats["failed"] += 1
            await self._notify_admin(f"Заказ #{order.id}: ошибка получения номера — {exc}")
            return

        order_mgr.set_hero_number(order.id, number.id, number.number)

        # Готовы принять код
        try:
            await self.hero_sms.set_status(number.id, 1)
        except HeroSmsError:
            pass

        # Отправляем номер в чат FunPay
        country_flag = "🇩🇪" if country_id == 43 else "🇬🇧" if country_id == 16 else ""
        chat_msg = (
            f"Здравствуйте! Пошаговая инструкция по верификации Claude.ai:\n\n"
            f"1. Откройте Claude.ai в браузере и войдите в свой аккаунт\n"
            f"2. Перейдите на страницу верификации номера телефона\n"
            f"3. Введите номер: +{number.number} ({country_flag} {country_name})\n"
            f"4. Нажмите «Отправить код»\n"
            f"5. SMS-код придёт в этот чат автоматически в течение нескольких минут\n\n"
            f"Не закрывайте страницу верификации, пока не получите код."
        )
        await self.funpay.send_chat_message(fp_order.order_id, chat_msg)

        # Ждём код
        await self._notify_admin(
            f"Заказ #{order.id}: номер +{number.number} ({country_name}) отправлен в чат FunPay. Жду код..."
        )

        try:
            code = await self.hero_sms.wait_for_code(
                number.id,
                timeout_seconds=self.config.hero_sms.wait_timeout,
                poll_interval=self.config.hero_sms.poll_interval,
            )
        except HeroSmsError as exc:
            log.error("Code wait failed for order #%d: %s", order.id, exc)
            self.stats["failed"] += 1
            await self._notify_admin(f"Заказ #{order.id}: код не пришёл — {exc}")
            # Отменяем номер на Hero SMS
            try:
                await self.hero_sms.cancel(number.id)
            except HeroSmsError:
                pass
            order_mgr.update_state(order.id, OrderState.CANCELLED)
            return

        # Код получен!
        order_mgr.set_sms_code(order.id, code.code, code.full_text)
        try:
            await self.hero_sms.finish(number.id)
        except HeroSmsError:
            pass

        # Отправляем код в чат FunPay
        code_msg = (
            f"Ваш SMS-код подтверждения: {code.code}\n\n"
            f"6. Введите код {code.code} на странице верификации Claude.ai\n"
            f"7. Дождитесь подтверждения на экране\n"
            f"8. Подтвердите получение заказа на FunPay\n\n"
            f"Если код не подошёл — напишите в этот чат, я отправлю повторный код."
        )
        sent = await self.funpay.send_chat_message(fp_order.order_id, code_msg)

        # Подтверждаем заказ на FunPay
        confirmed = await self.funpay.confirm_order(fp_order.order_id)

        order_mgr.update_state(order.id, OrderState.COMPLETED)
        self.stats["processed"] += 1
        self.stats["codes_sent"] += 1

        await self._notify_admin(
            f"Заказ #{order.id} ВЫПОЛНЕН\n"
            f"Страна: {country_name}\n"
            f"Номер: +{number.number}\n"
            f"Код: {code.code}\n"
            f"FunPay чат: {'отправлен' if sent else 'ошибка'}\n"
            f"FunPay подтверждение: {'да' if confirmed else 'нет'}"
        )

    def _match_order_to_tier(self, fp_order) -> tuple[int, str, Any]:
        """Определить страну по FunPay заказу. Возвращает (country_id, country_name, tier)."""
        order_title = fp_order.title.lower() if fp_order.title else ""
        order_price = fp_order.price

        # Сначала по цене + названию
        for tier in self.config.service.tiers:
            if not tier.hero_sms_country_id:
                continue
            # Совпадение цены
            if tier.price == int(order_price):
                country_name = self.config.orders.countries.get(tier.hero_sms_country_id, "")
                return tier.hero_sms_country_id, country_name, tier

        # По ключевым словам в названии
        de_keywords = ("germany", "герман", "de ", "deutsch", "🇩🇪")
        uk_keywords = ("uk", "united kingdom", "великобритан", "britain", "🇬🇧", "england", "англ")

        for keyword in de_keywords:
            if keyword in order_title:
                return 43, "🇩🇪 Германия", None
        for keyword in uk_keywords:
            if keyword in order_title:
                return 16, "🇬🇧 Великобритания", None

        # Фолбэк — дефолтная страна
        default_country = self.config.hero_sms.default_country
        default_name = self.config.orders.countries.get(default_country, f"Страна {default_country}")
        return default_country, default_name, None

    async def _notify_admin(self, text: str) -> None:
        """Уведомить продавца через Telegram."""
        for admin_id in self.config.telegram.admin_ids:
            await self._notify(admin_id, text)


# Тип колбэка для уведомлений через Telegram
_TgNotifyCallback = Callable[[int, str], Awaitable[None]]
