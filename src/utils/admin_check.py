"""
Lightweight admin / permission helpers for group settings.
"""
from __future__ import annotations
import logging
from pyrogram import Client
from pyrogram.types import Message, ChatMember
from pyrogram.enums import ChatMemberStatus

logger = logging.getLogger(__name__)


async def is_group_admin(client: Client, chat_id: int, user_id: int, config) -> bool:
    """
    Return True if user_id is an admin in chat_id OR is in config.admin_ids.
    Telegram admins = owner / administrator status.
    """
    if user_id in config.admin_ids:
        return True
    try:
        member: ChatMember = await client.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    except Exception as e:
        logger.warning("[AdminCheck] get_chat_member(%d, %d) failed: %s", chat_id, user_id, e)
        return False
