from __future__ import annotations

from pathlib import Path

from .config import AppConfig, ServiceTier


def render_catalog(config: AppConfig) -> str:
    lines = [
        f"{config.service.name}",
        "",
        f"Бренд: {config.service.brand}",
        f"Категории FunPay: {', '.join(config.funpay.category_ids) or 'не заданы'}",
        "",
    ]

    for index, tier in enumerate(config.service.tiers, start=1):
        old_price = f" вместо {tier.old_price} руб." if tier.old_price else ""
        lines.extend(
            [
                f"{index}. {tier.title}",
                f"Цена: {tier.price} руб.{old_price}",
                f"Качество: {tier.quality} | Метка: {tier.badge} | Гарантия: {tier.warranty}",
                f"Категория FunPay: {tier.funpay_category_id or ', '.join(config.funpay.category_ids) or 'не задана'}",
                tier.description,
                "",
            ]
        )

    return "\n".join(lines).strip()


def render_funpay_text(config: AppConfig, tier: ServiceTier) -> str:
    old_price = f"Старая цена: {tier.old_price} руб.\n" if tier.old_price else ""
    return (
        f"{tier.title}\n\n"
        f"Цена: {tier.price} руб.\n"
        f"{old_price}"
        f"Качество: {tier.quality}\n"
        f"Метка: {tier.badge}\n"
        f"Гарантия: {tier.warranty}\n\n"
        f"{tier.description}\n\n"
        "Что получает покупатель:\n"
        f"- Формат выдачи: {tier.delivery_format or 'данные/инструкция в чат FunPay'}.\n"
        "- Проверку перед выдачей, если это применимо к выбранному товару.\n"
        "- Короткую инструкцию по входу или активации.\n"
        "- Помощь в рамках указанной гарантии.\n\n"
        f"Выдача: {config.service.delivery_note}\n"
        f"Поддержка: {config.service.support_note}\n\n"
        "Важно: покупатель получает только то, что указано в описании конкретного товара."
    )


def render_all_funpay_texts(config: AppConfig) -> str:
    blocks: list[str] = [
        "# Готовые тексты для FunPay",
        "",
        "Скопируй нужный блок в описание предложения FunPay.",
        "",
    ]

    for tier in config.service.tiers:
        blocks.extend(
            [
                f"## {tier.id.upper()} - {tier.price} руб.",
                f"Категория FunPay: {tier.funpay_category_id or ', '.join(config.funpay.category_ids) or 'не задана'}",
                "",
                "```text",
                render_funpay_text(config, tier),
                "```",
                "",
            ]
        )

    return "\n".join(blocks).strip() + "\n"


def write_listing_preview(config: AppConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_all_funpay_texts(config), encoding="utf-8")
