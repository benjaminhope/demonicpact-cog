import asyncio
import logging
import random
import re
import time
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

WISE_OLD_MAN_URL = (
    "https://oldschool.runescape.wiki/images/thumb/Wise_Old_Man.png/"
    "260px-Wise_Old_Man.png?b2e69"
)

CURSE_DURATION = 3600

CURSE_NAMES = [
    "K'ril's Personal Cocksleeve",
    "Greater Demon's Cum Sock",
    "Bandos's Bitch Boy",
    "Gorilla Gangbang Survivor",
    "Zamorak's Used Condom",
    "Tzhaar Bathhouse Rentboy",
    "Hellhound's Favorite Hole",
    "Lesser Demon Fucktoy (Bronze)",
    "Trial of Getting Railed: Champ",
    "Tsutsaroth's OnlyFans Subscriber",
]

RANK_SUFFIX_RE = re.compile(r"\s*\[#[^\]]*\]\s*$")
NICK_MAX = 32


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


def _strip_rank_suffix(nick: str) -> str:
    return RANK_SUFFIX_RE.sub("", nick).strip()


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
            rs_to_discord={},
            previous_standings=[],
            curses={},
        )
        self._tasks: dict[int, asyncio.Task] = {}
        self._curse_tasks: dict[int, dict[int, asyncio.Task]] = {}
        self._startup_task = asyncio.create_task(self._resume_state())

    async def _resume_state(self):
        await self.bot.wait_until_red_ready()
        for guild in self.bot.guilds:
            data = await self.config.guild(guild).all()
            if data.get("channel_id") and data.get("interval_seconds"):
                self._start_task(guild.id)
            curses = data.get("curses") or {}
            now = time.time()
            for uid_str, entry in list(curses.items()):
                remaining = entry["expires_at"] - now
                if remaining <= 0:
                    await self._revert_curse(guild, int(uid_str))
                else:
                    self._schedule_curse_revert(guild, int(uid_str), remaining)

    def cog_unload(self):
        self._startup_task.cancel()
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        for user_tasks in self._curse_tasks.values():
            for task in user_tasks.values():
                task.cancel()
        self._curse_tasks.clear()

    # ---- Fetching ----

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

    # ---- Formatting ----

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

    # ---- Commands ----

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
        async with self.config.guild(ctx.guild).rs_to_discord() as links:
            links.pop(name, None)
        await ctx.send(f"Removed `{name}`.")

    @pact.command(name="link")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_link(
        self, ctx: commands.Context, user: discord.Member, *, rs_name: str
    ):
        """Link a Discord user to a tracked RuneScape name. Example: `!pact link @Joe iron jorky`"""
        rs_name = rs_name.strip()
        members = await self.config.guild(ctx.guild).members()
        if rs_name not in members:
            await ctx.send(
                f"`{rs_name}` is not tracked. Add it first with `!pact add <name>`."
            )
            return
        async with self.config.guild(ctx.guild).rs_to_discord() as links:
            links[rs_name] = user.id
        await ctx.send(f"Linked `{rs_name}` → {user.mention}.")

    @pact.command(name="unlink")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_unlink(self, ctx: commands.Context, *, rs_name: str):
        """Unlink a RuneScape name from its Discord user."""
        rs_name = rs_name.strip()
        async with self.config.guild(ctx.guild).rs_to_discord() as links:
            if rs_name not in links:
                await ctx.send(f"`{rs_name}` is not linked.")
                return
            del links[rs_name]
        await ctx.send(f"Unlinked `{rs_name}`.")

    @pact.command(name="links")
    async def pact_links(self, ctx: commands.Context):
        """List RS-to-Discord links."""
        links = await self.config.guild(ctx.guild).rs_to_discord()
        if not links:
            await ctx.send("No links configured.")
            return
        lines = []
        for rs_name, user_id in links.items():
            member = ctx.guild.get_member(user_id)
            who = member.mention if member else f"<@{user_id}> (not in server)"
            lines.append(f"`{rs_name}` → {who}")
        await ctx.send("**Links:**\n" + "\n".join(lines))

    @pact.command(name="applynicks")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_applynicks(self, ctx: commands.Context):
        """Recalculate rank suffixes on linked members' nicknames."""
        members = await self.config.guild(ctx.guild).members()
        if not members:
            await ctx.send("No members configured.")
            return
        async with ctx.typing():
            standings = await self._gather_standings(members)
            await self._update_nicknames(ctx.guild, standings)
        await ctx.send("Nicknames updated.")

    @pact.command(name="uncurse")
    @commands.admin_or_permissions(manage_guild=True)
    async def pact_uncurse(self, ctx: commands.Context, user: discord.Member):
        """Lift an active curse early."""
        curses = await self.config.guild(ctx.guild).curses()
        if str(user.id) not in curses:
            await ctx.send(f"{user.mention} is not cursed.")
            return
        await self._revert_curse(ctx.guild, user.id)
        await ctx.send(f"Lifted the curse on {user.mention}.")

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
        links = data.get("rs_to_discord") or {}
        curses = data.get("curses") or {}
        lines = [
            f"**Members tracked:** {len(members)}",
            f"**Discord links:** {len(links)}",
            f"**Active curses:** {len(curses)}",
        ]
        if ch_id and secs:
            ch = ctx.guild.get_channel(ch_id)
            ch_str = ch.mention if ch else f"<#{ch_id}> (missing)"
            lines.append(
                f"**Schedule:** every {format_interval(secs)} → {ch_str}"
            )
        else:
            lines.append("**Schedule:** not set")
        await ctx.send("\n".join(lines))

    # ---- Scheduling ----

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

        previous = data.get("previous_standings") or []
        await self._update_nicknames(guild, standings)
        overtakes = self._detect_overtakes(previous, standings)
        await self._handle_overtakes(guild, channel, overtakes)

        serialisable = [[n, r, p] for n, r, p in standings]
        await self.config.guild(guild).previous_standings.set(serialisable)

        await channel.send(embed=self._format_embed(standings))

    # ---- Overtake detection ----

    @staticmethod
    def _detect_overtakes(prev, curr) -> list[tuple[str, str]]:
        prev_order = [s[0] for s in prev if s[2] is not None]
        curr_order = [
            s[0]
            for s in sorted(curr, key=lambda r: (r[2] is None, -(r[2] or 0)))
            if s[2] is not None
        ]
        prev_pos = {name: i for i, name in enumerate(prev_order)}
        curr_pos = {name: i for i, name in enumerate(curr_order)}
        overtakes: list[tuple[str, str]] = []
        for victim in prev_order:
            if victim not in curr_pos:
                continue
            if curr_pos[victim] <= prev_pos[victim]:
                continue
            for cand in curr_order:
                if cand == victim or cand not in prev_pos:
                    continue
                if (
                    prev_pos[cand] > prev_pos[victim]
                    and curr_pos[cand] < curr_pos[victim]
                ):
                    overtakes.append((cand, victim))
                    break
        return overtakes

    async def _handle_overtakes(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        overtakes: list[tuple[str, str]],
    ):
        links = await self.config.guild(guild).rs_to_discord()
        curses = await self.config.guild(guild).curses()
        for overtaker_rs, victim_rs in overtakes:
            victim_uid = links.get(victim_rs)
            if not victim_uid:
                continue
            if str(victim_uid) in curses:
                continue
            victim = guild.get_member(victim_uid)
            if not victim:
                continue
            await self._apply_curse(
                guild, channel, victim, victim_rs, overtaker_rs
            )
            curses[str(victim_uid)] = True  # prevent double-curse within this tick

    # ---- Curses ----

    async def _apply_curse(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        victim: discord.Member,
        victim_rs: str,
        overtaker_rs: str,
    ):
        original_nick = victim.nick
        curse_name = random.choice(CURSE_NAMES)
        try:
            await victim.edit(
                nick=curse_name[:NICK_MAX],
                reason=f"Cursed by the Wise Old Man (overtaken by {overtaker_rs})",
            )
        except discord.Forbidden:
            log.warning("no permission to curse %s", victim)
        except discord.HTTPException:
            log.exception("failed to apply curse nick to %s", victim)

        expires_at = time.time() + CURSE_DURATION
        async with self.config.guild(guild).curses() as curses:
            curses[str(victim.id)] = {
                "original_nick": original_nick,
                "expires_at": expires_at,
                "rs_name": victim_rs,
                "overtaker": overtaker_rs,
                "curse_name": curse_name,
            }
        self._schedule_curse_revert(guild, victim.id, CURSE_DURATION)

        embed = discord.Embed(
            title="The Wise Old Man appears!",
            description=(
                f"**{overtaker_rs}** has just overtaken **{victim_rs}** on the leaderboards.\n"
                f"The Wise Old Man appears and curses {victim.mention} for an hour."
            ),
            color=discord.Color.dark_purple(),
        )
        embed.set_image(url=WISE_OLD_MAN_URL)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("failed to post curse message")

    def _schedule_curse_revert(
        self, guild: discord.Guild, user_id: int, delay: float
    ):
        guild_tasks = self._curse_tasks.setdefault(guild.id, {})
        existing = guild_tasks.pop(user_id, None)
        if existing:
            existing.cancel()
        guild_tasks[user_id] = asyncio.create_task(
            self._curse_revert_after(guild, user_id, delay)
        )

    async def _curse_revert_after(
        self, guild: discord.Guild, user_id: int, delay: float
    ):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        try:
            await self._revert_curse(guild, user_id)
        except Exception:
            log.exception("curse revert failed for user %s", user_id)

    async def _revert_curse(self, guild: discord.Guild, user_id: int):
        async with self.config.guild(guild).curses() as curses:
            entry = curses.pop(str(user_id), None)
        task = self._curse_tasks.get(guild.id, {}).pop(user_id, None)
        if task and not task.done():
            task.cancel()
        if not entry:
            return
        member = guild.get_member(user_id)
        if not member:
            return
        try:
            await member.edit(
                nick=entry.get("original_nick"),
                reason="Curse expired",
            )
        except discord.Forbidden:
            log.warning("no permission to lift curse on %s", member)
        except discord.HTTPException:
            log.exception("failed to revert curse nick on %s", member)

    # ---- Nicknames ----

    async def _update_nicknames(
        self,
        guild: discord.Guild,
        standings: list[tuple[str, Optional[int], Optional[int]]],
    ):
        links = await self.config.guild(guild).rs_to_discord()
        curses = await self.config.guild(guild).curses()
        cursed_ids = {int(uid) for uid in curses}
        ranked = sorted(
            standings,
            key=lambda r: (r[2] is None, -(r[2] or 0)),
        )
        for i, (rs_name, _, pts) in enumerate(ranked):
            user_id = links.get(rs_name)
            if not user_id or user_id in cursed_ids:
                continue
            member = guild.get_member(user_id)
            if not member:
                continue
            suffix = f" [#{i + 1}]" if pts is not None else " [#?]"
            base = _strip_rank_suffix(member.display_name)
            desired = (base + suffix)[:NICK_MAX]
            if member.nick == desired:
                continue
            try:
                await member.edit(nick=desired, reason="Demonic Pact rank")
            except discord.Forbidden:
                log.warning("no permission to change nick for %s", member)
            except discord.HTTPException:
                log.exception("failed to set nick for %s", member)
