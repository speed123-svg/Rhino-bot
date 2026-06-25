from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import re
import zipfile
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import discord
import psycopg
from discord import app_commands
from discord.ext import commands, tasks

from config import Settings, load_settings


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
LOGGER = logging.getLogger("rhino_bot")

NO_PERMISSION = "You do not have permission to use this command."
INVALID_DURATION = "Invalid duration format. Use values like 10m, 1h, or 1d."
MODMAIL_THREAD_RE = re.compile(r"^modmail-(?P<user_id>\d+)$")
TICKET_TOPIC_RE = re.compile(
    r"^rhino-ticket owner=(?P<owner_id>\d+) claimed=(?P<claimed_id>\d+) opened=(?P<opened_at>\S+)$"
)
MAX_TIMEOUT_DAYS = 28
MODMAIL_COOLDOWN_SECONDS = 60
MODMAIL_INACTIVITY_HOURS = 72
DM_INTRO_COOLDOWN_SECONDS = 15
DEFAULT_THUMBNAIL_URL = "https://raw.githubusercontent.com/speed123-svg/Rhino-bot/main/assets/lol_bhai_fad_diya.png"
SERVER_INFO_BANNER_PATH = Path("assets/northeast-esports-server-hub.png")
SERVER_INFO_BANNER_FILENAME = "northeast-esports-server-hub.png"
SERVER_INFO_INVITE_URL = "https://discord.gg/zagTby3ugE"
SERVER_INFO_INSTAGRAM_URL = "https://www.instagram.com/hok.ne.india?igsh=OTM3YW8zd2ZidW52"
SERVER_INFO_COMMUNITY_URL = (
    "https://camp.honorofkings.com/h5/app/index.html#/social-circle/home?"
    "open_id=7636819695207311922&current_group_id=1011020"
)
BRAND_FOOTER = "Northeast Esports"
QOTD_ROLE_NAME = "❓QOTD"
AUTOREACT_DATA_PATH = Path("autoreact_data.json")
REACTION_ROLE_DATA_PATH = Path("reaction_roles.json")
NO_LINK_DATA_PATH = Path("no_link_channels.json")
AFK_DATA_PATH = Path("afk_data.json")
PREFIX_DATA_PATH = Path("prefix_data.json")
TICKET_CONFIG_DATA_PATH = Path("ticket_config.json")
AFK_DEFAULT_REASON = "AFK"
AFK_REASON_LIMIT = 200
AFK_MENTION_REPLY_LIMIT = 5
DEFAULT_COMMAND_PREFIX = "!"
MAX_COMMAND_PREFIX_LENGTH = 10
REACTION_ROLE_FIELD_NAME = "Reaction Roles"
REACTION_ROLE_REMOVE_HINT = "Remove your reaction to remove the role."
REACTION_ROLE_GENERIC_DESCRIPTION = "React below to pick up roles."
SERVER_STATS_RENAME_COOLDOWN_SECONDS = 660
CHANNEL_SEND_THROTTLE_SECONDS = 1.1
DISCORD_RATE_LIMIT_STATUS = 429
DISCORD_UNKNOWN_INTERACTION_CODE = 10062
DISCORD_INTERACTION_ACKNOWLEDGED_CODE = 40060
URL_RE = re.compile(
    r"""
    (?:
        \b(?:https?://|www\.)[^\s<>()]+
        |
        \bdiscord(?:app)?\.com/invite/[^\s<>()]+
        |
        \bdiscord\.gg/[^\s<>()]+
        |
        (?<![@\w.-])
        (?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+
        (?:com|net|org|io|gg|co|me|dev|app|xyz|in|us|uk|ca|au|de|jp|fr|ru|br|info|biz|tv|to|ly|link|site|online|store|shop|cloud|ai)
        (?:[/?#][^\s<>()]*)?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class ModmailSession:
    user_id: int
    thread_id: int
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0


@dataclass
class ModLogEntry:
    guild_id: Optional[int]
    action: str
    user_id: int
    moderator_id: int
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_text: Optional[str] = None


@dataclass
class AFKStatus:
    guild_id: int
    user_id: int
    reason: str = AFK_DEFAULT_REASON
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class StaffApplicationDraft:
    selected_role: str
    motivation: str = ""
    relevant_experience: str = ""
    core_competencies: str = ""
    situational_assessment: str = ""
    role_specific_responsibilities: str = ""
    activity_and_availability: str = ""
    decision_making_and_judgment: str = ""
    commitment_and_declaration: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AntiRaidState:
    enabled: bool
    join_events: Deque[datetime] = field(default_factory=deque)
    lockdown_until: Optional[datetime] = None
    manual_lockdown: bool = False
    last_trigger_count: int = 0


@dataclass
class AutoReactionConfig:
    emojis: List[str] = field(default_factory=list)


@dataclass
class ReactionRoleConfig:
    channel_id: int
    message_id: int
    emoji: str
    role_id: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    if days:
        return f"{days} day{'s' if days != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def parse_duration(value: str) -> Optional[timedelta]:
    match = re.fullmatch(r"(\d+)([smhd])", value.strip().lower())
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]


def make_embed(
    title: str,
    description: str,
    color: discord.Color,
    *,
    footer: str = BRAND_FOOTER,
) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=utc_now())
    embed.set_footer(text=footer)
    set_default_thumbnail(embed)
    return embed


def set_default_thumbnail(embed: discord.Embed) -> None:
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL_URL)


def build_embed_send_kwargs(embed: discord.Embed, **kwargs) -> dict:
    return {"embed": embed, **kwargs}


def truncate_text(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def slugify_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"[^\w\s-]", "", normalized, flags=re.UNICODE)
    collapsed = re.sub(r"[-\s]+", "-", cleaned).strip("-")
    return collapsed.lower()


def format_channel_name(value: str, *, uppercase: bool = False) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"[^\w\s-]", "", normalized, flags=re.UNICODE)
    collapsed = re.sub(r"[-\s]+", "-", cleaned).strip("-")
    if uppercase:
        return collapsed.upper()
    return collapsed.lower()


def format_stats_display_name(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    cleaned = re.sub(r"[^\w\s:&-]", "", normalized, flags=re.UNICODE)
    return cleaned.upper()[:100]


def normalize_optional_text(value: str) -> Optional[str]:
    cleaned = value.strip()
    return cleaned or None


def parse_embed_color(value: str) -> Optional[discord.Color]:
    cleaned = value.strip().lower().removeprefix("#")
    if not cleaned:
        return discord.Color.blurple()
    if not re.fullmatch(r"[0-9a-f]{6}", cleaned):
        return None
    return discord.Color(int(cleaned, 16))


def is_valid_image_url(value: str) -> bool:
    return bool(re.fullmatch(r"https?://\S+", value.strip(), re.IGNORECASE))


TOKEN_REFERENCE_RE = re.compile(r"\{([#@&])([^{}]+)\}")


class OpenModmailView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Open Modmail",
                style=discord.ButtonStyle.primary,
                custom_id="modmail:open",
            )
        )


class CloseModmailView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Close Modmail",
                style=discord.ButtonStyle.danger,
                custom_id="modmail:close",
            )
        )


class TicketPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Create Ticket",
                emoji="🎫",
                style=discord.ButtonStyle.success,
                custom_id="ticket:create",
            )
        )


class TicketControlsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Claim",
                emoji="🙋",
                style=discord.ButtonStyle.primary,
                custom_id="ticket:claim",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Transcript",
                emoji="📄",
                style=discord.ButtonStyle.secondary,
                custom_id="ticket:transcript",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Close",
                emoji="🔒",
                style=discord.ButtonStyle.danger,
                custom_id="ticket:close",
            )
        )


class TicketCloseConfirmView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Confirm Close",
                style=discord.ButtonStyle.danger,
                custom_id="ticket:confirm_close",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Cancel",
                style=discord.ButtonStyle.secondary,
                custom_id="ticket:cancel_close",
            )
        )


class VerificationView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Northeast Esports Verification",
                style=discord.ButtonStyle.success,
                custom_id="verification:start",
            )
        )


class ServerInfoView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Get Verified",
                emoji="\u2705",
                style=discord.ButtonStyle.success,
                custom_id="verification:start",
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Invite",
                emoji="\U0001f517",
                style=discord.ButtonStyle.link,
                url=SERVER_INFO_INVITE_URL,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Instagram",
                emoji="\U0001f4f8",
                style=discord.ButtonStyle.link,
                url=SERVER_INFO_INSTAGRAM_URL,
            )
        )
        self.add_item(
            discord.ui.Button(
                label="Community",
                emoji="\U0001f3ae",
                style=discord.ButtonStyle.link,
                url=SERVER_INFO_COMMUNITY_URL,
            )
        )


class StaffApplicationPageOneModal(discord.ui.Modal, title="Referee Application 1/2"):
    discord_username = discord.ui.TextInput(
        label="1. Discord Username",
        style=discord.TextStyle.short,
        placeholder="Enter your Discord username.",
        max_length=100,
    )
    ign = discord.ui.TextInput(
        label="2. IGN",
        style=discord.TextStyle.short,
        placeholder="Enter your in-game name.",
        max_length=100,
    )
    hok_uid = discord.ui.TextInput(
        label="3. Game UID",
        style=discord.TextStyle.short,
        placeholder="Enter your game UID.",
        max_length=100,
    )
    relevant_experience = discord.ui.TextInput(
        label="4. Tournament / Referee Experience",
        style=discord.TextStyle.paragraph,
        placeholder="Share any tournament or referee experience you have.",
        max_length=1000,
    )
    core_competencies = discord.ui.TextInput(
        label="5. Rules Knowledge",
        style=discord.TextStyle.paragraph,
        placeholder="Do you know the competitive rules? Answer Yes/No and add any context if needed.",
        max_length=1000,
    )
    def __init__(self, bot: "RhinoBot", user_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.bot.staff_application_drafts.get(self.user_id)
        if draft is None:
            await interaction.response.send_message(
                "Your application session expired. Please start again from the panel.",
                ephemeral=True,
            )
            return

        draft.motivation = self.discord_username.value
        draft.role_specific_responsibilities = self.ign.value
        draft.situational_assessment = self.hok_uid.value
        draft.relevant_experience = self.relevant_experience.value
        draft.core_competencies = self.core_competencies.value
        await interaction.response.send_message(
            "Page 1 saved. Press `Open Final Page` to finish your referee application.",
            view=StaffApplicationContinueView(interaction.user.id, 2),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Staff application page 1 failed for %s", interaction.user, exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send("The application form failed. Please try again.", ephemeral=True)
        else:
            await interaction.response.send_message("The application form failed. Please try again.", ephemeral=True)


class StaffApplicationPageTwoModal(discord.ui.Modal, title="Referee Application 2/2"):
    skill_summary = discord.ui.TextInput(
        label="6. Skills",
        style=discord.TextStyle.paragraph,
        placeholder="Cover Draft & Ban, Lobby Management, Match Reporting, and Conflict Handling.",
        max_length=1000,
    )
    activity_and_availability = discord.ui.TextInput(
        label="7. Available Days & Time",
        style=discord.TextStyle.paragraph,
        placeholder="List the days and times you are available to referee matches.",
        max_length=1000,
    )
    setup_details = discord.ui.TextInput(
        label="8. Setup",
        style=discord.TextStyle.paragraph,
        placeholder="Share your device, Discord VC readiness (Yes/No), and internet stability (Yes/No).",
        max_length=1000,
    )
    commitment_and_declaration = discord.ui.TextInput(
        label="9. Agreement",
        style=discord.TextStyle.paragraph,
        placeholder="Confirm that you agree to follow all Crimson Cup rules and maintain fair play.",
        max_length=1000,
    )

    def __init__(self, bot: "RhinoBot", user_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.bot.staff_application_drafts.get(self.user_id)
        if draft is None:
            await interaction.response.send_message(
                "Your application session expired. Please start again from the panel.",
                ephemeral=True,
            )
            return

        draft.decision_making_and_judgment = self.skill_summary.value
        draft.activity_and_availability = self.activity_and_availability.value
        draft.selected_role = self.setup_details.value
        draft.commitment_and_declaration = self.commitment_and_declaration.value
        await self.bot.submit_staff_application(interaction, draft)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Staff application page 2 failed for %s", interaction.user, exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send("The application form failed. Please try again.", ephemeral=True)
        else:
            await interaction.response.send_message("The application form failed. Please try again.", ephemeral=True)


class StaffApplicationView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Tournament Referee",
                style=discord.ButtonStyle.success,
                custom_id="staff_application:referee",
            )
        )


class DisabledStaffApplicationView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Tournament Referee",
                style=discord.ButtonStyle.success,
                custom_id="staff_application:referee",
                disabled=True,
            )
        )


class StaffApplicationContinueView(discord.ui.View):
    def __init__(self, user_id: int, next_page: int) -> None:
        super().__init__(timeout=900)
        label = "Open Final Page" if next_page == 2 else f"Open Page {next_page}"
        self.add_item(
            discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.success,
                custom_id=f"staff_application:continue:{next_page}:{user_id}",
            )
        )


class EmbedBuilderModal(discord.ui.Modal, title="Embed Builder"):
    message_content = discord.ui.TextInput(
        label="Message Content",
        style=discord.TextStyle.paragraph,
        placeholder="Use {#channel}, {&role}, {@member} if needed.",
        required=False,
        max_length=2000,
    )
    embed_title = discord.ui.TextInput(
        label="Embed Title",
        placeholder="Optional embed title",
        required=False,
        max_length=256,
    )
    embed_description = discord.ui.TextInput(
        label="Embed Description",
        style=discord.TextStyle.paragraph,
        placeholder="Main embed content. Mentions: {#channel} {&role} {@member}",
        required=False,
        max_length=4000,
    )
    embed_color = discord.ui.TextInput(
        label="Embed Color",
        placeholder="Hex color like #5865F2",
        required=False,
        default="#5865F2",
        max_length=7,
    )
    image_url = discord.ui.TextInput(
        label="Image URL",
        placeholder="Optional https:// image URL",
        required=False,
        max_length=500,
    )

    def __init__(self, bot: "RhinoBot", target_channel: discord.TextChannel) -> None:
        super().__init__()
        self.bot = bot
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        content = normalize_optional_text(self.message_content.value)
        title = normalize_optional_text(self.embed_title.value)
        description = normalize_optional_text(self.embed_description.value)
        image_url = normalize_optional_text(self.image_url.value)
        color = parse_embed_color(self.embed_color.value)

        if color is None:
            await interaction.response.send_message(
                "Please use a valid hex color like `#5865F2`.",
                ephemeral=True,
            )
            return

        if image_url is not None and not is_valid_image_url(image_url):
            await interaction.response.send_message(
                "Please use a valid `http://` or `https://` image URL.",
                ephemeral=True,
            )
            return

        if content is None and title is None and description is None and image_url is None:
            await interaction.response.send_message(
                "Add some message content or embed content before sending.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "This embed builder can only be used inside a server.",
                ephemeral=True,
            )
            return

        content = self.bot.resolve_embed_references(interaction.guild, content)
        title = self.bot.resolve_embed_references(interaction.guild, title)
        description = self.bot.resolve_embed_references(interaction.guild, description)

        embed: Optional[discord.Embed] = None
        if title is not None or description is not None or image_url is not None:
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=utc_now(),
            )
            embed.set_footer(text=BRAND_FOOTER)
            if image_url is not None:
                embed.set_image(url=image_url)

        try:
            send_kwargs = {"content": content}
            if embed is not None:
                send_kwargs["embed"] = embed
            send_kwargs["allowed_mentions"] = discord.AllowedMentions(
                users=False,
                roles=False,
                everyone=False,
            )
            await self.target_channel.send(**send_kwargs)
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I do not have permission to send messages in {self.target_channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            LOGGER.exception("Failed to send embed builder message to channel %s", self.target_channel.id)
            await interaction.response.send_message(
                "I could not send that embed right now. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Embed sent in {self.target_channel.mention}.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Embed builder modal failed for %s", interaction.user, exc_info=error)
        if interaction.response.is_done():
            await interaction.followup.send("The embed builder failed. Please try again.", ephemeral=True)
        else:
            await interaction.response.send_message("The embed builder failed. Please try again.", ephemeral=True)


class RhinoBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True
        intents.presences = True
        intents.message_content = True
        intents.dm_messages = True
        intents.voice_states = True

        super().__init__(
            command_prefix=self.resolve_command_prefix,
            intents=intents,
            help_command=None,
        )

        self.settings = settings
        self.modmail_sessions: Dict[int, ModmailSession] = {}
        self.modmail_cooldowns: Dict[int, datetime] = {}
        self.dm_intro_cooldowns: Dict[int, datetime] = {}
        self.staff_application_drafts: Dict[int, StaffApplicationDraft] = {}
        self.mod_logs: List[ModLogEntry] = []
        self.anti_raid_states: Dict[int, AntiRaidState] = {}
        self.autoreact_configs: Dict[int, Dict[int, AutoReactionConfig]] = {}
        self.reaction_role_configs: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]] = {}
        self.no_link_channels: Dict[int, set[int]] = {}
        self.afk_statuses: Dict[int, Dict[int, AFKStatus]] = {}
        self.command_prefixes: Dict[int, str] = {}
        self.ticket_transcript_channels: Dict[int, int] = {}
        self.server_stats_logged_once = False
        self.server_stats_running = False
        self.stats_channel_last_rename_at: Dict[int, datetime] = {}
        self.channel_send_locks: Dict[int, asyncio.Lock] = {}
        self.channel_last_send_at: Dict[int, datetime] = {}
        self.ticket_creation_locks: Dict[int, asyncio.Lock] = {}
        self.guild_members_chunked: set[int] = set()
        self.previous_server_stats: Dict[int, tuple[Optional[int], Optional[int], int]] = {}
        self.uses_postgres = bool(self.settings.database_url)
        self.modmail_view = OpenModmailView()
        self.close_modmail_view = CloseModmailView()
        self.ticket_panel_view = TicketPanelView()
        self.ticket_controls_view = TicketControlsView()
        self.ticket_close_confirm_view = TicketCloseConfirmView()
        self.verification_view = VerificationView()
        self.staff_application_view = StaffApplicationView()

    async def resolve_command_prefix(self, bot: commands.Bot, message: discord.Message) -> List[str]:
        if message.guild is None:
            return commands.when_mentioned_or(DEFAULT_COMMAND_PREFIX)(bot, message)
        return commands.when_mentioned_or(self.get_guild_prefix(message.guild.id))(bot, message)

    async def setup_hook(self) -> None:
        if self.uses_postgres:
            await asyncio.to_thread(self.ensure_postgres_schema)
        await self.load_prefix_data()
        await self.load_ticket_config_data()
        await self.load_autoreact_data()
        await self.load_reaction_role_data()
        await self.load_no_link_data()
        await self.load_afk_data()
        self.register_commands()
        self.register_prefix_commands()
        self.add_view(self.modmail_view)
        self.add_view(self.close_modmail_view)
        self.add_view(self.ticket_panel_view)
        self.add_view(self.ticket_controls_view)
        self.add_view(self.ticket_close_confirm_view)
        self.add_view(self.verification_view)
        self.add_view(self.staff_application_view)
        self.cleanup_inactive_modmail.start()
        self.server_stats_loop.start()

    def ensure_postgres_schema(self) -> None:
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS mod_logs (
                            id BIGSERIAL PRIMARY KEY,
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            moderator_id BIGINT NOT NULL,
                            action TEXT NOT NULL,
                            reason TEXT NOT NULL,
                            duration_text TEXT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE INDEX IF NOT EXISTS mod_logs_guild_user_created_idx
                        ON mod_logs (guild_id, user_id, created_at DESC)
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS autoreact_configs (
                            guild_id BIGINT NOT NULL,
                            channel_id BIGINT NOT NULL,
                            emojis TEXT[] NOT NULL,
                            PRIMARY KEY (guild_id, channel_id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS reaction_role_configs (
                            guild_id BIGINT NOT NULL,
                            channel_id BIGINT NOT NULL,
                            message_id BIGINT NOT NULL,
                            emoji TEXT NOT NULL,
                            role_id BIGINT NOT NULL,
                            PRIMARY KEY (guild_id, message_id, emoji)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS no_link_channels (
                            guild_id BIGINT NOT NULL,
                            channel_id BIGINT NOT NULL,
                            PRIMARY KEY (guild_id, channel_id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS afk_statuses (
                            guild_id BIGINT NOT NULL,
                            user_id BIGINT NOT NULL,
                            reason TEXT NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            PRIMARY KEY (guild_id, user_id)
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS command_prefixes (
                            guild_id BIGINT PRIMARY KEY,
                            prefix TEXT NOT NULL
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS ticket_configs (
                            guild_id BIGINT PRIMARY KEY,
                            transcript_channel_id BIGINT NOT NULL
                        )
                        """
                    )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to ensure PostgreSQL schema.")

    async def on_ready(self) -> None:
        synced = await self.tree.sync()
        if self.user is not None:
            activity = discord.CustomActivity(name=self.settings.bot_status_text)
            await self.change_presence(status=discord.Status.idle, activity=activity)
        LOGGER.info("Bot online as %s (%s)", self.user, self.user.id if self.user else "unknown")
        LOGGER.info("Synced %s application commands", len(synced))
        await self.ensure_guild_member_caches()
        await self.validate_runtime_configuration()
        if not self.server_stats_logged_once:
            self.server_stats_logged_once = True
            await self.log_all_server_stats()

    async def ensure_guild_member_caches(self) -> None:
        for guild in self.guilds:
            if guild.id in self.guild_members_chunked:
                continue
            expected_members = guild.member_count or 0
            try:
                if not guild.chunked or (expected_members and len(guild.members) < expected_members):
                    await guild.chunk(cache=True)
                self.guild_members_chunked.add(guild.id)
                LOGGER.info(
                    "Guild member cache ready for %s (%s): cached=%s expected=%s",
                    guild.name,
                    guild.id,
                    len(guild.members),
                    expected_members or "unknown",
                )
            except discord.HTTPException:
                LOGGER.exception("Failed to chunk guild members for %s (%s)", guild.name, guild.id)

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        LOGGER.error("Application command failed", exc_info=error)
        await self.send_interaction_message(
            interaction,
            "An unexpected error occurred while running that command.",
            ephemeral=True,
        )

    async def on_command_error(self, context: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return

        LOGGER.error("Prefix command failed", exc_info=error)
        try:
            await context.send("An unexpected error occurred while running that command.")
        except discord.HTTPException:
            LOGGER.warning("Failed to send prefix command error message in channel %s", context.channel.id)

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:
        LOGGER.exception("Unhandled Discord event error in %s", event_method)

    def is_discord_rate_limit(self, error: discord.HTTPException) -> bool:
        return getattr(error, "status", None) == DISCORD_RATE_LIMIT_STATUS or "rate limited" in str(error).lower()

    def is_interaction_response_error(self, error: discord.HTTPException) -> bool:
        return getattr(error, "code", None) in {
            DISCORD_UNKNOWN_INTERACTION_CODE,
            DISCORD_INTERACTION_ACKNOWLEDGED_CODE,
        }

    def log_interaction_response_failure(self, interaction: discord.Interaction, error: discord.HTTPException) -> None:
        if self.is_interaction_response_error(error) or self.is_discord_rate_limit(error):
            LOGGER.warning(
                "Could not respond to interaction %s for %s: %s",
                interaction.id,
                interaction.user,
                error,
            )
            return
        LOGGER.exception("Could not respond to interaction %s for %s", interaction.id, interaction.user)

    async def send_interaction_message(
        self,
        interaction: discord.Interaction,
        content: Optional[str] = None,
        *,
        embed: Optional[discord.Embed] = None,
        ephemeral: bool = True,
        **kwargs,
    ) -> bool:
        send_kwargs = {"ephemeral": ephemeral, **kwargs}
        if embed is not None:
            send_kwargs = build_embed_send_kwargs(embed, **send_kwargs)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, **send_kwargs)
            else:
                await interaction.response.send_message(content, **send_kwargs)
            return True
        except discord.HTTPException as error:
            if getattr(error, "code", None) == DISCORD_INTERACTION_ACKNOWLEDGED_CODE and not interaction.response.is_done():
                try:
                    await interaction.followup.send(content, **send_kwargs)
                    return True
                except discord.HTTPException as followup_error:
                    error = followup_error
            self.log_interaction_response_failure(interaction, error)
            return False

    async def defer_interaction_once(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = True,
        thinking: bool = False,
    ) -> bool:
        if interaction.response.is_done():
            return True
        try:
            await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
            return True
        except discord.HTTPException as error:
            if getattr(error, "code", None) == DISCORD_INTERACTION_ACKNOWLEDGED_CODE:
                return True
            self.log_interaction_response_failure(interaction, error)
            return False

    async def safe_send_embed(
        self,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        label: str,
    ) -> Optional[discord.Message]:
        channel_id = getattr(channel, "id", None)
        if isinstance(channel_id, int):
            lock = self.channel_send_locks.setdefault(channel_id, asyncio.Lock())
            async with lock:
                last_send_at = self.channel_last_send_at.get(channel_id)
                if last_send_at is not None:
                    elapsed_seconds = (utc_now() - last_send_at).total_seconds()
                    wait_seconds = CHANNEL_SEND_THROTTLE_SECONDS - elapsed_seconds
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)
                message = await self._send_embed_without_throttle(channel, embed, label)
                self.channel_last_send_at[channel_id] = utc_now()
                return message

        return await self._send_embed_without_throttle(channel, embed, label)

    async def _send_embed_without_throttle(
        self,
        channel: discord.abc.Messageable,
        embed: discord.Embed,
        label: str,
    ) -> Optional[discord.Message]:
        try:
            return await channel.send(**build_embed_send_kwargs(embed))
        except discord.HTTPException as error:
            channel_id = getattr(channel, "id", "unknown")
            if self.is_discord_rate_limit(error):
                LOGGER.warning("%s skipped because Discord rate-limited channel %s: %s", label, channel_id, error)
            else:
                LOGGER.exception("%s failed for channel %s", label, channel_id)
            return None

    async def on_message(self, message: discord.Message) -> None:
        if isinstance(message.channel, discord.DMChannel):
            if message.author.bot:
                return
            LOGGER.info("DM received from %s (%s): %s", message.author, message.author.id, message.content or "[no text]")
            await self.handle_user_dm(message)
            return

        if message.guild is not None:
            await self.handle_autoreactions(message)

        if message.author.bot:
            return

        if message.guild is not None and isinstance(message.author, discord.Member):
            await self.clear_afk_on_message(message)

        if message.guild is not None:
            if await self.handle_no_link_message(message):
                return

        if message.guild is not None and isinstance(message.author, discord.Member):
            await self.handle_afk_mentions(message)

        context = await self.get_context(message)
        if context.valid:
            await self.invoke(context)
            return

        if isinstance(message.channel, discord.Thread):
            await self.handle_moderator_reply(message)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self.handle_reaction_role_payload(payload, add_role=True)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self.handle_reaction_role_payload(payload, add_role=False)

    async def on_member_join(self, member: discord.Member) -> None:
        if member.guild is None:
            return
        await self.log_member_join(member)
        await self.handle_anti_raid_join(member)
        await self.send_welcome_message(member)

    async def on_member_remove(self, member: discord.Member) -> None:
        await self.log_member_leave(member)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        await self.log_member_profile_update(before, after)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await self.log_voice_state_update(member, before, after)

    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log_member_ban(guild, user)

    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self.log_member_unban(guild, user)

    async def on_message_delete(self, message: discord.Message) -> None:
        await self.log_message_delete(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        await self.log_message_edit(before, after)
        if after.author.bot:
            return
        await self.handle_no_link_message(after)

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_channel_event("Channel Created", channel, discord.Color.green())

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        await self.log_channel_update(before, after)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.log_channel_event("Channel Deleted", channel, discord.Color.red())

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self.log_role_event("Role Created", role, discord.Color.green())

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.log_role_event("Role Deleted", role, discord.Color.red())

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        await self.log_role_update(before, after)

    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: List[discord.Emoji],
        after: List[discord.Emoji],
    ) -> None:
        await self.log_emoji_update(guild, before, after)

    async def on_invite_create(self, invite: discord.Invite) -> None:
        await self.log_invite_create(invite)

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        await self.log_invite_delete(invite)

    async def on_bulk_message_delete(self, messages: List[discord.Message]) -> None:
        await self.log_bulk_message_delete(messages)

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is discord.InteractionType.component:
            custom_id = getattr(interaction.data, "get", lambda _key, _default=None: None)("custom_id")
            if custom_id == "modmail:open":
                LOGGER.info(
                    "Open Modmail button clicked by %s (%s) in %s",
                    interaction.user,
                    interaction.user.id,
                    "guild" if interaction.guild_id else "dm",
                )
                await self.open_modmail_from_button(interaction)
                return
            if custom_id == "modmail:close":
                LOGGER.info(
                    "Close Modmail button clicked by %s (%s) in %s",
                    interaction.user,
                    interaction.user.id,
                    "guild" if interaction.guild_id else "dm",
                )
                await self.close_modmail_from_button(interaction)
                return
            if custom_id == "ticket:create":
                await self.create_ticket_from_button(interaction)
                return
            if custom_id == "ticket:claim":
                await self.claim_ticket_from_button(interaction)
                return
            if custom_id == "ticket:transcript":
                await self.send_ticket_transcript(interaction)
                return
            if custom_id == "ticket:close":
                await self.request_ticket_close(interaction)
                return
            if custom_id == "ticket:confirm_close":
                await self.close_ticket_from_button(interaction)
                return
            if custom_id == "ticket:cancel_close":
                await self.cancel_ticket_close(interaction)
                return
            if custom_id == "staff_application:referee":
                LOGGER.info("Tournament referee application opened by %s (%s)", interaction.user, interaction.user.id)
                self.staff_application_drafts[interaction.user.id] = StaffApplicationDraft(selected_role="Tournament Referee")
                await interaction.response.send_modal(StaffApplicationPageOneModal(self, interaction.user.id))
                return
            if custom_id and custom_id.startswith("staff_application:continue:"):
                await self.handle_staff_application_continue(interaction, custom_id)
                return
            if custom_id == "staff_application:open":
                await self.send_interaction_message(
                    interaction,
                    "This referee application panel is outdated. Please use a newly posted panel.",
                    ephemeral=True,
                )
                return
            if custom_id == "verification:start":
                LOGGER.info("Verification button clicked by %s (%s)", interaction.user, interaction.user.id)
                await self.handle_verification_button(interaction)
                return

    def register_commands(self) -> None:
        tree = self.tree

        @tree.command(name="help", description="Show the available moderation and modmail commands")
        async def help_command(interaction: discord.Interaction) -> None:
            embed = discord.Embed(
                title="Northeast Esports Bot Help",
                description="Moderation and modmail tools available in this server.",
                color=discord.Color.blurple(),
                timestamp=utc_now(),
            )
            embed.add_field(
                name="Moderation",
                value=(
                    "`/warn` warn a member\n"
                    "`/mute` timeout a member\n"
                    "`/unmute` remove a member timeout\n"
                    "`/kick` kick a member\n"
                    "`/ban` ban a member\n"
                    "`/unban` unban by user ID\n"
                    "`/addrole` add a role to a member\n"
                    "`/removerole` remove a role from a member\n"
                    "`/role add` or `/role remove` manage member roles\n"
                    "`/clear` bulk delete messages\n"
                    "`/modlogs` view moderation history"
                ),
                inline=False,
            )
            embed.add_field(
                name="Modmail",
                value="DM the bot and press `Open Modmail`. Staff can close active threads with the `Close Modmail` button.",
                inline=False,
            )
            embed.add_field(
                name="Tickets",
                value=(
                    "`/ticket panel` post the ticket panel\n"
                    "`/ticket add` or `/ticket remove` manage participants\n"
                    "`/ticket claim`, `/ticket transcript`, and `/ticket close` manage an active ticket\n"
                    "`/ticket setlog` lets an administrator choose the transcript log channel"
                ),
                inline=False,
            )
            embed.add_field(
                name="Referee Application",
                value=(
                    "`/staffapplypanel post` post the referee application panel\n"
                    "`/staffapplypanel disable` disable the active referee application panel in a channel\n"
                    "Members can press the button to start the 2-page referee application form"
                ),
                inline=False,
            )
            embed.add_field(
                name="Verification",
                value="`/verificationpanel` post the Northeast Esports verification panel.",
                inline=False,
            )
            embed.add_field(
                name="Community",
                value=(
                    "`/afk` set yourself as away until you send a message again\n"
                    "`/prefix show` view the command prefix\n"
                    "`/prefix set` or `/prefix reset` manage prefix commands"
                ),
                inline=False,
            )
            embed.add_field(
                name="Anti-Raid",
                value=(
                    "`/antiraid status` show protection status\n"
                    "`/antiraid on` or `/antiraid off` enable or disable monitoring\n"
                    "`/antiraid activate` or `/antiraid deactivate` control raid mode manually"
                ),
                inline=False,
            )
            embed.add_field(
                name="Embeds & Info",
                value=(
                    "`/embed` open a modal to build and send an embed message.\n"
                    "`/serverinfo post` post the modern server-info hub."
                ),
                inline=False,
            )
            embed.add_field(
                name="Auto-Reactions",
                value=(
                    "`/autoreact activate` react to every message in a channel\n"
                    "`/autoreact deactivate` turn off auto-reactions in a channel"
                ),
                inline=False,
            )
            embed.add_field(
                name="Reaction Roles",
                value=(
                    "`/reactionrole create` post a new reaction-role panel\n"
                    "`/reactionrole add` bind a role to an existing message reaction\n"
                    "`/reactionrole remove` delete a reaction-role binding\n"
                    "`/reactionrole list` show active reaction roles"
                ),
                inline=False,
            )
            embed.add_field(
                name="QOTD",
                value="`/qotd` post a Question of the Day, ping the QOTD role, and open a reply thread",
                inline=False,
            )
            embed.add_field(
                name="No-Link Channels",
                value=(
                    "`/nolink activate` delete link messages in a selected channel\n"
                    "`/nolink deactivate` turn off link blocking in a selected channel"
                ),
                inline=False,
            )
            set_default_thumbnail(embed)
            await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

        @tree.command(name="warn", description="Warn a member")
        @app_commands.describe(user="Member to warn", reason="Reason for the warning")
        async def warn(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None) -> None:
            await self.handle_warn(interaction, user, reason or "No reason provided")

        @tree.command(name="mute", description="Timeout a member")
        @app_commands.describe(user="Member to timeout", duration="Duration like 10m, 1h, 1d", reason="Reason for the timeout")
        async def mute(
            interaction: discord.Interaction,
            user: discord.Member,
            duration: str,
            reason: Optional[str] = None,
        ) -> None:
            await self.handle_mute(interaction, user, duration, reason or "No reason provided")

        @tree.command(name="unmute", description="Remove a member timeout")
        @app_commands.describe(user="Member to unmute", reason="Reason for removing the timeout")
        async def unmute(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None) -> None:
            await self.handle_unmute(interaction, user, reason or "No reason provided")

        @tree.command(name="kick", description="Kick a member")
        @app_commands.describe(user="Member to kick", reason="Reason for the kick")
        async def kick(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = None) -> None:
            await self.handle_kick(interaction, user, reason or "No reason provided")

        @tree.command(name="ban", description="Ban a member")
        @app_commands.describe(user="Member to ban", reason="Reason for the ban", delete_days="Delete up to 7 days of messages")
        async def ban(
            interaction: discord.Interaction,
            user: discord.Member,
            reason: Optional[str] = None,
            delete_days: app_commands.Range[int, 0, 7] = 0,
        ) -> None:
            await self.handle_ban(interaction, user, reason or "No reason provided", delete_days)

        @tree.command(name="unban", description="Unban a user by ID")
        @app_commands.describe(user_id="The user ID to unban", reason="Reason for the unban")
        async def unban(interaction: discord.Interaction, user_id: str, reason: Optional[str] = None) -> None:
            await self.handle_unban(interaction, user_id, reason or "No reason provided")

        @tree.command(name="addrole", description="Add a role to a member")
        @app_commands.describe(user="Member to update", role="Role to add", reason="Reason for adding the role")
        async def addrole(
            interaction: discord.Interaction,
            user: discord.Member,
            role: discord.Role,
            reason: Optional[str] = None,
        ) -> None:
            await self.handle_role_add(interaction, user, role, reason or "No reason provided")

        @tree.command(name="removerole", description="Remove a role from a member")
        @app_commands.describe(user="Member to update", role="Role to remove", reason="Reason for removing the role")
        async def removerole(
            interaction: discord.Interaction,
            user: discord.Member,
            role: discord.Role,
            reason: Optional[str] = None,
        ) -> None:
            await self.handle_role_remove(interaction, user, role, reason or "No reason provided")

        role_group = app_commands.Group(name="role", description="Add or remove member roles")

        @role_group.command(name="add", description="Add a role to a member")
        @app_commands.describe(user="Member to update", role="Role to add", reason="Reason for adding the role")
        async def role_add(
            interaction: discord.Interaction,
            user: discord.Member,
            role: discord.Role,
            reason: Optional[str] = None,
        ) -> None:
            await self.handle_role_add(interaction, user, role, reason or "No reason provided", command_name="/role add")

        @role_group.command(name="remove", description="Remove a role from a member")
        @app_commands.describe(user="Member to update", role="Role to remove", reason="Reason for removing the role")
        async def role_remove(
            interaction: discord.Interaction,
            user: discord.Member,
            role: discord.Role,
            reason: Optional[str] = None,
        ) -> None:
            await self.handle_role_remove(interaction, user, role, reason or "No reason provided", command_name="/role remove")

        @tree.command(name="clear", description="Bulk delete recent messages")
        @app_commands.describe(amount="How many recent messages to remove", user="Only remove messages from this user")
        async def clear(
            interaction: discord.Interaction,
            amount: app_commands.Range[int, 1, 1000],
            user: Optional[discord.Member] = None,
        ) -> None:
            await self.handle_clear(interaction, amount, user)

        @tree.command(name="modlogs", description="Show recent moderation entries for a user")
        @app_commands.describe(user="Member to inspect")
        async def modlogs(interaction: discord.Interaction, user: discord.User) -> None:
            await self.handle_modlogs(interaction, user)

        @tree.command(name="afk", description="Set yourself as AFK until you send a message")
        @app_commands.describe(reason="Optional reason to show when someone mentions you")
        async def afk(interaction: discord.Interaction, reason: Optional[str] = None) -> None:
            await self.handle_afk(interaction, reason)

        prefix_group = app_commands.Group(name="prefix", description="Manage the server command prefix")

        @prefix_group.command(name="show", description="Show this server's command prefix")
        async def prefix_show(interaction: discord.Interaction) -> None:
            await self.handle_prefix_show(interaction)

        @prefix_group.command(name="set", description="Set this server's command prefix")
        @app_commands.describe(prefix=f"New prefix, up to {MAX_COMMAND_PREFIX_LENGTH} characters")
        async def prefix_set(interaction: discord.Interaction, prefix: str) -> None:
            await self.handle_prefix_set(interaction, prefix)

        @prefix_group.command(name="reset", description="Reset this server's command prefix")
        async def prefix_reset(interaction: discord.Interaction) -> None:
            await self.handle_prefix_reset(interaction)

        staffapplypanel = app_commands.Group(
            name="staffapplypanel",
            description="Manage the referee application panel",
        )

        @staffapplypanel.command(name="post", description="Post the referee application panel")
        @app_commands.describe(channel="Channel where the referee application panel should be posted")
        async def staffapplypanel_post(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_staff_apply_panel(interaction, channel)

        @staffapplypanel.command(name="disable", description="Disable the referee application panel in a channel")
        @app_commands.describe(channel="Channel containing the referee application panel")
        async def staffapplypanel_disable(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_staff_apply_panel_disable(interaction, channel)

        tree.add_command(staffapplypanel)

        @tree.command(name="verificationpanel", description="Post the Northeast Esports verification panel")
        @app_commands.describe(channel="Channel where the verification panel should be posted")
        async def verificationpanel(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_verification_panel(interaction, channel)

        serverinfo = app_commands.Group(
            name="serverinfo",
            description="Post modern server information panels",
        )

        @serverinfo.command(name="post", description="Post the server-info hub")
        @app_commands.describe(channel="Channel where the server-info hub should be posted")
        async def serverinfo_post(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_server_info_post(interaction, channel)

        tree.add_command(serverinfo)

        @tree.command(name="embed", description="Open an embed builder and send it to a channel")
        @app_commands.describe(channel="Channel where the embed should be posted")
        async def embed(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_embed_builder(interaction, channel)

        @tree.command(name="qotd", description="Post a Question of the Day and open a reply thread")
        @app_commands.describe(
            question="The Question of the Day text",
            channel="Channel where the QOTD should be posted",
            auto_archive_hours="How long until the thread auto-archives",
        )
        async def qotd(
            interaction: discord.Interaction,
            question: str,
            channel: Optional[discord.TextChannel] = None,
            auto_archive_hours: app_commands.Range[int, 1, 168] = 24,
        ) -> None:
            await self.handle_qotd(interaction, question, channel, auto_archive_hours)

        autoreact = app_commands.Group(name="autoreact", description="Manage automatic message reactions")

        @autoreact.command(name="activate", description="React to every message in a channel")
        @app_commands.describe(
            emoji="One or more emojis, separated by commas, like 🔥,❤️,👍",
            channel="Channel where the bot should auto-react",
        )
        async def autoreact_activate(
            interaction: discord.Interaction,
            emoji: str,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_autoreact_activate(interaction, emoji, channel)

        @autoreact.command(name="deactivate", description="Turn off auto-reactions in a channel")
        @app_commands.describe(channel="Channel where the bot should stop auto-reacting")
        async def autoreact_deactivate(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_autoreact_deactivate(interaction, channel)

        reaction_role = app_commands.Group(name="reactionrole", description="Manage reaction role messages")

        @reaction_role.command(name="create", description="Post a new reaction-role panel")
        @app_commands.describe(
            role="Role members receive when they react",
            emoji="Emoji members should react with",
            channel="Channel where the panel should be posted",
            title="Optional panel title",
            description="Optional panel text",
        )
        async def reactionrole_create(
            interaction: discord.Interaction,
            role: discord.Role,
            emoji: str,
            channel: Optional[discord.TextChannel] = None,
            title: Optional[str] = None,
            description: Optional[str] = None,
        ) -> None:
            await self.handle_reaction_role_create(interaction, role, emoji, channel, title, description)

        @reaction_role.command(name="add", description="Bind a role to a reaction on an existing message")
        @app_commands.describe(
            message_id="Message ID or message link to bind",
            emoji="Emoji members should react with",
            role="Role members receive when they react",
            channel="Channel containing the message",
        )
        async def reactionrole_add(
            interaction: discord.Interaction,
            message_id: str,
            emoji: str,
            role: discord.Role,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_reaction_role_add(interaction, message_id, emoji, role, channel)

        @reaction_role.command(name="remove", description="Remove a reaction-role binding")
        @app_commands.describe(
            message_id="Message ID or message link to unbind",
            emoji="Emoji to unbind",
        )
        async def reactionrole_remove(
            interaction: discord.Interaction,
            message_id: str,
            emoji: str,
        ) -> None:
            await self.handle_reaction_role_remove(interaction, message_id, emoji)

        @reaction_role.command(name="list", description="Show active reaction-role bindings")
        async def reactionrole_list(interaction: discord.Interaction) -> None:
            await self.handle_reaction_role_list(interaction)

        no_link = app_commands.Group(name="nolink", description="Manage link blocking in channels")

        @no_link.command(name="activate", description="Delete link messages in a channel")
        @app_commands.describe(channel="Channel where links should be blocked")
        async def nolink_activate(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_no_link_activate(interaction, channel)

        @no_link.command(name="deactivate", description="Allow links again in a channel")
        @app_commands.describe(channel="Channel where link blocking should stop")
        async def nolink_deactivate(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_no_link_deactivate(interaction, channel)

        anti_raid = app_commands.Group(name="antiraid", description="Manage anti-raid protection")

        @anti_raid.command(name="status", description="Show anti-raid status for this server")
        async def antiraid_status(interaction: discord.Interaction) -> None:
            await self.handle_antiraid_status(interaction)

        @anti_raid.command(name="on", description="Enable anti-raid monitoring")
        async def antiraid_on(interaction: discord.Interaction) -> None:
            await self.handle_antiraid_toggle(interaction, True)

        @anti_raid.command(name="off", description="Disable anti-raid monitoring")
        async def antiraid_off(interaction: discord.Interaction) -> None:
            await self.handle_antiraid_toggle(interaction, False)

        @anti_raid.command(name="activate", description="Manually activate raid mode now")
        async def antiraid_activate(interaction: discord.Interaction) -> None:
            await self.handle_antiraid_activate(interaction)

        @anti_raid.command(name="deactivate", description="Manually turn off active raid mode")
        async def antiraid_deactivate(interaction: discord.Interaction) -> None:
            await self.handle_antiraid_deactivate(interaction)

        ticket = app_commands.Group(name="ticket", description="Create and manage private support tickets")

        @ticket.command(name="panel", description="Post the ticket creation panel")
        @app_commands.describe(channel="Channel where the ticket panel should be posted")
        async def ticket_panel(
            interaction: discord.Interaction,
            channel: Optional[discord.TextChannel] = None,
        ) -> None:
            await self.handle_ticket_panel(interaction, channel)

        @ticket.command(name="add", description="Add a member to this ticket")
        @app_commands.describe(user="Member who should be able to access this ticket")
        async def ticket_add(interaction: discord.Interaction, user: discord.Member) -> None:
            await self.handle_ticket_participant(interaction, user, add=True)

        @ticket.command(name="remove", description="Remove a member from this ticket")
        @app_commands.describe(user="Member whose ticket access should be removed")
        async def ticket_remove(interaction: discord.Interaction, user: discord.Member) -> None:
            await self.handle_ticket_participant(interaction, user, add=False)

        @ticket.command(name="claim", description="Claim this ticket as a staff member")
        async def ticket_claim(interaction: discord.Interaction) -> None:
            await self.claim_ticket_from_button(interaction)

        @ticket.command(name="transcript", description="Export this ticket as an HTML transcript")
        async def ticket_transcript(interaction: discord.Interaction) -> None:
            await self.send_ticket_transcript(interaction)

        @ticket.command(name="close", description="Save a transcript and close this ticket")
        @app_commands.describe(reason="Reason for closing the ticket")
        async def ticket_close(interaction: discord.Interaction, reason: Optional[str] = None) -> None:
            await self.close_ticket_from_command(interaction, reason or "Issue resolved")

        @ticket.command(name="setlog", description="Set the channel where closed-ticket transcripts are saved")
        @app_commands.describe(channel="Private staff channel that should receive ticket transcripts")
        async def ticket_setlog(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
            await self.handle_ticket_set_log_channel(interaction, channel)

        tree.add_command(anti_raid)
        tree.add_command(ticket)
        tree.add_command(autoreact)
        tree.add_command(reaction_role)
        tree.add_command(no_link)
        tree.add_command(prefix_group)
        tree.add_command(role_group)

    def register_prefix_commands(self) -> None:
        if self.get_command("help") is not None:
            return

        @commands.command(name="help")
        async def help_prefix(context: commands.Context) -> None:
            await self.handle_prefix_help(context)

        @commands.command(name="afk")
        async def afk_prefix(context: commands.Context, *, reason: Optional[str] = None) -> None:
            await self.handle_afk_prefix(context, reason)

        @commands.command(name="prefix")
        async def prefix_prefix(
            context: commands.Context,
            action: Optional[str] = None,
            *,
            prefix: Optional[str] = None,
        ) -> None:
            await self.handle_prefix_command(context, action, prefix)

        self.add_command(help_prefix)
        self.add_command(afk_prefix)
        self.add_command(prefix_prefix)

    def has_staff_access(self, member: discord.Member, permission: str) -> bool:
        if member.guild_permissions.administrator:
            return True

        role_ids = {role.id for role in member.roles}
        if self.settings.admin_role_id in role_ids or self.settings.moderator_role_id in role_ids:
            return True

        return getattr(member.guild_permissions, permission)

    def has_admin_access(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        return any(role.id == self.settings.admin_role_id for role in member.roles)

    def can_act_on_target(self, moderator: discord.Member, target: discord.Member) -> Optional[str]:
        if moderator.id == target.id:
            return "You cannot moderate yourself."
        if target.bot:
            return "You cannot use this moderation command on a bot."
        if target.guild.owner_id == target.id:
            return "You cannot moderate the server owner."
        if moderator.guild.owner_id != moderator.id and target.top_role >= moderator.top_role:
            return "You cannot moderate a member with an equal or higher role."
        me = target.guild.me
        if me is None:
            return "I could not verify my own server role."
        if target.top_role >= me.top_role:
            return "I cannot moderate that member because their role is higher than or equal to mine."
        return None

    def can_manage_role(self, moderator: discord.Member, role: discord.Role) -> Optional[str]:
        if role == moderator.guild.default_role:
            return "You cannot add or remove the default @everyone role."
        if role.managed:
            return "That role is managed by an integration and cannot be changed manually."
        if moderator.guild.owner_id != moderator.id and role >= moderator.top_role:
            return "You cannot manage a role that is equal to or higher than your top role."
        me = moderator.guild.me
        if me is None:
            return "I could not verify my own server role."
        if role >= me.top_role:
            return "I cannot manage that role because it is higher than or equal to my top role."
        return None

    def create_modmail_intro_embed(self) -> discord.Embed:
        return make_embed(
            "Support Desk",
            (
                f"Welcome to **{self.settings.server_name}**.\n\n"
                "If you need assistance, please use **Open Modmail** to contact the moderation team privately.\n\n"
                "This system can be used for reports, appeals, rule clarifications, or safety-related concerns.\n\n"
                "All moderator replies will be sent here in direct messages."
            ),
            discord.Color.purple(),
        )

    def create_modmail_thread_embed(self, user: discord.abc.User, reason: str) -> discord.Embed:
        embed = discord.Embed(
            title="New Modmail Thread",
            color=discord.Color.purple(),
            timestamp=utc_now(),
        )
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Opened", value=reason, inline=False)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=BRAND_FOOTER)
        return embed

    def create_staff_application_panel_embed(self) -> discord.Embed:
        embed = make_embed(
            "Northeast Esports",
            (
                "**Crimson Cup - Referee Application**\n\n"
                "Apply to become a tournament referee for Crimson Cup.\n\n"
                "Press the button below and fill out the form in 2 pages. "
                "Your application will be sent privately to the review team."
            ),
            discord.Color.gold(),
        )
        embed.add_field(
            name="Application Sections",
            value=(
                "1. Discord Username\n"
                "2. IGN\n"
                "3. Game UID\n"
                "4. Tournament / Referee Experience\n"
                "5. Knowledge of Competitive Rules\n"
                "6. Skills\n"
                "7. Available Days & Time\n"
                "8. Setup\n"
                "9. Agreement"
            ),
            inline=False,
        )
        embed.add_field(
            name="Before You Apply",
            value="Be honest, give complete answers, and keep your DMs open in case tournament staff contact you.",
            inline=False,
        )
        return embed

    def create_verification_panel_embed(self, guild: discord.Guild) -> discord.Embed:
        verified_role = self.get_verified_role(guild)
        verified_role_text = verified_role.mention if verified_role is not None else "@Verified"
        return make_embed(
            "Northeast Esports Verification",
            (
                "Welcome! To unlock full access to the server, simply complete a quick verification.\n\n"
                "**How It Works**\n"
                "- Tap the Northeast Esports Verification button below\n"
                "- Verification will be completed instantly\n\n"
                "**After Verification**\n"
                f"- You will receive the {verified_role_text} role\n"
                "- Full access to all channels and features will be unlocked\n\n"
                "**Note**\n"
                "- Do not spam the button\n"
                "- Contact staff if you face any issues\n\n"
                "Tap the button below to get verified.\n\n"
                "Quick | Simple | Secure"
            ),
            discord.Color.green(),
            footer="Northeast Esports - Verification",
        )

    def get_verified_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        if self.settings.verified_role_id:
            role = guild.get_role(self.settings.verified_role_id)
            if role is not None:
                return role

        return discord.utils.get(guild.roles, name="Verified")

    def find_text_channel_by_name(self, guild: discord.Guild, channel_name: str) -> Optional[discord.TextChannel]:
        normalized_target = channel_name.strip().lower().lstrip("#")
        for channel in guild.text_channels:
            if channel.name.lower() == normalized_target:
                return channel
        return None

    def format_channel_reference(self, guild: discord.Guild, channel_name: str) -> str:
        channel = self.find_text_channel_by_name(guild, channel_name)
        return channel.mention if channel is not None else f"#{channel_name.lstrip('#')}"

    def find_role_by_name(self, guild: discord.Guild, role_name: str) -> Optional[discord.Role]:
        normalized_target = role_name.strip().lower().lstrip("@&")
        for role in guild.roles:
            if role.name.lower() == normalized_target:
                return role
        return None

    def create_server_info_banner_file(self) -> Optional[discord.File]:
        if not SERVER_INFO_BANNER_PATH.is_file():
            return None
        return discord.File(SERVER_INFO_BANNER_PATH, filename=SERVER_INFO_BANNER_FILENAME)

    def create_server_info_embed(self, title: str, description: str, color: int) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color(color),
            timestamp=utc_now(),
        )
        embed.set_footer(text=BRAND_FOOTER)
        return embed

    def create_server_info_level_lines(self, guild: discord.Guild) -> str:
        level_roles = (
            ("Level I", 1000),
            ("Level II", 10000),
            ("Level III", 30000),
            ("Level IV", 50000),
            ("Level V", 75000),
            ("Level VI", 100000),
            ("Level VII", 150000),
            ("Level VIII", 175000),
            ("Level IX", 200000),
            ("Level X", 300000),
        )
        lines = []
        for index, (role_name, score) in enumerate(level_roles, start=1):
            role = self.find_role_by_name(guild, role_name)
            role_text = role.mention if role is not None else f"@{role_name}"
            lines.append(f"`{index:02}` {role_text} - **{score:,}** score")
        return "\n".join(lines)

    def create_server_info_embeds(self, guild: discord.Guild) -> List[discord.Embed]:
        verify_channel = self.format_channel_reference(guild, "verify")
        intro_channel = self.format_channel_reference(guild, "intro")
        general_chat_channel = self.format_channel_reference(guild, "general-chat")
        announcements_channel = self.format_channel_reference(guild, "announcements")
        roles_channel = self.format_channel_reference(guild, "roles")

        welcome_embed = self.create_server_info_embed(
            "Honor of Kings Northeast India",
            (
                f"Welcome to **{self.settings.server_name}**, a home for Honor of Kings players "
                "across Northeast India.\n\n"
                "Squad up, discuss strategy, share clips, find teammates, and stay close to the "
                "community events happening around the server.\n\n"
                f"Start with {intro_channel}, chat in {general_chat_channel}, and keep an eye on "
                f"{announcements_channel} for important updates.\n\n"
                "**Play smart. Respect people. Bring good energy.**"
            ),
            0x2F80ED,
        )

        rules_embed = self.create_server_info_embed(
            "\U0001f4dc Rules and Standards",
            (
                "Keep the community useful, safe, and enjoyable for everyone.\n\n"
                "`01` Respect every member. No harassment, hate speech, or personal attacks.\n"
                "`02` Keep channels on-topic and avoid spam, flooding, or repeated pings.\n"
                "`03` No scams, unsafe links, impersonation, or suspicious downloads.\n"
                "`04` Keep competitive talk healthy. Debate plays, not people.\n"
                "`05` Follow Discord Community Guidelines and staff instructions.\n\n"
                "Rule-breaking can lead to message removal, timeout, kick, or ban depending on severity."
            ),
            0xF2C94C,
        )

        links_embed = self.create_server_info_embed(
            "\U0001f517 Official Links",
            "Use the buttons below for quick access, or copy the links here if you are on mobile.",
            0x56CCF2,
        )
        links_embed.add_field(name="Permanent Discord Server Link", value=SERVER_INFO_INVITE_URL, inline=False)
        links_embed.add_field(name="Instagram", value=SERVER_INFO_INSTAGRAM_URL, inline=False)
        links_embed.add_field(name="Honor of Kings Community Group", value=SERVER_INFO_COMMUNITY_URL, inline=False)
        links_embed.add_field(
            name="Broken Link?",
            value="Contact an admin or moderator so the link can be refreshed.",
            inline=False,
        )

        verification_embed = self.create_server_info_embed(
            "\u2705 Verification System",
            (
                "Verification keeps the server cleaner and unlocks full member access.\n\n"
                f"`01` Go to {verify_channel}\n"
                "`02` Tap **Get Verified** below\n"
                "`03` Start exploring the community once your role is added\n\n"
                f"Already verified? Pick up optional community roles in {roles_channel}."
            ),
            0x27AE60,
        )

        levels_embed = self.create_server_info_embed(
            "\U0001f3af Leveling System",
            (
                "Stay active, help other players, and climb the server score ladder.\n\n"
                f"{self.create_server_info_level_lines(guild)}\n\n"
                "Healthy conversation counts. Spam does not help the community."
            ),
            0x9B51E0,
        )

        return [welcome_embed, rules_embed, links_embed, verification_embed, levels_embed]

    def find_member_reference(self, guild: discord.Guild, member_text: str) -> Optional[discord.Member]:
        cleaned = member_text.strip().lstrip("@")
        if cleaned.isdigit():
            return guild.get_member(int(cleaned))

        normalized_target = cleaned.lower()
        for member in guild.members:
            if member.display_name.lower() == normalized_target or member.name.lower() == normalized_target:
                return member
        return None

    def resolve_embed_references(self, guild: discord.Guild, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        def replace_token(match: re.Match[str]) -> str:
            token_type = match.group(1)
            token_value = match.group(2).strip()

            if token_type == "#":
                channel = self.find_text_channel_by_name(guild, token_value)
                return channel.mention if channel is not None else match.group(0)
            if token_type == "&":
                role = self.find_role_by_name(guild, token_value)
                return role.mention if role is not None else match.group(0)

            member = self.find_member_reference(guild, token_value)
            return member.mention if member is not None else match.group(0)

        return TOKEN_REFERENCE_RE.sub(replace_token, value)

    async def get_welcome_channel(self) -> Optional[discord.TextChannel]:
        if not self.settings.welcome_channel_id:
            return None
        channel = self.get_channel(self.settings.welcome_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.settings.welcome_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch welcome channel %s", self.settings.welcome_channel_id)
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        LOGGER.warning("Configured welcome channel is not a text channel: %s", self.settings.welcome_channel_id)
        return None

    def create_welcome_embed(self, member: discord.Member) -> discord.Embed:
        verify_channel = self.format_channel_reference(member.guild, "verify")
        server_info_channel = self.format_channel_reference(member.guild, "server-info")
        intro_channel = self.format_channel_reference(member.guild, "intro")
        general_chat_channel = self.format_channel_reference(member.guild, "general-chat")

        embed = discord.Embed(
            color=discord.Color.green(),
            timestamp=utc_now(),
        )
        embed.description = (
            "✨ 👑 **Welcome to Northeast Esports** 👑 ✨\n\n"
            f"Hey {member.mention}, welcome to the community! ⚔️\n"
            "Get ready to battle, squad up, and connect with the Northeast Esports community.\n\n"
            f"<a:arrow_arrow:1505550701843976412> Verify yourself in {verify_channel}\n"
            f"<a:arrow_arrow:1505550701843976412> Read {server_info_channel} for rules & updates\n"
            f"<a:arrow_arrow:1505550701843976412> Introduce yourself in {intro_channel}\n"
            f"<a:arrow_arrow:1505550701843976412> Chat with everyone in {general_chat_channel}\n"
            "<a:arrow_arrow:1505550701843976412> Find teammates, squad up & enjoy the server!\n\n"
            "🔥 **Play • Compete • Conquer** 🔥"
        )
        embed.set_footer(text=BRAND_FOOTER)
        set_default_thumbnail(embed)
        return embed

    async def send_welcome_message(self, member: discord.Member) -> None:
        channel = await self.get_welcome_channel()
        if channel is None:
            return
        await channel.send(
            **build_embed_send_kwargs(
                self.create_welcome_embed(member),
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        )

    def create_staff_application_embed(
        self,
        user: discord.abc.User,
        draft: StaffApplicationDraft,
        guild: Optional[discord.Guild],
    ) -> discord.Embed:
        embed = discord.Embed(title="Crimson Cup - Referee Application", color=discord.Color.gold(), timestamp=utc_now())
        embed.add_field(name="Applicant", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Server", value=guild.name if guild else "Direct Message", inline=False)
        embed.add_field(name="Discord Username", value=draft.motivation, inline=False)
        embed.add_field(name="IGN", value=draft.role_specific_responsibilities, inline=False)
        embed.add_field(name="Game UID", value=draft.situational_assessment, inline=False)
        embed.add_field(name="Tournament / Referee Experience", value=draft.relevant_experience, inline=False)
        embed.add_field(name="Knowledge of Competitive Rules", value=draft.core_competencies, inline=False)
        embed.add_field(name="Skills", value=draft.decision_making_and_judgment, inline=False)
        embed.add_field(name="Available Days & Time", value=draft.activity_and_availability, inline=False)
        embed.add_field(name="Setup", value=draft.selected_role, inline=False)
        embed.add_field(name="Agreement", value=draft.commitment_and_declaration, inline=False)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=BRAND_FOOTER)
        return embed

    def create_ticket_panel_embed(self) -> discord.Embed:
        embed = make_embed(
            "Support Tickets",
            (
                "Need help from the team? Press **Create Ticket** below.\n\n"
                "A private channel will be created for you and authorized staff. "
                "Please describe your issue clearly once the ticket opens."
            ),
            discord.Color.blurple(),
        )
        embed.add_field(
            name="Privacy",
            value="Ticket messages may be exported to an HTML transcript when the ticket is closed.",
            inline=False,
        )
        return embed

    @staticmethod
    def parse_ticket_channel(channel: discord.abc.GuildChannel) -> Optional[Tuple[int, int, str]]:
        if not isinstance(channel, discord.TextChannel) or not channel.topic:
            return None
        match = TICKET_TOPIC_RE.fullmatch(channel.topic)
        if match is None:
            return None
        return (
            int(match.group("owner_id")),
            int(match.group("claimed_id")),
            match.group("opened_at"),
        )

    @staticmethod
    def build_ticket_topic(owner_id: int, claimed_id: int, opened_at: str) -> str:
        return f"rhino-ticket owner={owner_id} claimed={claimed_id} opened={opened_at}"

    async def get_ticket_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        if self.settings.ticket_category_id:
            category = guild.get_channel(self.settings.ticket_category_id)
            if category is None:
                fetched = await self.fetch_channel(self.settings.ticket_category_id)
                category = fetched if isinstance(fetched, discord.CategoryChannel) else None
            if not isinstance(category, discord.CategoryChannel) or category.guild.id != guild.id:
                raise ValueError("TICKET_CATEGORY_ID does not point to a category in this server.")
            return category

        existing = discord.utils.find(lambda item: item.name.lower() == "tickets", guild.categories)
        if existing is not None:
            return existing
        return await guild.create_category("Tickets", reason="Ticket system setup")

    def find_open_ticket(self, guild: discord.Guild, owner_id: int) -> Optional[discord.TextChannel]:
        for channel in guild.text_channels:
            ticket = self.parse_ticket_channel(channel)
            if ticket is not None and ticket[0] == owner_id:
                return channel
        return None

    def create_modlog_embed(
        self,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str,
    ) -> discord.Embed:
        colors = {
            "WARN": discord.Color.yellow(),
            "MUTE": discord.Color.orange(),
            "UNMUTE": discord.Color.green(),
            "KICK": discord.Color.red(),
            "BAN": discord.Color.dark_red(),
            "UNBAN": discord.Color.green(),
            "CLEAR": discord.Color.blurple(),
            "ANTI-RAID": discord.Color.dark_orange(),
        }
        embed = discord.Embed(
            title=f"{action} Action",
            color=colors.get(action, discord.Color.blurple()),
            timestamp=utc_now(),
        )
        embed.add_field(name="User", value=f"{target} ({target.id})", inline=False)
        embed.add_field(name="Moderator", value=f"{moderator} ({moderator.id})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=BRAND_FOOTER)
        set_default_thumbnail(embed)
        return embed

    async def send_modlog(self, embed: discord.Embed) -> None:
        channel = self.get_channel(self.settings.mod_log_channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.settings.mod_log_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch mod log channel %s", self.settings.mod_log_channel_id)
                return
        if isinstance(channel, discord.TextChannel):
            await self.safe_send_embed(channel, embed, "Mod log message")
        else:
            LOGGER.warning("Configured mod log channel is not a text channel: %s", self.settings.mod_log_channel_id)

    async def get_server_log_channel(self) -> Optional[discord.TextChannel]:
        channel_id = self.settings.server_log_channel_id or self.settings.mod_log_channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch server log channel %s", channel_id)
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        LOGGER.warning("Configured server log channel is not a text channel: %s", channel_id)
        return None

    async def get_server_stats_channel(self) -> Optional[discord.abc.GuildChannel]:
        channel_id = self.settings.server_stats_channel_id
        if channel_id <= 0:
            return None

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch server stats channel %s", channel_id)
                return None

        if isinstance(channel, discord.abc.GuildChannel):
            return channel

        LOGGER.warning("Configured server stats channel is not a guild channel: %s", channel_id)
        return None

    async def get_configured_guild_channel(self, channel_id: int, label: str) -> Optional[discord.abc.GuildChannel]:
        if channel_id <= 0:
            return None

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch %s channel %s", label, channel_id)
                return None

        if isinstance(channel, discord.abc.GuildChannel):
            return channel

        LOGGER.warning("Configured %s channel is not a guild channel: %s", label, channel_id)
        return None

    async def get_invite_log_channel(self) -> Optional[discord.TextChannel]:
        channel_id = self.settings.invite_log_channel_id or self.settings.server_log_channel_id or self.settings.mod_log_channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch invite log channel %s", channel_id)
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        LOGGER.warning("Configured invite log channel is not a text channel: %s", channel_id)
        return None

    async def get_verification_log_channel(self) -> Optional[discord.TextChannel]:
        channel_id = self.settings.verification_log_channel_id or self.settings.server_log_channel_id or self.settings.mod_log_channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch verification log channel %s", channel_id)
                return None
        if isinstance(channel, discord.TextChannel):
            return channel
        LOGGER.warning("Configured verification log channel is not a text channel: %s", channel_id)
        return None

    async def send_server_log(self, embed: discord.Embed) -> Optional[discord.Message]:
        channel = await self.get_server_log_channel()
        if channel is not None:
            return await self.safe_send_embed(channel, embed, "Server log message")
        return None

    async def send_invite_log(self, embed: discord.Embed) -> None:
        channel = await self.get_invite_log_channel()
        if channel is not None:
            await self.safe_send_embed(channel, embed, "Invite log message")

    async def send_verification_log(self, embed: discord.Embed) -> None:
        channel = await self.get_verification_log_channel()
        if channel is not None:
            await self.safe_send_embed(channel, embed, "Verification log message")

    def create_server_log_embed(self, title: str, color: discord.Color) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=utc_now())
        embed.set_footer(text=BRAND_FOOTER)
        set_default_thumbnail(embed)
        return embed

    async def fetch_guild_counts(self, guild: discord.Guild) -> tuple[Optional[int], Optional[int]]:
        approximate_online: Optional[int] = None
        approximate_total: Optional[int] = guild.member_count or len(guild.members)
        try:
            fetched_guild = await self.fetch_guild(guild.id, with_counts=True)
        except discord.HTTPException:
            LOGGER.exception("Could not fetch approximate counts for guild %s", guild.id)
            return approximate_online, approximate_total

        approximate_online = fetched_guild.approximate_presence_count
        if fetched_guild.approximate_member_count is not None:
            approximate_total = fetched_guild.approximate_member_count
        return approximate_online, approximate_total

    def get_precise_guild_stats(
        self,
        guild: discord.Guild,
        *,
        fallback_online: Optional[int],
        fallback_total: Optional[int],
    ) -> dict[str, int]:
        members = list(guild.members)
        expected_total = max(fallback_total or 0, guild.member_count or 0)
        has_full_member_cache = bool(members) and (expected_total == 0 or len(members) >= expected_total)

        if has_full_member_cache:
            all_members = len(members)
            bots = sum(1 for member in members if member.bot)
            human_members = all_members - bots
            if self.intents.presences:
                online_members = sum(
                    1 for member in members if not member.bot and getattr(member, "status", discord.Status.offline) != discord.Status.offline
                )
            else:
                online_members = fallback_online or 0
        else:
            all_members = expected_total
            bots = 0
            human_members = max(0, all_members - bots)
            online_members = fallback_online or 0

        return {
            "all_members": all_members,
            "members": human_members,
            "bots": bots,
            "boosts": guild.premium_subscription_count or 0,
            "online_members": online_members,
        }

    def create_server_stats_embed(
        self,
        guild: discord.Guild,
        previous_online_members: Optional[int],
        previous_total_members: Optional[int],
        previous_boosters: int,
        online_members: Optional[int],
        total_members: Optional[int],
        boosters: int,
    ) -> discord.Embed:
        embed = self.create_server_log_embed("Server Member Stats", discord.Color.blurple())
        embed.add_field(name="Server", value=f"{guild.name} ({guild.id})", inline=False)
        embed.add_field(
            name="Before Online Members",
            value=str(previous_online_members) if previous_online_members is not None else "Unavailable",
            inline=True,
        )
        embed.add_field(
            name="After Online Members",
            value=str(online_members) if online_members is not None else "Unavailable",
            inline=True,
        )
        embed.add_field(name="Before Total Members", value=str(previous_total_members) if previous_total_members is not None else "Unavailable", inline=True)
        embed.add_field(name="After Total Members", value=str(total_members) if total_members is not None else "Unavailable", inline=True)
        embed.add_field(name="Before Boosters", value=str(previous_boosters), inline=True)
        embed.add_field(name="After Boosters", value=str(boosters), inline=True)
        return embed

    def format_server_stats_channel_name(
        self,
        *,
        guild: discord.Guild,
        online_members: Optional[int],
        total_members: Optional[int],
        boosters: int,
    ) -> str:
        try:
            raw_name = self.settings.server_stats_channel_format.format(
                guild=guild.name,
                online=online_members if online_members is not None else "unknown",
                total=total_members if total_members is not None else "unknown",
                boosters=boosters,
            )
        except (IndexError, KeyError, ValueError):
            LOGGER.exception(
                "Invalid SERVER_STATS_CHANNEL_FORMAT %r; falling back to members-{total}",
                self.settings.server_stats_channel_format,
            )
            raw_name = f"MEMBERS: {total_members if total_members is not None else 'UNKNOWN'}"
        return raw_name.strip()[:100] or "MEMBERS"

    def render_stats_channel_name(self, channel: discord.abc.GuildChannel, raw_name: str) -> str:
        if isinstance(channel, discord.TextChannel):
            return format_channel_name(raw_name, uppercase=True)[:100] or "STATS"
        return format_stats_display_name(raw_name) or "STATS"

    def can_rename_stats_channel(self, channel: discord.abc.GuildChannel, new_name: str) -> bool:
        if channel.name == new_name:
            return False

        last_rename_at = self.stats_channel_last_rename_at.get(channel.id)
        if last_rename_at is None:
            return True

        elapsed_seconds = (utc_now() - last_rename_at).total_seconds()
        if elapsed_seconds >= SERVER_STATS_RENAME_COOLDOWN_SECONDS:
            return True

        LOGGER.info(
            "Skipping stats channel %s rename to %s; last rename was %.0fs ago.",
            channel.id,
            new_name,
            elapsed_seconds,
        )
        return False

    async def update_server_stats_channel_name(
        self,
        guild: discord.Guild,
        *,
        online_members: Optional[int],
        total_members: Optional[int],
        boosters: int,
    ) -> None:
        channel = await self.get_server_stats_channel()
        if channel is None:
            return
        if channel.guild.id != guild.id:
            LOGGER.warning(
                "Skipping server stats channel rename for guild %s because configured channel %s belongs to guild %s",
                guild.id,
                channel.id,
                channel.guild.id,
            )
            return

        raw_name = self.format_server_stats_channel_name(
            guild=guild,
            online_members=online_members,
            total_members=total_members,
            boosters=boosters,
        )
        new_name = self.render_stats_channel_name(channel, raw_name)
        if not self.can_rename_stats_channel(channel, new_name):
            return

        try:
            await channel.edit(name=new_name, reason="Updating server stats channel name")
            self.stats_channel_last_rename_at[channel.id] = utc_now()
            LOGGER.info("Updated server stats channel %s name to %s for guild %s", channel.id, new_name, guild.id)
        except discord.HTTPException as error:
            if self.is_discord_rate_limit(error):
                self.stats_channel_last_rename_at[channel.id] = utc_now()
                LOGGER.warning("Discord rate-limited server stats channel %s rename for guild %s: %s", channel.id, guild.id, error)
            else:
                LOGGER.exception("Failed to update server stats channel %s for guild %s", channel.id, guild.id)

    async def update_named_stats_channel(
        self,
        *,
        guild: discord.Guild,
        channel_id: int,
        label: str,
        value: int,
    ) -> None:
        channel = await self.get_configured_guild_channel(channel_id, label)
        if channel is None:
            return
        if channel.guild.id != guild.id:
            LOGGER.warning(
                "Skipping %s channel rename for guild %s because configured channel %s belongs to guild %s",
                label,
                guild.id,
                channel.id,
                channel.guild.id,
            )
            return

        new_name = self.render_stats_channel_name(channel, f"{label}: {value}")
        if not self.can_rename_stats_channel(channel, new_name):
            return

        try:
            await channel.edit(name=new_name, reason="Updating server stats channel name")
            self.stats_channel_last_rename_at[channel.id] = utc_now()
            LOGGER.info("Updated %s channel %s name to %s for guild %s", label, channel.id, new_name, guild.id)
        except discord.HTTPException as error:
            if self.is_discord_rate_limit(error):
                self.stats_channel_last_rename_at[channel.id] = utc_now()
                LOGGER.warning("Discord rate-limited %s channel %s rename for guild %s: %s", label, channel.id, guild.id, error)
            else:
                LOGGER.exception("Failed to update %s channel %s for guild %s", label, channel.id, guild.id)

    async def update_detailed_server_stats_channels(self, guild: discord.Guild, stats: dict[str, int]) -> None:
        await self.update_named_stats_channel(
            guild=guild,
            channel_id=self.settings.all_members_stats_channel_id,
            label="All Members",
            value=stats["all_members"],
        )
        await self.update_named_stats_channel(
            guild=guild,
            channel_id=self.settings.members_stats_channel_id,
            label="Members",
            value=stats["members"],
        )
        await self.update_named_stats_channel(
            guild=guild,
            channel_id=self.settings.bots_stats_channel_id,
            label="Bots",
            value=stats["bots"],
        )
        await self.update_named_stats_channel(
            guild=guild,
            channel_id=self.settings.boosts_stats_channel_id,
            label="Boosts",
            value=stats["boosts"],
        )
        await self.update_named_stats_channel(
            guild=guild,
            channel_id=self.settings.online_members_stats_channel_id,
            label="Online Members",
            value=stats["online_members"],
        )

    async def log_guild_server_stats(self, guild: discord.Guild) -> None:
        channel: Optional[discord.TextChannel] = None
        try:
            online_members, total_members = await self.fetch_guild_counts(guild)
            stats = self.get_precise_guild_stats(
                guild,
                fallback_online=online_members,
                fallback_total=total_members,
            )
            boosters = stats["boosts"]
            previous_online_members, previous_total_members, previous_boosters = self.previous_server_stats.get(
                guild.id,
                (None, None, 0),
            )
            channel = await self.get_server_log_channel()
            if channel is not None:
                LOGGER.info("Sending server stats for guild %s to channel %s", guild.id, channel.id)
                embed = self.create_server_stats_embed(
                    guild,
                    previous_online_members,
                    previous_total_members,
                    previous_boosters,
                    online_members,
                    total_members,
                    boosters,
                )
                await self.safe_send_embed(channel, embed, "Server stats message")
            else:
                LOGGER.warning("No server log channel available for guild %s; skipping stats embed.", guild.id)

            await self.update_server_stats_channel_name(
                guild,
                online_members=online_members,
                total_members=total_members,
                boosters=boosters,
            )
            await self.update_detailed_server_stats_channels(guild, stats)
            self.previous_server_stats[guild.id] = (stats["online_members"], stats["all_members"], boosters)
            LOGGER.info(
                "Sent server stats for guild %s | before=(online=%s total=%s boosters=%s) after=(online=%s total=%s boosters=%s)",
                guild.id,
                previous_online_members,
                previous_total_members,
                previous_boosters,
                stats["online_members"],
                stats["all_members"],
                boosters,
            )
        except discord.HTTPException as error:
            if self.is_discord_rate_limit(error):
                LOGGER.warning(
                    "Discord rate-limited server stats for guild %s in channel %s: %s",
                    guild.id,
                    getattr(channel, "id", "unknown"),
                    error,
                )
            else:
                LOGGER.exception("Failed to send server stats for guild %s to channel %s", guild.id, getattr(channel, "id", "unknown"))

    async def log_all_server_stats(self) -> None:
        if self.server_stats_running:
            LOGGER.info("Server stats loop is already running; skipping overlapping run.")
            return

        self.server_stats_running = True
        LOGGER.info("Running server stats loop for %s guild(s)", len(self.guilds))
        try:
            for guild in self.guilds:
                await self.log_guild_server_stats(guild)
        finally:
            self.server_stats_running = False

    async def find_recent_audit_actor(
        self,
        guild: discord.Guild,
        target_id: int,
        *actions: discord.AuditLogAction,
        within_seconds: int = 10,
        attempts: int = 3,
        retry_delay: float = 1.0,
    ) -> Optional[discord.abc.User]:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        for attempt in range(attempts):
            now = utc_now()
            for action in actions:
                try:
                    async for entry in guild.audit_logs(limit=5, action=action):
                        entry_target_id = getattr(entry.target, "id", None)
                        if entry_target_id != target_id:
                            continue
                        if abs((now - entry.created_at).total_seconds()) > within_seconds:
                            continue
                        return entry.user
                except discord.Forbidden:
                    return None
                except discord.HTTPException:
                    LOGGER.warning("Could not read audit log for %s in guild %s", action, guild.id)
                    return None
            if attempt < attempts - 1:
                await asyncio.sleep(retry_delay)
        return None

    async def add_audit_actor_field(
        self,
        embed: discord.Embed,
        guild: discord.Guild,
        target_id: int,
        *actions: discord.AuditLogAction,
        within_seconds: int = 10,
    ) -> None:
        actor = await self.find_recent_audit_actor(
            guild,
            target_id,
            *actions,
            within_seconds=within_seconds,
        )
        if actor is not None:
            embed.add_field(name="Action By", value=actor.mention, inline=False)

    async def enrich_server_log_with_audit_actor(
        self,
        message: Optional[discord.Message],
        guild: discord.Guild,
        target_id: int,
        *actions: discord.AuditLogAction,
        within_seconds: int = 10,
    ) -> None:
        if message is None or not message.embeds:
            return

        actor = await self.find_recent_audit_actor(
            guild,
            target_id,
            *actions,
            within_seconds=within_seconds,
        )
        if actor is None:
            return

        embed = message.embeds[0].copy()
        if any(field.name == "Action By" for field in embed.fields):
            return
        embed.add_field(name="Action By", value=actor.mention, inline=False)

        try:
            await message.edit(embed=embed)
        except discord.HTTPException:
            LOGGER.warning("Could not update server log message %s with audit actor", message.id)

    def create_verification_log_embed(self, member: discord.Member, role: discord.Role) -> discord.Embed:
        embed = self.create_server_log_embed("Member Verified", discord.Color.green())
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Role Granted", value=role.mention, inline=False)
        embed.add_field(name="Verified At", value=discord.utils.format_dt(utc_now(), "F"), inline=False)
        return embed

    def format_role_list(self, roles: List[discord.Role]) -> str:
        if not roles:
            return "None"
        sorted_roles = sorted(roles, key=lambda role: role.position, reverse=True)
        return truncate_text(", ".join(role.mention for role in sorted_roles), 1024)

    def format_voice_channel(self, channel: Optional[discord.abc.Connectable]) -> str:
        if channel is None:
            return "None"
        mention = getattr(channel, "mention", None)
        if mention is not None:
            return f"{mention} ({channel.name})"
        return f"{channel.name} ({channel.id})"

    def format_message_channel(self, channel: discord.abc.Messageable) -> str:
        if isinstance(channel, discord.Thread):
            return f"{channel.mention} (thread)"
        if isinstance(channel, discord.TextChannel):
            return channel.mention
        return str(channel)

    def format_channel(self, channel: discord.abc.GuildChannel) -> str:
        mention = getattr(channel, "mention", None)
        if mention is not None:
            return f"{mention} ({channel.id})"
        return f"{channel.name} ({channel.id})"

    def get_timeout_until(self, member: discord.Member) -> Optional[datetime]:
        value = getattr(member, "timed_out_until", None)
        if value is None:
            value = getattr(member, "communication_disabled_until", None)
        return value

    def is_image_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = attachment.content_type or ""
        if content_type.startswith("image/"):
            return True
        return attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"))

    def add_change_field(self, embed: discord.Embed, name: str, before: object, after: object) -> None:
        if before == after:
            return
        embed.add_field(name=name, value=truncate_text(f"{before} -> {after}", 1024), inline=False)

    def describe_invite(self, invite: discord.Invite) -> str:
        parts = [f"Code: `{invite.code}`"]
        if invite.channel is not None:
            parts.append(f"Channel: {self.format_channel(invite.channel)}")
        if invite.inviter is not None:
            parts.append(f"Inviter: {invite.inviter} ({invite.inviter.id})")
        max_uses = "Unlimited" if invite.max_uses == 0 else str(invite.max_uses)
        parts.append(f"Max Uses: {max_uses}")
        if invite.max_age:
            parts.append(f"Expires After: {format_duration(timedelta(seconds=invite.max_age))}")
        else:
            parts.append("Expires After: Never")
        return "\n".join(parts)

    async def log_member_join(self, member: discord.Member) -> None:
        embed = self.create_server_log_embed("Member Joined", discord.Color.green())
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "F"), inline=False)
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(utc_now(), "F"), inline=False)
        await self.send_server_log(embed)

    async def log_invite_join(self, member: discord.Member, invite_info: str) -> None:
        embed = self.create_server_log_embed("Invite Used", discord.Color.green())
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Invite", value=truncate_text(invite_info, 1024), inline=False)
        await self.send_invite_log(embed)

    async def log_member_leave(self, member: discord.Member) -> None:
        embed = self.create_server_log_embed("Member Left", discord.Color.orange())
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        if member.joined_at is not None:
            embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, "F"), inline=False)
        if member.roles:
            role_mentions = [role.mention for role in member.roles if role != member.guild.default_role]
            if role_mentions:
                embed.add_field(name="Roles", value=truncate_text(", ".join(role_mentions), 1024), inline=False)
        await self.send_server_log(embed)

    async def log_member_profile_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.nick != after.nick:
            embed = self.create_server_log_embed("Nickname Changed", discord.Color.blurple())
            embed.add_field(name="Member", value=f"{after} ({after.id})", inline=False)
            embed.add_field(name="Before", value=before.nick or before.name, inline=True)
            embed.add_field(name="After", value=after.nick or after.name, inline=True)
            await self.send_server_log(embed)

        before_timeout = self.get_timeout_until(before)
        after_timeout = self.get_timeout_until(after)
        if before_timeout != after_timeout:
            embed = self.create_server_log_embed("Member Timeout Updated", discord.Color.orange())
            embed.add_field(name="Member", value=f"{after} ({after.id})", inline=False)
            before_value = discord.utils.format_dt(before_timeout, "F") if before_timeout is not None else "None"
            after_value = discord.utils.format_dt(after_timeout, "F") if after_timeout is not None else "None"
            embed.add_field(name="Before", value=before_value, inline=True)
            embed.add_field(name="After", value=after_value, inline=True)
            message = await self.send_server_log(embed)
            asyncio.create_task(
                self.enrich_server_log_with_audit_actor(
                    message,
                    after.guild,
                    after.id,
                    discord.AuditLogAction.member_update,
                )
            )

        before_roles = {
            role.id: role
            for role in before.roles
            if role != before.guild.default_role
        }
        after_roles = {
            role.id: role
            for role in after.roles
            if role != after.guild.default_role
        }
        added_roles = [role for role_id, role in after_roles.items() if role_id not in before_roles]
        removed_roles = [role for role_id, role in before_roles.items() if role_id not in after_roles]
        if not added_roles and not removed_roles:
            return

        if added_roles:
            embed = self.create_server_log_embed("Member Role Added", discord.Color.green())
            embed.add_field(name="Member", value=f"{after} ({after.id})", inline=False)
            embed.add_field(name="Added", value=self.format_role_list(added_roles), inline=False)
            message = await self.send_server_log(embed)
            asyncio.create_task(
                self.enrich_server_log_with_audit_actor(
                    message,
                    after.guild,
                    after.id,
                    discord.AuditLogAction.member_role_update,
                )
            )
        if removed_roles:
            embed = self.create_server_log_embed("Member Role Removed", discord.Color.red())
            embed.add_field(name="Member", value=f"{after} ({after.id})", inline=False)
            embed.add_field(name="Removed", value=self.format_role_list(removed_roles), inline=False)
            message = await self.send_server_log(embed)
            asyncio.create_task(
                self.enrich_server_log_with_audit_actor(
                    message,
                    after.guild,
                    after.id,
                    discord.AuditLogAction.member_role_update,
                )
            )

    async def log_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if before.channel == after.channel:
            return

        if before.channel is None and after.channel is not None:
            title = "Voice Joined"
            color = discord.Color.green()
        elif before.channel is not None and after.channel is None:
            title = "Voice Left"
            color = discord.Color.orange()
        else:
            title = "Voice Moved"
            color = discord.Color.blurple()

        embed = self.create_server_log_embed(title, color)
        embed.add_field(name="Member", value=f"{member} ({member.id})", inline=False)
        embed.add_field(name="Before", value=self.format_voice_channel(before.channel), inline=True)
        embed.add_field(name="After", value=self.format_voice_channel(after.channel), inline=True)
        message = await self.send_server_log(embed)
        asyncio.create_task(
            self.enrich_server_log_with_audit_actor(
                message,
                member.guild,
                member.id,
                discord.AuditLogAction.member_move,
                discord.AuditLogAction.member_disconnect,
            )
        )

    async def log_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        embed = self.create_server_log_embed("Member Banned", discord.Color.dark_red())
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Server", value=f"{guild.name} ({guild.id})", inline=False)
        message = await self.send_server_log(embed)
        asyncio.create_task(
            self.enrich_server_log_with_audit_actor(
                message,
                guild,
                user.id,
                discord.AuditLogAction.ban,
            )
        )

    async def log_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        embed = self.create_server_log_embed("Member Unbanned", discord.Color.green())
        embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
        embed.add_field(name="Server", value=f"{guild.name} ({guild.id})", inline=False)
        message = await self.send_server_log(embed)
        asyncio.create_task(
            self.enrich_server_log_with_audit_actor(
                message,
                guild,
                user.id,
                discord.AuditLogAction.unban,
            )
        )

    async def log_message_delete(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        channel_value = self.format_message_channel(message.channel)
        image_attachments = [attachment for attachment in message.attachments if self.is_image_attachment(attachment)]
        title = "Image Deleted" if image_attachments else "Message Deleted"
        embed = self.create_server_log_embed(title, discord.Color.red())
        embed.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=False)
        embed.add_field(name="Channel", value=channel_value, inline=False)
        content = message.content.strip() if message.content else ""
        embed.add_field(name="Content", value=truncate_text(content or "[no text content]", 1024), inline=False)
        if image_attachments:
            image_names = ", ".join(attachment.filename for attachment in image_attachments)
            embed.add_field(name="Images", value=truncate_text(image_names, 1024), inline=False)
        if message.attachments:
            filenames = ", ".join(attachment.filename for attachment in message.attachments)
            embed.add_field(name="Attachments", value=truncate_text(filenames, 1024), inline=False)
        await self.send_server_log(embed)

    async def log_bulk_message_delete(self, messages: List[discord.Message]) -> None:
        if not messages:
            return
        first_message = messages[0]
        if first_message.guild is None:
            return
        channel_value = self.format_message_channel(first_message.channel)
        user_ids = {message.author.id for message in messages if message.author is not None}
        image_count = sum(
            1
            for message in messages
            for attachment in message.attachments
            if self.is_image_attachment(attachment)
        )
        embed = self.create_server_log_embed("Bulk Message Delete", discord.Color.dark_red())
        embed.add_field(name="Channel", value=channel_value, inline=False)
        embed.add_field(name="Deleted Messages", value=str(len(messages)), inline=True)
        embed.add_field(name="Unique Authors", value=str(len(user_ids)), inline=True)
        if image_count:
            embed.add_field(name="Deleted Images", value=str(image_count), inline=True)
        await self.send_server_log(embed)

    async def log_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return
        channel_value = self.format_message_channel(before.channel)
        embed = self.create_server_log_embed("Message Edited", discord.Color.gold())
        embed.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=False)
        embed.add_field(name="Channel", value=channel_value, inline=False)
        embed.add_field(name="Before", value=truncate_text(before.content or "[no text content]", 1024), inline=False)
        embed.add_field(name="After", value=truncate_text(after.content or "[no text content]", 1024), inline=False)
        await self.send_server_log(embed)

    async def log_channel_event(
        self,
        action: str,
        channel: discord.abc.GuildChannel,
        color: discord.Color,
    ) -> None:
        embed = self.create_server_log_embed(action, color)
        embed.add_field(name="Channel", value=f"{channel.name} ({channel.id})", inline=False)
        embed.add_field(name="Type", value=str(channel.type), inline=False)
        category = channel.category.name if channel.category is not None else "No category"
        embed.add_field(name="Category", value=category, inline=False)
        await self.send_server_log(embed)

    async def log_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        embed = self.create_server_log_embed("Channel Updated", discord.Color.gold())
        embed.add_field(name="Channel", value=self.format_channel(after), inline=False)
        self.add_change_field(embed, "Name", before.name, after.name)
        self.add_change_field(embed, "Type", before.type, after.type)
        before_category = before.category.name if before.category is not None else "No category"
        after_category = after.category.name if after.category is not None else "No category"
        self.add_change_field(embed, "Category", before_category, after_category)
        for attr, label in (
            ("position", "Position"),
            ("slowmode_delay", "Slowmode"),
            ("nsfw", "NSFW"),
            ("bitrate", "Bitrate"),
            ("user_limit", "User Limit"),
        ):
            if hasattr(before, attr) and hasattr(after, attr):
                self.add_change_field(embed, label, getattr(before, attr), getattr(after, attr))
        if len(embed.fields) > 1:
            await self.send_server_log(embed)

    async def log_role_event(self, action: str, role: discord.Role, color: discord.Color) -> None:
        embed = self.create_server_log_embed(action, color)
        embed.add_field(name="Role", value=f"{role.mention} ({role.id})", inline=False)
        embed.add_field(name="Name", value=role.name, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        embed.add_field(name="Mentionable", value=str(role.mentionable), inline=True)
        await self.send_server_log(embed)

    async def log_role_update(self, before: discord.Role, after: discord.Role) -> None:
        embed = self.create_server_log_embed("Role Updated", discord.Color.gold())
        embed.add_field(name="Role", value=f"{after.mention} ({after.id})", inline=False)
        self.add_change_field(embed, "Name", before.name, after.name)
        self.add_change_field(embed, "Color", before.color, after.color)
        self.add_change_field(embed, "Hoisted", before.hoist, after.hoist)
        self.add_change_field(embed, "Mentionable", before.mentionable, after.mentionable)
        self.add_change_field(embed, "Permissions", before.permissions.value, after.permissions.value)
        if len(embed.fields) > 1:
            await self.send_server_log(embed)

    async def log_emoji_update(
        self,
        guild: discord.Guild,
        before: List[discord.Emoji],
        after: List[discord.Emoji],
    ) -> None:
        before_by_id = {emoji.id: emoji for emoji in before}
        after_by_id = {emoji.id: emoji for emoji in after}

        for emoji_id, emoji in after_by_id.items():
            if emoji_id not in before_by_id:
                embed = self.create_server_log_embed("Emoji Created", discord.Color.green())
                embed.add_field(name="Emoji", value=f"{emoji} `{emoji.name}` ({emoji.id})", inline=False)
                embed.add_field(name="Server", value=f"{guild.name} ({guild.id})", inline=False)
                await self.send_server_log(embed)

        for emoji_id, emoji in before_by_id.items():
            if emoji_id not in after_by_id:
                embed = self.create_server_log_embed("Emoji Deleted", discord.Color.red())
                embed.add_field(name="Emoji", value=f"`{emoji.name}` ({emoji.id})", inline=False)
                embed.add_field(name="Server", value=f"{guild.name} ({guild.id})", inline=False)
                await self.send_server_log(embed)

        for emoji_id, before_emoji in before_by_id.items():
            after_emoji = after_by_id.get(emoji_id)
            if after_emoji is None or before_emoji.name == after_emoji.name:
                continue
            embed = self.create_server_log_embed("Emoji Name Changed", discord.Color.gold())
            embed.add_field(name="Emoji", value=f"{after_emoji} ({after_emoji.id})", inline=False)
            embed.add_field(name="Before", value=before_emoji.name, inline=True)
            embed.add_field(name="After", value=after_emoji.name, inline=True)
            await self.send_server_log(embed)

    async def log_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = self.create_server_log_embed("Invite Created", discord.Color.green())
        embed.add_field(name="Invite Info", value=truncate_text(self.describe_invite(invite), 1024), inline=False)
        embed.add_field(name="Server", value=f"{invite.guild.name} ({invite.guild.id})", inline=False)
        await self.send_invite_log(embed)

    async def log_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None:
            return
        embed = self.create_server_log_embed("Invite Deleted", discord.Color.red())
        embed.add_field(name="Invite Info", value=truncate_text(self.describe_invite(invite), 1024), inline=False)
        embed.add_field(name="Server", value=f"{invite.guild.name} ({invite.guild.id})", inline=False)
        await self.send_invite_log(embed)

    async def log_moderator_command(
        self,
        interaction: discord.Interaction,
        action: str,
        target: discord.abc.User,
        reason: str,
    ) -> None:
        if interaction.guild is None:
            return
        embed = self.create_server_log_embed("Moderator Command", discord.Color.blurple())
        embed.add_field(name="Command", value=action, inline=True)
        embed.add_field(name="Moderator", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Target", value=f"{target} ({target.id})", inline=False)
        embed.add_field(name="Reason/Details", value=truncate_text(reason, 1024), inline=False)
        await self.send_server_log(embed)

    async def get_staff_application_channel(self) -> Optional[discord.TextChannel]:
        channel = self.get_channel(self.settings.staff_application_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.settings.staff_application_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        LOGGER.warning(
            "Configured staff application channel is not a text channel: %s",
            self.settings.staff_application_channel_id,
        )
        return None

    async def add_modlog(
        self,
        action: str,
        target: discord.abc.User,
        moderator: discord.abc.User,
        guild_id: Optional[int],
        reason: str,
        duration_text: Optional[str] = None,
    ) -> None:
        entry = ModLogEntry(
            guild_id=guild_id,
            action=action,
            user_id=target.id,
            moderator_id=moderator.id,
            reason=reason,
            duration_text=duration_text,
        )
        self.mod_logs.append(entry)
        if self.uses_postgres and guild_id is not None:
            await asyncio.to_thread(self.persist_modlog, entry)

    def persist_modlog(self, entry: ModLogEntry) -> None:
        if entry.guild_id is None:
            return

        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO mod_logs (
                            guild_id,
                            user_id,
                            moderator_id,
                            action,
                            reason,
                            duration_text,
                            created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            entry.guild_id,
                            entry.user_id,
                            entry.moderator_id,
                            entry.action,
                            entry.reason,
                            entry.duration_text,
                            entry.created_at,
                        ),
                    )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to persist moderation log for guild=%s user=%s", entry.guild_id, entry.user_id)

    def load_modlogs_from_postgres(self, guild_id: int, user_id: int, *, limit: int = 10) -> List[ModLogEntry]:
        entries: List[ModLogEntry] = []
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, action, user_id, moderator_id, reason, created_at, duration_text
                        FROM mod_logs
                        WHERE guild_id = %s AND user_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                        """,
                        (guild_id, user_id, limit),
                    )
                    for row_guild_id, action, row_user_id, moderator_id, reason, created_at, duration_text in cur.fetchall():
                        entries.append(
                            ModLogEntry(
                                guild_id=int(row_guild_id),
                                action=str(action),
                                user_id=int(row_user_id),
                                moderator_id=int(moderator_id),
                                reason=str(reason),
                                created_at=created_at if isinstance(created_at, datetime) else utc_now(),
                                duration_text=str(duration_text) if duration_text else None,
                            )
                        )
        except Exception:
            LOGGER.exception("Failed to load moderation logs from PostgreSQL for guild=%s user=%s", guild_id, user_id)
            return []
        return entries

    async def load_prefix_data(self) -> None:
        self.command_prefixes = await asyncio.to_thread(self._load_prefix_data_sync)

    def _load_prefix_data_sync(self) -> Dict[int, str]:
        if self.uses_postgres:
            loaded_data = self._load_prefix_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_prefix_data_from_json()
            if fallback_data:
                self._save_prefix_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL prefix data from %s", PREFIX_DATA_PATH)
            return fallback_data

        return self._load_prefix_data_from_json()

    def _load_prefix_data_from_json(self) -> Dict[int, str]:
        loaded_data: Dict[int, str] = {}
        if not PREFIX_DATA_PATH.exists():
            LOGGER.info("Prefix data file %s not found. A new one will be created on first use.", PREFIX_DATA_PATH)
            return loaded_data

        try:
            raw = json.loads(PREFIX_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load prefix data from %s", PREFIX_DATA_PATH)
            return loaded_data

        for guild_id, prefix in (raw if isinstance(raw, dict) else {}).items():
            try:
                parsed_guild_id = int(guild_id)
            except (TypeError, ValueError):
                continue

            parsed_prefix, error = self.validate_command_prefix(str(prefix))
            if error is None and parsed_prefix is not None:
                loaded_data[parsed_guild_id] = parsed_prefix

        LOGGER.info("Loaded prefix data for %s guild(s) from %s", len(loaded_data), PREFIX_DATA_PATH)
        return loaded_data

    def _load_prefix_data_from_postgres(self) -> Dict[int, str]:
        loaded_data: Dict[int, str] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, prefix
                        FROM command_prefixes
                        """
                    )
                    for guild_id, prefix in cur.fetchall():
                        parsed_prefix, error = self.validate_command_prefix(str(prefix))
                        if error is None and parsed_prefix is not None:
                            loaded_data[int(guild_id)] = parsed_prefix
        except Exception:
            LOGGER.exception("Failed to load prefix data from PostgreSQL.")
            return {}

        LOGGER.info("Loaded prefix data for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_prefix_data(self) -> None:
        if self.uses_postgres:
            self._save_prefix_data_to_postgres(self.command_prefixes)
            return
        self._save_prefix_data_to_json(self.command_prefixes)

    def _save_prefix_data_to_json(self, prefixes: Dict[int, str]) -> None:
        serialized = {str(guild_id): prefix for guild_id, prefix in prefixes.items()}
        try:
            PREFIX_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save prefix data to %s", PREFIX_DATA_PATH)

    def _save_prefix_data_to_postgres(self, prefixes: Dict[int, str]) -> None:
        rows = list(prefixes.items())
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM command_prefixes")
                    if rows:
                        cur.executemany(
                            """
                            INSERT INTO command_prefixes (guild_id, prefix)
                            VALUES (%s, %s)
                            """,
                            rows,
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save prefix data to PostgreSQL.")

    async def persist_prefix_data(self) -> None:
        await asyncio.to_thread(self.save_prefix_data)

    def get_guild_prefix(self, guild_id: int) -> str:
        return self.command_prefixes.get(guild_id, DEFAULT_COMMAND_PREFIX)

    @staticmethod
    def format_prefix_for_display(prefix: str) -> str:
        escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(prefix))
        return f"`{escaped}`"

    @staticmethod
    def format_command_example(prefix: str, command: str) -> str:
        escaped = discord.utils.escape_markdown(discord.utils.escape_mentions(f"{prefix}{command}"))
        return f"`{escaped}`"

    @staticmethod
    def validate_command_prefix(prefix: str) -> tuple[Optional[str], Optional[str]]:
        cleaned = prefix.strip()
        if not cleaned:
            return None, "Please provide a prefix."
        if len(cleaned) > MAX_COMMAND_PREFIX_LENGTH:
            return None, f"Prefix must be {MAX_COMMAND_PREFIX_LENGTH} characters or fewer."
        if any(character.isspace() for character in cleaned):
            return None, "Prefix cannot contain spaces."
        if cleaned.startswith("/"):
            return None, "Prefix cannot start with `/` because slash commands already use that."
        if cleaned.startswith("<@"):
            return None, "Mention prefixes are already supported automatically."
        return cleaned, None

    async def set_guild_prefix(self, guild_id: int, prefix: str) -> str:
        self.command_prefixes[guild_id] = prefix
        await self.persist_prefix_data()
        return prefix

    async def reset_guild_prefix(self, guild_id: int) -> None:
        self.command_prefixes.pop(guild_id, None)
        await self.persist_prefix_data()

    async def load_ticket_config_data(self) -> None:
        self.ticket_transcript_channels = await asyncio.to_thread(self._load_ticket_config_data_sync)

    def _load_ticket_config_data_sync(self) -> Dict[int, int]:
        if self.uses_postgres:
            loaded_data = self._load_ticket_config_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_ticket_config_data_from_json()
            if fallback_data:
                self._save_ticket_config_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL ticket configuration from %s", TICKET_CONFIG_DATA_PATH)
            return fallback_data

        return self._load_ticket_config_data_from_json()

    def _load_ticket_config_data_from_json(self) -> Dict[int, int]:
        if not TICKET_CONFIG_DATA_PATH.exists():
            LOGGER.info(
                "Ticket configuration file %s not found. A new one will be created when an admin uses /ticket setlog.",
                TICKET_CONFIG_DATA_PATH,
            )
            return {}
        try:
            raw = json.loads(TICKET_CONFIG_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load ticket configuration from %s", TICKET_CONFIG_DATA_PATH)
            return {}

        loaded_data: Dict[int, int] = {}
        for guild_id, channel_id in (raw if isinstance(raw, dict) else {}).items():
            try:
                parsed_guild_id = int(guild_id)
                parsed_channel_id = int(channel_id)
            except (TypeError, ValueError):
                continue
            if parsed_guild_id > 0 and parsed_channel_id > 0:
                loaded_data[parsed_guild_id] = parsed_channel_id
        LOGGER.info("Loaded ticket configuration for %s guild(s) from %s", len(loaded_data), TICKET_CONFIG_DATA_PATH)
        return loaded_data

    def _load_ticket_config_data_from_postgres(self) -> Dict[int, int]:
        loaded_data: Dict[int, int] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT guild_id, transcript_channel_id FROM ticket_configs")
                    for guild_id, channel_id in cur.fetchall():
                        loaded_data[int(guild_id)] = int(channel_id)
        except Exception:
            LOGGER.exception("Failed to load ticket configuration from PostgreSQL.")
            return {}
        LOGGER.info("Loaded ticket configuration for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_ticket_config_data(self) -> None:
        if self.uses_postgres:
            self._save_ticket_config_data_to_postgres(self.ticket_transcript_channels)
            return
        try:
            serialized = {
                str(guild_id): channel_id
                for guild_id, channel_id in self.ticket_transcript_channels.items()
            }
            TICKET_CONFIG_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save ticket configuration to %s", TICKET_CONFIG_DATA_PATH)

    def _save_ticket_config_data_to_postgres(self, configs: Dict[int, int]) -> None:
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM ticket_configs")
                    if configs:
                        cur.executemany(
                            "INSERT INTO ticket_configs (guild_id, transcript_channel_id) VALUES (%s, %s)",
                            list(configs.items()),
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save ticket configuration to PostgreSQL.")

    async def persist_ticket_config_data(self) -> None:
        await asyncio.to_thread(self.save_ticket_config_data)

    def get_ticket_transcript_channel_id(self, guild_id: int) -> int:
        return (
            self.ticket_transcript_channels.get(guild_id)
            or self.settings.ticket_transcript_channel_id
            or self.settings.mod_log_channel_id
        )

    async def load_autoreact_data(self) -> None:
        self.autoreact_configs = await asyncio.to_thread(self._load_autoreact_data_sync)

    def _load_autoreact_data_sync(self) -> Dict[int, Dict[int, AutoReactionConfig]]:
        if self.uses_postgres:
            loaded_data = self._load_autoreact_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_autoreact_data_from_json()
            if fallback_data:
                self._save_autoreact_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL auto-reaction data from %s", AUTOREACT_DATA_PATH)
            return fallback_data

        return self._load_autoreact_data_from_json()

    def _load_autoreact_data_from_json(self) -> Dict[int, Dict[int, AutoReactionConfig]]:
        loaded_data: Dict[int, Dict[int, AutoReactionConfig]] = {}
        if not AUTOREACT_DATA_PATH.exists():
            LOGGER.info("Auto-reaction data file %s not found. A new one will be created on first activation.", AUTOREACT_DATA_PATH)
            return loaded_data

        try:
            raw = json.loads(AUTOREACT_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load auto-reaction data from %s", AUTOREACT_DATA_PATH)
            return loaded_data

        for guild_id, rules in (raw if isinstance(raw, dict) else {}).items():
            try:
                parsed_guild_id = int(guild_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(rules, dict):
                continue

            parsed_configs: Dict[int, AutoReactionConfig] = {}
            for channel_id, emoji_value in rules.items():
                try:
                    parsed_channel_id = int(channel_id)
                except (TypeError, ValueError):
                    continue

                parsed_emojis: List[str] = []
                raw_emojis = emoji_value if isinstance(emoji_value, list) else [emoji_value]
                for raw_emoji in raw_emojis:
                    emoji = self.normalize_autoreact_emoji(str(raw_emoji))
                    if emoji is not None and emoji not in parsed_emojis:
                        parsed_emojis.append(emoji)

                if not parsed_emojis or parsed_channel_id <= 0:
                    continue
                parsed_configs[parsed_channel_id] = AutoReactionConfig(emojis=parsed_emojis)

            loaded_data[parsed_guild_id] = parsed_configs

        LOGGER.info("Loaded auto-reaction data for %s guild(s) from %s", len(loaded_data), AUTOREACT_DATA_PATH)
        return loaded_data

    def _load_autoreact_data_from_postgres(self) -> Dict[int, Dict[int, AutoReactionConfig]]:
        loaded_data: Dict[int, Dict[int, AutoReactionConfig]] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, channel_id, emojis
                        FROM autoreact_configs
                        """
                    )
                    for guild_id, channel_id, raw_emojis in cur.fetchall():
                        parsed_emojis: List[str] = []
                        raw_list = raw_emojis if isinstance(raw_emojis, list) else []
                        for raw_emoji in raw_list:
                            emoji = self.normalize_autoreact_emoji(str(raw_emoji))
                            if emoji is not None and emoji not in parsed_emojis:
                                parsed_emojis.append(emoji)
                        if not parsed_emojis:
                            continue
                        guild_configs = loaded_data.setdefault(int(guild_id), {})
                        guild_configs[int(channel_id)] = AutoReactionConfig(emojis=parsed_emojis)
        except Exception:
            LOGGER.exception("Failed to load auto-reaction data from PostgreSQL.")
            return {}

        LOGGER.info("Loaded auto-reaction data for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_autoreact_data(self) -> None:
        if self.uses_postgres:
            self._save_autoreact_data_to_postgres(self.autoreact_configs)
            return
        self._save_autoreact_data_to_json(self.autoreact_configs)

    def _save_autoreact_data_to_json(self, configs: Dict[int, Dict[int, AutoReactionConfig]]) -> None:
        serialized = {
            str(guild_id): {
                str(channel_id): config.emojis
                for channel_id, config in channel_configs.items()
            }
            for guild_id, channel_configs in configs.items()
        }
        try:
            AUTOREACT_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save auto-reaction data to %s", AUTOREACT_DATA_PATH)

    def _save_autoreact_data_to_postgres(self, configs: Dict[int, Dict[int, AutoReactionConfig]]) -> None:
        rows = [
            (guild_id, channel_id, config.emojis)
            for guild_id, channel_configs in configs.items()
            for channel_id, config in channel_configs.items()
            if config.emojis
        ]
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM autoreact_configs")
                    if rows:
                        cur.executemany(
                            """
                            INSERT INTO autoreact_configs (guild_id, channel_id, emojis)
                            VALUES (%s, %s, %s)
                            """,
                            rows,
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save auto-reaction data to PostgreSQL.")

    async def persist_autoreact_data(self) -> None:
        await asyncio.to_thread(self.save_autoreact_data)

    async def load_reaction_role_data(self) -> None:
        self.reaction_role_configs = await asyncio.to_thread(self._load_reaction_role_data_sync)

    def _load_reaction_role_data_sync(self) -> Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]]:
        if self.uses_postgres:
            loaded_data = self._load_reaction_role_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_reaction_role_data_from_json()
            if fallback_data:
                self._save_reaction_role_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL reaction-role data from %s", REACTION_ROLE_DATA_PATH)
            return fallback_data

        return self._load_reaction_role_data_from_json()

    def _store_reaction_role_config(
        self,
        configs: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]],
        guild_id: object,
        channel_id: object,
        message_id: object,
        emoji: object,
        role_id: object,
    ) -> None:
        try:
            parsed_guild_id = int(guild_id)
            parsed_channel_id = int(channel_id)
            parsed_message_id = int(message_id)
            parsed_role_id = int(role_id)
        except (TypeError, ValueError):
            return

        if parsed_guild_id <= 0 or parsed_channel_id <= 0 or parsed_message_id <= 0 or parsed_role_id <= 0:
            return

        normalized_emoji = self.normalize_reaction_role_emoji(str(emoji))
        if normalized_emoji is None:
            return

        configs.setdefault(parsed_guild_id, {}).setdefault(parsed_message_id, {})[normalized_emoji] = ReactionRoleConfig(
            channel_id=parsed_channel_id,
            message_id=parsed_message_id,
            emoji=normalized_emoji,
            role_id=parsed_role_id,
        )

    def _load_reaction_role_data_from_json(self) -> Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]]:
        loaded_data: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]] = {}
        if not REACTION_ROLE_DATA_PATH.exists():
            LOGGER.info("Reaction-role data file %s not found. A new one will be created on first use.", REACTION_ROLE_DATA_PATH)
            return loaded_data

        try:
            raw = json.loads(REACTION_ROLE_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load reaction-role data from %s", REACTION_ROLE_DATA_PATH)
            return loaded_data

        for guild_id, messages in (raw if isinstance(raw, dict) else {}).items():
            if not isinstance(messages, dict):
                continue

            for message_id, payload in messages.items():
                if not isinstance(payload, dict):
                    continue
                channel_id = payload.get("channel_id")
                emoji_roles = payload.get("emoji_roles", {})
                if isinstance(emoji_roles, dict):
                    for emoji, role_id in emoji_roles.items():
                        self._store_reaction_role_config(
                            loaded_data,
                            guild_id,
                            channel_id,
                            message_id,
                            emoji,
                            role_id,
                        )

        LOGGER.info("Loaded reaction-role data for %s guild(s) from %s", len(loaded_data), REACTION_ROLE_DATA_PATH)
        return loaded_data

    def _load_reaction_role_data_from_postgres(self) -> Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]]:
        loaded_data: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, channel_id, message_id, emoji, role_id
                        FROM reaction_role_configs
                        """
                    )
                    for guild_id, channel_id, message_id, emoji, role_id in cur.fetchall():
                        self._store_reaction_role_config(
                            loaded_data,
                            guild_id,
                            channel_id,
                            message_id,
                            emoji,
                            role_id,
                        )
        except Exception:
            LOGGER.exception("Failed to load reaction-role data from PostgreSQL.")
            return {}

        LOGGER.info("Loaded reaction-role data for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_reaction_role_data(self) -> None:
        if self.uses_postgres:
            self._save_reaction_role_data_to_postgres(self.reaction_role_configs)
            return
        self._save_reaction_role_data_to_json(self.reaction_role_configs)

    def _save_reaction_role_data_to_json(
        self,
        configs: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]],
    ) -> None:
        serialized: Dict[str, Dict[str, dict]] = {}
        for guild_id, message_configs in configs.items():
            serialized_messages: Dict[str, dict] = {}
            for message_id, emoji_configs in message_configs.items():
                if not emoji_configs:
                    continue
                first_config = next(iter(emoji_configs.values()))
                serialized_messages[str(message_id)] = {
                    "channel_id": first_config.channel_id,
                    "emoji_roles": {
                        emoji: config.role_id
                        for emoji, config in emoji_configs.items()
                    },
                }
            if serialized_messages:
                serialized[str(guild_id)] = serialized_messages

        try:
            REACTION_ROLE_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save reaction-role data to %s", REACTION_ROLE_DATA_PATH)

    def _save_reaction_role_data_to_postgres(
        self,
        configs: Dict[int, Dict[int, Dict[str, ReactionRoleConfig]]],
    ) -> None:
        rows = [
            (guild_id, config.channel_id, message_id, emoji, config.role_id)
            for guild_id, message_configs in configs.items()
            for message_id, emoji_configs in message_configs.items()
            for emoji, config in emoji_configs.items()
        ]
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM reaction_role_configs")
                    if rows:
                        cur.executemany(
                            """
                            INSERT INTO reaction_role_configs (guild_id, channel_id, message_id, emoji, role_id)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            rows,
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save reaction-role data to PostgreSQL.")

    async def persist_reaction_role_data(self) -> None:
        await asyncio.to_thread(self.save_reaction_role_data)

    def normalize_reaction_role_emoji(self, value: str) -> Optional[str]:
        return self.normalize_autoreact_emoji(value)

    @staticmethod
    def parse_reaction_role_message_id(value: str) -> Optional[int]:
        cleaned = value.strip()
        if cleaned.isdigit():
            return int(cleaned)

        match = re.search(r"/(\d{15,25})(?:\?.*)?$", cleaned)
        if match:
            return int(match.group(1))
        return None

    def get_reaction_role_configs(self, guild_id: int) -> Dict[int, Dict[str, ReactionRoleConfig]]:
        return self.reaction_role_configs.setdefault(guild_id, {})

    def get_reaction_role_config(
        self,
        guild_id: int,
        message_id: int,
        emoji: str,
    ) -> Optional[ReactionRoleConfig]:
        return self.reaction_role_configs.get(guild_id, {}).get(message_id, {}).get(emoji)

    async def set_reaction_role_config(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        emoji: str,
        role_id: int,
    ) -> ReactionRoleConfig:
        config = ReactionRoleConfig(
            channel_id=channel_id,
            message_id=message_id,
            emoji=emoji,
            role_id=role_id,
        )
        self.get_reaction_role_configs(guild_id).setdefault(message_id, {})[emoji] = config
        await self.persist_reaction_role_data()
        return config

    async def remove_reaction_role_config(self, guild_id: int, message_id: int, emoji: str) -> Optional[ReactionRoleConfig]:
        guild_configs = self.reaction_role_configs.get(guild_id)
        if not guild_configs:
            return None

        emoji_configs = guild_configs.get(message_id)
        if not emoji_configs:
            return None

        removed = emoji_configs.pop(emoji, None)
        if not emoji_configs:
            guild_configs.pop(message_id, None)
        if not guild_configs:
            self.reaction_role_configs.pop(guild_id, None)
        if removed is not None:
            await self.persist_reaction_role_data()
        return removed

    async def get_reaction_role_member(
        self,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
    ) -> Optional[discord.Member]:
        payload_member = getattr(payload, "member", None)
        if isinstance(payload_member, discord.Member):
            return payload_member

        member = guild.get_member(payload.user_id)
        if member is not None:
            return member

        try:
            return await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            LOGGER.warning("Could not fetch reaction-role member %s in guild %s", payload.user_id, guild.id)
            return None

    async def handle_reaction_role_payload(
        self,
        payload: discord.RawReactionActionEvent,
        *,
        add_role: bool,
    ) -> None:
        if payload.guild_id is None:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return

        emoji = self.normalize_reaction_role_emoji(str(payload.emoji))
        if emoji is None:
            return

        config = self.get_reaction_role_config(payload.guild_id, payload.message_id, emoji)
        if config is None:
            return

        guild = self.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(config.role_id)
        if role is None:
            LOGGER.warning(
                "Reaction-role target role %s was not found in guild %s for message %s",
                config.role_id,
                guild.id,
                config.message_id,
            )
            return

        member = await self.get_reaction_role_member(guild, payload)
        if member is None or member.bot:
            return

        reason = f"Reaction role {emoji} on message {config.message_id}"
        try:
            if add_role:
                if role not in member.roles:
                    await member.add_roles(role, reason=reason)
                return

            if role in member.roles:
                await member.remove_roles(role, reason=reason)
        except discord.HTTPException:
            LOGGER.exception(
                "Failed to %s reaction role %s for member %s in guild %s",
                "add" if add_role else "remove",
                role.id,
                member.id,
                guild.id,
            )

    async def handle_autoreactions(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        channel_configs = self.autoreact_configs.get(message.guild.id, {})
        config = channel_configs.get(message.channel.id)
        if config is None:
            return

        for emoji in config.emojis:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                LOGGER.warning(
                    "Failed to add auto-reaction %s in guild %s channel %s message %s",
                    emoji,
                    message.guild.id,
                    message.channel.id,
                    message.id,
                )

    async def load_no_link_data(self) -> None:
        self.no_link_channels = await asyncio.to_thread(self._load_no_link_data_sync)

    def _load_no_link_data_sync(self) -> Dict[int, set[int]]:
        if self.uses_postgres:
            loaded_data = self._load_no_link_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_no_link_data_from_json()
            if fallback_data:
                self._save_no_link_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL no-link data from %s", NO_LINK_DATA_PATH)
            return fallback_data

        return self._load_no_link_data_from_json()

    def _load_no_link_data_from_json(self) -> Dict[int, set[int]]:
        loaded_data: Dict[int, set[int]] = {}
        if not NO_LINK_DATA_PATH.exists():
            LOGGER.info("No-link data file %s not found. A new one will be created on first activation.", NO_LINK_DATA_PATH)
            return loaded_data

        try:
            raw = json.loads(NO_LINK_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load no-link data from %s", NO_LINK_DATA_PATH)
            return loaded_data

        for guild_id, channels in (raw if isinstance(raw, dict) else {}).items():
            try:
                parsed_guild_id = int(guild_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(channels, list):
                continue

            parsed_channels = {
                int(channel_id)
                for channel_id in channels
                if str(channel_id).isdigit() and int(channel_id) > 0
            }
            if parsed_channels:
                loaded_data[parsed_guild_id] = parsed_channels

        LOGGER.info("Loaded no-link channel data for %s guild(s) from %s", len(loaded_data), NO_LINK_DATA_PATH)
        return loaded_data

    def _load_no_link_data_from_postgres(self) -> Dict[int, set[int]]:
        loaded_data: Dict[int, set[int]] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, channel_id
                        FROM no_link_channels
                        """
                    )
                    for guild_id, channel_id in cur.fetchall():
                        loaded_data.setdefault(int(guild_id), set()).add(int(channel_id))
        except Exception:
            LOGGER.exception("Failed to load no-link data from PostgreSQL.")
            return {}

        LOGGER.info("Loaded no-link channel data for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_no_link_data(self) -> None:
        if self.uses_postgres:
            self._save_no_link_data_to_postgres(self.no_link_channels)
            return
        self._save_no_link_data_to_json(self.no_link_channels)

    def _save_no_link_data_to_json(self, no_link_channels: Dict[int, set[int]]) -> None:
        serialized = {
            str(guild_id): sorted(channel_ids)
            for guild_id, channel_ids in no_link_channels.items()
            if channel_ids
        }
        try:
            NO_LINK_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save no-link data to %s", NO_LINK_DATA_PATH)

    def _save_no_link_data_to_postgres(self, no_link_channels: Dict[int, set[int]]) -> None:
        rows = [
            (guild_id, channel_id)
            for guild_id, channel_ids in no_link_channels.items()
            for channel_id in sorted(channel_ids)
        ]
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM no_link_channels")
                    if rows:
                        cur.executemany(
                            """
                            INSERT INTO no_link_channels (guild_id, channel_id)
                            VALUES (%s, %s)
                            """,
                            rows,
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save no-link data to PostgreSQL.")

    async def persist_no_link_data(self) -> None:
        await asyncio.to_thread(self.save_no_link_data)

    async def load_afk_data(self) -> None:
        self.afk_statuses = await asyncio.to_thread(self._load_afk_data_sync)

    def _load_afk_data_sync(self) -> Dict[int, Dict[int, AFKStatus]]:
        if self.uses_postgres:
            loaded_data = self._load_afk_data_from_postgres()
            if loaded_data:
                return loaded_data

            fallback_data = self._load_afk_data_from_json()
            if fallback_data:
                self._save_afk_data_to_postgres(fallback_data)
                LOGGER.info("Seeded PostgreSQL AFK data from %s", AFK_DATA_PATH)
            return fallback_data

        return self._load_afk_data_from_json()

    def _load_afk_data_from_json(self) -> Dict[int, Dict[int, AFKStatus]]:
        loaded_data: Dict[int, Dict[int, AFKStatus]] = {}
        if not AFK_DATA_PATH.exists():
            LOGGER.info("AFK data file %s not found. A new one will be created on first use.", AFK_DATA_PATH)
            return loaded_data

        try:
            raw = json.loads(AFK_DATA_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.exception("Failed to load AFK data from %s", AFK_DATA_PATH)
            return loaded_data

        for guild_id, users in (raw if isinstance(raw, dict) else {}).items():
            try:
                parsed_guild_id = int(guild_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(users, dict):
                continue

            parsed_statuses: Dict[int, AFKStatus] = {}
            for user_id, payload in users.items():
                try:
                    parsed_user_id = int(user_id)
                except (TypeError, ValueError):
                    continue

                if isinstance(payload, dict):
                    raw_reason = payload.get("reason", AFK_DEFAULT_REASON)
                    created_at = self.parse_stored_datetime(payload.get("created_at"))
                else:
                    raw_reason = payload
                    created_at = utc_now()

                parsed_statuses[parsed_user_id] = AFKStatus(
                    guild_id=parsed_guild_id,
                    user_id=parsed_user_id,
                    reason=self.normalize_afk_reason(str(raw_reason)),
                    created_at=created_at,
                )

            if parsed_statuses:
                loaded_data[parsed_guild_id] = parsed_statuses

        LOGGER.info("Loaded AFK data for %s guild(s) from %s", len(loaded_data), AFK_DATA_PATH)
        return loaded_data

    def _load_afk_data_from_postgres(self) -> Dict[int, Dict[int, AFKStatus]]:
        loaded_data: Dict[int, Dict[int, AFKStatus]] = {}
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT guild_id, user_id, reason, created_at
                        FROM afk_statuses
                        """
                    )
                    for guild_id, user_id, reason, created_at in cur.fetchall():
                        parsed_guild_id = int(guild_id)
                        loaded_data.setdefault(parsed_guild_id, {})[int(user_id)] = AFKStatus(
                            guild_id=parsed_guild_id,
                            user_id=int(user_id),
                            reason=self.normalize_afk_reason(str(reason)),
                            created_at=self.parse_stored_datetime(created_at),
                        )
        except Exception:
            LOGGER.exception("Failed to load AFK data from PostgreSQL.")
            return {}

        LOGGER.info("Loaded AFK data for %s guild(s) from PostgreSQL", len(loaded_data))
        return loaded_data

    def save_afk_data(self) -> None:
        if self.uses_postgres:
            self._save_afk_data_to_postgres(self.afk_statuses)
            return
        self._save_afk_data_to_json(self.afk_statuses)

    def _save_afk_data_to_json(self, statuses: Dict[int, Dict[int, AFKStatus]]) -> None:
        serialized = {
            str(guild_id): {
                str(user_id): {
                    "reason": status.reason,
                    "created_at": status.created_at.astimezone(timezone.utc).isoformat(),
                }
                for user_id, status in user_statuses.items()
            }
            for guild_id, user_statuses in statuses.items()
            if user_statuses
        }
        try:
            AFK_DATA_PATH.write_text(json.dumps(serialized, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("Failed to save AFK data to %s", AFK_DATA_PATH)

    def _save_afk_data_to_postgres(self, statuses: Dict[int, Dict[int, AFKStatus]]) -> None:
        rows = [
            (guild_id, user_id, status.reason, status.created_at)
            for guild_id, user_statuses in statuses.items()
            for user_id, status in user_statuses.items()
        ]
        try:
            with psycopg.connect(self.settings.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM afk_statuses")
                    if rows:
                        cur.executemany(
                            """
                            INSERT INTO afk_statuses (guild_id, user_id, reason, created_at)
                            VALUES (%s, %s, %s, %s)
                            """,
                            rows,
                        )
                conn.commit()
        except Exception:
            LOGGER.exception("Failed to save AFK data to PostgreSQL.")

    async def persist_afk_data(self) -> None:
        await asyncio.to_thread(self.save_afk_data)

    @staticmethod
    def parse_stored_datetime(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return utc_now()
        else:
            return utc_now()

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def format_afk_reason_for_display(reason: str) -> str:
        return discord.utils.escape_markdown(discord.utils.escape_mentions(reason))

    def normalize_afk_reason(self, reason: Optional[str]) -> str:
        cleaned = normalize_optional_text(reason or "")
        if cleaned is None:
            return AFK_DEFAULT_REASON
        return truncate_text(cleaned, AFK_REASON_LIMIT)

    def get_afk_status(self, guild_id: int, user_id: int) -> Optional[AFKStatus]:
        return self.afk_statuses.get(guild_id, {}).get(user_id)

    async def set_afk_status(self, guild_id: int, user_id: int, reason: Optional[str]) -> AFKStatus:
        status = AFKStatus(
            guild_id=guild_id,
            user_id=user_id,
            reason=self.normalize_afk_reason(reason),
        )
        self.afk_statuses.setdefault(guild_id, {})[user_id] = status
        await self.persist_afk_data()
        return status

    async def clear_afk_status(self, guild_id: int, user_id: int) -> Optional[AFKStatus]:
        guild_statuses = self.afk_statuses.get(guild_id)
        if not guild_statuses:
            return None

        status = guild_statuses.pop(user_id, None)
        if not guild_statuses:
            self.afk_statuses.pop(guild_id, None)
        if status is not None:
            await self.persist_afk_data()
        return status

    def format_afk_elapsed(self, status: AFKStatus) -> str:
        elapsed = utc_now() - status.created_at
        if elapsed.total_seconds() < 0:
            elapsed = timedelta(seconds=0)
        return format_duration(elapsed)

    async def clear_afk_on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        status = await self.clear_afk_status(message.guild.id, message.author.id)
        if status is None:
            return

        try:
            await message.channel.send(
                f"Welcome back {message.author.mention}, I removed your AFK status.",
                delete_after=8,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        except discord.HTTPException:
            LOGGER.warning(
                "Failed to send AFK return message in guild %s channel %s",
                message.guild.id,
                message.channel.id,
            )

    async def handle_afk_mentions(self, message: discord.Message) -> None:
        if message.guild is None or not message.mentions:
            return

        guild_statuses = self.afk_statuses.get(message.guild.id, {})
        if not guild_statuses:
            return

        lines = []
        seen_user_ids: set[int] = set()
        for user in message.mentions:
            if user.id == message.author.id or user.id in seen_user_ids:
                continue

            status = guild_statuses.get(user.id)
            if status is None:
                continue

            seen_user_ids.add(user.id)
            display_name = discord.utils.escape_markdown(getattr(user, "display_name", user.name))
            reason = self.format_afk_reason_for_display(status.reason)
            lines.append(f"**{display_name}** is AFK: {reason} (since {self.format_afk_elapsed(status)} ago)")

        if not lines:
            return

        if len(lines) > AFK_MENTION_REPLY_LIMIT:
            hidden_count = len(lines) - AFK_MENTION_REPLY_LIMIT
            lines = lines[:AFK_MENTION_REPLY_LIMIT]
            lines.append(f"...and {hidden_count} more AFK member{'s' if hidden_count != 1 else ''}.")

        try:
            await message.reply(
                "\n".join(lines),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            LOGGER.warning(
                "Failed to send AFK mention reply in guild %s channel %s",
                message.guild.id,
                message.channel.id,
            )

    def message_contains_blocked_link(self, content: str) -> bool:
        return bool(URL_RE.search(content))

    async def handle_no_link_message(self, message: discord.Message) -> bool:
        if message.guild is None or not isinstance(message.channel, discord.TextChannel):
            return False

        blocked_channels = self.no_link_channels.get(message.guild.id, set())
        if message.channel.id not in blocked_channels:
            return False
        if not self.message_contains_blocked_link(message.content):
            return False

        try:
            await message.delete()
        except discord.HTTPException:
            LOGGER.warning(
                "Failed to delete blocked link message in guild %s channel %s message %s",
                message.guild.id,
                message.channel.id,
                message.id,
            )
            return False

        try:
            warning = await message.channel.send(
                f"{message.author.mention} I removed your message because links are not allowed in this channel.",
                delete_after=8,
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
            LOGGER.debug("Posted no-link warning message %s", warning.id)
        except discord.HTTPException:
            LOGGER.warning("Failed to send no-link warning in channel %s", message.channel.id)
        return True

    async def ensure_staff(self, interaction: discord.Interaction, permission: str) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None or not self.has_staff_access(member, permission):
            await self.send_interaction_message(interaction, NO_PERMISSION, ephemeral=True)
            return False
        return True

    async def safe_dm(self, user: discord.abc.User, embed: discord.Embed) -> None:
        try:
            await user.send(**build_embed_send_kwargs(embed))
        except discord.HTTPException:
            LOGGER.warning("Could not DM %s (%s)", user, user.id)

    def normalize_autoreact_emoji(self, value: str) -> Optional[str]:
        cleaned = value.strip()
        if not cleaned:
            return None

        partial = discord.PartialEmoji.from_str(cleaned)
        if partial.id is not None:
            return str(partial)
        if partial.name:
            return partial.name
        return cleaned

    def parse_autoreact_emojis(self, value: str) -> List[str]:
        parsed_emojis: List[str] = []
        for part in value.split(","):
            emoji = self.normalize_autoreact_emoji(part)
            if emoji is not None and emoji not in parsed_emojis:
                parsed_emojis.append(emoji)
        return parsed_emojis

    def get_autoreact_configs(self, guild_id: int) -> Dict[int, AutoReactionConfig]:
        return self.autoreact_configs.setdefault(guild_id, {})

    def create_autoreact_embed(self, guild: discord.Guild) -> discord.Embed:
        channel_configs = self.get_autoreact_configs(guild.id)
        if not channel_configs:
            return make_embed(
                "Auto-Reactions",
                "No auto-reaction channels are configured for this server yet.",
                discord.Color.blurple(),
            )

        lines = []
        for channel_id, config in sorted(channel_configs.items()):
            lines.append(f"Channel: <#{channel_id}> | Emojis: {' '.join(config.emojis)}")

        return make_embed("Auto-Reactions", "\n".join(lines), discord.Color.blurple())

    def create_reaction_role_panel_embed(
        self,
        role: discord.Role,
        emoji: str,
        title: Optional[str],
        description: Optional[str],
    ) -> discord.Embed:
        cleaned_title = truncate_text(normalize_optional_text(title or "") or "Reaction Role", 256)
        cleaned_description = normalize_optional_text(description or "")
        instruction = f"React with {emoji} to get {role.mention}.\n{REACTION_ROLE_REMOVE_HINT}"
        if cleaned_description is None:
            cleaned_description = instruction
        elif REACTION_ROLE_REMOVE_HINT not in cleaned_description:
            cleaned_description = f"{cleaned_description}\n\n{instruction}"

        return make_embed(cleaned_title, truncate_text(cleaned_description, 4000), discord.Color.blurple())

    def create_reaction_role_list_embed(self, guild: discord.Guild) -> discord.Embed:
        guild_configs = self.reaction_role_configs.get(guild.id, {})
        if not guild_configs:
            return make_embed(
                "Reaction Roles",
                "No reaction-role bindings are configured for this server yet.",
                discord.Color.blurple(),
            )

        lines = []
        for message_id, emoji_configs in sorted(guild_configs.items()):
            for emoji, config in sorted(emoji_configs.items()):
                role = guild.get_role(config.role_id)
                role_text = role.mention if role is not None else f"`{config.role_id}`"
                lines.append(f"{emoji} -> {role_text} in <#{config.channel_id}> (`{message_id}`)")

        return make_embed("Reaction Roles", truncate_text("\n".join(lines), 4000), discord.Color.blurple())

    def create_reaction_role_instruction_value(self, guild: discord.Guild, message_id: int) -> Optional[str]:
        emoji_configs = self.reaction_role_configs.get(guild.id, {}).get(message_id, {})
        if not emoji_configs:
            return None

        lines = []
        for emoji, config in sorted(emoji_configs.items()):
            role = guild.get_role(config.role_id)
            role_text = role.mention if role is not None else f"`{config.role_id}`"
            lines.append(f"React with {emoji} to get {role_text}.")
        lines.append(REACTION_ROLE_REMOVE_HINT)
        return truncate_text("\n".join(lines), 1024)

    def is_generated_reaction_role_panel_embed(self, embed: discord.Embed) -> bool:
        title = (embed.title or "").strip().lower()
        description = embed.description or ""
        field_names = {str(field.name).strip().lower() for field in embed.fields}
        return (
            title in {"reaction role", "reaction roles"}
            or REACTION_ROLE_REMOVE_HINT in description
            or {"role", "emoji"}.issubset(field_names)
        )

    def create_updated_reaction_role_embed(
        self,
        message: discord.Message,
        guild: discord.Guild,
    ) -> tuple[List[discord.Embed], bool]:
        embeds = [embed.copy() for embed in message.embeds]
        if embeds:
            embed = embeds[0]
        else:
            embed = make_embed(
                "Reaction Roles",
                REACTION_ROLE_GENERIC_DESCRIPTION,
                discord.Color.blurple(),
            )
            embeds.append(embed)

        is_generated_panel = self.is_generated_reaction_role_panel_embed(embed)
        fields = [
            (field.name, field.value, field.inline)
            for field in embed.fields
            if str(field.name).strip().lower() != REACTION_ROLE_FIELD_NAME.lower()
            and not (is_generated_panel and str(field.name).strip().lower() in {"role", "emoji"})
        ]
        embed.clear_fields()
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)

        instruction_value = self.create_reaction_role_instruction_value(guild, message.id)
        if is_generated_panel:
            embed.description = instruction_value or REACTION_ROLE_GENERIC_DESCRIPTION
        elif instruction_value is not None:
            embed.add_field(name=REACTION_ROLE_FIELD_NAME, value=instruction_value, inline=False)

        embeds[0] = embed
        return embeds[:10], instruction_value is not None

    async def update_reaction_role_message_embed(
        self,
        message: discord.Message,
        guild: discord.Guild,
    ) -> tuple[bool, Optional[str]]:
        if self.user is None or message.author.id != self.user.id:
            return False, "I can only edit reaction-role instructions on messages I sent."

        embeds, _has_instruction = self.create_updated_reaction_role_embed(message, guild)
        try:
            await message.edit(embeds=embeds)
        except discord.Forbidden:
            return False, "I do not have permission to edit that message."
        except discord.HTTPException:
            LOGGER.exception("Failed to update reaction-role embed on message %s", message.id)
            return False, "I could not update that message embed right now."
        return True, None

    async def fetch_reaction_role_message(
        self,
        channel: discord.TextChannel,
        message_id: int,
    ) -> tuple[Optional[discord.Message], Optional[str]]:
        try:
            return await channel.fetch_message(message_id), None
        except discord.NotFound:
            return None, f"I could not find message `{message_id}` in {channel.mention}."
        except discord.Forbidden:
            return None, f"I do not have permission to read message history in {channel.mention}."
        except discord.HTTPException:
            LOGGER.exception("Failed to fetch reaction-role message %s in channel %s", message_id, channel.id)
            return None, "I could not fetch that message right now. Please try again."

    async def resolve_reaction_role_channel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> Optional[discord.TextChannel]:
        if channel is not None:
            return channel
        if isinstance(interaction.channel, discord.TextChannel):
            return interaction.channel
        await self.send_interaction_message(interaction, "Please choose a text channel.", ephemeral=True)
        return None

    def get_qotd_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        return discord.utils.get(guild.roles, name=QOTD_ROLE_NAME)

    def create_qotd_embed(self, question: str) -> discord.Embed:
        return make_embed(
            "📌 Question of the Day",
            question,
            discord.Color.gold(),
            footer="Reply in the thread below 👇",
        )

    def create_qotd_thread_name(self, question: str) -> str:
        short_text = truncate_text(question, 45)
        slug = slugify_text(short_text)
        if slug:
            return truncate_text(f"QOTD - {slug}", 100)
        return f"QOTD - {utc_now().strftime('%Y-%m-%d')}"

    def normalize_thread_archive_duration(self, hours: int) -> int:
        requested_minutes = max(60, hours * 60)
        valid_durations = (60, 1440, 4320, 10080)
        return min(valid_durations, key=lambda duration: (abs(duration - requested_minutes), duration))

    async def handle_warn(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.safe_dm(
            user,
            make_embed(
                "Warning",
                f"You have been warned in **{interaction.guild.name}**.\n\nReason: {reason}",
                discord.Color.yellow(),
            ),
        )

        embed = self.create_modlog_embed("WARN", user, interaction.user, reason)
        await self.send_modlog(embed)
        await self.add_modlog("WARN", user, interaction.user, interaction.guild.id if interaction.guild else None, reason)
        await self.log_moderator_command(interaction, "/warn", user, reason)
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_mute(self, interaction: discord.Interaction, user: discord.Member, duration_text: str, reason: str) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return

        duration = parse_duration(duration_text)
        if duration is None:
            await interaction.response.send_message(INVALID_DURATION, ephemeral=True)
            return
        if duration > timedelta(days=MAX_TIMEOUT_DAYS):
            await interaction.response.send_message("Duration cannot exceed 28 days.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await user.timeout(duration, reason=reason)
        await self.safe_dm(
            user,
            make_embed(
                "Timeout",
                f"You have been timed out in **{interaction.guild.name}** for {format_duration(duration)}.\n\nReason: {reason}",
                discord.Color.orange(),
            ),
        )

        embed = self.create_modlog_embed("MUTE", user, interaction.user, reason)
        embed.add_field(name="Duration", value=format_duration(duration), inline=False)
        await self.send_modlog(embed)
        await self.add_modlog(
            "MUTE",
            user,
            interaction.user,
            interaction.guild.id if interaction.guild else None,
            reason,
            format_duration(duration),
        )
        await self.log_moderator_command(interaction, "/mute", user, f"{reason} | Duration: {format_duration(duration)}")
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_unmute(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return

        timed_out_until = user.timed_out_until
        if timed_out_until is None or timed_out_until <= utc_now():
            await interaction.response.send_message(f"{user.mention} is not currently timed out.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await user.timeout(None, reason=reason)
        await self.safe_dm(
            user,
            make_embed(
                "Timeout Removed",
                f"Your timeout in **{interaction.guild.name}** has been removed.\n\nReason: {reason}",
                discord.Color.green(),
            ),
        )

        embed = self.create_modlog_embed("UNMUTE", user, interaction.user, reason)
        await self.send_modlog(embed)
        await self.add_modlog("UNMUTE", user, interaction.user, interaction.guild.id if interaction.guild else None, reason)
        await self.log_moderator_command(interaction, "/unmute", user, reason)
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_kick(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        if not await self.ensure_staff(interaction, "kick_members"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.safe_dm(
            user,
            make_embed(
                "Kick",
                f"You have been kicked from **{interaction.guild.name}**.\n\nReason: {reason}",
                discord.Color.red(),
            ),
        )
        await user.kick(reason=reason)

        embed = self.create_modlog_embed("KICK", user, interaction.user, reason)
        await self.send_modlog(embed)
        await self.add_modlog("KICK", user, interaction.user, interaction.guild.id if interaction.guild else None, reason)
        await self.log_moderator_command(interaction, "/kick", user, reason)
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_ban(self, interaction: discord.Interaction, user: discord.Member, reason: str, delete_days: int) -> None:
        if not await self.ensure_staff(interaction, "ban_members"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.safe_dm(
            user,
            make_embed(
                "Ban",
                f"You have been banned from **{interaction.guild.name}**.\n\nReason: {reason}",
                discord.Color.dark_red(),
            ),
        )
        await interaction.guild.ban(user, reason=reason, delete_message_seconds=delete_days * 86400)

        embed = self.create_modlog_embed("BAN", user, interaction.user, reason)
        if delete_days:
            embed.add_field(name="Deleted Messages", value=f"{delete_days} day(s)", inline=False)
        await self.send_modlog(embed)
        await self.add_modlog("BAN", user, interaction.user, interaction.guild.id if interaction.guild else None, reason)
        await self.log_moderator_command(interaction, "/ban", user, f"{reason} | Delete days: {delete_days}")
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_unban(self, interaction: discord.Interaction, user_id: str, reason: str) -> None:
        if not await self.ensure_staff(interaction, "ban_members"):
            return
        if not re.fullmatch(r"\d{17,20}", user_id):
            await interaction.response.send_message("Please provide a valid Discord user ID.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        target = discord.Object(id=int(user_id))
        try:
            ban_entry = await interaction.guild.fetch_ban(target)
        except discord.NotFound:
            await interaction.followup.send("That user is not banned.", ephemeral=True)
            return

        await interaction.guild.unban(ban_entry.user, reason=reason)
        embed = self.create_modlog_embed("UNBAN", ban_entry.user, interaction.user, reason)
        await self.send_modlog(embed)
        await self.add_modlog("UNBAN", ban_entry.user, interaction.user, interaction.guild.id if interaction.guild else None, reason)
        await self.log_moderator_command(interaction, "/unban", ban_entry.user, reason)
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_role_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: discord.Role,
        reason: str,
        *,
        command_name: str = "/addrole",
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user) or self.can_manage_role(moderator, role)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return
        if role in user.roles:
            await interaction.response.send_message(f"{user.mention} already has {role.mention}.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await user.add_roles(role, reason=f"{reason} | Added by {interaction.user} ({interaction.user.id})")
        embed = self.create_modlog_embed("ROLE ADD", user, interaction.user, reason)
        embed.add_field(name="Role", value=f"{role.mention} ({role.id})", inline=False)
        await self.send_modlog(embed)
        await self.add_modlog(
            "ROLE ADD",
            user,
            interaction.user,
            interaction.guild.id if interaction.guild else None,
            f"{reason} | Role: {role.name}",
        )
        await self.log_moderator_command(interaction, command_name, user, f"{reason} | Role: {role.name}")
        await interaction.followup.send(f"Added {role.mention} to {user.mention}.", ephemeral=True)

    async def handle_role_remove(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        role: discord.Role,
        reason: str,
        *,
        command_name: str = "/removerole",
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        moderator = interaction.user
        if not isinstance(moderator, discord.Member):
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return
        blocked_reason = self.can_act_on_target(moderator, user) or self.can_manage_role(moderator, role)
        if blocked_reason:
            await interaction.response.send_message(blocked_reason, ephemeral=True)
            return
        if role not in user.roles:
            await interaction.response.send_message(f"{user.mention} does not have {role.mention}.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await user.remove_roles(role, reason=f"{reason} | Removed by {interaction.user} ({interaction.user.id})")
        embed = self.create_modlog_embed("ROLE REMOVE", user, interaction.user, reason)
        embed.add_field(name="Role", value=f"{role.mention} ({role.id})", inline=False)
        await self.send_modlog(embed)
        await self.add_modlog(
            "ROLE REMOVE",
            user,
            interaction.user,
            interaction.guild.id if interaction.guild else None,
            f"{reason} | Role: {role.name}",
        )
        await self.log_moderator_command(interaction, command_name, user, f"{reason} | Role: {role.name}")
        await interaction.followup.send(f"Removed {role.mention} from {user.mention}.", ephemeral=True)

    async def handle_clear(self, interaction: discord.Interaction, amount: int, user: Optional[discord.Member]) -> None:
        if not await self.ensure_staff(interaction, "manage_messages"):
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This command can only be used in a text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        remaining = amount

        def should_delete(message: discord.Message) -> bool:
            nonlocal remaining
            if remaining <= 0:
                return False
            if user is not None and message.author.id != user.id:
                return False
            if (utc_now() - message.created_at) >= timedelta(days=14):
                return False
            remaining -= 1
            return True

        deleted = await interaction.channel.purge(limit=min(1000, amount + 200), check=should_delete, bulk=True)
        target = user or interaction.user
        embed = self.create_modlog_embed("CLEAR", target, interaction.user, f"Cleared {len(deleted)} message(s)")
        await self.send_modlog(embed)
        await self.add_modlog(
            "CLEAR",
            target,
            interaction.user,
            interaction.guild.id if interaction.guild else None,
            f"Cleared {len(deleted)} message(s)",
        )
        await self.log_moderator_command(interaction, "/clear", target, f"Cleared {len(deleted)} message(s)")
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)

    async def handle_modlogs(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return

        if self.uses_postgres and interaction.guild is not None:
            related = await asyncio.to_thread(self.load_modlogs_from_postgres, interaction.guild.id, user.id, limit=10)
        else:
            guild_id = interaction.guild.id if interaction.guild is not None else None
            related = [
                entry
                for entry in reversed(self.mod_logs)
                if entry.user_id == user.id and (guild_id is None or entry.guild_id == guild_id)
            ][:10]
        description = "\n".join(
            f"`{entry.action}` by <@{entry.moderator_id}> - {entry.reason}"
            + (f" ({entry.duration_text})" if entry.duration_text else "")
            for entry in related
        ) or "No moderation entries found for this user yet."

        embed = make_embed(
            "Moderation Logs",
            f"User: **{user}** (`{user.id}`)\n\n{description}",
            discord.Color.blurple(),
        )
        await self.send_interaction_message(interaction, embed=embed, ephemeral=True)

    async def handle_afk(self, interaction: discord.Interaction, reason: Optional[str]) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(
                interaction,
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        status = await self.set_afk_status(interaction.guild.id, interaction.user.id, reason)
        safe_reason = self.format_afk_reason_for_display(status.reason)
        await self.send_interaction_message(
            interaction,
            f"You're now AFK: **{safe_reason}**\nI'll let people know when they mention you.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_afk_prefix(self, context: commands.Context, reason: Optional[str]) -> None:
        if context.guild is None:
            await context.send("This command can only be used inside a server.")
            return

        status = await self.set_afk_status(context.guild.id, context.author.id, reason)
        safe_reason = self.format_afk_reason_for_display(status.reason)
        await context.send(
            f"You're now AFK: **{safe_reason}**\nI'll let people know when they mention you.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_prefix_show(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.send_interaction_message(
                interaction,
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        prefix = self.get_guild_prefix(interaction.guild.id)
        await self.send_interaction_message(
            interaction,
            (
                f"Current command prefix: {self.format_prefix_for_display(prefix)}\n"
                "Mentioning the bot also works as a prefix."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_prefix_set(self, interaction: discord.Interaction, prefix: str) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(
                interaction,
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        parsed_prefix, error = self.validate_command_prefix(prefix)
        if error is not None or parsed_prefix is None:
            await self.send_interaction_message(interaction, error or "Invalid prefix.", ephemeral=True)
            return

        await self.set_guild_prefix(interaction.guild.id, parsed_prefix)
        await self.send_interaction_message(
            interaction,
            f"Command prefix set to {self.format_prefix_for_display(parsed_prefix)}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_prefix_reset(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(
                interaction,
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        await self.reset_guild_prefix(interaction.guild.id)
        await self.send_interaction_message(
            interaction,
            f"Command prefix reset to {self.format_prefix_for_display(DEFAULT_COMMAND_PREFIX)}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def can_manage_prefix(self, context: commands.Context) -> bool:
        return isinstance(context.author, discord.Member) and self.has_staff_access(context.author, "manage_guild")

    async def handle_prefix_help(self, context: commands.Context) -> None:
        prefix = self.get_guild_prefix(context.guild.id) if context.guild is not None else DEFAULT_COMMAND_PREFIX
        embed = discord.Embed(
            title="Northeast Esports Prefix Commands",
            description=(
                f"Current prefix: {self.format_prefix_for_display(prefix)}\n"
                "Mentioning the bot also works as a prefix."
            ),
            color=discord.Color.blurple(),
            timestamp=utc_now(),
        )
        embed.add_field(
            name="Community",
            value=self.format_command_example(prefix, "afk [reason]"),
            inline=False,
        )
        embed.add_field(
            name="Prefix",
            value=(
                f"{self.format_command_example(prefix, 'prefix show')}\n"
                f"{self.format_command_example(prefix, 'prefix set ?')}\n"
                f"{self.format_command_example(prefix, 'prefix reset')}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Slash Commands",
            value="Use `/help` for the full slash-command list.",
            inline=False,
        )
        embed.set_footer(text=BRAND_FOOTER)
        set_default_thumbnail(embed)
        await context.send(**build_embed_send_kwargs(embed, allowed_mentions=discord.AllowedMentions.none()))

    async def handle_prefix_command(
        self,
        context: commands.Context,
        action: Optional[str],
        prefix: Optional[str],
    ) -> None:
        if context.guild is None:
            await context.send("This command can only be used inside a server.")
            return

        current_prefix = self.get_guild_prefix(context.guild.id)
        normalized_action = (action or "show").strip().lower()

        if normalized_action in {"show", "current"}:
            await context.send(
                (
                    f"Current command prefix: {self.format_prefix_for_display(current_prefix)}\n"
                    "Mentioning the bot also works as a prefix."
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if normalized_action in {"set", "change"}:
            if not self.can_manage_prefix(context):
                await context.send(NO_PERMISSION)
                return
            if prefix is None:
                await context.send(
                    f"Usage: {self.format_command_example(current_prefix, 'prefix set ?')}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            parsed_prefix, error = self.validate_command_prefix(prefix)
            if error is not None or parsed_prefix is None:
                await context.send(error or "Invalid prefix.")
                return

            await self.set_guild_prefix(context.guild.id, parsed_prefix)
            await context.send(
                f"Command prefix set to {self.format_prefix_for_display(parsed_prefix)}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if normalized_action in {"reset", "default"}:
            if not self.can_manage_prefix(context):
                await context.send(NO_PERMISSION)
                return
            await self.reset_guild_prefix(context.guild.id)
            await context.send(
                f"Command prefix reset to {self.format_prefix_for_display(DEFAULT_COMMAND_PREFIX)}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await context.send(
            (
                "Usage:\n"
                f"{self.format_command_example(current_prefix, 'prefix show')}\n"
                f"{self.format_command_example(current_prefix, 'prefix set ?')}\n"
                f"{self.format_command_example(current_prefix, 'prefix reset')}"
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_qotd(
        self,
        interaction: discord.Interaction,
        question: str,
        channel: Optional[discord.TextChannel],
        auto_archive_hours: int,
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        cleaned_question = normalize_optional_text(question)
        if cleaned_question is None:
            await interaction.response.send_message("Please provide a Question of the Day.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel for the QOTD post.", ephemeral=True)
                return

        qotd_role = self.get_qotd_role(interaction.guild)
        role_mention = qotd_role.mention if qotd_role is not None else f"@{QOTD_ROLE_NAME}"
        content = f"{role_mention}\nNew Question of the Day is up."
        embed = self.create_qotd_embed(cleaned_question)
        archive_duration = self.normalize_thread_archive_duration(auto_archive_hours)

        try:
            message = await target_channel.send(
                **build_embed_send_kwargs(
                    embed,
                    content=content,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to post QOTD message in channel %s", target_channel.id)
            await interaction.response.send_message(
                f"I could not send the QOTD message in {target_channel.mention}. Check my permissions there.",
                ephemeral=True,
            )
            return

        thread_name = self.create_qotd_thread_name(cleaned_question)
        try:
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=archive_duration,
            )
            try:
                await thread.send("Reply to today’s question here so the main channel stays clean.")
            except discord.HTTPException:
                LOGGER.warning("Failed to send QOTD thread prompt in thread %s", thread.id)
        except discord.HTTPException:
            LOGGER.exception("Failed to create QOTD thread for message %s", message.id)
            await interaction.response.send_message(
                (
                    f"QOTD posted in {target_channel.mention}, but I could not create the thread. "
                    "Check my thread permissions in that channel."
                ),
                ephemeral=True,
            )
            return

        archive_text = format_duration(timedelta(minutes=archive_duration))
        role_text = role_mention if qotd_role is not None else f"`{QOTD_ROLE_NAME}` role not found"
        await interaction.response.send_message(
            (
                f"QOTD posted in {target_channel.mention} and thread {thread.mention} opened. "
                f"Pinged: {role_text}. Auto-archive: {archive_text}."
            ),
            ephemeral=True,
        )

    async def handle_autoreact_activate(
        self,
        interaction: discord.Interaction,
        emoji: str,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        normalized_emojis = self.parse_autoreact_emojis(emoji)
        if not normalized_emojis:
            await interaction.response.send_message(
                "Please provide one or more valid emojis, separated by commas.",
                ephemeral=True,
            )
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel for auto-reaction.", ephemeral=True)
                return

        channel_configs = self.get_autoreact_configs(interaction.guild.id)
        config = channel_configs.setdefault(target_channel.id, AutoReactionConfig())
        added_emojis = [item for item in normalized_emojis if item not in config.emojis]
        if not added_emojis:
            await interaction.response.send_message(
                f"Those emojis are already active for auto-reactions in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        config.emojis.extend(added_emojis)
        await self.persist_autoreact_data()
        await interaction.response.send_message(
            f"Auto-reaction updated in {target_channel.mention}. Added {' '.join(added_emojis)}. Active emojis: {' '.join(config.emojis)}.",
            ephemeral=True,
        )

    async def handle_autoreact_deactivate(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel to deactivate.", ephemeral=True)
                return

        channel_configs = self.get_autoreact_configs(interaction.guild.id)
        if target_channel.id not in channel_configs:
            await interaction.response.send_message(
                f"Auto-reaction is not active in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        channel_configs.pop(target_channel.id, None)
        await self.persist_autoreact_data()
        await interaction.response.send_message(
            f"Auto-reaction deactivated in {target_channel.mention}.",
            ephemeral=True,
        )

    async def handle_reaction_role_create(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        emoji: str,
        channel: Optional[discord.TextChannel],
        title: Optional[str],
        description: Optional[str],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        moderator = interaction.user if isinstance(interaction.user, discord.Member) else None
        if moderator is None:
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return

        normalized_emoji = self.normalize_reaction_role_emoji(emoji)
        if normalized_emoji is None:
            await interaction.response.send_message("Please provide a valid emoji.", ephemeral=True)
            return

        role_error = self.can_manage_role(moderator, role)
        if role_error is not None:
            await interaction.response.send_message(role_error, ephemeral=True)
            return

        target_channel = await self.resolve_reaction_role_channel(interaction, channel)
        if target_channel is None:
            return

        bot_member = interaction.guild.me
        if bot_member is None and self.user is not None:
            bot_member = interaction.guild.get_member(self.user.id)
        if bot_member is None:
            await interaction.response.send_message("I could not verify my channel permissions right now.", ephemeral=True)
            return

        permissions = target_channel.permissions_for(bot_member)
        if not permissions.send_messages:
            await interaction.response.send_message(
                f"I do not have permission to send messages in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        if not permissions.add_reactions:
            await interaction.response.send_message(
                f"I do not have permission to add reactions in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        if not permissions.embed_links:
            await interaction.response.send_message(
                f"I need `Embed Links` in {target_channel.mention} so I can update the reaction-role instructions.",
                ephemeral=True,
            )
            return

        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return

        embed = self.create_reaction_role_panel_embed(role, normalized_emoji, title, description)
        try:
            message = await target_channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to send reaction-role panel in channel %s", target_channel.id)
            await interaction.followup.send(
                f"I could not send the reaction-role panel in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        try:
            await message.add_reaction(normalized_emoji)
        except discord.HTTPException:
            LOGGER.exception("Failed to add reaction-role emoji %s to message %s", normalized_emoji, message.id)
            await interaction.followup.send(
                "The panel was posted, but I could not add that emoji, so no reaction role was saved.",
                ephemeral=True,
            )
            return

        await self.set_reaction_role_config(
            interaction.guild.id,
            target_channel.id,
            message.id,
            normalized_emoji,
            role.id,
        )
        await interaction.followup.send(
            f"Reaction role created in {target_channel.mention}: {normalized_emoji} gives {role.mention}.",
            ephemeral=True,
        )

    async def handle_reaction_role_add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        role: discord.Role,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        moderator = interaction.user if isinstance(interaction.user, discord.Member) else None
        if moderator is None:
            await interaction.response.send_message(NO_PERMISSION, ephemeral=True)
            return

        parsed_message_id = self.parse_reaction_role_message_id(message_id)
        if parsed_message_id is None:
            await interaction.response.send_message("Please provide a valid message ID or message link.", ephemeral=True)
            return

        normalized_emoji = self.normalize_reaction_role_emoji(emoji)
        if normalized_emoji is None:
            await interaction.response.send_message("Please provide a valid emoji.", ephemeral=True)
            return

        role_error = self.can_manage_role(moderator, role)
        if role_error is not None:
            await interaction.response.send_message(role_error, ephemeral=True)
            return

        target_channel = await self.resolve_reaction_role_channel(interaction, channel)
        if target_channel is None:
            return

        bot_member = interaction.guild.me
        if bot_member is None and self.user is not None:
            bot_member = interaction.guild.get_member(self.user.id)
        if bot_member is None:
            await interaction.response.send_message("I could not verify my channel permissions right now.", ephemeral=True)
            return

        permissions = target_channel.permissions_for(bot_member)
        if not permissions.read_message_history:
            await interaction.response.send_message(
                f"I do not have permission to read message history in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        if not permissions.add_reactions:
            await interaction.response.send_message(
                f"I do not have permission to add reactions in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        if not permissions.embed_links:
            await interaction.response.send_message(
                f"I need `Embed Links` in {target_channel.mention} so I can update the reaction-role instructions.",
                ephemeral=True,
            )
            return

        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return

        message, error = await self.fetch_reaction_role_message(target_channel, parsed_message_id)
        if message is None:
            await interaction.followup.send(error or "I could not fetch that message.", ephemeral=True)
            return

        try:
            await message.add_reaction(normalized_emoji)
        except discord.HTTPException:
            LOGGER.exception("Failed to add reaction-role emoji %s to message %s", normalized_emoji, message.id)
            await interaction.followup.send(
                "I could not add that reaction to the message, so no reaction role was saved.",
                ephemeral=True,
            )
            return

        existing = self.get_reaction_role_config(interaction.guild.id, message.id, normalized_emoji)
        await self.set_reaction_role_config(
            interaction.guild.id,
            target_channel.id,
            message.id,
            normalized_emoji,
            role.id,
        )
        embed_updated, embed_error = await self.update_reaction_role_message_embed(message, interaction.guild)
        action = "Updated" if existing is not None else "Added"
        embed_note = " Updated the message embed with reaction instructions."
        if not embed_updated:
            embed_note = f" The reaction was added, but the embed was not updated: {embed_error}"
        await interaction.followup.send(
            f"{action} reaction role in {target_channel.mention}: {normalized_emoji} gives {role.mention}.{embed_note}",
            ephemeral=True,
        )

    async def handle_reaction_role_remove(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        parsed_message_id = self.parse_reaction_role_message_id(message_id)
        if parsed_message_id is None:
            await interaction.response.send_message("Please provide a valid message ID or message link.", ephemeral=True)
            return

        normalized_emoji = self.normalize_reaction_role_emoji(emoji)
        if normalized_emoji is None:
            await interaction.response.send_message("Please provide a valid emoji.", ephemeral=True)
            return

        config = self.get_reaction_role_config(interaction.guild.id, parsed_message_id, normalized_emoji)
        if config is None:
            await interaction.response.send_message(
                "No reaction-role binding exists for that message and emoji.",
                ephemeral=True,
            )
            return

        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return

        await self.remove_reaction_role_config(interaction.guild.id, parsed_message_id, normalized_emoji)

        channel = interaction.guild.get_channel(config.channel_id)
        if channel is None:
            try:
                fetched_channel = await self.fetch_channel(config.channel_id)
            except discord.HTTPException:
                fetched_channel = None
            channel = fetched_channel if isinstance(fetched_channel, discord.TextChannel) else None

        embed_note = ""
        if isinstance(channel, discord.TextChannel) and self.user is not None:
            message, _error = await self.fetch_reaction_role_message(channel, parsed_message_id)
            if message is not None:
                try:
                    await message.remove_reaction(normalized_emoji, self.user)
                except discord.HTTPException:
                    LOGGER.warning(
                        "Removed reaction-role config but could not remove emoji %s from message %s",
                        normalized_emoji,
                        parsed_message_id,
                    )
                embed_updated, embed_error = await self.update_reaction_role_message_embed(message, interaction.guild)
                if embed_updated:
                    embed_note = " Updated the message embed."
                else:
                    embed_note = f" The embed was not updated: {embed_error}"

        await interaction.followup.send(
            f"Removed reaction-role binding for {normalized_emoji} on message `{parsed_message_id}`.{embed_note}",
            ephemeral=True,
        )

    async def handle_reaction_role_list(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff(interaction, "manage_roles"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        await self.send_interaction_message(
            interaction,
            embed=self.create_reaction_role_list_embed(interaction.guild),
            ephemeral=True,
        )

    async def handle_no_link_activate(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel for no-link mode.", ephemeral=True)
                return

        bot_member = interaction.guild.me
        if bot_member is None and self.user is not None:
            bot_member = interaction.guild.get_member(self.user.id)
        if bot_member is None:
            await interaction.response.send_message("I could not verify my channel permissions right now.", ephemeral=True)
            return

        permissions = target_channel.permissions_for(bot_member)
        if not permissions.view_channel:
            await interaction.response.send_message(
                f"I do not have permission to view {target_channel.mention}.",
                ephemeral=True,
            )
            return
        if not permissions.manage_messages:
            await interaction.response.send_message(
                f"I need `Manage Messages` in {target_channel.mention} before no-link mode can delete links there.",
                ephemeral=True,
            )
            return
        if not permissions.send_messages:
            await interaction.response.send_message(
                f"I need `Send Messages` in {target_channel.mention} so I can warn members when links are deleted.",
                ephemeral=True,
            )
            return

        blocked_channels = self.no_link_channels.setdefault(interaction.guild.id, set())
        if target_channel.id in blocked_channels:
            await interaction.response.send_message(
                f"No-link mode is already active in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        blocked_channels.add(target_channel.id)
        await self.persist_no_link_data()
        await interaction.response.send_message(
            f"No-link mode activated in {target_channel.mention}. Messages containing links will be deleted there.",
            ephemeral=True,
        )

    async def handle_no_link_deactivate(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel to disable no-link mode.", ephemeral=True)
                return

        blocked_channels = self.no_link_channels.setdefault(interaction.guild.id, set())
        if target_channel.id not in blocked_channels:
            await interaction.response.send_message(
                f"No-link mode is not active in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        blocked_channels.discard(target_channel.id)
        if not blocked_channels:
            self.no_link_channels.pop(interaction.guild.id, None)
        await self.persist_no_link_data()
        await interaction.response.send_message(
            f"No-link mode deactivated in {target_channel.mention}.",
            ephemeral=True,
        )

    async def handle_embed_builder(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await interaction.response.send_message("Please choose a text channel for the embed.", ephemeral=True)
                return

        bot_member = interaction.guild.me
        if bot_member is None and self.user is not None:
            bot_member = interaction.guild.get_member(self.user.id)
        if bot_member is None:
            await interaction.response.send_message("I could not verify my channel permissions right now.", ephemeral=True)
            return

        if not target_channel.permissions_for(bot_member).send_messages:
            await interaction.response.send_message(
                f"I do not have permission to send messages in {target_channel.mention}.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(EmbedBuilderModal(self, target_channel))

    def get_anti_raid_state(self, guild_id: int) -> AntiRaidState:
        state = self.anti_raid_states.get(guild_id)
        if state is None:
            state = AntiRaidState(enabled=self.settings.anti_raid_enabled)
            self.anti_raid_states[guild_id] = state
        return state

    def anti_raid_is_active(self, state: AntiRaidState) -> bool:
        return state.lockdown_until is not None and state.lockdown_until > utc_now()

    def prune_anti_raid_events(self, state: AntiRaidState, now: datetime) -> None:
        window = timedelta(seconds=self.settings.anti_raid_window_seconds)
        while state.join_events and (now - state.join_events[0]) > window:
            state.join_events.popleft()

    async def send_anti_raid_alert(self, guild: discord.Guild, title: str, description: str) -> None:
        embed = make_embed(title, description, discord.Color.dark_orange())
        embed.add_field(name="Server", value=f"{guild.name} (`{guild.id}`)", inline=False)
        await self.send_modlog(embed)

    def create_anti_raid_status_embed(self, guild: discord.Guild, state: AntiRaidState) -> discord.Embed:
        active = self.anti_raid_is_active(state)
        remaining = "Inactive"
        if active and state.lockdown_until is not None:
            remaining = format_duration(state.lockdown_until - utc_now())

        embed = make_embed(
            "Anti-Raid Status",
            f"Protection for **{guild.name}** is {'enabled' if state.enabled else 'disabled'}.",
            discord.Color.dark_orange() if active else discord.Color.blurple(),
        )
        embed.add_field(name="Raid Mode", value="Active" if active else "Inactive", inline=True)
        embed.add_field(name="Remaining", value=remaining, inline=True)
        embed.add_field(
            name="Trigger Rule",
            value=(
                f"{self.settings.anti_raid_join_threshold} joins in "
                f"{self.settings.anti_raid_window_seconds} seconds"
            ),
            inline=False,
        )
        embed.add_field(
            name="Auto Timeout",
            value=(
                f"Accounts newer than {self.settings.anti_raid_account_age_minutes} minute(s) "
                f"are timed out for {self.settings.anti_raid_timeout_minutes} minute(s) during raid mode."
            ),
            inline=False,
        )
        if state.last_trigger_count:
            embed.add_field(name="Last Trigger Count", value=str(state.last_trigger_count), inline=True)
        return embed

    async def activate_anti_raid(
        self,
        guild: discord.Guild,
        triggered_by: Optional[discord.abc.User],
        reason: str,
        *,
        manual: bool = False,
        trigger_count: Optional[int] = None,
    ) -> AntiRaidState:
        state = self.get_anti_raid_state(guild.id)
        state.enabled = True
        state.manual_lockdown = manual
        state.lockdown_until = utc_now() + timedelta(minutes=self.settings.anti_raid_lockdown_minutes)
        if trigger_count is not None:
            state.last_trigger_count = trigger_count

        source = f"Activated by {triggered_by} ({triggered_by.id})" if triggered_by else "Activated automatically"
        description = (
            f"{reason}\n\n"
            f"{source}\n"
            f"Raid mode will stay active for {self.settings.anti_raid_lockdown_minutes} minute(s)."
        )
        if trigger_count is not None:
            description += f"\nObserved joins in window: {trigger_count}"
        await self.send_anti_raid_alert(guild, "Anti-Raid Activated", description)
        return state

    async def deactivate_anti_raid(
        self,
        guild: discord.Guild,
        actor: Optional[discord.abc.User],
        reason: str,
    ) -> AntiRaidState:
        state = self.get_anti_raid_state(guild.id)
        state.lockdown_until = None
        state.manual_lockdown = False
        await self.send_anti_raid_alert(
            guild,
            "Anti-Raid Deactivated",
            f"{reason}\n\nDeactivated by {actor} ({actor.id})" if actor else reason,
        )
        return state

    async def handle_anti_raid_join(self, member: discord.Member) -> None:
        state = self.get_anti_raid_state(member.guild.id)
        if not state.enabled or member.bot:
            return

        now = utc_now()
        self.prune_anti_raid_events(state, now)
        state.join_events.append(now)
        trigger_count = len(state.join_events)

        if trigger_count >= self.settings.anti_raid_join_threshold and not self.anti_raid_is_active(state):
            await self.activate_anti_raid(
                member.guild,
                None,
                "Join-rate threshold reached. Raid mode was enabled automatically.",
                manual=False,
                trigger_count=trigger_count,
            )

        if not self.anti_raid_is_active(state):
            return

        account_age = now - member.created_at
        minimum_age = timedelta(minutes=self.settings.anti_raid_account_age_minutes)
        if account_age > minimum_age:
            return

        timeout_for = timedelta(minutes=self.settings.anti_raid_timeout_minutes)
        try:
            await member.timeout(timeout_for, reason="Anti-raid protection triggered")
        except discord.HTTPException:
            LOGGER.exception("Failed to timeout suspected raid account %s in guild %s", member.id, member.guild.id)
            return

        reason = (
            f"Auto-timeout during raid mode. Account age: {format_duration(account_age)}. "
            f"Timeout: {format_duration(timeout_for)}."
        )
        embed = self.create_modlog_embed("ANTI-RAID", member, self.user or member.guild.me or member, reason)
        await self.send_modlog(embed)
        await self.add_modlog("ANTI-RAID", member, self.user or member.guild.me or member, member.guild.id, reason)

    async def handle_antiraid_status(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        state = self.get_anti_raid_state(interaction.guild.id)
        await self.send_interaction_message(
            interaction,
            embed=self.create_anti_raid_status_embed(interaction.guild, state),
            ephemeral=True,
        )

    async def handle_antiraid_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        state = self.get_anti_raid_state(interaction.guild.id)
        state.enabled = enabled
        if not enabled:
            state.lockdown_until = None
            state.manual_lockdown = False

        await interaction.response.send_message(
            f"Anti-raid monitoring has been {'enabled' if enabled else 'disabled'} for this server.",
            ephemeral=True,
        )

    async def handle_antiraid_activate(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.activate_anti_raid(
            interaction.guild,
            interaction.user,
            "Raid mode was activated manually by staff.",
            manual=True,
        )
        await interaction.followup.send(
            f"Raid mode is now active for {self.settings.anti_raid_lockdown_minutes} minute(s).",
            ephemeral=True,
        )

    async def handle_antiraid_deactivate(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await interaction.response.send_message("This command can only be used inside a server.", ephemeral=True)
            return

        state = self.get_anti_raid_state(interaction.guild.id)
        if not self.anti_raid_is_active(state):
            await interaction.response.send_message("Raid mode is not active right now.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.deactivate_anti_raid(interaction.guild, interaction.user, "Raid mode was turned off manually by staff.")
        await interaction.followup.send("Raid mode has been deactivated.", ephemeral=True)

    async def handle_close(self, interaction: discord.Interaction, reason: str) -> None:
        if not await self.ensure_staff(interaction, "moderate_members"):
            return
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This command can only be used in a modmail thread.", ephemeral=True)
            return

        session = self.get_session_by_thread(interaction.channel.id)
        if session is None:
            await interaction.response.send_message("This thread is not an active modmail session.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await self.close_modmail(session.user_id, interaction.user, reason)
        await interaction.followup.send("Modmail closed.", ephemeral=True)

    async def handle_staff_apply_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await self.send_interaction_message(
                    interaction,
                    "Please choose a text channel for the application panel.",
                    ephemeral=True,
                )
                return

        if not await self.defer_interaction_once(interaction, ephemeral=True):
            return

        try:
            await target_channel.send(
                **build_embed_send_kwargs(self.create_staff_application_panel_embed(), view=self.staff_application_view)
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to post referee application panel in channel %s", target_channel.id)
            await self.send_interaction_message(
                interaction,
                "I could not post the application panel right now. Please try again later.",
                ephemeral=True,
            )
            return

        await self.send_interaction_message(
            interaction,
            f"Referee application panel posted in {target_channel.mention}.",
            ephemeral=True,
        )

    async def find_staff_application_panel_message(self, channel: discord.TextChannel) -> Optional[discord.Message]:
        async for message in channel.history(limit=200):
            if not message.embeds:
                continue
            embed = message.embeds[0]
            if embed.title != "Northeast Esports":
                continue
            if not message.components:
                continue

            custom_ids = [
                child.custom_id
                for row in message.components
                for child in getattr(row, "children", [])
                if hasattr(child, "custom_id") and child.custom_id is not None
            ]
            if "staff_application:referee" in custom_ids:
                return message
        return None

    async def handle_staff_apply_panel_disable(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await self.send_interaction_message(
                    interaction,
                    "Please choose a text channel containing the application panel.",
                    ephemeral=True,
                )
                return

        if not await self.defer_interaction_once(interaction, ephemeral=True):
            return

        panel_message = await self.find_staff_application_panel_message(target_channel)
        if panel_message is None:
            await self.send_interaction_message(
                interaction,
                "I could not find the active referee application panel message in that channel.",
                ephemeral=True,
            )
            return

        try:
            await panel_message.edit(view=DisabledStaffApplicationView())
        except discord.HTTPException:
            LOGGER.exception(
                "Failed to disable referee application panel in channel %s",
                target_channel.id,
            )
            await self.send_interaction_message(
                interaction,
                "I could not disable the panel right now. Please try again later.",
                ephemeral=True,
            )
            return

        await self.send_interaction_message(
            interaction,
            f"Referee application panel disabled in {target_channel.mention}.",
            ephemeral=True,
        )

    async def handle_server_info_post(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await self.send_interaction_message(
                    interaction,
                    "Please choose a text channel for the server-info hub.",
                    ephemeral=True,
                )
                return

        if not await self.defer_interaction_once(interaction, ephemeral=True):
            return

        try:
            banner_file = self.create_server_info_banner_file()
            if banner_file is not None:
                await target_channel.send(
                    file=banner_file,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            await target_channel.send(
                embeds=self.create_server_info_embeds(interaction.guild),
                view=ServerInfoView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to post server-info hub in channel %s", target_channel.id)
            await self.send_interaction_message(
                interaction,
                "I could not post the server-info hub right now. Please check my permissions and try again.",
                ephemeral=True,
            )
            return

        await self.send_interaction_message(
            interaction,
            f"Server-info hub posted in {target_channel.mention}.",
            ephemeral=True,
        )

    async def handle_verification_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if not await self.ensure_staff(interaction, "manage_guild"):
            return
        if interaction.guild is None:
            await self.send_interaction_message(interaction, "This command can only be used inside a server.", ephemeral=True)
            return

        target_channel = channel
        if target_channel is None:
            if isinstance(interaction.channel, discord.TextChannel):
                target_channel = interaction.channel
            else:
                await self.send_interaction_message(
                    interaction,
                    "Please choose a text channel for the verification panel.",
                    ephemeral=True,
                )
                return

        if not await self.defer_interaction_once(interaction, ephemeral=True):
            return

        try:
            await target_channel.send(
                **build_embed_send_kwargs(self.create_verification_panel_embed(interaction.guild), view=self.verification_view)
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to post verification panel in channel %s", target_channel.id)
            await self.send_interaction_message(
                interaction,
                "I could not post the verification panel right now. Please try again later.",
                ephemeral=True,
            )
            return

        await self.send_interaction_message(
            interaction,
            f"Verification panel posted in {target_channel.mention}.",
            ephemeral=True,
        )

    async def handle_verification_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(
                interaction,
                "This verification button can only be used inside the server.",
                ephemeral=True,
            )
            return

        if not await self.defer_interaction_once(interaction, ephemeral=True):
            return

        role = self.get_verified_role(interaction.guild)
        if role is None:
            await self.send_interaction_message(
                interaction,
                "The `Verified` role was not found. Ask a moderator to create it or set `VERIFIED_ROLE_ID`.",
                ephemeral=True,
            )
            return

        role_error = self.can_manage_role(interaction.guild.me or interaction.user, role)
        if role_error is not None:
            await self.send_interaction_message(interaction, role_error, ephemeral=True)
            return

        if role in interaction.user.roles:
            await self.send_interaction_message(
                interaction,
                "You have already verified and already have the Verified role.",
                ephemeral=True,
            )
            return

        try:
            await interaction.user.add_roles(role, reason="Northeast Esports verification completed")
        except discord.HTTPException:
            LOGGER.exception("Failed to assign verified role to %s in guild %s", interaction.user.id, interaction.guild.id)
            await self.send_interaction_message(
                interaction,
                "I could not assign the verification role. Please contact a moderator.",
                ephemeral=True,
            )
            return

        await self.send_verification_log(self.create_verification_log_embed(interaction.user, role))
        await self.send_interaction_message(
            interaction,
            f"Verification complete. You have been given {role.mention} and can now access all server channels.",
            ephemeral=True,
        )

    async def handle_staff_application_continue(self, interaction: discord.Interaction, custom_id: str) -> None:
        match = re.fullmatch(r"staff_application:continue:(\d):(\d+)", custom_id)
        if match is None:
            await interaction.response.send_message("That application page is invalid. Please start again.", ephemeral=True)
            return

        next_page = int(match.group(1))
        owner_id = int(match.group(2))
        if interaction.user.id != owner_id:
            await interaction.response.send_message(
                "This application page belongs to someone else.",
                ephemeral=True,
            )
            return

        draft = self.staff_application_drafts.get(owner_id)
        if draft is None:
            await interaction.response.send_message(
                "Your application session expired. Please start again from the panel.",
                ephemeral=True,
            )
            return

        if next_page == 2:
            await interaction.response.send_modal(StaffApplicationPageTwoModal(self, owner_id))
            return

        await interaction.response.send_message("That application page is invalid. Please start again.", ephemeral=True)

    async def submit_staff_application(
        self,
        interaction: discord.Interaction,
        draft: StaffApplicationDraft,
    ) -> None:
        try:
            channel = await self.get_staff_application_channel()
        except discord.HTTPException:
            LOGGER.exception(
                "Could not fetch staff application review channel %s",
                self.settings.staff_application_channel_id,
            )
            await interaction.response.send_message(
                "I could not find the staff application review channel. Please tell an admin to check the channel ID.",
                ephemeral=True,
            )
            return

        if channel is None:
            await interaction.response.send_message(
                "The configured staff application review channel is invalid. Please tell an admin to update it.",
                ephemeral=True,
            )
            return

        embed = self.create_staff_application_embed(interaction.user, draft, interaction.guild)
        try:
            await channel.send(
                content=f"<@&{self.settings.admin_role_id}> New referee application received.",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to send staff application for %s", interaction.user)
            await interaction.response.send_message(
                "I could not send your application right now. Please try again later.",
                ephemeral=True,
            )
            return

        self.staff_application_drafts.pop(interaction.user.id, None)
        await interaction.response.send_message(
            "Your referee application has been submitted successfully.",
            ephemeral=True,
        )

    async def handle_ticket_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel],
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This command can only be used in a server.")
            return
        if not self.has_staff_access(interaction.user, "manage_channels"):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return

        target = channel or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if target is None:
            await self.send_interaction_message(interaction, "Choose a text channel for the ticket panel.")
            return
        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return

        try:
            await target.send(embed=self.create_ticket_panel_embed(), view=self.ticket_panel_view)
        except discord.HTTPException:
            LOGGER.exception("Failed to post ticket panel in channel %s", target.id)
            await self.send_interaction_message(interaction, "I could not post the ticket panel in that channel.")
            return
        await self.send_interaction_message(interaction, f"Ticket panel posted in {target.mention}.")

    async def handle_ticket_set_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This command can only be used in a server.")
            return
        if not self.has_admin_access(interaction.user):
            await self.send_interaction_message(interaction, "Only an administrator can set the ticket transcript channel.")
            return
        if channel.guild.id != interaction.guild.id:
            await self.send_interaction_message(interaction, "Choose a channel from this server.")
            return

        me = interaction.guild.me
        if me is None:
            await self.send_interaction_message(interaction, "I could not verify my channel permissions.")
            return
        permissions = channel.permissions_for(me)
        if not permissions.view_channel or not permissions.send_messages or not permissions.attach_files:
            await self.send_interaction_message(
                interaction,
                "I need View Channel, Send Messages, and Attach Files permissions in that channel.",
            )
            return

        self.ticket_transcript_channels[interaction.guild.id] = channel.id
        await self.persist_ticket_config_data()
        await self.send_interaction_message(
            interaction,
            f"Closed-ticket transcripts will now be saved in {channel.mention}.",
        )

    async def create_ticket_from_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "Tickets can only be opened inside a server.")
            return
        if interaction.user.bot:
            await self.send_interaction_message(interaction, "Bots cannot open tickets.")
            return

        lock = self.ticket_creation_locks.setdefault(interaction.guild.id, asyncio.Lock())
        async with lock:
            existing = self.find_open_ticket(interaction.guild, interaction.user.id)
            if existing is not None:
                await self.send_interaction_message(
                    interaction,
                    f"You already have an open ticket: {existing.mention}",
                )
                return
            if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
                return

            try:
                category = await self.get_ticket_category(interaction.guild)
                overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                    interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                    interaction.user: discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                        embed_links=True,
                    ),
                }
                me = interaction.guild.me
                if me is not None:
                    overwrites[me] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        manage_channels=True,
                        read_message_history=True,
                        attach_files=True,
                    )
                for role_id in {self.settings.moderator_role_id, self.settings.admin_role_id}:
                    role = interaction.guild.get_role(role_id)
                    if role is not None:
                        overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            attach_files=True,
                            embed_links=True,
                        )

                user_slug = slugify_text(interaction.user.display_name) or "member"
                channel_name = f"ticket-{user_slug[:70]}-{str(interaction.user.id)[-4:]}"
                opened_at = utc_now().isoformat()
                ticket_channel = await interaction.guild.create_text_channel(
                    channel_name[:100],
                    category=category,
                    topic=self.build_ticket_topic(interaction.user.id, 0, opened_at),
                    overwrites=overwrites,
                    reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
                )
            except (discord.HTTPException, ValueError) as error:
                LOGGER.exception("Failed to create ticket for %s", interaction.user)
                await self.send_interaction_message(interaction, f"I could not create your ticket: {error}")
                return

            welcome = make_embed(
                "Ticket Opened",
                (
                    f"Welcome {interaction.user.mention}. Please explain how the team can help.\n\n"
                    "Use the controls below to claim, export, or close this ticket."
                ),
                discord.Color.green(),
            )
            try:
                await ticket_channel.send(
                    content=f"{interaction.user.mention} <@&{self.settings.moderator_role_id}>",
                    embed=welcome,
                    view=self.ticket_controls_view,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True),
                )
            except discord.HTTPException:
                LOGGER.exception("Ticket %s was created but its welcome message failed", ticket_channel.id)

            await self.send_interaction_message(
                interaction,
                f"Your private ticket is ready: {ticket_channel.mention}",
            )

    def ticket_action_allowed(self, member: discord.Member, owner_id: int) -> bool:
        return member.id == owner_id or self.has_staff_access(member, "manage_channels")

    async def claim_ticket_from_button(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await self.send_interaction_message(interaction, "Use this inside a ticket channel.")
            return
        ticket = self.parse_ticket_channel(interaction.channel)
        if ticket is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This is not an active ticket channel.")
            return
        if not self.has_staff_access(interaction.user, "manage_channels"):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return

        owner_id, claimed_id, opened_at = ticket
        if claimed_id == interaction.user.id:
            await self.send_interaction_message(interaction, "You have already claimed this ticket.")
            return
        if claimed_id:
            await self.send_interaction_message(interaction, f"This ticket is already claimed by <@{claimed_id}>.")
            return

        try:
            await interaction.channel.edit(
                topic=self.build_ticket_topic(owner_id, interaction.user.id, opened_at),
                reason=f"Ticket claimed by {interaction.user}",
            )
            await interaction.channel.send(
                embed=make_embed(
                    "Ticket Claimed",
                    f"{interaction.user.mention} is now handling this ticket.",
                    discord.Color.blurple(),
                )
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to claim ticket channel %s", interaction.channel.id)
            await self.send_interaction_message(interaction, "I could not claim this ticket.")
            return
        await self.send_interaction_message(interaction, "Ticket claimed.")

    async def handle_ticket_participant(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        *,
        add: bool,
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await self.send_interaction_message(interaction, "Use this command inside a ticket channel.")
            return
        ticket = self.parse_ticket_channel(interaction.channel)
        if ticket is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This is not an active ticket channel.")
            return
        if not self.has_staff_access(interaction.user, "manage_channels"):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return
        if not add and user.id == ticket[0]:
            await self.send_interaction_message(interaction, "The ticket opener cannot be removed.")
            return

        try:
            overwrite = (
                discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True,
                )
                if add
                else None
            )
            await interaction.channel.set_permissions(
                user,
                overwrite=overwrite,
                reason=f"Ticket participant {'added' if add else 'removed'} by {interaction.user}",
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to update participant in ticket %s", interaction.channel.id)
            await self.send_interaction_message(interaction, "I could not update that member's ticket access.")
            return

        action = "added to" if add else "removed from"
        await interaction.channel.send(
            embed=make_embed(
                "Participant Updated",
                f"{user.mention} was {action} the ticket by {interaction.user.mention}.",
                discord.Color.green() if add else discord.Color.orange(),
            )
        )
        await self.send_interaction_message(interaction, f"{user.mention} was {action} this ticket.")

    async def build_ticket_transcript(self, channel: discord.TextChannel) -> Tuple[bytes, str, int]:
        ticket = self.parse_ticket_channel(channel)
        if ticket is None:
            raise ValueError("This is not an active ticket channel.")

        rows: List[str] = []
        message_count = 0
        async for message in channel.history(limit=None, oldest_first=True):
            message_count += 1
            author_name = html.escape(str(message.author))
            author_id = html.escape(str(message.author.id))
            avatar_url = html.escape(str(message.author.display_avatar.url), quote=True)
            content = html.escape(message.content or "")
            content_html = f'<div class="content">{content}</div>' if content else ""
            attachment_html = "".join(
                f'<div class="attachment">📎 <a href="{html.escape(item.url, quote=True)}">'
                f'{html.escape(item.filename)}</a></div>'
                for item in message.attachments
            )
            embed_html = "".join(
                '<div class="discord-embed">'
                f'<strong>{html.escape(item.title or "Embed")}</strong>'
                f'<div>{html.escape(item.description or "")}</div>'
                "</div>"
                for item in message.embeds
            )
            sticker_html = "".join(
                f'<div class="attachment">Sticker: {html.escape(item.name)}</div>' for item in message.stickers
            )
            edited = " (edited)" if message.edited_at else ""
            rows.append(
                '<article class="message">'
                f'<img class="avatar" src="{avatar_url}" alt="">'
                '<div class="body">'
                f'<div><span class="author">{author_name}</span> '
                f'<span class="meta">{author_id} • {message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")}{edited}</span></div>'
                f'{content_html}{attachment_html}{embed_html}{sticker_html}'
                "</div></article>"
            )

        owner_id, claimed_id, opened_at = ticket
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Transcript #{html.escape(channel.name)}</title>
<style>
body{{margin:0;background:#313338;color:#dbdee1;font:15px Arial,sans-serif}}main{{max-width:1000px;margin:auto;padding:32px}}
h1{{color:#fff;margin-bottom:6px}}.summary{{color:#b5bac1;margin-bottom:28px}}.message{{display:flex;gap:14px;padding:10px 6px}}
.message:hover{{background:#2e3035}}.avatar{{width:40px;height:40px;border-radius:50%}}.body{{min-width:0;flex:1}}
.author{{font-weight:700;color:#f2f3f5}}.meta{{font-size:12px;color:#949ba4}}.content{{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:4px}}
a{{color:#00a8fc}}.attachment{{margin-top:6px}}.discord-embed{{border-left:4px solid #5865f2;background:#2b2d31;padding:10px;margin-top:7px;max-width:620px}}
</style></head><body><main><h1>#{html.escape(channel.name)}</h1>
<div class="summary">Server: {html.escape(channel.guild.name)} • Owner ID: {owner_id} • Claimed ID: {claimed_id or 'Unclaimed'}<br>
Opened: {html.escape(opened_at)} • Exported: {utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')} • Messages: {message_count}</div>
{''.join(rows)}</main></body></html>"""
        payload = document.encode("utf-8")
        filename = f"transcript-{channel.name}-{channel.id}.html"
        if len(payload) > 7_500_000:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.writestr(filename, payload)
            payload = archive.getvalue()
            filename = f"transcript-{channel.name}-{channel.id}.zip"
        return payload, filename, message_count

    async def send_ticket_transcript(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await self.send_interaction_message(interaction, "Use this inside a ticket channel.")
            return
        ticket = self.parse_ticket_channel(interaction.channel)
        if ticket is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This is not an active ticket channel.")
            return
        if not self.ticket_action_allowed(interaction.user, ticket[0]):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return
        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return
        try:
            payload, filename, count = await self.build_ticket_transcript(interaction.channel)
            await interaction.followup.send(
                f"Transcript ready ({count} messages).",
                file=discord.File(io.BytesIO(payload), filename=filename),
                ephemeral=True,
            )
        except (discord.HTTPException, ValueError):
            LOGGER.exception("Failed to export ticket transcript for channel %s", interaction.channel.id)
            await self.send_interaction_message(interaction, "I could not export this ticket transcript.")

    async def request_ticket_close(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await self.send_interaction_message(interaction, "Use this inside a ticket channel.")
            return
        ticket = self.parse_ticket_channel(interaction.channel)
        if ticket is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This is not an active ticket channel.")
            return
        if not self.ticket_action_allowed(interaction.user, ticket[0]):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return
        await self.send_interaction_message(
            interaction,
            "Close this ticket? A transcript will be saved before the channel is deleted.",
            view=self.ticket_close_confirm_view,
        )

    async def close_ticket_from_button(self, interaction: discord.Interaction) -> None:
        await self.close_ticket_from_command(interaction, "Closed from the ticket controls")

    async def cancel_ticket_close(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.response.edit_message(content="Ticket closure cancelled.", view=None)
        except discord.HTTPException:
            await self.send_interaction_message(interaction, "Ticket closure cancelled.")

    async def close_ticket_from_command(self, interaction: discord.Interaction, reason: str) -> None:
        if not isinstance(interaction.channel, discord.TextChannel) or interaction.guild is None:
            await self.send_interaction_message(interaction, "Use this inside a ticket channel.")
            return
        ticket = self.parse_ticket_channel(interaction.channel)
        if ticket is None or not isinstance(interaction.user, discord.Member):
            await self.send_interaction_message(interaction, "This is not an active ticket channel.")
            return
        if not self.ticket_action_allowed(interaction.user, ticket[0]):
            await self.send_interaction_message(interaction, NO_PERMISSION)
            return
        if not await self.defer_interaction_once(interaction, ephemeral=True, thinking=True):
            return

        channel = interaction.channel
        try:
            payload, filename, count = await self.build_ticket_transcript(channel)
        except (discord.HTTPException, ValueError):
            LOGGER.exception("Refusing to close ticket %s because transcript generation failed", channel.id)
            await self.send_interaction_message(
                interaction,
                "The transcript could not be created, so the ticket was not closed.",
            )
            return

        owner_id = ticket[0]
        close_embed = make_embed(
            "Ticket Closed",
            (
                f"Channel: **#{channel.name}** (`{channel.id}`)\n"
                f"Opened by: <@{owner_id}> (`{owner_id}`)\n"
                f"Closed by: {interaction.user.mention} (`{interaction.user.id}`)\n"
                f"Reason: {truncate_text(reason, 500)}\n"
                f"Messages: {count}"
            ),
            discord.Color.red(),
        )

        transcript_channel_id = self.get_ticket_transcript_channel_id(interaction.guild.id)
        try:
            transcript_channel = self.get_channel(transcript_channel_id) or await self.fetch_channel(transcript_channel_id)
            if isinstance(transcript_channel, discord.TextChannel):
                await transcript_channel.send(
                    embed=close_embed,
                    file=discord.File(io.BytesIO(payload), filename=filename),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                LOGGER.warning("Ticket transcript channel %s is not a text channel", transcript_channel_id)
        except discord.HTTPException:
            LOGGER.exception("Failed to send transcript for ticket %s to log channel", channel.id)

        try:
            owner = self.get_user(owner_id) or await self.fetch_user(owner_id)
            await owner.send(
                embed=make_embed(
                    "Your Ticket Was Closed",
                    f"Reason: {truncate_text(reason, 500)}\nMessages: {count}",
                    discord.Color.red(),
                ),
                file=discord.File(io.BytesIO(payload), filename=filename),
            )
        except (discord.Forbidden, discord.NotFound):
            LOGGER.info("Could not DM ticket transcript to user %s", owner_id)
        except discord.HTTPException:
            LOGGER.exception("Failed to DM ticket transcript to user %s", owner_id)

        try:
            await interaction.followup.send(
                "Transcript saved. This ticket will close shortly.",
                file=discord.File(io.BytesIO(payload), filename=filename),
                ephemeral=True,
            )
        except discord.HTTPException:
            LOGGER.exception("Failed to send closing transcript response for ticket %s", channel.id)

        try:
            await channel.send(embed=close_embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            LOGGER.exception("Failed to post closing notice in ticket channel %s", channel.id)

        await asyncio.sleep(3)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}: {reason[:200]}")
        except discord.HTTPException:
            LOGGER.exception("Failed to delete ticket channel %s", channel.id)
            await self.send_interaction_message(interaction, "The transcript was saved, but I could not delete the ticket channel.")

    def get_session_by_thread(self, thread_id: int) -> Optional[ModmailSession]:
        for session in self.modmail_sessions.values():
            if session.thread_id == thread_id:
                return session
        return None

    def is_on_cooldown(self, user_id: int) -> bool:
        started = self.modmail_cooldowns.get(user_id)
        return started is not None and (utc_now() - started) < timedelta(seconds=MODMAIL_COOLDOWN_SECONDS)

    @staticmethod
    def interaction_response_kwargs(interaction: discord.Interaction) -> dict:
        return {"ephemeral": True} if interaction.guild_id is not None else {}

    def should_send_dm_intro(self, user_id: int) -> bool:
        sent_at = self.dm_intro_cooldowns.get(user_id)
        return sent_at is None or (utc_now() - sent_at) >= timedelta(seconds=DM_INTRO_COOLDOWN_SECONDS)

    async def handle_user_dm(self, message: discord.Message) -> None:
        session = self.modmail_sessions.get(message.author.id)
        if session is None:
            if self.should_send_dm_intro(message.author.id):
                await message.author.send(
                    **build_embed_send_kwargs(self.create_modmail_intro_embed(), view=self.modmail_view)
                )
                self.dm_intro_cooldowns[message.author.id] = utc_now()
            return

        await self.relay_user_message(message, session)

    async def open_modmail_from_button(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        response_kwargs = self.interaction_response_kwargs(interaction)

        try:
            if self.is_on_cooldown(user_id):
                await interaction.response.send_message("Please wait a moment before opening another modmail.", **response_kwargs)
                return

            if user_id in self.modmail_sessions:
                await interaction.response.send_message("You already have an active modmail thread.", **response_kwargs)
                return

            if interaction.guild_id is None:
                await interaction.response.send_message("Opening your modmail...", **response_kwargs)
            else:
                await interaction.response.defer(thinking=True, **response_kwargs)

            forum = self.get_channel(self.settings.modmail_forum_id)
            if forum is None:
                forum = await self.fetch_channel(self.settings.modmail_forum_id)
            if not isinstance(forum, discord.ForumChannel):
                if interaction.guild_id is None:
                    await interaction.channel.send("MODMAIL_FORUM_ID is not a forum channel.")
                else:
                    await interaction.followup.send("MODMAIL_FORUM_ID is not a forum channel.", **response_kwargs)
                return

            thread = await forum.create_thread(
                name=f"modmail-{interaction.user.id}",
                content=f"<@&{self.settings.moderator_role_id}> Modmail opened by {interaction.user.mention}",
                embed=self.create_modmail_thread_embed(interaction.user, "Opened via DM button"),
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            await thread.thread.send(
                "Use the button below to close this modmail thread when the case is resolved.",
                view=self.close_modmail_view,
            )

            self.modmail_sessions[user_id] = ModmailSession(user_id=user_id, thread_id=thread.thread.id)
            self.modmail_cooldowns[user_id] = utc_now()
            await self.safe_dm(
                interaction.user,
                make_embed(
                    "Modmail Opened",
                    "Your private modmail thread has been created. Send messages here and the moderation team will receive them.",
                    discord.Color.green(),
                ),
            )
            if interaction.guild_id is not None:
                await interaction.followup.send("Your modmail has been opened.", **response_kwargs)
        except discord.HTTPException:
            LOGGER.exception("Failed to create modmail thread for %s", interaction.user)
            try:
                if interaction.response.is_done():
                    if interaction.guild_id is None:
                        await interaction.channel.send("I could not create a modmail thread. Check my forum permissions and channel IDs.")
                    else:
                        await interaction.followup.send(
                            "I could not create a modmail thread. Check my forum permissions and channel IDs.",
                            **response_kwargs,
                        )
                else:
                    await interaction.response.send_message(
                        "I could not create a modmail thread. Check my forum permissions and channel IDs.",
                        **response_kwargs,
                    )
            except discord.HTTPException:
                LOGGER.exception("Failed to send modmail creation error response to %s", interaction.user)
        except Exception:
            LOGGER.exception("Unexpected error while opening modmail for %s", interaction.user)
            try:
                if interaction.response.is_done():
                    if interaction.guild_id is None:
                        await interaction.channel.send("Something went wrong while opening modmail.")
                    else:
                        await interaction.followup.send("Something went wrong while opening modmail.", **response_kwargs)
                else:
                    await interaction.response.send_message("Something went wrong while opening modmail.", **response_kwargs)
            except discord.HTTPException:
                LOGGER.exception("Failed to send unexpected modmail error response to %s", interaction.user)

    async def close_modmail_from_button(self, interaction: discord.Interaction) -> None:
        response_kwargs = self.interaction_response_kwargs(interaction)

        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("This button can only be used inside an active modmail thread.", **response_kwargs)
            return

        session = self.get_session_by_thread(interaction.channel.id)
        if session is None:
            await interaction.response.send_message("This thread is not an active modmail session.", **response_kwargs)
            return

        if interaction.guild_id is not None:
            member = interaction.user if isinstance(interaction.user, discord.Member) else None
            if member is None or not self.has_staff_access(member, "moderate_members"):
                await interaction.response.send_message(NO_PERMISSION, **response_kwargs)
                return

        await interaction.response.defer(**response_kwargs)
        await self.close_modmail(session.user_id, interaction.user, "Issue resolved by the moderation team")
        await interaction.followup.send("Modmail closed.", **response_kwargs)

    async def relay_user_message(self, message: discord.Message, session: ModmailSession) -> None:
        thread = self.get_channel(session.thread_id)
        if thread is None:
            thread = await self.fetch_channel(session.thread_id)
        if not isinstance(thread, discord.Thread):
            self.modmail_sessions.pop(message.author.id, None)
            await message.author.send("Your previous modmail thread is no longer available. Please open a new one.")
            return

        session.last_activity = utc_now()
        session.message_count += 1
        embed = discord.Embed(
            title="User Message",
            description=message.content or "*No text content*",
            color=discord.Color.blurple(),
            timestamp=utc_now(),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.set_footer(text=BRAND_FOOTER)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(attachment.url for attachment in message.attachments), inline=False)

        await thread.send(embed=embed)

    async def handle_moderator_reply(self, message: discord.Message) -> None:
        match = MODMAIL_THREAD_RE.match(message.channel.name)
        if match is None:
            return

        session = self.modmail_sessions.get(int(match.group("user_id")))
        if session is None:
            return

        user = self.get_user(session.user_id) or await self.fetch_user(session.user_id)
        session.last_activity = utc_now()
        embed = discord.Embed(
            title=f"{self.settings.server_name} Moderator",
            description=message.content or "*No text content*",
            color=discord.Color.purple(),
            timestamp=utc_now(),
        )
        embed.set_footer(text=BRAND_FOOTER)
        set_default_thumbnail(embed)
        if message.attachments:
            embed.add_field(name="Attachments", value="\n".join(attachment.url for attachment in message.attachments), inline=False)

        await user.send(**build_embed_send_kwargs(embed))

    async def close_modmail(self, user_id: int, closed_by: discord.abc.User, reason: str) -> None:
        session = self.modmail_sessions.pop(user_id, None)
        if session is None:
            return

        thread = self.get_channel(session.thread_id)
        if thread is None:
            thread = await self.fetch_channel(session.thread_id)

        if isinstance(thread, discord.Thread):
            await thread.send(
                **build_embed_send_kwargs(
                    make_embed(
                        "Modmail Closed",
                        f"Closed by **{closed_by}**.\nReason: {reason}",
                        discord.Color.red(),
                    )
                )
            )
            await thread.edit(archived=True, locked=True)

        user = self.get_user(user_id) or await self.fetch_user(user_id)
        await self.safe_dm(
            user,
            make_embed(
                "Modmail Closed",
                f"Your modmail thread has been closed.\n\nReason: {reason}",
                discord.Color.red(),
            ),
        )

    async def validate_runtime_configuration(self) -> None:
        try:
            forum = self.get_channel(self.settings.modmail_forum_id) or await self.fetch_channel(self.settings.modmail_forum_id)
            if isinstance(forum, discord.ForumChannel):
                LOGGER.info("Modmail forum found: %s (%s)", forum.name, forum.id)
            else:
                LOGGER.warning("MODMAIL_FORUM_ID is not a forum channel: %s", self.settings.modmail_forum_id)
        except discord.HTTPException:
            LOGGER.exception("Could not fetch modmail forum channel %s", self.settings.modmail_forum_id)

        try:
            log_channel = self.get_channel(self.settings.mod_log_channel_id) or await self.fetch_channel(self.settings.mod_log_channel_id)
            if isinstance(log_channel, discord.TextChannel):
                LOGGER.info("Mod log channel found: %s (%s)", log_channel.name, log_channel.id)
            else:
                LOGGER.warning("MOD_LOG_CHANNEL_ID is not a text channel: %s", self.settings.mod_log_channel_id)
        except discord.HTTPException:
            LOGGER.exception("Could not fetch mod log channel %s", self.settings.mod_log_channel_id)

        server_log_channel_id = self.settings.server_log_channel_id or self.settings.mod_log_channel_id
        if self.settings.server_log_channel_id:
            try:
                server_log_channel = self.get_channel(server_log_channel_id) or await self.fetch_channel(server_log_channel_id)
                if isinstance(server_log_channel, discord.TextChannel):
                    LOGGER.info("Server log channel found: %s (%s)", server_log_channel.name, server_log_channel.id)
                else:
                    LOGGER.warning("SERVER_LOG_CHANNEL_ID is not a text channel: %s", server_log_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch server log channel %s", server_log_channel_id)
        else:
            LOGGER.info("SERVER_LOG_CHANNEL_ID not set. Server logs will use MOD_LOG_CHANNEL_ID.")

        invite_log_channel_id = (
            self.settings.invite_log_channel_id
            or self.settings.server_log_channel_id
            or self.settings.mod_log_channel_id
        )
        if self.settings.invite_log_channel_id:
            try:
                invite_log_channel = self.get_channel(invite_log_channel_id) or await self.fetch_channel(invite_log_channel_id)
                if isinstance(invite_log_channel, discord.TextChannel):
                    LOGGER.info("Invite log channel found: %s (%s)", invite_log_channel.name, invite_log_channel.id)
                else:
                    LOGGER.warning("INVITE_LOG_CHANNEL_ID is not a text channel: %s", invite_log_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch invite log channel %s", invite_log_channel_id)
        else:
            LOGGER.info("INVITE_LOG_CHANNEL_ID not set. Invite logs will use SERVER_LOG_CHANNEL_ID or MOD_LOG_CHANNEL_ID.")

        verification_log_channel_id = (
            self.settings.verification_log_channel_id
            or self.settings.server_log_channel_id
            or self.settings.mod_log_channel_id
        )
        if self.settings.verification_log_channel_id:
            try:
                verification_log_channel = self.get_channel(verification_log_channel_id) or await self.fetch_channel(
                    verification_log_channel_id
                )
                if isinstance(verification_log_channel, discord.TextChannel):
                    LOGGER.info("Verification log channel found: %s (%s)", verification_log_channel.name, verification_log_channel.id)
                else:
                    LOGGER.warning("VERIFICATION_LOG_CHANNEL_ID is not a text channel: %s", verification_log_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch verification log channel %s", verification_log_channel_id)
        else:
            LOGGER.info(
                "VERIFICATION_LOG_CHANNEL_ID not set. Verification logs will use SERVER_LOG_CHANNEL_ID or MOD_LOG_CHANNEL_ID."
            )

        if self.settings.welcome_channel_id:
            try:
                welcome_channel = self.get_channel(self.settings.welcome_channel_id) or await self.fetch_channel(
                    self.settings.welcome_channel_id
                )
                if isinstance(welcome_channel, discord.TextChannel):
                    LOGGER.info("Welcome channel found: %s (%s)", welcome_channel.name, welcome_channel.id)
                else:
                    LOGGER.warning("WELCOME_CHANNEL_ID is not a text channel: %s", self.settings.welcome_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not fetch welcome channel %s", self.settings.welcome_channel_id)
        else:
            LOGGER.info("WELCOME_CHANNEL_ID not set. Automatic welcome messages are disabled.")

        try:
            application_channel = self.get_channel(self.settings.staff_application_channel_id) or await self.fetch_channel(
                self.settings.staff_application_channel_id
            )
            if isinstance(application_channel, discord.TextChannel):
                LOGGER.info(
                    "Staff application channel found: %s (%s)",
                    application_channel.name,
                    application_channel.id,
                )
            else:
                LOGGER.warning(
                    "STAFF_APPLICATION_CHANNEL_ID is not a text channel: %s",
                    self.settings.staff_application_channel_id,
                )
        except discord.HTTPException:
            LOGGER.exception(
                "Could not fetch staff application channel %s",
                self.settings.staff_application_channel_id,
            )

        if self.settings.verified_role_id:
            for guild in self.guilds:
                role = guild.get_role(self.settings.verified_role_id)
                if role is not None:
                    LOGGER.info("Verified role found in %s: %s (%s)", guild.name, role.name, role.id)
                    break
            else:
                LOGGER.warning("VERIFIED_ROLE_ID was set but no matching role was found in the connected guilds.")
        else:
            LOGGER.info("VERIFIED_ROLE_ID not set. Verification falls back to the role name `Verified`.")

        LOGGER.info(
            "Anti-raid config | enabled=%s threshold=%s window=%ss lockdown=%sm account_age=%sm timeout=%sm",
            self.settings.anti_raid_enabled,
            self.settings.anti_raid_join_threshold,
            self.settings.anti_raid_window_seconds,
            self.settings.anti_raid_lockdown_minutes,
            self.settings.anti_raid_account_age_minutes,
            self.settings.anti_raid_timeout_minutes,
        )
        if self.uses_postgres:
            LOGGER.info("Persistent storage backend: PostgreSQL")
        else:
            LOGGER.info(
                "Persistent storage backend: local JSON files for auto-react, no-link, AFK, and prefix data; modlogs stay in memory."
            )

    @tasks.loop(minutes=5)
    async def cleanup_inactive_modmail(self) -> None:
        expiry = utc_now() - timedelta(hours=MODMAIL_INACTIVITY_HOURS)
        stale_users = [user_id for user_id, session in self.modmail_sessions.items() if session.last_activity < expiry]
        if self.user is None:
            return
        for user_id in stale_users:
            await self.close_modmail(user_id, self.user, "Inactivity timeout")

    @cleanup_inactive_modmail.before_loop
    async def before_cleanup_inactive_modmail(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def server_stats_loop(self) -> None:
        await self.log_all_server_stats()

    @server_stats_loop.before_loop
    async def before_server_stats_loop(self) -> None:
        await self.wait_until_ready()

def main() -> None:
    settings = load_settings()
    bot = RhinoBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
