"""Tests for notifications module — Telegram and Discord webhooks."""

from unittest.mock import patch, MagicMock

import pytest

import notifications


@pytest.fixture
def mock_config():
    return {
        "telegram_enabled": True,
        "telegram_bot_token": "token123",
        "telegram_chat_id": "chat456",
        "telegram_log_types": ["album_error", "partial_success"],
        "discord_enabled": True,
        "discord_webhook_url": "https://discord.com/api/webhooks/test",
        "discord_log_types": ["album_error", "partial_success"],
    }


# --- send_telegram ---


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_sends_message(mock_cfg, mock_post, mock_config):
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="album_error")
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["text"] == "test msg"
    assert payload["chat_id"] == "chat456"


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_filters_log_type(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="download_started")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_no_log_type_sends(
    mock_cfg, mock_post, mock_config
):
    """When log_type is None, no filtering occurs."""
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg")
    mock_post.assert_called_once()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_disabled(mock_cfg, mock_post, mock_config):
    mock_config["telegram_enabled"] = False
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="album_error")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_missing_token(mock_cfg, mock_post, mock_config):
    mock_config["telegram_bot_token"] = ""
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="album_error")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_missing_chat_id(mock_cfg, mock_post, mock_config):
    mock_config["telegram_chat_id"] = ""
    mock_cfg.return_value = mock_config
    notifications.send_telegram("test msg", log_type="album_error")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_exception_logged(
    mock_cfg, mock_post, mock_config, caplog
):
    mock_cfg.return_value = mock_config
    mock_post.side_effect = Exception("network error")
    notifications.send_telegram("test msg", log_type="album_error")
    assert "Telegram notification failed" in caplog.text


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_non_200_logged(
    mock_cfg, mock_post, mock_config, caplog
):
    mock_cfg.return_value = mock_config
    resp = MagicMock()
    resp.status_code = 400
    resp.text = '{"ok":false,"description":"bad photo url"}'
    mock_post.return_value = resp
    notifications.send_telegram(
        "msg", log_type="album_error",
        photo_url="https://bad.example/img.jpg",
    )
    assert "Telegram API returned 400" in caplog.text
    assert "bad photo url" in caplog.text


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_non_2xx_logged(
    mock_cfg, mock_post, mock_config, caplog
):
    mock_cfg.return_value = mock_config
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "webhook gone"
    mock_post.return_value = resp
    notifications.send_discord("msg", log_type="album_error")
    assert "Discord webhook returned 404" in caplog.text


