from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .auto_verify import AutoVerifier
from .catalog import render_all_funpay_texts, render_catalog, write_listing_preview
from .config import AppConfig
from .funpay_client import BumpResult, CreateOfferResult, FunPayClient, PublishPlanItem
from .hero_sms import HeroSmsClient, HeroSmsError, NumberInfo, SmsCode
from .orders import AbuseConfig, Order, OrderManager, OrderState
from .scheduler import AutoBumpScheduler


STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime_state.json"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
PANEL_VERSION = "2026-05-07.2"


async def run_telegram_bot(
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
    hero_sms: HeroSmsClient | None = None,
    order_manager: OrderManager | None = None,
    auto_verifier: AutoVerifier | None = None,
) -> None:
    if not config.telegram.token:
        raise RuntimeError("Заполни telegram.token в config.json или TELEGRAM_BOT_TOKEN в .env.")

    if hero_sms is None and config.hero_sms.api_key:
        hero_sms = HeroSmsClient(
            api_key=config.hero_sms.api_key,
            timeout=30,
            proxy=config.hero_sms.proxy,
        )

    if order_manager is None:
        order_manager = OrderManager(
            abuse_config=AbuseConfig(
                max_concurrent_orders=config.orders.max_concurrent,
                cooldown_seconds=config.orders.cooldown_seconds,
                max_orders_per_day=config.orders.max_per_day,
                max_hero_requests_per_minute=config.orders.hero_rate_per_minute,
                max_remind_count=config.orders.max_remind_count,
                remind_interval_seconds=config.orders.remind_interval_seconds,
                confirm_timeout_seconds=config.orders.confirm_timeout_seconds,
            ),
        )

    bot = Bot(token=config.telegram.token)
    dispatcher = Dispatcher()
    dispatcher.include_router(_build_router(config, client, scheduler, hero_sms, order_manager, auto_verifier))
    await dispatcher.start_polling(bot)


