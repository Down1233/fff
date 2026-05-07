import asyncio
import logging
from pathlib import Path

from funpaybot.catalog import write_listing_preview
from funpaybot.config import load_config
from funpaybot.funpay_client import FunPayClient
from funpaybot.scheduler import AutoBumpScheduler
from funpaybot.tg_bot import run_telegram_bot


ROOT = Path(__file__).resolve().parent


def _write_publish_plan(config, client, path: Path) -> None:
    lines = [
        "# План публикации на FunPay",
        "",
        "Категория Claude Accounts: https://funpay.com/lots/3172/",
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
    write_listing_preview(config, ROOT / "data" / "funpay_listing_preview.md")
    _write_publish_plan(config, client, ROOT / "data" / "funpay_publish_plan.md")

    scheduler = AutoBumpScheduler(config=config, client=client)
    await scheduler.start()

    await run_telegram_bot(config=config, client=client, scheduler=scheduler)


if __name__ == "__main__":
    asyncio.run(main())
