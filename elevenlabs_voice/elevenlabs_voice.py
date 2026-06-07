"""ElevenLabsVoice — summon an ElevenLabs Conversational AI agent to a voice channel."""

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

try:
    import davey as _davey
    HAS_DAVEY = True
except ImportError:
    HAS_DAVEY = False
    _davey = None  # type: ignore

from .bridge import ElevenLabsConversation
from .sources import StreamingPCMSource, VoiceCaptureSink

log = logging.getLogger("red.elevenlabs_voice")

def _apply_dave_patch(vc) -> None:
    """Patch voice_recv's PacketDecryptor to also run DAVE decryption.

    voice_recv only handles the RTP transport layer (xchacha20). Discord 2.x
    adds a second DAVE E2E layer on top; without this patch every Opus frame
    is still DAVE-encrypted when it reaches the Opus decoder → corrupted stream.
    """
    if not HAS_DAVEY:
        return
    reader = getattr(vc, "_reader", None)
    if reader is None:
        return

    original_fn = reader.decryptor.decrypt_rtp

    def _dave_decrypt_rtp(packet):
        result = original_fn(packet)
        ds = getattr(getattr(vc, "_connection", None), "dave_session", None)
        if not ds or not ds.ready:
            return result
        uid = vc._ssrc_to_id.get(packet.ssrc)
        if uid is None:
            return result
        try:
            return ds.decrypt(uid, _davey.MediaType.audio, result)
        except Exception:
            log.debug("DAVE decrypt failed ssrc=%s uid=%s", packet.ssrc, uid)
            return result

    reader.decryptor.decrypt_rtp = _dave_decrypt_rtp
    log.debug("DAVE patch applied to reader for guild %s", getattr(vc.guild, "id", "?"))


_VOICE_RECV_INSTALL_MSG = (
    "`discord-ext-voice-recv` is not installed.\n"
    "Install it in the bot's environment:\n"
    "```\npip install discord-ext-voice-recv\n```\n"
    "Then reload this cog."
)


@dataclass
class VoiceSession:
    guild_id: int
    voice_client: discord.VoiceClient
    conversation: ElevenLabsConversation
    source: StreamingPCMSource
    sink: object
    _watchdog: Optional[asyncio.Task] = None

    def start_watchdog(self) -> None:
        self._watchdog = asyncio.get_event_loop().create_task(
            self._listen_watchdog(), name=f"elv-watchdog-{self.guild_id}"
        )

    async def _listen_watchdog(self) -> None:
        """Restart voice_recv listening if its router crashes on a bad Opus packet."""
        await asyncio.sleep(2)  # let the initial listen settle
        while True:
            await asyncio.sleep(1)
            if not self.voice_client.is_connected():
                return
            is_listening = getattr(self.voice_client, "is_listening", lambda: True)
            if not is_listening():
                log.info("voice_recv listener stopped (crashed); restarting for guild %s", self.guild_id)
                try:
                    self.voice_client.listen(self.sink)
                    _apply_dave_patch(self.voice_client)
                except Exception:
                    log.exception("failed to restart listener")

    async def teardown(self) -> None:
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
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


