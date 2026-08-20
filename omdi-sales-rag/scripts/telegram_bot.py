from __future__ import annotations

import asyncio

from app.config import get_settings
from app.services.telegram import TelegramSalesBot


def main() -> None:
    settings = get_settings()
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not configured. Create a bot with @BotFather "
            "and add its token to .env."
        )
    state_path = settings.telegram_state_path or (settings.data_dir / "telegram-state.json")
    bot = TelegramSalesBot(
        token=token,
        app_base_url=settings.telegram_app_base_url,
        public_base_url=settings.telegram_public_base_url or settings.public_base_url,
        default_company_slug=settings.telegram_default_company_slug,
        state_path=state_path,
        poll_timeout_seconds=settings.telegram_poll_timeout_seconds,
    )
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
