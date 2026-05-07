from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .catalog import render_all_funpay_texts, render_catalog, write_listing_preview
from .config import AppConfig
from .funpay_client import BumpResult, FunPayClient, PublishPlanItem
from .scheduler import AutoBumpScheduler


STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "runtime_state.json"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


async def run_telegram_bot(
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
) -> None:
    if not config.telegram.token:
        raise RuntimeError("Заполни telegram.token в config.json или TELEGRAM_BOT_TOKEN в .env.")

    bot = Bot(token=config.telegram.token)
    dispatcher = Dispatcher()
    dispatcher.include_router(_build_router(config, client, scheduler))
    await dispatcher.start_polling(bot)


def _build_router(config: AppConfig, client: FunPayClient, scheduler: AutoBumpScheduler) -> Router:
    router = Router()
    authorized = _load_authorized_users(config)
    pending_inputs: dict[int, str] = {}

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
            await message.answer("FunPayBot готов. Выбирай действие:", reply_markup=_main_keyboard())
            return
        await message.answer("Привет. Отправь пароль для входа в FunPayBot.")

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(scheduler.status_text(), reply_markup=_main_keyboard())

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

    @router.message(Command("settings"))
    async def settings(message: Message) -> None:
        if not await require_auth(message):
            return
        await message.answer(_settings_text(config, client), reply_markup=_settings_keyboard())

    @router.callback_query(F.data == "status")
    async def status_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(scheduler.status_text(), reply_markup=_main_keyboard())
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

    @router.callback_query(F.data == "check_session")
    async def check_session_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        try:
            result = await client.check_session()
        except Exception as exc:
            result = f"Ошибка проверки: {exc}"
        await callback.message.answer(result, reply_markup=_main_keyboard())
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
        await callback.message.answer(_format_results(results), reply_markup=_main_keyboard())
        await callback.answer("Готово")

    @router.callback_query(F.data == "autobump_on")
    async def autobump_on_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        scheduler.set_enabled(True)
        await callback.message.answer("Авто-поднятие включено в текущем запуске.", reply_markup=_main_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "autobump_off")
    async def autobump_off_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        scheduler.set_enabled(False)
        await callback.message.answer("Авто-поднятие выключено в текущем запуске.", reply_markup=_main_keyboard())
        await callback.answer()

    @router.callback_query(F.data == "settings")
    async def settings_callback(callback: CallbackQuery) -> None:
        if not await require_auth_callback(callback):
            return
        await callback.message.answer(_settings_text(config, client), reply_markup=_settings_keyboard())
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
        if _is_allowed(user_id, authorized, config):
            setting_key = pending_inputs.pop(user_id, "")
            if setting_key:
                try:
                    result = _apply_setting(setting_key, text, config, client, scheduler)
                except Exception as exc:
                    result = f"Не получилось сохранить: {exc}"
                await message.answer(result, reply_markup=_settings_keyboard())
                return
            await message.answer("Меню:", reply_markup=_main_keyboard())
            return
        if text == config.telegram.password:
            authorized.add(user_id)
            _save_authorized_users(authorized)
            await message.answer("Доступ открыт. FunPayBot готов.", reply_markup=_main_keyboard())
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


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Статус", callback_data="status"),
                InlineKeyboardButton(text="Каталог", callback_data="catalog"),
            ],
            [
                InlineKeyboardButton(text="Превью текста", callback_data="preview"),
                InlineKeyboardButton(text="Проверить FunPay", callback_data="check_session"),
            ],
            [
                InlineKeyboardButton(text="План публикации", callback_data="publish_plan"),
            ],
            [
                InlineKeyboardButton(text="Поднять сейчас", callback_data="bump_now"),
            ],
            [
                InlineKeyboardButton(text="Авто ON", callback_data="autobump_on"),
                InlineKeyboardButton(text="Авто OFF", callback_data="autobump_off"),
            ],
            [
                InlineKeyboardButton(text="Настройки", callback_data="settings"),
            ],
        ]
    )


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
                InlineKeyboardButton(text="Dry-run ON/OFF", callback_data="toggle:dry_run"),
                InlineKeyboardButton(text="Real writes ON/OFF", callback_data="toggle:marketplace_writes"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="cancel"),
            ],
        ]
    )


def _confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, выполнить", callback_data=action),
                InlineKeyboardButton(text="Отмена", callback_data="cancel"),
            ]
        ]
    )


def _cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="cancel")]]
    )


def _format_results(results: list[BumpResult]) -> str:
    lines = ["Результат поднятия:"]
    for result in results:
        marker = "OK" if result.ok else "ERR"
        lines.append(f"{marker} {result.category_id}: {result.message}")
    return "\n".join(lines)


def _format_publish_plan(items: list[PublishPlanItem]) -> str:
    lines = [
        "План публикации на FunPay:",
        "Категория Claude Accounts: https://funpay.com/lots/3172/",
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


def _settings_text(config: AppConfig, client: FunPayClient) -> str:
    token_note = "задан" if config.telegram.token else "пусто"
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
            f"Dry-run: {'ON' if config.funpay.dry_run else 'OFF'}",
            f"Real writes: {'ON' if config.funpay.marketplace_writes_enabled else 'OFF'}",
            f"Можно реально отправлять запросы: {'да' if client.can_write else 'нет'}",
            f"Интервал поднятия: {config.auto_bump.interval_seconds} сек.",
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

    raise ValueError("Неизвестная настройка.")


def _toggle_setting(
    setting_key: str,
    config: AppConfig,
    client: FunPayClient,
    scheduler: AutoBumpScheduler,
) -> str:
    if setting_key == "dry_run":
        value = not config.funpay.dry_run
        _save_config_value(("funpay", "dry_run"), value)
        config.funpay.dry_run = value
        return f"Dry-run теперь {'ON' if value else 'OFF'}."

    if setting_key == "marketplace_writes":
        value = not config.funpay.marketplace_writes_enabled
        _save_config_value(("funpay", "marketplace_writes_enabled"), value)
        config.funpay.marketplace_writes_enabled = value
        if value and config.funpay.dry_run:
            return "Real writes включен, но dry-run тоже включен. Для реальных действий выключи dry-run."
        if value and not config.funpay.golden_key:
            return "Real writes включен, но golden_key пустой."
        return f"Real writes теперь {'ON' if value else 'OFF'}."

    raise ValueError("Неизвестный переключатель.")


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
