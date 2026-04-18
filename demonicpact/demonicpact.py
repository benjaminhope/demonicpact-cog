import asyncio
import logging
import re
from typing import Optional

import aiohttp
import discord
from redbot.core import Config, commands
from redbot.core.bot import Red

log = logging.getLogger("red.demonicpact")

HISCORES_URL = (
    "https://secure.runescape.com/m=hiscore_oldschool_seasonal/hiscorepersonal"
)

DEFAULT_MEMBERS = ["He Plops", "rTill", "Gershyee", "iron jorky", "fuqgagec"]

LEAGUE_POINTS_RE = re.compile(
    r">League Points</a>\s*</td>\s*"
    r"<td[^>]*>\s*([\d,\-]+)\s*</td>\s*"
    r"<td[^>]*>\s*([\d,\-]+)\s*</td>",
    re.IGNORECASE,
)

USER_AGENT = "DemonicPactCog/1.0 (+Red-DiscordBot)"


def parse_interval(s: str) -> Optional[int]:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s.lower())
    if not m:
        return None
    n = int(m.group(1))
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n * mult


def format_interval(seconds: int) -> str:
    for unit, mult in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= mult and seconds % mult == 0:
            return f"{seconds // mult}{unit}"
    return f"{seconds}s"


class DemonicPact(commands.Cog):
    """Track Demonic Pact OSRS Leagues standings."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(
            self, identifier=0xDEA11AC7, force_registration=True
        )
        self.config.register_guild(
            members=DEFAULT_MEMBERS,
            channel_id=None,
            interval_seconds=None,
        )
        self._tasks: dict[int, asyncio.Task] = {}
        self._startup_task = asyncio.create_task(self._resume_schedules())

    async def _resume_schedules(self):
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            data = await self.config.guild(guild).all()
            if data.get("channel_id") and data.get("interval_seconds"):
                self._start_task(guild.id)

    def cog_unload(self):
        self._startup_task.cancel()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    async def _fetch_points(
        self, session: aiohttp.ClientSession, name: str
    ) -> tuple[Optional[int], Optional[int]]:
        try:
            async with session.get(
                HISCORES_URL,
                params={"user1": name},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return None, None
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("fetch failed for %s", name, exc_info=True)
            return None, None

        m = LEAGUE_POINTS_RE.search(html)
        if not m:
            return None, None
        try:
            rank = int(m.group(1).replace(",", ""))
            points = int(m.group(2).replace(",", ""))
        except ValueError:
            return None, None
        if rank < 0 or points < 0:
            return None, None
        return rank, points

    async def _gather_standings(
        self, members: list[str]
    ) -> list[tuple[str, Optional[int], Optional[int]]]:
        headers = {"User-Agent": USER_AGENT}
        async with aiohttp.ClientSession(headers=headers) as session:
            results = await asyncio.gather(
                *(self._fetch_points(session, m) for m in members)
            )
        return [(name, rank, pts) for name, (rank, pts) in zip(members, results)]

    def _format_embed(self, standings) -> discord.Embed:
        ranked = sorted(
            standings,
            key=lambda r: (r[2] is None, -(r[2] or 0)),
        )
        lines = []
        medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
        for i, (name, rank, pts) in enumerate(ranked):
            prefix = medals[i] if i < 3 and pts is not None else f"`{i + 1}.`"
            if pts is None:
                lines.append(f"{prefix} **{name}** — _not ranked_")
            else:
                lines.append(
                    f"{prefix} **{name}** — {pts:,} LP (rank #{rank:,})"
                )
        leader = next((r for r in ranked if r[2] is not None), None)
        title = "Demonic Pact — League Points"
        if leader:
            title = f"Demonic Pact — {leader[0]} leads ({leader[2]:,} LP)"
        return discord.Embed(
            title=title,
            description="\n".join(lines) if lines else "No members configured.",
            color=discord.Color.dark_red(),
        )

    @commands.group(name="pact", invoke_without_command=True)
    @commands.guild_only()
    async def pact(self, ctx: commands.Context):
        """Demonic Pact commands. Try `[p]pact leaders`."""
        await ctx.send_help()

    @pact.command(name="leaders")
    async def pact_leaders(self, ctx: commands.Context):
        """Show current League Points standings."""
        members = await self.config.guild(ctx.guild).members()
        if not members:
            await ctx.send("No members configured. Use `!pact add <name>`.")
            return
        async with ctx.typing():
            standings = await self._gather_standings(members)
        await ctx.send(embed=self._format_embed(standings))

    @pact.command(name="members")
    async def pact_members(self, ctx: commands.Context):
        """List tracked members."""
        members = await self.config.guild(ctx.guild).members()
        if not members:
            await ctx.send("No members configured.")
            return
        await ctx.send(
            "**Tracked members:** " + ", ".join(f"`{m}`" for m in members)
        )

    @pact.command(name="add")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_add(self, ctx: commands.Context, *, name: str):
        """Add a member to the tracked list."""
        name = name.strip()
        async with self.config.guild(ctx.guild).members() as members:
            if name in members:
                await ctx.send(f"`{name}` is already tracked.")
                return
            members.append(name)
        await ctx.send(f"Added `{name}`.")

    @pact.command(name="remove")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_remove(self, ctx: commands.Context, *, name: str):
        """Remove a member from the tracked list."""
        name = name.strip()
        async with self.config.guild(ctx.guild).members() as members:
            if name not in members:
                await ctx.send(f"`{name}` is not tracked.")
                return
            members.remove(name)
        await ctx.send(f"Removed `{name}`.")

    @pact.command(name="schedule")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_schedule(
        self,
        ctx: commands.Context,
        interval: str,
        channel: Optional[discord.TextChannel] = None,
    ):
        """Post standings on a schedule. Examples: `30m`, `1h`, `6h`, `1d`."""
        seconds = parse_interval(interval)
        if seconds is None or seconds < 60:
            await ctx.send(
                "Invalid interval. Use e.g. `30m`, `1h`, `1d` (minimum 60s)."
            )
            return
        target = channel or ctx.channel
        await self.config.guild(ctx.guild).channel_id.set(target.id)
        await self.config.guild(ctx.guild).interval_seconds.set(seconds)
        self._start_task(ctx.guild.id)
        await ctx.send(
            f"Scheduled standings every **{format_interval(seconds)}** in {target.mention}."
        )

    @pact.command(name="unschedule")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_unschedule(self, ctx: commands.Context):
        """Stop scheduled posting."""
        await self.config.guild(ctx.guild).channel_id.set(None)
        await self.config.guild(ctx.guild).interval_seconds.set(None)
        self._stop_task(ctx.guild.id)
        await ctx.send("Schedule cleared.")

    @pact.command(name="status")
    async def pact_status(self, ctx: commands.Context):
        """Show current configuration."""
        data = await self.config.guild(ctx.guild).all()
        members = data.get("members") or []
        ch_id = data.get("channel_id")
        secs = data.get("interval_seconds")
        lines = [f"**Members tracked:** {len(members)}"]
        if ch_id and secs:
            ch = ctx.guild.get_channel(ch_id)
            ch_str = ch.mention if ch else f"<#{ch_id}> (missing)"
            lines.append(
                f"**Schedule:** every {format_interval(secs)} \u2192 {ch_str}"
            )
        else:
            lines.append("**Schedule:** not set")
        await ctx.send("\n".join(lines))

    def _start_task(self, guild_id: int):
        self._stop_task(guild_id)
        self._tasks[guild_id] = asyncio.create_task(self._run_loop(guild_id))

    def _stop_task(self, guild_id: int):
        task = self._tasks.pop(guild_id, None)
        if task:
            task.cancel()

    async def _run_loop(self, guild_id: int):
        while True:
            try:
                await self._tick(guild_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled tick failed for guild %s", guild_id)
            secs = await self.config.guild_from_id(guild_id).interval_seconds()
            if not secs:
                return
            try:
                await asyncio.sleep(secs)
            except asyncio.CancelledError:
                raise

    async def _tick(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        data = await self.config.guild(guild).all()
        ch_id = data.get("channel_id")
        members = data.get("members") or []
        if not ch_id or not members:
            return
        channel = guild.get_channel(ch_id)
        if not channel:
            return
        standings = await self._gather_standings(members)
        await channel.send(embed=self._format_embed(standings))
