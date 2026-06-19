from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_token: str
    modmail_forum_id: int
    mod_log_channel_id: int
    staff_application_channel_id: int
    moderator_role_id: int
    admin_role_id: int
    server_stats_channel_id: int = 0
    all_members_stats_channel_id: int = 0
    members_stats_channel_id: int = 0
    bots_stats_channel_id: int = 0
    boosts_stats_channel_id: int = 0
    online_members_stats_channel_id: int = 0
    server_log_channel_id: int = 0
    invite_log_channel_id: int = 0
    verification_log_channel_id: int = 0
    welcome_channel_id: int = 0
    ticket_category_id: int = 0
    ticket_transcript_channel_id: int = 0
    verified_role_id: int = 0
    database_url: str = ""
    anti_raid_enabled: bool = True
    anti_raid_join_threshold: int = 5
    anti_raid_window_seconds: int = 20
    anti_raid_lockdown_minutes: int = 10
    anti_raid_account_age_minutes: int = 30
    anti_raid_timeout_minutes: int = 30
    server_name: str = "Northeast Esports"
    bot_status_text: str = "Guardian of Northeast Esports"
    server_stats_channel_format: str = "members-{total}"


def _require_int(name: str) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        raise RuntimeError(f"Missing required environment variable: {name}")

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be a boolean value like true/false.")


def _get_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        value = default
    else:
        try:
            value = int(raw_value.strip())
        except ValueError as exc:
            raise RuntimeError(f"Environment variable {name} must be an integer.") from exc

    if value < minimum:
        raise RuntimeError(f"Environment variable {name} must be at least {minimum}.")
    return value


def _get_optional_int(name: str) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return 0

    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing required environment variable: DISCORD_TOKEN")

    return Settings(
        discord_token=token,
        modmail_forum_id=_require_int("MODMAIL_FORUM_ID"),
        mod_log_channel_id=_require_int("MOD_LOG_CHANNEL_ID"),
        server_stats_channel_id=_get_optional_int("SERVER_STATS_CHANNEL_ID"),
        all_members_stats_channel_id=_get_optional_int("ALL_MEMBERS_STATS_CHANNEL_ID"),
        members_stats_channel_id=_get_optional_int("MEMBERS_STATS_CHANNEL_ID"),
        bots_stats_channel_id=_get_optional_int("BOTS_STATS_CHANNEL_ID"),
        boosts_stats_channel_id=_get_optional_int("BOOSTS_STATS_CHANNEL_ID"),
        online_members_stats_channel_id=_get_optional_int("ONLINE_MEMBERS_STATS_CHANNEL_ID"),
        server_log_channel_id=_get_optional_int("SERVER_LOG_CHANNEL_ID"),
        invite_log_channel_id=_get_optional_int("INVITE_LOG_CHANNEL_ID"),
        verification_log_channel_id=_get_optional_int("VERIFICATION_LOG_CHANNEL_ID"),
        welcome_channel_id=_get_optional_int("WELCOME_CHANNEL_ID"),
        ticket_category_id=_get_optional_int("TICKET_CATEGORY_ID"),
        ticket_transcript_channel_id=_get_optional_int("TICKET_TRANSCRIPT_CHANNEL_ID"),
        staff_application_channel_id=_require_int("STAFF_APPLICATION_CHANNEL_ID"),
        moderator_role_id=_require_int("MODERATOR_ROLE_ID"),
        admin_role_id=_require_int("ADMIN_ROLE_ID"),
        verified_role_id=_get_optional_int("VERIFIED_ROLE_ID"),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        anti_raid_enabled=_get_bool("ANTI_RAID_ENABLED", True),
        anti_raid_join_threshold=_get_int("ANTI_RAID_JOIN_THRESHOLD", 5, minimum=2),
        anti_raid_window_seconds=_get_int("ANTI_RAID_WINDOW_SECONDS", 20, minimum=5),
        anti_raid_lockdown_minutes=_get_int("ANTI_RAID_LOCKDOWN_MINUTES", 10, minimum=1),
        anti_raid_account_age_minutes=_get_int("ANTI_RAID_ACCOUNT_AGE_MINUTES", 30, minimum=0),
        anti_raid_timeout_minutes=_get_int("ANTI_RAID_TIMEOUT_MINUTES", 30, minimum=1),
        bot_status_text=os.getenv("BOT_STATUS_TEXT", "Guardian of Northeast Esports").strip()
        or "Guardian of Northeast Esports",
        server_stats_channel_format=os.getenv("SERVER_STATS_CHANNEL_FORMAT", "members-{total}").strip() or "members-{total}",
    )