def _build_router(
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
    hero_sms: HeroSmsClient | None,
    order_mgr: OrderManager,
    auto_verifier: AutoVerifier | None = None,
) -> Router:
    router = Router()
    authorized = _load_authorized_users(config)
    pending_inputs: dict[int, str] = {}
    # Текущий активный заказ пользователя: user_id -> Order
    user_active_order: dict[int, Order] = {}
    # Мастер настройки: user_id -> текущий шаг
    setup_step: dict[int, int] = {}

    # Шаги мастера настройки
    SETUP_STEPS = [
        {
            "key": "telegram_password",
            "title": "Шаг 1/6: Пароль бота",
            "prompt": (
                "Придумай пароль для входа в панель бота.\n"
                "Минимум 4 символа. Этот пароль будешь вводить при /start.\n\n"
                "Отправь пароль:"
            ),
        },
        {
            "key": "funpay_golden_key",
            "title": "Шаг 2/6: FunPay golden_key",
            "prompt": (
                "Нужен cookie golden_key от авторизованного FunPay.\n"
                "Как получить:\n"
                "1. Открой funpay.com в браузере и войди в аккаунт\n"
                "2. Нажми F12 → вкладка Application → Cookies\n"
                "3. Скопируй значение cookie с именем golden_key\n\n"
                "Отправь golden_key (или '-' чтобы пропустить):"
            ),
        },
        {
            "key": "funpay_category_ids",
            "title": "Шаг 3/6: ID категории FunPay",
            "prompt": (
                "ID категории FunPay, в которой твои предложения.\n"
                "Сейчас стоит: 3172 (Claude.ai)\n"
                "Если подходит — отправь 3172\n"
                "Если нужна другая — отправь ID (можно несколько через запятую)\n\n"
                "Отправь ID категории:"
            ),
        },
        {
            "key": "hero_sms_api_key",
            "title": "Шаг 4/6: Hero SMS API-ключ",
            "prompt": (
                "API-ключ от hero-sms.com для покупки виртуальных номеров.\n"
                "Как получить:\n"
                "1. Зарегистрируйся на hero-sms.com\n"
                "2. Пополнить баланс\n"
                "3. API-ключ в личном кабинете\n\n"
                "Отправь API-ключ (или '-' чтобы пропустить):"
            ),
        },
        {
            "key": "hero_sms_country",
            "title": "Шаг 5/6: Страна по умолчанию",
            "prompt": (
                "ID страны для виртуальных номеров:\n"
                "• 43 — Германия 🇩🇪\n"
                "• 16 — Великобритания 🇬🇧\n"
                "• 0 — Россия 🇷🇺\n\n"
                "Отправь ID страны (по умолчанию 43):"
            ),
        },
        {
            "key": "auto_bump_interval",
            "title": "Шаг 6/6: Интервал автоподнятия",
            "prompt": (
                "Как часто бот будет поднимать предложения на FunPay.\n"
                "Минимум 600 сек (10 мин). Рекомендую 3900 (65 мин).\n\n"
                "Отправь интервал в секундах (или '-' чтобы оставить 3900):"
            ),
        },
    ]

    async def require_auth(message: Message) -> bool:
        if _is_allowed(message.from_user.id if message.from_user else 0, authorized, config):
            return True
        await message.answer("Сначала введи пароль от бота.")
        return False

    async def require_auth_callback(callback: CallbackQuery) -> bool:
        user_id = callback.from_user.id if callback.from_user else 0
        if _is_allowed(user_id, authorized, config):
            return True
        await callback.answer("Нет доступа. Введи пароль через /start.", show_alert=True)
        return False

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if _is_allowed(user_id, authorized, config):
            await message.answer(
                _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr),
                reply_markup=_main_keyboard(),
            )
            return
        # Если конфиг пустой — предлагаем мастер настройки
        needs_setup = (
            not config.telegram.password or config.telegram.password == "change_me"
        )
        if needs_setup:
            await message.answer(
                "Привет! FunPayBot ещё не настроен.\n\n"
                "Нажми кнопку ниже, и я пошагово проведу тебя по всем настройкам прямо тут в чате.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Начать настройку", callback_data="setup_start")],
                ]),
            )
            return
        await message.answer("Привет. Отправь пароль для входа в FunPayBot.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not await require_auth(message):
            return
        text = _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr)
        await message.answer(text, reply_markup=_main_keyboard())

    @router.message(Command("catalog"))
    async def catalog(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(render_catalog(config))

    @router.message(Command("preview"))
    async def preview(message: Message) -> None:
        if not await require_auth(message):
            return
        await _send_long_text(message, render_all_funpay_texts(config))

    @router.message(Command("publish_plan"))
    async def publish_plan(message: Message) -> None:
        if not await require_auth(message):
            return
        await _send_long_text(message, _format_publish_plan(client.build_publish_plan(config)))

    @router.message(Command("bump_now"))
    async def bump_now(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(
            "Подтвердить ручное поднятие категорий FunPay?",
            reply_markup=_confirm_keyboard("confirm_bump"),
        )

    @router.message(Command("autobump_on"))
    async def autobump_on(message: Message) -> None:
        if not await require_auth(message):
            return
        scheduler.set_enabled(True)
        await message.answer("Авто-поднятие включено в текущем запуске.", reply_markup=_main_keyboard())

    @router.message(Command("autobump_off"))
    async def autobump_off(message: Message) -> None:
        if not await require_auth(message):
            return
        scheduler.set_enabled(False)
        await message.answer("Авто-поднятие выключено в текущем запуске.", reply_markup=_main_keyboard())

    @router.message(Command("verify_on"))
    async def verify_on(message: Message) -> None:
        if not await require_auth(message):
            return
        if not auto_verifier:
            await message.answer("Авто-верификация не настроена. Проверь hero_sms.api_key и funpay.golden_key.")
            return
        auto_verifier.set_enabled(True)
        await message.answer("Авто-верификация включена. Заказы будут обрабатываться автоматически.", reply_markup=_main_keyboard())

    @router.message(Command("verify_off"))
    async def verify_off(message: Message) -> None:
        if not await require_auth(message):
            return
        if not auto_verifier:
            await message.answer("Авто-верификация не настроена.")
            return
        auto_verifier.set_enabled(False)
        await message.answer("Авто-верификация выключена. Заказы нужно обрабатывать вручную.", reply_markup=_main_keyboard())

    @router.message(Command("verify_status"))
    async def verify_status(message: Message) -> None:
        if not await require_auth(message):
            return
        if not auto_verifier:
            await message.answer("Авто-верификация не настроена. Нужен hero_sms.api_key и funpay.golden_key.")
            return
        text = auto_verifier.status_text()
        active = order_mgr.active_orders
        if active:
            text += "\n\nДетали активных заказов:"
            for o in active:
                text += f"\n  {o} | Код: {o.sms_code or '—'} | Номер: +{o.hero_number or '—'}"
        await message.answer(text, reply_markup=_main_keyboard())

    @router.message(Command("settings"))
    async def settings(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(_settings_text(config, client, hero_sms, auto_verifier), reply_markup=_settings_keyboard())

    # ── Флоу заказа ───────────────────────────────────────

    @router.message(Command("new_order"))
    async def new_order(message: Message) -> None:
        if not await require_auth(message):
            return
        if not hero_sms:
            await message.answer("Hero SMS не настроен: укажи hero_sms.api_key в настройках.")
            return
        user_id = message.from_user.id if message.from_user else 0
        active = order_mgr.active_orders_for_user(user_id)
        if active:
            await message.answer(
                f"У тебя уже есть активный заказ: {active[0]}\nЗаверши или отмени его сначала.",
                reply_markup=_order_keyboard(order_mgr, active[0].id),
            )
            return
        await message.answer(
            "Клиент купил товар. Выбери страну для верификации:",
            reply_markup=_country_keyboard(config),
        )

    @router.message(Command("orders"))
    async def list_orders(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(_orders_list_text(order_mgr), reply_markup=_orders_keyboard())

    @router.message(Command("hero_balance"))
    async def hero_balance(message: Message) -> None:
        if not await require_auth(message):
            return
        if not hero_sms:
            await message.answer("Hero SMS не настроен.")
            return
        try:
            balance = await hero_sms.get_balance()
        except HeroSmsError as exc:
            await message.answer(f"Ошибка: {exc}")
            return
        await message.answer(f"Баланс Hero SMS: {balance}", reply_markup=_hero_keyboard())

    # ── Мастер настройки ────────────────────────────────────

    @router.message(Command("setup"))
    async def setup_cmd(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        setup_step[user_id] = 0
        step = SETUP_STEPS[0]
        await message.answer(
            f"🔧 Мастер настройки FunPayBot\n\n{step['title']}\n\n{step['prompt']}",
            reply_markup=_setup_keyboard(),
        )

    @router.callback_query(F.data == "setup_start")
    async def setup_start_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        setup_step[user_id] = 0
        # Автоматически авторизуем того, кто запускает настройку
        authorized.add(user_id)
        _save_authorized_users(authorized)
        step = SETUP_STEPS[0]
        await callback.message.answer(
            f"🔧 Мастер настройки FunPayBot\n\n{step['title']}\n\n{step['prompt']}",
            reply_markup=_setup_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "setup_skip")
    async def setup_skip_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        step_idx = setup_step.get(user_id, -1)
        if step_idx < 0 or step_idx >= len(SETUP_STEPS):
            await callback.message.answer("Настройка завершена.", reply_markup=_main_keyboard())
            await callback.answer()
            return
        step = SETUP_STEPS[step_idx]
        await callback.message.answer(f"Пропущено: {step['title'].split(': ', 1)[-1]}")
        # Переходим к следующему шагу
        await _advance_setup(callback.message, user_id, step_idx + 1)
        await callback.answer()

    @router.callback_query(F.data == "setup_cancel")
    async def setup_cancel_callback(callback: CallbackQuery) -> None:
        user_id = callback.from_user.id if callback.from_user else 0
        setup_step.pop(user_id, None)
        await callback.message.answer("Настройка отменена.", reply_markup=_main_keyboard())
        await callback.answer()

    async def _advance_setup(message: Message, user_id: int, next_step: int) -> None:
        if next_step >= len(SETUP_STEPS):
            setup_step.pop(user_id, None)
            await message.answer(
                "✅ Настройка завершена!\n\n"
                "Все данные сохранены в config.json. Бот готов к работе.\n\n"
                "Что можно сделать дальше:\n"
                "• /status — проверить состояние\n"
                "• «Опубликовать все» — создать предложения на FunPay\n"
                "• «Проверить FunPay» — проверить golden_key\n"
                "• /settings — изменить настройки позже",
                reply_markup=_main_keyboard(),
            )
            return
        setup_step[user_id] = next_step
        step = SETUP_STEPS[next_step]
        await message.answer(
            f"{step['title']}\n\n{step['prompt']}",
            reply_markup=_setup_keyboard(),
        )

    def _apply_setup_step(setting_key: str, text: str) -> str:
        """Применить значение шага настройки. Возвращает сообщение о результате."""
        value = "" if text == "-" else text

        if setting_key == "telegram_password":
            if value and len(value) < 4:
                raise ValueError("Пароль должен быть хотя бы 4 символа.")
            if not value:
                return "Пароль пропущен (оставлен текущий)."
            _save_config_value(("telegram", "password"), value)
            config.telegram.password = value
            return "Пароль сохранен."

        if setting_key == "funpay_golden_key":
            if not value:
                return "golden_key пропущен. Укажи позже через /settings."
            _save_config_value(("funpay", "golden_key"), value)
            config.funpay.golden_key = value
            client.refresh_session()
            return "FunPay golden_key сохранен."

        if setting_key == "funpay_category_ids":
            if not value:
                return "Категория пропущена (оставлена 3172)."
            values = _parse_list(text)
            _save_config_value(("funpay", "category_ids"), values)
            config.funpay.category_ids = values
            return f"ID категорий сохранены: {', '.join(values)}"

        if setting_key == "hero_sms_api_key":
            if not value:
                return "Hero SMS API-ключ пропущен. Укажи позже через /settings."
            _save_config_value(("hero_sms", "api_key"), value)
            config.hero_sms.api_key = value
            return "Hero SMS API-ключ сохранен. Перезапусти бота для применения."

        if setting_key == "hero_sms_country":
            if not value:
                value = "43"
            country_id = int(value)
            _save_config_value(("hero_sms", "default_country"), country_id)
            config.hero_sms.default_country = country_id
            return f"Страна сохранена: {country_id}"

        if setting_key == "auto_bump_interval":
            if not value:
                return "Интервал оставлен по умолчанию (3900 сек)."
            seconds = int(value)
            if seconds < 600:
                raise ValueError("Интервал должен быть минимум 600 секунд.")
            _save_config_value(("auto_bump", "interval_seconds"), seconds)
            config.auto_bump.interval_seconds = seconds
            scheduler.next_run_at = None
            return f"Интервал автоподнятия сохранен: {seconds} сек."

        return "Сохранено."

    @router.message(Command("publish_all"))
    async def publish_all_cmd(message: Message) -> None:
        if not await require_auth(message):
            return
        if not client.can_write:
            await message.answer("Нужен funpay.golden_key для публикации предложений.")
            return
        tier_lines = [f"{i+1}. {t.title} — {t.price} ₽" for i, t in enumerate(config.service.tiers)]
        await message.answer(
            "Создать все предложения на FunPay автоматически?\n\n"
            + "\n".join(tier_lines)
            + f"\n\nКатегория: {', '.join(config.funpay.category_ids) or 'не задана'}\n"
            f"Всего: {len(config.service.tiers)} предложений",
            reply_markup=_confirm_keyboard("confirm_publish"),
        )

    # ── Выбор страны ───────────────────────────────────────

    @router.callback_query(F.data == "new_order")
    async def new_order_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not hero_sms:
            await callback.message.answer("Hero SMS не настроен.")
            await callback.answer()
            return
        user_id = callback.from_user.id if callback.from_user else 0
        active = order_mgr.active_orders_for_user(user_id)
        if active:
            await callback.message.answer(
                f"Уже есть активный заказ: {active[0]}",
                reply_markup=_order_keyboard(order_mgr, active[0].id),
            )
            await callback.answer()
            return
        await callback.message.answer("Выбери страну для верификации:", reply_markup=_country_keyboard(config))
        await callback.answer()

    @router.callback_query(F.data.startswith("order_country:"))
    async def select_country_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        user_id = callback.from_user.id if callback.from_user else 0
        country_id = int(callback.data.split(":", 1)[1])
        country_name = config.orders.countries.get(country_id, f"Страна {country_id}")

        result = order_mgr.create_order(user_id, country_id, country_name)
        if isinstance(result, str):
            await callback.message.answer(f"Нельзя создать заказ: {result}")
            await callback.answer()
            return

        order = result
        user_active_order[user_id] = order
        order_mgr.update_state(order.id, OrderState.WAITING_AUTH)

        await callback.message.answer(
            f"Заказ #{order.id} создан ({country_name}).\n\n"
            "Шаг 1: Попроси клиента открыть Claude.ai и начать авторизацию.\n"
            "Шаг 2: Когда клиент дойдёт до этапа верификации номера — нажми кнопку ниже.",
            reply_markup=_order_keyboard(order_mgr, order.id),
        )
        await callback.answer()

    # ── Клиент дошёл до верификации → получить номер ──────

    @router.callback_query(F.data.startswith("order_get_number:"))
    async def order_get_number_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not hero_sms:
            await callback.message.answer("Hero SMS не настроен.")
            await callback.answer()
            return

        order_id = int(callback.data.split(":", 1)[1])
        order = order_mgr.get_order(order_id)
        if not order or not order.is_active:
            await callback.message.answer("Заказ не найден или уже завершён.")
            await callback.answer()
            return

        # Защита от абуза
        rate_err = order_mgr.abuse.check_hero_rate()
        if rate_err:
            await callback.message.answer(rate_err)
            await callback.answer()
            return
        order_mgr.abuse.record_hero_request()

        await callback.message.answer("Получаю номер...")
        try:
            number = await hero_sms.get_number(
                config.hero_sms.default_service,
                order.country_id,
                config.hero_sms.max_price,
            )
        except HeroSmsError as exc:
            await callback.message.answer(f"Ошибка получения номера: {exc}")
            await callback.answer()
            return

        order_mgr.set_hero_number(order.id, number.id, number.number)
        # Сообщаем Hero SMS что готовы принять код
        try:
            await hero_sms.set_status(number.id, 1)  # STATUS_READY
        except HeroSmsError:
            pass

        # Запускаем фоновое ожидание кода
        order_mgr.start_code_wait(
            order.id,
            hero_sms,
            poll_interval=config.hero_sms.poll_interval,
            timeout=config.hero_sms.wait_timeout,
        )

        await callback.message.answer(
            f"Номер получен: +{number.number}\n"
            f"Страна: {order.country_name}\n\n"
            f"Отправь этот номер клиенту в чат FunPay.\n"
            f"Код придёт автоматически — бот сразу его пришлёт.",
            reply_markup=_order_keyboard(order_mgr, order.id),
        )
        await callback.answer()

    # ── Проверить код вручную ─────────────────────────────

    @router.callback_query(F.data.startswith("order_check_code:"))
    async def order_check_code_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not hero_sms:
            await callback.message.answer("Hero SMS не настроен.")
            await callback.answer()
            return

        order_id = int(callback.data.split(":", 1)[1])
        order = order_mgr.get_order(order_id)
        if not order or not order.is_active:
            await callback.message.answer("Заказ не найден.")
            await callback.answer()
            return

        if order.state == OrderState.CODE_RECEIVED or order.sms_code:
            await callback.message.answer(
                f"Код уже получен: {order.sms_code}\n"
                f"Номер: +{order.hero_number}\n\n"
                f"Отправь код клиенту и подтверди заказ на FunPay.",
                reply_markup=_order_keyboard(order_mgr, order.id),
            )
            await callback.answer()
            return

        if not order.hero_activation_id:
            await callback.message.answer("Номер ещё не получен. Сначала нажми «Получить номер».")
            await callback.answer()
            return

        try:
            result = await hero_sms.get_status(order.hero_activation_id)
        except HeroSmsError as exc:
            await callback.message.answer(f"Ошибка: {exc}")
            await callback.answer()
            return

        if isinstance(result, SmsCode):
            order_mgr.set_sms_code(order.id, result.code, result.full_text)
            try:
                await hero_sms.finish(order.hero_activation_id)
            except HeroSmsError:
                pass
            order_mgr.update_state(order.id, OrderState.WAITING_CONFIRM)
            await callback.message.answer(
                f"Код: {result.code}\nПолный текст: {result.full_text}\nНомер: +{order.hero_number}\n\n"
                f"Отправь код клиенту и подтверди заказ на FunPay!",
                reply_markup=_order_keyboard(order_mgr, order.id),
            )
        else:
            status_map = {"waiting": "Ожидание SMS...", "wait_retry": "Ожидание повторного кода..."}
            await callback.message.answer(
                f"{status_map.get(str(result), f'Статус: {result}')}\nНомер: +{order.hero_number}",
                reply_markup=_order_keyboard(order_mgr, order.id),
            )
        await callback.answer()

    # ── Подтвердить заказ ──────────────────────────────────

    @router.callback_query(F.data.startswith("order_confirm:"))
    async def order_confirm_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        order_id = int(callback.data.split(":", 1)[1])
        order = order_mgr.get_order(order_id)
        if not order:
            await callback.message.answer("Заказ не найден.")
            await callback.answer()
            return
        order_mgr.update_state(order.id, OrderState.COMPLETED)
        user_id = callback.from_user.id if callback.from_user else 0
        user_active_order.pop(user_id, None)
        await callback.message.answer(
            f"Заказ #{order.id} подтверждён и завершён.\n"
            f"Код: {order.sms_code} | Номер: +{order.hero_number}",
            reply_markup=_main_keyboard(),
        )
        await callback.answer("Готово")

    # ── Отменить заказ ─────────────────────────────────────

    @router.callback_query(F.data.startswith("order_cancel:"))
    async def order_cancel_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        order_id = int(callback.data.split(":", 1)[1])
        order = order_mgr.get_order(order_id)
        if not order:
            await callback.message.answer("Заказ не найден.")
            await callback.answer()
            return
        # Отменяем активацию на Hero SMS если номер был получен
        if order.hero_activation_id and hero_sms:
            try:
                await hero_sms.cancel(order.hero_activation_id)
            except HeroSmsError:
                pass
        order_mgr.update_state(order.id, OrderState.CANCELLED)
        user_id = callback.from_user.id if callback.from_user else 0
        user_active_order.pop(user_id, None)
        await callback.message.answer(f"Заказ #{order.id} отменён.", reply_markup=_main_keyboard())
        await callback.answer()

    # ── Баланс Hero SMS ────────────────────────────────────

    @router.callback_query(F.data == "hero_balance")
    async def hero_balance_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not hero_sms:
            await callback.message.answer("Hero SMS не настроен.")
            await callback.answer()
            return
        try:
            balance = await hero_sms.get_balance()
        except HeroSmsError as exc:
            await callback.message.answer(f"Ошибка: {exc}")
            await callback.answer()
            return
        await callback.message.answer(f"Баланс Hero SMS: {balance}", reply_markup=_hero_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_main")
    async def menu_main_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        user_id = callback.from_user.id if callback.from_user else 0
        pending_inputs.pop(user_id, None)
        await callback.message.answer(
            _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr),
            reply_markup=_main_keyboard(),
        )
        await callback.answer()

    @router.callback_query(F.data == "menu_funpay")
    async def menu_funpay_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_funpay_menu_text(config, client, scheduler), reply_markup=_funpay_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_publish")
    async def menu_publish_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_publish_menu_text(config), reply_markup=_publish_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_orders")
    async def menu_orders_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_orders_menu_text(order_mgr, auto_verifier), reply_markup=_orders_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "orders_list")
    async def orders_list_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_orders_list_text(order_mgr), reply_markup=_orders_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_hero")
    async def menu_hero_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_hero_menu_text(config, hero_sms), reply_markup=_hero_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "hero_menu")
    async def legacy_hero_menu_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_hero_menu_text(config, hero_sms), reply_markup=_hero_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "menu_auto")
    async def menu_auto_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_auto_menu_text(scheduler, auto_verifier), reply_markup=_auto_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "status")
    async def status_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        text = _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr)
        await callback.message.answer(text, reply_markup=_main_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "catalog")
    async def catalog_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(render_catalog(config))
        await callback.answer()

    @router.callback_query(F.data == "preview")
    async def preview_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await _send_long_text(callback.message, render_all_funpay_texts(config))
        await callback.answer()

    @router.callback_query(F.data == "publish_plan")
    async def publish_plan_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await _send_long_text(callback.message, _format_publish_plan(client.build_publish_plan(config)))
        await callback.answer()

    @router.callback_query(F.data == "publish_all")
    async def publish_all_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not client.can_write:
            await callback.message.answer("Нужен funpay.golden_key для публикации.")
            await callback.answer()
            return
        tier_lines = [f"{i+1}. {t.title} — {t.price} ₽" for i, t in enumerate(config.service.tiers)]
        await callback.message.answer(
            "Создать все предложения на FunPay автоматически?\n\n"
            + "\n".join(tier_lines)
            + f"\n\nКатегория: {', '.join(config.funpay.category_ids) or 'не задана'}\n"
            f"Всего: {len(config.service.tiers)} предложений",
            reply_markup=_confirm_keyboard("confirm_publish"),
        )
        await callback.answer()

    @router.callback_query(F.data == "confirm_publish")
    async def confirm_publish_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer("Создаю предложения на FunPay... Это может занять около минуты.")
        try:
            results = await client.create_all_offers(config)
        except Exception as exc:
            await callback.message.answer(f"Ошибка при публикации: {exc}", reply_markup=_publish_keyboard())
            await callback.answer()
            return
        await callback.message.answer(_format_offer_results(results), reply_markup=_publish_keyboard())
        await callback.answer("Готово")

    @router.callback_query(F.data == "check_session")
    async def check_session_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        try:
            result = await client.check_session()
        except Exception as exc:
            result = f"Ошибка проверки: {exc}"
        await callback.message.answer(result, reply_markup=_funpay_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "bump_now")
    async def bump_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(
            "Подтвердить ручное поднятие категорий FunPay?",
            reply_markup=_confirm_keyboard("confirm_bump"),
        )
        await callback.answer()

    @router.callback_query(F.data == "confirm_bump")
    async def confirm_bump_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        results = await scheduler.bump_now()
        await callback.message.answer(_format_results(results), reply_markup=_funpay_keyboard())
        await callback.answer("Готово")

    @router.callback_query(F.data == "autobump_on")
    async def autobump_on_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        scheduler.set_enabled(True)
        await callback.message.answer("Авто-поднятие включено в текущем запуске.", reply_markup=_auto_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "autobump_off")
    async def autobump_off_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        scheduler.set_enabled(False)
        await callback.message.answer("Авто-поднятие выключено в текущем запуске.", reply_markup=_auto_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "verify_on")
    async def verify_on_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not auto_verifier:
            await callback.message.answer("Авто-верификация не настроена. Нужен hero_sms.api_key и funpay.golden_key.")
            await callback.answer()
            return
        auto_verifier.set_enabled(True)
        await callback.message.answer("Авто-верификация включена. Заказы обрабатываются автоматически.", reply_markup=_auto_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "verify_off")
    async def verify_off_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        if not auto_verifier:
            await callback.message.answer("Авто-верификация не настроена.")
            await callback.answer()
            return
        auto_verifier.set_enabled(False)
        await callback.message.answer("Авто-верификация выключена.", reply_markup=_auto_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "settings")
    async def settings_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_settings_text(config, client, hero_sms, auto_verifier), reply_markup=_settings_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("set:"))
    async def set_value_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        setting_key = callback.data.split(":", 1)[1]
        user_id = callback.from_user.id if callback.from_user else 0
        pending_inputs[user_id] = setting_key
        await callback.message.answer(_setting_prompt(setting_key), reply_markup=_cancel_keyboard())
        await callback.answer()

    @router.callback_query(F.data.startswith("toggle:"))
    async def toggle_value_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        setting_key = callback.data.split(":", 1)[1]
        try:
            message = _toggle_setting(setting_key, config, client, scheduler)
        except Exception as exc:
            message = f"Не получилось сохранить настройку: {exc}"
        await callback.message.answer(message, reply_markup=_settings_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "cancel")
    async def cancel_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        user_id = callback.from_user.id if callback.from_user else 0
        pending_inputs.pop(user_id, None)
        await callback.message.answer("Отменено.", reply_markup=_main_keyboard())
        await callback.answer()

    @router.message(F.text)
    async def password_or_menu(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        text = (message.text or "").strip()

        # Обработка шагов мастера настройки
        step_idx = setup_step.get(user_id, -1)
        if 0 <= step_idx < len(SETUP_STEPS):
            step = SETUP_STEPS[step_idx]
            try:
                result = _apply_setup_step(step["key"], text)
            except Exception as exc:
                await message.answer(f"Ошибка: {exc}\n\nПопробуй ещё раз или нажми «Пропустить».", reply_markup=_setup_keyboard())
                return
            await message.answer(f"✅ {result}")
            await _advance_setup(message, user_id, step_idx + 1)
            return

        if _is_allowed(user_id, authorized, config):
            setting_key = pending_inputs.pop(user_id, "")
            if setting_key:
                try:
                    result = _apply_setting(setting_key, text, config, client, scheduler)
                except Exception as exc:
                    result = f"Не получилось сохранить: {exc}"
                await message.answer(result, reply_markup=_settings_keyboard())
                return
            await message.answer(
                _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr),
                reply_markup=_main_keyboard(),
            )
            return
        if text == config.telegram.password:
            authorized.add(user_id)
            _save_authorized_users(authorized)
            await message.answer(
                "Доступ открыт.\n\n" + _dashboard_text(config, client, scheduler, hero_sms, auto_verifier, order_mgr),
                reply_markup=_main_keyboard(),
            )
            return
        await message.answer("Пароль неверный.")

    return router


