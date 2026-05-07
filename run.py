import asyncio
import logging
from pathlib import Path

from funpaybot.auto_verify import AutoVerifier
from funpaybot.catalog import write_listing_preview
from funpaybot.config import load_config
from funpaybot.funpay_client import FunPayClient
from funpaybot.hero_sms import HeroSmsClient
from funpaybot.orders import AbuseConfig, OrderManager
from funpaybot.scheduler import AutoBumpScheduler
from funpaybot.tg_bot import run_telegram_bot


ROOT = Path(__file__).resolve().parent


def _write_publish_plan(config, client, path: Path) -> None:
    lines = [
        "# План публикации на FunPay",
        "",
        "Категория Claude.ai / верификация номера: https://funpay.com/lots/3172/",
        "Создание предложений: https://funpay.com/lots/3172/trade",
        "",
    ]
    for index, item in enumerate(client.build_publish_plan(config), start=1):
        lines.extend([
            f"## {index}. {item.title}",
            "",
            f"ID: {item.tier_id}",
            f"Цена: {item.price} ₽",
            f"Категория: {item.category_id}",
            f"Ссылка создания: {item.url}",
            "",
        ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = load_config(ROOT / "config.json")
    client = FunPayClient(config.funpay)
    hero_sms = HeroSmsClient(
        api_key=config.hero_sms.api_key,
        timeout=30,
        proxy=config.hero_sms.proxy,
    ) if config.hero_sms.api_key else None
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
    write_listing_preview(config, ROOT / "data" / "funpay_listing_preview.md")
    _write_publish_plan(config, client, ROOT / "data" / "funpay_publish_plan.md")

    scheduler = AutoBumpScheduler(config=config, client=client)
    await scheduler.start()

    # Авто-верификация: работает только если есть и golden_key, и hero_sms api_key
    auto_verifier: AutoVerifier | None = None
    if hero_sms and config.funpay.golden_key:
        auto_verifier = AutoVerifier(
            config=config,
            funpay=client,
            hero_sms=hero_sms,
            order_mgr=order_manager,
        )
        auto_verifier.start()
        logging.getLogger(__name__).info("AutoVerifier started (polling FunPay orders)")

    await run_telegram_bot(
        config=config,
        client=client,
        scheduler=scheduler,
        hero_sms=hero_sms,
        order_manager=order_manager,
        auto_verifier=auto_verifier,
    )


if __name__ == "__main__":
    asyncio.run(main())