class ElevenLabsVoice(commands.Cog):
    """Summon an ElevenLabs Conversational AI agent to a voice channel."""

    default_guild = {
        "agent_id": None,
        "system_prompt": None,
        "first_message": None,
    }
    default_global = {
        "api_key": None,
    }

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xE11A_7013, force_registration=True)
        self.config.register_guild(**self.default_guild)
        self.config.register_global(**self.default_global)
        self._sessions: dict[int, VoiceSession] = {}
        self._lock = asyncio.Lock()

    async def cog_unload(self) -> None:
        for sess in list(self._sessions.values()):
            try:
                await sess.teardown()
            except Exception:
                log.exception("teardown during unload failed")
        self._sessions.clear()

    # ---- configuration -------------------------------------------------------

    @commands.group(name="elvoice", aliases=["elv"])
    async def elvoice(self, ctx: commands.Context) -> None:
        """ElevenLabs voice agent commands."""

    @elvoice.command(name="setapikey")
    @checks.is_owner()
    async def elvoice_setapikey(self, ctx: commands.Context, *, api_key: str) -> None:
        """Store the ElevenLabs API key (bot-owner only)."""
        await self.config.api_key.set(api_key.strip())
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        await ctx.send("ElevenLabs API key saved.")

    @elvoice.command(name="setagent")
    @checks.admin_or_permissions(manage_guild=True)
    async def elvoice_setagent(self, ctx: commands.Context, agent_id: str) -> None:
        """Set the ElevenLabs agent ID for this server."""
        await self.config.guild(ctx.guild).agent_id.set(agent_id.strip())
        await ctx.send(f"Agent ID set to `{agent_id}`.")

    @elvoice.command(name="setprompt")
    @checks.admin_or_permissions(manage_guild=True)
    async def elvoice_setprompt(self, ctx: commands.Context, *, prompt: str = "") -> None:
        """Override the agent's system prompt for this server. Leave blank to clear."""
        value = prompt.strip() or None
        await self.config.guild(ctx.guild).system_prompt.set(value)
        await ctx.send("System prompt cleared." if value is None else "System prompt saved.")

    @elvoice.command(name="setfirst")
    @checks.admin_or_permissions(manage_guild=True)
    async def elvoice_setfirst(self, ctx: commands.Context, *, message: str = "") -> None:
        """Override the agent's first message for this server. Leave blank to clear."""
        value = message.strip() or None
        await self.config.guild(ctx.guild).first_message.set(value)
        await ctx.send("First message cleared." if value is None else "First message saved.")

    @elvoice.command(name="status")
    @commands.guild_only()
    async def elvoice_status(self, ctx: commands.Context) -> None:
        """Show the active session for this server."""
        sess = self._sessions.get(ctx.guild.id)
        if sess is None:
            await ctx.send("No active voice session.")
            return
        ch = sess.voice_client.channel
        conv_id = sess.conversation.conversation_id or "connecting…"
        await ctx.send(f"Active in {ch.mention} — conversation ID: `{conv_id}`")

    # ---- summon / dismiss ----------------------------------------------------

    @elvoice.command(name="summon")
    @commands.guild_only()
    async def elvoice_summon(self, ctx: commands.Context) -> None:
        """Join your voice channel and start the ElevenLabs agent."""
        if not HAS_VOICE_RECV:
            await ctx.send(_VOICE_RECV_INSTALL_MSG)
            return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("You need to be in a voice channel first.")
            return

        async with self._lock:
            if ctx.guild.id in self._sessions:
                await ctx.send(
                    "Already active in a voice channel. Use `[p]elvoice dismiss` first."
                )
                return

            api_key = await self.config.api_key()
            agent_id = await self.config.guild(ctx.guild).agent_id()
            if not api_key:
                await ctx.send(
                    "No API key set. The bot owner must run `[p]elvoice setapikey <key>`."
                )
                return
            if not agent_id:
                await ctx.send(
                    "No agent ID set. Run `[p]elvoice setagent <id>`."
                )
                return

            system_prompt = await self.config.guild(ctx.guild).system_prompt()
            first_message = await self.config.guild(ctx.guild).first_message()

            channel = ctx.author.voice.channel
            try:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient, timeout=15.0)
            except discord.ClientException as e:
                await ctx.send(
                    f"Voice connect failed: {e}\n"
                    "The bot may already be connected via another cog. Disconnect that first."
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
                system_prompt_override=system_prompt,
                first_message_override=first_message,
                dynamic_variables={"summoner": ctx.author.display_name},
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

            sink = VoiceCaptureSink(
                on_pcm=lambda uid, name, pcm: conversation.submit_user_pcm(uid, pcm)
            )
            try:
                vc.listen(sink)
                _apply_dave_patch(vc)
            except Exception:
                log.exception("voice_recv listen failed")
                await conversation.stop()
                await vc.disconnect(force=True)
                await ctx.send("Failed to start voice receive.")
                return

            vc.play(source, after=lambda err: err and log.warning("player error: %r", err))

            sess = VoiceSession(
                guild_id=ctx.guild.id,
                voice_client=vc,
                conversation=conversation,
                source=source,
                sink=sink,
            )
            sess.start_watchdog()
            self._sessions[ctx.guild.id] = sess

        await ctx.send(f"Joined {channel.mention}. The agent is listening.")

    @elvoice.command(name="dismiss", aliases=["leave", "stop"])
    @commands.guild_only()
    async def elvoice_dismiss(self, ctx: commands.Context) -> None:
        """Disconnect the agent and leave the voice channel."""
        async with self._lock:
            sess = self._sessions.pop(ctx.guild.id, None)
        if sess is None:
            await ctx.send("No active voice session.")
            return
        await sess.teardown()
        await ctx.send("Left the voice channel.")

    # ---- helpers -------------------------------------------------------------

    def _make_event_handler(self, guild_id: int, source: StreamingPCMSource):
        async def _handler(evt: dict) -> None:
            etype = evt.get("type")
            if etype == "interruption":
                source.flush()
            elif etype in ("error", "conversation_ended"):
                log.info("Session ended (%s) for guild %s", etype, guild_id)
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
        if member.id != self.bot.user.id:
            return
        if before.channel is not None and after.channel is None:
            sess = self._sessions.pop(member.guild.id, None)
            if sess is not None:
                await sess.teardown()