def _is_allowed(user_id: int, authorized: set[int], config: AppConfig) -> bool:
    if user_id <= 0:
        return False
    return user_id in authorized or user_id in set(config.telegram.admin_ids)


def _load_authorized_users(config: AppConfig) -> set[int]:
    users = set(config.telegram.admin_ids)
    if not STATE_PATH.exists():
        return users

    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return users

    for value in raw.get("authorized_users", []):
        try:
            users.add(int(value))
        except (TypeError, ValueError):
            continue
    return users


def _save_authorized_users(users: set[int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"authorized_users": sorted(users)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _dashboard_text(
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
    hero_sms: HeroSmsClient | None,
    auto_verifier: AutoVerifier | None,
    order_mgr: OrderManager,
) -> str:
    funpay_status = "готов" if client.can_write else "нужен golden_key"
    hero_status = "подключен" if hero_sms else "не настроен"
    verify_status = "включена" if auto_verifier and auto_verifier.is_enabled else "выключена" if auto_verifier else "не настроена"
    active_orders = len(order_mgr.active_orders)
    next_bump = scheduler.next_run_at.isoformat(sep=" ", timespec="minutes") if scheduler.next_run_at else "не запланирован"
    return "\n".join(
        [
            "FunPayBot — главное меню",
            f"Версия панели: {PANEL_VERSION}",
            "",
            "Состояние:",
            f"FunPay: {funpay_status}",
            f"Hero SMS: {hero_status}",
            f"Авто-поднятие: {'включено' if scheduler.enabled else 'выключено'}",
            f"Авто-верификация: {verify_status}",
            f"Активные заказы: {active_orders}",
            f"Следующее поднятие: {next_bump}",
            "",
            "Выбери раздел ниже.",
        ]
    )


def _funpay_menu_text(config: AppConfig, client: FunPayClient, scheduler: AutoBumpScheduler) -> str:
    return "\n".join(
        [
            "Раздел FunPay",
            "",
            f"Сессия: {'golden_key задан' if client.can_write else 'golden_key не задан'}",
            f"Категории: {', '.join(config.funpay.category_ids) or 'не заданы'}",
            f"Авто-поднятие: {'включено' if scheduler.enabled else 'выключено'}",
            "",
            "Действия: проверить сессию, поднять предложения, перейти к публикации.",
        ]
    )


def _publish_menu_text(config: AppConfig) -> str:
    return "\n".join(
        [
            "Раздел публикации",
            "",
            f"Товаров в витрине: {len(config.service.tiers)}",
            f"Категории: {', '.join(config.funpay.category_ids) or 'не заданы'}",
            "",
            "Сначала открой план/превью. Затем пробуй публикацию.",
            "Если FunPay не подтвердит создание, бот покажет ERR с причиной.",
        ]
    )


def _orders_menu_text(order_mgr: OrderManager, auto_verifier: AutoVerifier | None) -> str:
    active = order_mgr.active_orders
    lines = [
        "Раздел заказов",
        "",
        f"Активных заказов: {len(active)}",
        f"Авто-верификация: {'включена' if auto_verifier and auto_verifier.is_enabled else 'выключена' if auto_verifier else 'не настроена'}",
    ]
    if active:
        lines.append("")
        lines.append("Активные:")
        for order in active[:10]:
            lines.append(f"{order.id}. {order.country_name} — {order.state.value}")
    return "\n".join(lines)


def _orders_list_text(order_mgr: OrderManager) -> str:
    active = order_mgr.active_orders
    if not active:
        return "Активных заказов сейчас нет."
    lines = ["Активные заказы:"]
    for order in active:
        lines.append(
            f"{order.id}. {order.country_name} — {order.state.value}\n"
            f"Номер: +{order.hero_number or '—'} | Код: {order.sms_code or '—'}"
        )
    return "\n\n".join(lines)


def _hero_menu_text(config: AppConfig, hero_sms: HeroSmsClient | None) -> str:
    return "\n".join(
        [
            "Раздел Hero SMS",
            "",
            f"Статус: {'подключен' if hero_sms else 'не настроен'}",
            f"Сервис: {config.hero_sms.default_service}",
            f"Страна: {config.hero_sms.default_country}",
            "",
            "Здесь можно проверить баланс и создать ручной заказ.",
        ]
    )


def _auto_menu_text(scheduler: AutoBumpScheduler, auto_verifier: AutoVerifier | None) -> str:
    return "\n\n".join(
        [
            "Раздел автоматизации",
            scheduler.status_text(),
            auto_verifier.status_text() if auto_verifier else "Авто-верификация не настроена: нужен FunPay golden_key и Hero SMS api_key.",
        ]
    )


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="FunPay", callback_data="menu_funpay"),
                InlineKeyboardButton(text="Публикация", callback_data="menu_publish"),
            ],
            [
                InlineKeyboardButton(text="Заказы", callback_data="menu_orders"),
                InlineKeyboardButton(text="Hero SMS", callback_data="menu_hero"),
            ],
            [
                InlineKeyboardButton(text="Автоматизация", callback_data="menu_auto"),
                InlineKeyboardButton(text="Настройки", callback_data="settings"),
            ],
            [
                InlineKeyboardButton(text="Обновить статус", callback_data="status"),
            ],
        ]
    )


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Главное меню", callback_data="menu_main")]]
    )


