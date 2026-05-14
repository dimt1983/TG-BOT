"""
admin.py — минимальный модуль с константами и уведомлениями.

Старая полнофункциональная админка через /admin команду бота
сохранена в admin_legacy.py.bak (на случай если что-то понадобится).
Реальная админка теперь живёт в TMA Mini App (tma_static/index.html).

Здесь остались только три экспортируемых имени, которые импортируются
из bot.py / tma_handler.py / tma_static_server.py:
- ADMIN_IDS                — список tg_id админов (Дмитрий, Алёна)
- notify_new_order(...)    — короткое уведомление админам о новом заказе
- register_admin_handlers  — no-op, чтобы bot.py не падал на импорте
"""

from aiogram import Bot, Dispatcher

ADMIN_IDS = [466755177, 1403852636]  # Дмитрий, Алёна


async def notify_new_order(bot: Bot, order_id: int, user_name: str, total: float):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🔔 *Новый заказ №{order_id}!*\n👤 {user_name}\n💰 {total:.0f} ₽",
                parse_mode="Markdown",
            )
        except Exception:
            pass


def register_admin_handlers(dp: Dispatcher, bot: Bot) -> None:
    """No-op: старая bot-админка отключена, всё работает через TMA."""
    return None