# --- send_discord ---


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_sends_plain_message(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_discord("plain msg", log_type="album_error")
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["content"] == "plain msg"


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_sends_embed(mock_cfg, mock_post, mock_config):
    mock_cfg.return_value = mock_config
    embed = {
        "title": "Test",
        "description": "desc",
        "color": 0xFF0000,
    }
    notifications.send_discord(
        "msg", log_type="album_error", embed_data=embed
    )
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert "embeds" in payload
    assert payload["embeds"][0]["title"] == "Test"
    assert payload["embeds"][0]["color"] == 0xFF0000


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_embed_with_thumbnail(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    embed = {
        "title": "T",
        "description": "d",
        "thumbnail": "https://img.example.com/art.jpg",
    }
    notifications.send_discord(
        "msg", log_type="album_error", embed_data=embed
    )
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["embeds"][0]["thumbnail"]["url"] == embed["thumbnail"]


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_embed_with_fields(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    fields = [{"name": "Artist", "value": "Test", "inline": True}]
    embed = {"title": "T", "description": "d", "fields": fields}
    notifications.send_discord(
        "msg", log_type="album_error", embed_data=embed
    )
    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    assert payload["embeds"][0]["fields"] == fields


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_filters_log_type(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_discord("msg", log_type="download_started")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_disabled(mock_cfg, mock_post, mock_config):
    mock_config["discord_enabled"] = False
    mock_cfg.return_value = mock_config
    notifications.send_discord("msg", log_type="album_error")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_no_webhook_url(mock_cfg, mock_post, mock_config):
    mock_config["discord_webhook_url"] = ""
    mock_cfg.return_value = mock_config
    notifications.send_discord("msg", log_type="album_error")
    mock_post.assert_not_called()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_no_log_type_sends(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_discord("msg")
    mock_post.assert_called_once()


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_exception_logged(
    mock_cfg, mock_post, mock_config, caplog
):
    mock_cfg.return_value = mock_config
    mock_post.side_effect = Exception("webhook error")
    notifications.send_discord("msg", log_type="album_error")
    assert "Discord notification failed" in caplog.text


# --- send_notifications ---


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_notifications_calls_both(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_notifications("msg", log_type="album_error")
    assert mock_post.call_count == 2


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_notifications_passes_embed(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    embed = {"title": "T", "description": "d", "color": 0x00FF00}
    notifications.send_notifications(
        "msg", log_type="album_error", embed_data=embed
    )
    assert mock_post.call_count == 2
    # Discord call should have embed
    discord_call = mock_post.call_args_list[1]
    payload = (
        discord_call.kwargs.get("json")
        or discord_call[1].get("json")
    )
    assert "embeds" in payload


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_notifications_filtered_sends_none(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_notifications(
        "msg", log_type="download_started"
    )
    mock_post.assert_not_called()


# --- MarkdownV2 helpers ---


def test_md2_escape_handles_specials():
    raw = "Hello (world)! v1.0_beta-2"
    escaped = notifications.md2_escape(raw)
    # Each MD2 special must be backslash-escaped exactly once.
    for ch in "()!._-":
        assert f"\\{ch}" in escaped
    # Non-special chars are untouched.
    assert "Hello" in escaped


def test_md2_escape_none_and_empty():
    assert notifications.md2_escape(None) == ""
    assert notifications.md2_escape("") == ""


def test_md2_escape_coerces_non_string():
    assert notifications.md2_escape(42) == "42"


def test_md2_link_escapes_label_and_url():
    link = notifications.md2_link("Open (here)", "https://x/y(z)")
    # Label parens are escaped.
    assert "Open \\(here\\)" in link
    # URL closing paren is escaped so MD2 link parsing terminates correctly.
    assert "\\)" in link.split("](", 1)[1]


def test_build_lidarr_album_link_returns_empty_when_missing():
    assert notifications.build_lidarr_album_link("", "abc") == ""
    assert notifications.build_lidarr_album_link("http://l", "") == ""


def test_build_lidarr_album_link_strips_trailing_slash():
    link = notifications.build_lidarr_album_link(
        "http://lidarr/", "abc-123",
    )
    assert "http://lidarr/album/abc-123" in link
    # MD2 label should be present and escaped (the label has no
    # specials so it appears verbatim).
    assert "Open in Lidarr" in link


def test_build_musicbrainz_link_uses_release_group():
    link = notifications.build_musicbrainz_link("mbid-xyz")
    assert "musicbrainz.org/release-group/mbid-xyz" in link


def test_build_musicbrainz_link_empty():
    assert notifications.build_musicbrainz_link("") == ""


# --- Telegram sendPhoto path ---


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_uses_sendphoto_when_photo_url(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_telegram(
        "caption body",
        log_type="album_error",
        photo_url="https://img.example.com/cover.jpg",
        parse_mode="MarkdownV2",
    )
    call = mock_post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    payload = call.kwargs["json"]
    assert url.endswith("/sendPhoto")
    assert payload["photo"] == "https://img.example.com/cover.jpg"
    assert payload["caption"] == "caption body"
    assert payload["parse_mode"] == "MarkdownV2"


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_uses_sendmessage_without_photo(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_telegram(
        "body", log_type="album_error", parse_mode="MarkdownV2",
    )
    call = mock_post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    payload = call.kwargs["json"]
    assert url.endswith("/sendMessage")
    assert payload["text"] == "body"
    assert payload["parse_mode"] == "MarkdownV2"


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_truncates_long_caption(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    long_body = "x" * 2000
    notifications.send_telegram(
        long_body, log_type="album_error",
        photo_url="https://i/c.jpg",
    )
    payload = mock_post.call_args.kwargs["json"]
    assert len(payload["caption"]) <= 1024


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_telegram_disable_notification_passthrough(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_telegram(
        "msg", log_type="album_error", disable_notification=True,
    )
    payload = mock_post.call_args.kwargs["json"]
    assert payload["disable_notification"] is True


# --- send_notifications routes telegram-specific body ---


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_notifications_uses_telegram_message_when_provided(
    mock_cfg, mock_post, mock_config
):
    mock_cfg.return_value = mock_config
    notifications.send_notifications(
        "plain fallback",
        log_type="album_error",
        telegram_message="*MD2* body",
        telegram_parse_mode="MarkdownV2",
        photo_url="https://i/c.jpg",
    )
    # Two calls: telegram (sendPhoto) + discord
    assert mock_post.call_count == 2
    tg_payload = mock_post.call_args_list[0].kwargs["json"]
    assert tg_payload["caption"] == "*MD2* body"
    assert tg_payload["parse_mode"] == "MarkdownV2"
    assert tg_payload["photo"] == "https://i/c.jpg"


@patch("notifications.requests.post")
@patch("notifications.load_config")
def test_send_discord_embed_with_url(mock_cfg, mock_post, mock_config):
    mock_cfg.return_value = mock_config
    embed = {"title": "T", "description": "d", "url": "http://l/album/x"}
    notifications.send_discord(
        "msg", log_type="album_error", embed_data=embed,
    )
    payload = mock_post.call_args.kwargs["json"]
    assert payload["embeds"][0]["url"] == "http://l/album/x"