def _funpay_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Проверить сессию", callback_data="check_session"),
                InlineKeyboardButton(text="Поднять сейчас", callback_data="bump_now"),
            ],
            [
                InlineKeyboardButton(text="Публикация", callback_data="menu_publish"),
                InlineKeyboardButton(text="Настройки FunPay", callback_data="settings"),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu_main")],
        ]
    )


def _publish_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Каталог товаров", callback_data="catalog"),
                InlineKeyboardButton(text="Тексты лотов", callback_data="preview"),
            ],
            [
                InlineKeyboardButton(text="План публикации", callback_data="publish_plan"),
                InlineKeyboardButton(text="Опубликовать все", callback_data="publish_all"),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu_main")],
        ]
    )


def _orders_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Новый ручной заказ", callback_data="new_order"),
                InlineKeyboardButton(text="Активные заказы", callback_data="orders_list"),
            ],
            [
                InlineKeyboardButton(text="Верификация ON", callback_data="verify_on"),
                InlineKeyboardButton(text="Верификация OFF", callback_data="verify_off"),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu_main")],
        ]
    )


def _auto_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Авто-поднятие ON", callback_data="autobump_on"),
                InlineKeyboardButton(text="Авто-поднятие OFF", callback_data="autobump_off"),
            ],
            [
                InlineKeyboardButton(text="Авто-верификация ON", callback_data="verify_on"),
                InlineKeyboardButton(text="Авто-верификация OFF", callback_data="verify_off"),
            ],
            [InlineKeyboardButton(text="Главное меню", callback_data="menu_main")],
        ]
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Мастер настройки", callback_data="setup_start"),
            ],
            [
                InlineKeyboardButton(text="FunPay golden_key", callback_data="set:funpay_golden_key"),
                InlineKeyboardButton(text="User-Agent", callback_data="set:funpay_user_agent"),
            ],
            [
                InlineKeyboardButton(text="FunPay proxy", callback_data="set:funpay_proxy"),
                InlineKeyboardButton(text="ID категорий", callback_data="set:funpay_category_ids"),
            ],
            [
                InlineKeyboardButton(text="TG token", callback_data="set:telegram_token"),
                InlineKeyboardButton(text="Пароль бота", callback_data="set:telegram_password"),
            ],
            [
                InlineKeyboardButton(text="Admin IDs", callback_data="set:telegram_admin_ids"),
                InlineKeyboardButton(text="Интервал", callback_data="set:auto_bump_interval"),
            ],
            [
                InlineKeyboardButton(text="Hero SMS api_key", callback_data="set:hero_sms_api_key"),
                InlineKeyboardButton(text="Hero SMS сервис", callback_data="set:hero_sms_service"),
            ],
            [
                InlineKeyboardButton(text="Hero SMS страна", callback_data="set:hero_sms_country"),
                InlineKeyboardButton(text="Hero SMS proxy", callback_data="set:hero_sms_proxy"),
            ],
            [
                InlineKeyboardButton(text="Главное меню", callback_data="menu_main"),
            ],
        ]
    )


