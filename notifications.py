"""Telegram and Discord webhook notifications."""

import logging

import requests

from config import load_config

logger = logging.getLogger(__name__)


# Telegram MarkdownV2 reserves these characters; any literal occurrence in
# user-supplied text must be backslash-escaped or Telegram rejects the
# request. See https://core.telegram.org/bots/api#markdownv2-style.
_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_TELEGRAM_CAPTION_LIMIT = 1024
_TELEGRAM_TEXT_LIMIT = 4096


def md2_escape(text):
    """Escape MarkdownV2 special characters in user-supplied text.

    Args:
        text: Arbitrary string. ``None`` is treated as empty.

    Returns:
        String safe to interpolate into a MarkdownV2 message body.
    """
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in _MD2_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def md2_link(label, url):
    """Build a MarkdownV2 inline link with a properly escaped label.

    Args:
        label: Visible link text (will be MD2-escaped).
        url: Target URL. Parentheses and backslashes are escaped per
            the MarkdownV2 link rules.
    """
    safe_label = md2_escape(label)
    safe_url = (url or "").replace("\\", "\\\\").replace(")", "\\)")
    return f"[{safe_label}]({safe_url})"


def build_musicbrainz_link(album_mbid):
    """Return a MarkdownV2 link to the MusicBrainz release group."""
    if not album_mbid:
        return ""
    return md2_link(
        "MusicBrainz",
        f"https://musicbrainz.org/release-group/{album_mbid}",
    )


def _truncate_caption(text, limit):
    if len(text) <= limit:
        return text
    # Reserve room for the ellipsis; keep the last full character.
    return text[: limit - 1] + "…"


def send_telegram(
    message, log_type=None, *, parse_mode=None,
    photo_url=None, disable_notification=False,
):
    """Send a message via Telegram bot API.

    Args:
        message: Text body. When ``parse_mode`` is ``"MarkdownV2"`` the
            caller is responsible for escaping any literal MD2 specials
            via ``md2_escape`` / ``md2_link``.
        log_type: If set, only send when this type appears in the
            configured ``telegram_log_types`` list.
        parse_mode: ``None`` (plain text) or ``"MarkdownV2"``.
        photo_url: When set, use ``sendPhoto`` so the artwork renders
            inline with ``message`` as the caption. Captions are
            truncated to Telegram's 1024-char limit.
        disable_notification: If true, the message arrives silently
            (no sound / vibration on the recipient device).
    """
    config = load_config()
    if not (
        config.get("telegram_enabled")
        and config.get("telegram_bot_token")
        and config.get("telegram_chat_id")
    ):
        return

    if log_type is not None:
        allowed_types = config.get("telegram_log_types", [])
        if log_type not in allowed_types:
            return

    try:
        token = config["telegram_bot_token"]
        chat_id = config["telegram_chat_id"]
        if photo_url:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            payload = {
                "chat_id": chat_id,
                "photo": photo_url,
                "caption": _truncate_caption(
                    message, _TELEGRAM_CAPTION_LIMIT,
                ),
                "disable_notification": disable_notification,
            }
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": _truncate_caption(
                    message, _TELEGRAM_TEXT_LIMIT,
                ),
                "disable_notification": disable_notification,
            }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            logger.warning(
                "Telegram API returned %d: %s",
                response.status_code, response.text[:500],
            )
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


def send_discord(message, log_type=None, embed_data=None):
    """Send a message or embed via Discord webhook.

    Args:
        message: Fallback text content.
        log_type: If set, only send when this type is in the
            configured discord_log_types list.
        embed_data: Optional dict with title, description, color,
            thumbnail, and fields for a Discord embed.
    """
    config = load_config()
    if not config.get("discord_enabled"):
        return
    webhook_url = config.get("discord_webhook_url", "")
    if not webhook_url:
        return
    if log_type is not None:
        allowed_types = config.get("discord_log_types", [])
        if log_type not in allowed_types:
            return
    try:
        payload = {}
        if embed_data:
            embed = {
                "title": embed_data.get("title", ""),
                "description": embed_data.get("description", ""),
                "color": embed_data.get("color", 0x10B981),
            }
            if embed_data.get("thumbnail"):
                embed["thumbnail"] = {"url": embed_data["thumbnail"]}
            if embed_data.get("fields"):
                embed["fields"] = embed_data["fields"]
            if embed_data.get("url"):
                embed["url"] = embed_data["url"]
            payload["embeds"] = [embed]
        else:
            payload["content"] = message
        response = requests.post(webhook_url, json=payload, timeout=10)
        if response.status_code >= 300:
            logger.warning(
                "Discord webhook returned %d: %s",
                response.status_code, response.text[:500],
            )
    except Exception as e:
        logger.warning(f"Discord notification failed: {e}")


def send_notifications(
    message, log_type=None, embed_data=None, *,
    telegram_message=None, telegram_parse_mode=None,
    photo_url=None, disable_notification=False,
):
    """Send a notification to all configured channels.

    Args:
        message: Plain-text fallback used by both channels when no
            channel-specific override is provided.
        log_type: Filter key for per-channel log type filtering.
        embed_data: Optional Discord embed payload (see ``send_discord``).
            May include a ``thumbnail`` URL and a ``url`` deep link.
        telegram_message: Optional Telegram-only body. Use this when
            the Telegram message uses ``MarkdownV2`` formatting that
            would render badly on Discord.
        telegram_parse_mode: Optional Telegram parse mode (e.g.
            ``"MarkdownV2"``). Required if ``telegram_message`` contains
            MD2 markup.
        photo_url: Optional cover-art URL. Telegram will render it via
            ``sendPhoto`` with the message as the caption. Discord
            already receives the same URL via ``embed_data['thumbnail']``
            when set by the caller.
        disable_notification: Telegram-only silent delivery flag.
    """
    send_telegram(
        telegram_message if telegram_message is not None else message,
        log_type=log_type,
        parse_mode=telegram_parse_mode,
        photo_url=photo_url,
        disable_notification=disable_notification,
    )
    send_discord(message, log_type=log_type, embed_data=embed_data)
