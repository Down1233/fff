from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional until requirements are installed.
    load_dotenv = None


@dataclass(slots=True)
class TelegramConfig:
    token: str
    password: str
    admin_ids: list[int] = field(default_factory=list)
    proxy: str = ""


@dataclass(slots=True)
class FunPayConfig:
    golden_key: str
    user_agent: str
    proxy: str = ""
    requests_timeout: int = 30
    dry_run: bool = True
    marketplace_writes_enabled: bool = False
    category_ids: list[str] = field(default_factory=list)
    lot_urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QuietHoursConfig:
    enabled: bool = False
    start: str = "03:00"
    end: str = "08:00"
    timezone: str = "Europe/Moscow"


@dataclass(slots=True)
class AutoBumpConfig:
    enabled: bool = True
    interval_seconds: int = 3900
    jitter_seconds: int = 420
    quiet_hours: QuietHoursConfig = field(default_factory=QuietHoursConfig)


@dataclass(slots=True)
class ServiceTier:
    id: str
    title: str
    price: int
    old_price: int | None
    quality: str
    badge: str
    warranty: str
    description: str
    funpay_category_id: str = ""
    delivery_format: str = ""


@dataclass(slots=True)
class ServiceConfig:
    name: str
    brand: str
    delivery_note: str
    support_note: str
    tiers: list[ServiceTier]


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    funpay: FunPayConfig
    auto_bump: AutoBumpConfig
    service: ServiceConfig


def _env(name: str, fallback: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else fallback


def _as_str_list(values: list[Any]) -> list[str]:
    return [str(value).strip() for value in values if str(value).strip()]


def _as_int_list(values: list[Any]) -> list[int]:
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


def load_config(path: Path) -> AppConfig:
    if load_dotenv:
        load_dotenv(path.parent / ".env")

    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    telegram_raw = raw.get("telegram", {})
    funpay_raw = raw.get("funpay", {})
    auto_bump_raw = raw.get("auto_bump", {})
    quiet_raw = auto_bump_raw.get("quiet_hours", {})
    service_raw = raw.get("service", {})

    tiers = [
        ServiceTier(
            id=str(item.get("id", "")).strip(),
            title=str(item.get("title", "")).strip(),
            price=int(item.get("price", 0)),
            old_price=int(item["old_price"]) if item.get("old_price") else None,
            quality=str(item.get("quality", "")).strip(),
            badge=str(item.get("badge", "")).strip(),
            warranty=str(item.get("warranty", "")).strip(),
            description=str(item.get("description", "")).strip(),
            funpay_category_id=str(item.get("funpay_category_id", "")).strip(),
            delivery_format=str(item.get("delivery_format", "")).strip(),
        )
        for item in service_raw.get("tiers", [])
    ]

    config = AppConfig(
        telegram=TelegramConfig(
            token=_env("TELEGRAM_BOT_TOKEN", str(telegram_raw.get("token", ""))).strip(),
            password=_env("BOT_PASSWORD", str(telegram_raw.get("password", "change_me"))).strip(),
            admin_ids=_as_int_list(telegram_raw.get("admin_ids", [])),
            proxy=str(telegram_raw.get("proxy", "")).strip(),
        ),
        funpay=FunPayConfig(
            golden_key=_env("FUNPAY_GOLDEN_KEY", str(funpay_raw.get("golden_key", ""))).strip(),
            user_agent=str(funpay_raw.get("user_agent", "")).strip(),
            proxy=str(funpay_raw.get("proxy", "")).strip(),
            requests_timeout=int(funpay_raw.get("requests_timeout", 30)),
            dry_run=bool(funpay_raw.get("dry_run", True)),
            marketplace_writes_enabled=bool(funpay_raw.get("marketplace_writes_enabled", False)),
            category_ids=_as_str_list(funpay_raw.get("category_ids", [])),
            lot_urls=_as_str_list(funpay_raw.get("lot_urls", [])),
        ),
        auto_bump=AutoBumpConfig(
            enabled=bool(auto_bump_raw.get("enabled", True)),
            interval_seconds=int(auto_bump_raw.get("interval_seconds", 3900)),
            jitter_seconds=int(auto_bump_raw.get("jitter_seconds", 420)),
            quiet_hours=QuietHoursConfig(
                enabled=bool(quiet_raw.get("enabled", False)),
                start=str(quiet_raw.get("start", "03:00")),
                end=str(quiet_raw.get("end", "08:00")),
                timezone=str(quiet_raw.get("timezone", "Europe/Moscow")),
            ),
        ),
        service=ServiceConfig(
            name=str(service_raw.get("name", "")).strip(),
            brand=str(service_raw.get("brand", "")).strip(),
            delivery_note=str(service_raw.get("delivery_note", "")).strip(),
            support_note=str(service_raw.get("support_note", "")).strip(),
            tiers=tiers,
        ),
    )

    _validate_config(config)
    return config


def _validate_config(config: AppConfig) -> None:
    if not config.service.tiers:
        raise ValueError("В config.json должен быть хотя бы один тариф service.tiers.")

    for tier in config.service.tiers:
        if not tier.id or not tier.title:
            raise ValueError("У каждого тарифа должны быть id и title.")
        if tier.price <= 0:
            raise ValueError(f"У тарифа {tier.id} должна быть цена больше 0.")

    if config.auto_bump.interval_seconds < 600:
        raise ValueError("auto_bump.interval_seconds лучше держать не меньше 600 секунд.")