def _confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, выполнить", callback_data=action),
                InlineKeyboardButton(text="Отмена", callback_data="menu_main"),
            ]
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="menu_main")]]
    )


def _setup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Пропустить", callback_data="setup_skip"),
                InlineKeyboardButton(text="Отмена", callback_data="setup_cancel"),
            ]
        ]
    )


def _hero_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Баланс", callback_data="hero_balance"),
                InlineKeyboardButton(text="Новый заказ", callback_data="new_order"),
            ],
            [
                InlineKeyboardButton(text="Активные заказы", callback_data="orders_list"),
                InlineKeyboardButton(text="Главное меню", callback_data="menu_main"),
            ],
        ]
    )


def _country_keyboard(config: AppConfig) -> InlineKeyboardMarkup:
    buttons = []
    for cid, name in config.orders.countries.items():
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"order_country:{cid}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _order_keyboard(order_mgr: OrderManager, order_id: int) -> InlineKeyboardMarkup:
    order = order_mgr.get_order(order_id)
    if not order:
        return _main_keyboard()

    rows: list[list[InlineKeyboardButton]] = []

    if order.state == OrderState.WAITING_AUTH:
        rows.append([
            InlineKeyboardButton(text="Клиент дошёл до верификации", callback_data=f"order_get_number:{order_id}"),
        ])
    elif order.state == OrderState.WAITING_VERIFICATION:
        rows.append([
            InlineKeyboardButton(text="Получить номер", callback_data=f"order_get_number:{order_id}"),
        ])
    elif order.state == OrderState.NUMBER_RECEIVED:
        rows.append([
            InlineKeyboardButton(text="Проверить код", callback_data=f"order_check_code:{order_id}"),
        ])
    elif order.state in (OrderState.CODE_RECEIVED, OrderState.WAITING_CONFIRM):
        rows.append([
            InlineKeyboardButton(text="Подтвердить заказ", callback_data=f"order_confirm:{order_id}"),
        ])

    rows.append([
        InlineKeyboardButton(text="Отменить заказ", callback_data=f"order_cancel:{order_id}"),
        InlineKeyboardButton(text="Главное меню", callback_data="menu_main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_results(results: list[BumpResult]) -> str:
    lines = ["Результат поднятия:"]
    for result in results:
        marker = "OK" if result.ok else "ERR"
        lines.append(f"{marker} {result.category_id}: {result.message}")
    return "\n".join(lines)


def _format_offer_results(results: list[CreateOfferResult]) -> str:
    ok_count = sum(1 for r in results if r.ok)
    err_count = len(results) - ok_count
    lines = [
        "Публикация FunPay завершена",
        "",
        f"Создано: {ok_count}/{len(results)}",
        f"Ошибок: {err_count}",
        "",
    ]
    if ok_count:
        lines.append("Успешно:")
        for result in results:
            if result.ok:
                lines.append(f"OK {result.tier_id} — {result.message}")
        lines.append("")
    if err_count:
        lines.append("Ошибки:")
        for result in results:
            if not result.ok:
                lines.append(f"ERR {result.tier_id} ({result.category_id})")
                lines.append(f"Причина: {result.message}")
                hint = _publish_error_hint(result.message)
                if hint:
                    lines.append(f"Что сделать: {hint}")
                lines.append("")
    return "\n".join(lines)


def _publish_error_hint(message: str) -> str:
    lower = message.lower()
    if "страница входа" in lower or "golden_key" in lower or "сесси" in lower:
        return "обнови FunPay golden_key в настройках и нажми «Проверить сессию»."
    if "форм" in lower or "валидац" in lower or "другие поля" in lower:
        return "FunPay требует поля, которых бот не знает. Нужен HAR-запрос создания лота из браузера без секретов."
    if "частоту" in lower or "429" in lower:
        return "подожди 10-20 минут и попробуй ещё раз."
    if "права аккаунта" in lower:
        return "проверь, что аккаунт FunPay может создавать предложения в этой категории."
    return ""


def _format_publish_plan(items: list[PublishPlanItem]) -> str:
    lines = [
        "План публикации на FunPay:",
        "Категория Claude.ai / верификация номера: https://funpay.com/lots/3172/",
        "Для создания предложений открой ссылку trade у каждого товара и вставь текст из /preview.",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{index}. {item.title}",
                f"ID товара: {item.tier_id}",
                f"Цена: {item.price} ₽",
                f"Категория: {item.category_id or 'не задана'}",
                f"Ссылка создания: {item.url or 'не задана'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


async def _send_long_text(message: Message, text: str) -> None:
    max_len = 3900
    for start in range(0, len(text), max_len):
        await message.answer(text[start : start + max_len])


def _settings_text(config: AppConfig, client: FunPayClient, hero_sms: HeroSmsClient | None, auto_verifier: AutoVerifier | None = None) -> str:
    token_note = "задан" if config.telegram.token else "пусто"
    hero_key_note = _mask_secret(config.hero_sms.api_key) if config.hero_sms.api_key else "пусто"
    hero_status = "подключен" if hero_sms else "не настроен"
    verify_status = "включена" if auto_verifier and auto_verifier.is_enabled else "выключена" if auto_verifier else "не настроена"
    return "\n".join(
        [
            "Настройки:",
            f"Telegram token: {token_note}",
            f"Пароль бота: {_mask_secret(config.telegram.password)}",
            f"Admin IDs: {', '.join(map(str, config.telegram.admin_ids)) or 'пусто'}",
            f"FunPay golden_key: {_mask_secret(config.funpay.golden_key)}",
            f"FunPay User-Agent: {config.funpay.user_agent or 'пусто'}",
            f"FunPay proxy: {_mask_secret(config.funpay.proxy) if config.funpay.proxy else 'пусто'}",
            f"Категории: {', '.join(config.funpay.category_ids) or 'пусто'}",
            f"FunPay запросы: {'готовы' if client.can_write else 'нужен golden_key'}",
            f"Интервал поднятия: {config.auto_bump.interval_seconds} сек.",
            "",
            f"Hero SMS api_key: {hero_key_note}",
            f"Hero SMS статус: {hero_status}",
            f"Hero SMS сервис: {config.hero_sms.default_service}",
            f"Hero SMS страна: {config.hero_sms.default_country}",
            "",
            f"Авто-верификация: {verify_status}",
            "",
            "TG token можно сохранить тут, но чтобы бот перешел на новый токен, нужен перезапуск.",
            "Для очистки поля отправь: -",
        ]
    )


def _setting_prompt(setting_key: str) -> str:
    prompts = {
        "funpay_golden_key": "Отправь FunPay golden_key. Его можно взять из cookies авторизованного FunPay.",
        "funpay_user_agent": "Отправь User-Agent того же браузера, где открыт FunPay.",
        "funpay_proxy": "Отправь прокси для FunPay или '-' чтобы очистить.",
        "funpay_category_ids": "Отправь ID категорий FunPay через запятую или пробел. Например: 123, 456",
        "telegram_token": "Отправь новый Telegram token. Он сохранится, но для применения нужен перезапуск.",
        "telegram_password": "Отправь новый пароль для входа в Telegram-бота.",
        "telegram_admin_ids": "Отправь Telegram admin IDs через запятую или пробел.",
        "auto_bump_interval": "Отправь интервал автоподнятия в секундах. Минимум 600.",
        "hero_sms_api_key": "Отправь API-ключ Hero SMS. Его можно взять в личном кабинете hero-sms.com.",
        "hero_sms_service": "Отправь код сервиса Hero SMS. Например: cl (Claude.ai), tg (Telegram), wa (WhatsApp).",
        "hero_sms_country": "Отправь ID страны Hero SMS. Например: 0 (Россия), 16 (UK), 43 (Германия).",
        "hero_sms_proxy": "Отправь прокси для Hero SMS или '-' чтобы очистить.",
    }
    return prompts.get(setting_key, "Отправь новое значение.")


def _apply_setting(
    setting_key: str,
    text: str,
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
) -> str:
    value = "" if text == "-" else text

    if setting_key == "funpay_golden_key":
        _save_config_value(("funpay", "golden_key"), value)
        config.funpay.golden_key = value
        client.refresh_session()
        return "FunPay golden_key сохранен."

    if setting_key == "funpay_user_agent":
        _save_config_value(("funpay", "user_agent"), value)
        config.funpay.user_agent = value
        client.refresh_session()
        return "FunPay User-Agent сохранен."

    if setting_key == "funpay_proxy":
        _save_config_value(("funpay", "proxy"), value)
        config.funpay.proxy = value
        client.refresh_session()
        return "FunPay proxy сохранен."

    if setting_key == "funpay_category_ids":
        values = _parse_list(text)
        _save_config_value(("funpay", "category_ids"), values)
        config.funpay.category_ids = values
        return "ID категорий FunPay сохранены."

    if setting_key == "telegram_token":
        _save_config_value(("telegram", "token"), value)
        config.telegram.token = value
        return "Telegram token сохранен. Перезапусти бота, чтобы применить новый token."

    if setting_key == "telegram_password":
        if len(value) < 4:
            raise ValueError("Пароль должен быть хотя бы 4 символа.")
        _save_config_value(("telegram", "password"), value)
        config.telegram.password = value
        return "Пароль Telegram-бота сохранен."

    if setting_key == "telegram_admin_ids":
        values = [int(item) for item in _parse_list(text)]
        _save_config_value(("telegram", "admin_ids"), values)
        config.telegram.admin_ids = values
        return "Admin IDs сохранены."

    if setting_key == "auto_bump_interval":
        seconds = int(text)
        if seconds < 600:
            raise ValueError("Интервал должен быть минимум 600 секунд.")
        _save_config_value(("auto_bump", "interval_seconds"), seconds)
        config.auto_bump.interval_seconds = seconds
        scheduler.next_run_at = None
        return "Интервал автоподнятия сохранен."

    if setting_key == "hero_sms_api_key":
        _save_config_value(("hero_sms", "api_key"), value)
        config.hero_sms.api_key = value
        return "Hero SMS api_key сохранен. Перезапусти бота для применения."

    if setting_key == "hero_sms_service":
        _save_config_value(("hero_sms", "default_service"), value)
        config.hero_sms.default_service = value
        return f"Hero SMS сервис сохранен: {value}"

    if setting_key == "hero_sms_country":
        country_id = int(text)
        _save_config_value(("hero_sms", "default_country"), country_id)
        config.hero_sms.default_country = country_id
        return f"Hero SMS страна сохранена: {country_id}"

    if setting_key == "hero_sms_proxy":
        _save_config_value(("hero_sms", "proxy"), value)
        config.hero_sms.proxy = value
        return "Hero SMS proxy сохранен. Перезапусти бота для применения."

    raise ValueError("Неизвестная настройка.")


def _toggle_setting(
    setting_key: str,
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
) -> str:
    raise ValueError("Переключатели режимов убраны: укажи golden_key и ID категории.")


def _save_config_value(path: tuple[str, ...], value: Any) -> None:
    raw = _load_config_json()
    current = raw
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value
    _save_config_json(raw)


def _load_config_json() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _save_config_json(raw: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_list(text: str) -> list[str]:
    return [item.strip() for item in text.replace(",", " ").split() if item.strip()]


def _mask_secret(value: str) -> str:
    if not value:
        return "пусто"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"
