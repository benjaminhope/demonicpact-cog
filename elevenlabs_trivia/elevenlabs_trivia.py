"""ElevenLabsTrivia — Red cog that runs voice trivia via an ElevenLabs Conversational AI agent."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import discord
from redbot.core import Config, checks, commands
from redbot.core.bot import Red

try:
    from discord.ext import voice_recv
    HAS_VOICE_RECV = True
except ImportError:
    HAS_VOICE_RECV = False
    voice_recv = None  # type: ignore

from .bridge import ElevenLabsConversation
from .sources import StreamingPCMSource, TriviaCaptureSink
from .topics import TOPICS, get as get_topic, list_keys as list_topic_keys

log = logging.getLogger("red.elevenlabs_trivia")


@dataclass
class GuildSession:
    guild_id: int
    voice_client: discord.VoiceClient
    conversation: ElevenLabsConversation
    source: StreamingPCMSource
    sink: object  # voice_recv.AudioSink at runtime
    topic_key: str

    async def teardown(self) -> None:
        try:
            self.source.close()
        except Exception:
            log.exception("source close failed")
        try:
            if self.voice_client.is_playing():
                self.voice_client.stop()
        except Exception:
            pass
        try:
            stop_listening = getattr(self.voice_client, "stop_listening", None)
            if callable(stop_listening):
                stop_listening()
        except Exception:
            pass
        try:
            await self.conversation.stop()
        except Exception:
            log.exception("conversation stop failed")
        try:
            if self.voice_client.is_connected():
                await self.voice_client.disconnect(force=True)
        except Exception:
            log.exception("voice disconnect failed")


class ElevenLabsTrivia(commands.Cog):
    """Voice trivia hosted by an ElevenLabs Conversational AI agent."""

    default_guild = {
        "agent_id": None,
        "default_topic": "osrs",
    }
    default_global = {
        "api_key": None,
    }

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xE11A_7717, force_registration=True)
        self.config.register_guild(**self.default_guild)
        self.config.register_global(**self.default_global)
        self._sessions: dict[int, GuildSession] = {}
        self._sessions_lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        for sess in list(self._sessions.values()):
            try:
                await sess.teardown()
            except Exception:
                log.exception("teardown during cog_unload failed")
        self._sessions.clear()

    # ----- configuration commands ------------------------------------------

    @commands.group(name="eltrivia", aliases=["eltriv"])
    async def eltrivia(self, ctx: commands.Context) -> None:
        """ElevenLabs voice trivia."""

    @eltrivia.command(name="setapikey")
    @checks.is_owner()
    async def eltrivia_setapikey(self, ctx: commands.Context, *, api_key: str) -> None:
        """Store the ElevenLabs API key (bot-owner only, global)."""
        await self.config.api_key.set(api_key.strip())
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("ElevenLabs API key saved.")

    @eltrivia.command(name="setagent")
    @checks.admin_or_permissions(manage_guild=True)
    async def eltrivia_setagent(self, ctx: commands.Context, agent_id: str) -> None:
        """Set the ElevenLabs agent ID for this server."""
        await self.config.guild(ctx.guild).agent_id.set(agent_id.strip())
        await ctx.send(f"Agent ID set to `{agent_id}`.")

    @eltrivia.command(name="settopic")
    @checks.admin_or_permissions(manage_guild=True)
    async def eltrivia_settopic(self, ctx: commands.Context, topic: str) -> None:
        """Set the default trivia topic for this server."""
        if get_topic(topic) is None:
            await ctx.send(f"Unknown topic. Try one of: {', '.join(list_topic_keys())}")
            return
        await self.config.guild(ctx.guild).default_topic.set(topic.lower())
        await ctx.send(f"Default topic set to **{TOPICS[topic.lower()].display_name}**.")

    @eltrivia.command(name="topics")
    async def eltrivia_topics(self, ctx: commands.Context) -> None:
        """List available trivia topics."""
        lines = [f"`{t.key}` — {t.display_name}" for t in TOPICS.values()]
        await ctx.send("**Topics**\n" + "\n".join(lines))

    @eltrivia.command(name="status")
    async def eltrivia_status(self, ctx: commands.Context) -> None:
        """Show the running trivia session for this server."""
        sess = self._sessions.get(ctx.guild.id)
        if sess is None:
            await ctx.send("No trivia session running.")
            return
        topic = TOPICS.get(sess.topic_key)
        topic_name = topic.display_name if topic else sess.topic_key
        ch = sess.voice_client.channel
        await ctx.send(
            f"Running **{topic_name}** in {ch.mention}. "
            f"Conversation ID: `{sess.conversation.conversation_id}`"
        )

    # ----- start / stop ----------------------------------------------------

    @eltrivia.command(name="start")
    @commands.guild_only()
    async def eltrivia_start(self, ctx: commands.Context, topic: Optional[str] = None) -> None:
        """Have the bot join your voice channel and start trivia."""
        if not HAS_VOICE_RECV:
            await ctx.send(
                "`discord-ext-voice-recv` is not installed. Run "
                "`[p]pipinstall discord-ext-voice-recv` (owner-only) "
                "or install it manually in the bot's environment, then reload this cog."
            )
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return

        async with self._sessions_lock:
            if ctx.guild.id in self._sessions:
                await ctx.send("A trivia session is already running. Use `[p]eltrivia stop` first.")
                return

            api_key = await self.config.api_key()
            agent_id = await self.config.guild(ctx.guild).agent_id()
            if not api_key:
                await ctx.send("API key is not set. The bot owner must run `[p]eltrivia setapikey`.")
                return
            if not agent_id:
                await ctx.send("Agent ID is not set. Run `[p]eltrivia setagent <id>`.")
                return

            topic_key = (topic or await self.config.guild(ctx.guild).default_topic() or "osrs").lower()
            topic_obj = get_topic(topic_key)
            if topic_obj is None:
                await ctx.send(f"Unknown topic. Try: {', '.join(list_topic_keys())}")
                return

            channel = ctx.author.voice.channel
            try:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=15.0)
            except discord.ClientException as e:
                # Already connected somewhere else in this guild via another voice client.
                await ctx.send(
                    f"Voice connect failed: {e}. The bot may already be connected via "
                    "another cog (e.g. Audio). Disconnect that first."
                )
                return
            except asyncio.TimeoutError:
                await ctx.send("Timed out connecting to the voice channel.")
                return

            loop = asyncio.get_running_loop()
            source = StreamingPCMSource(loop=loop)

            conversation = ElevenLabsConversation(
                api_key=api_key,
                agent_id=agent_id,
                system_prompt_override=topic_obj.system_prompt,
                first_message_override=topic_obj.first_message,
                dynamic_variables={
                    "topic": topic_obj.display_name,
                    "topic_key": topic_obj.key,
                    "host_user": ctx.author.display_name,
                },
                on_agent_pcm=source.feed,
                on_event=self._make_event_handler(ctx.guild.id, source),
                loop=loop,
            )

            try:
                await conversation.start()
            except Exception as e:
                log.exception("ElevenLabs start failed")
                await vc.disconnect(force=True)
                await ctx.send(f"Failed to connect to ElevenLabs: {e}")
                return

            sink = TriviaCaptureSink(on_pcm=lambda uid, name, pcm: conversation.submit_user_pcm(uid, pcm))
            try:
                vc.listen(sink)
            except Exception:
                log.exception("voice_recv listen failed")
                await conversation.stop()
                await vc.disconnect(force=True)
                await ctx.send("Failed to start voice receive.")
                return

            def _after_play(error: Optional[BaseException]) -> None:
                if error:
                    log.warning("Voice player ended with error: %r", error)

            vc.play(source, after=_after_play)

            self._sessions[ctx.guild.id] = GuildSession(
                guild_id=ctx.guild.id,
                voice_client=vc,
                conversation=conversation,
                source=source,
                sink=sink,
                topic_key=topic_obj.key,
            )

            await ctx.send(
                f"Joined {channel.mention} for **{topic_obj.display_name}** trivia. "
                "Speak up to answer!"
            )

    @eltrivia.command(name="stop")
    @commands.guild_only()
    async def eltrivia_stop(self, ctx: commands.Context) -> None:
        """Stop the running trivia session and leave the voice channel."""
        async with self._sessions_lock:
            sess = self._sessions.pop(ctx.guild.id, None)
        if sess is None:
            await ctx.send("No trivia session to stop.")
            return
        await sess.teardown()
        await ctx.send("Trivia session ended.")

    # ----- helpers ---------------------------------------------------------

    def _make_event_handler(self, guild_id: int, source: StreamingPCMSource):
        async def _handler(evt: dict) -> None:
            etype = evt.get("type")
            if etype == "interruption":
                # Drop any agent audio still queued so the new turn starts clean.
                source.flush()
            elif etype in ("error", "conversation_ended"):
                log.info("ElevenLabs ended session (%s) for guild %s", etype, guild_id)
                sess = self._sessions.pop(guild_id, None)
                if sess is not None:
                    await sess.teardown()
        return _handler

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        # If the bot itself is disconnected, clean up its session.
        if member.id != self.bot.user.id:
            return
        if before.channel is not None and after.channel is None:
            sess = self._sessions.pop(member.guild.id, None)
            if sess is not None:
                await sess.teardown()
