"""Audio bridge between Discord voice and the ElevenLabs Conversational AI WS.

Discord voice  : 48 kHz, 16-bit, stereo, 20 ms frames (3840 bytes per frame).
ElevenLabs ConvAI inbound (we send): 16 kHz, 16-bit, mono PCM, base64-encoded.
ElevenLabs ConvAI outbound (we receive): same format the agent is configured
for, default 16 kHz mono PCM.

Protocol summary (https://elevenlabs.io/docs/conversational-ai):
  Connect:  wss://api.elevenlabs.io/v1/convai/conversation?agent_id=<id>
  Headers:  xi-api-key: <key>
  First message from server: conversation_initiation_metadata (contains
  conversation id and the audio formats for the session).
  Client may immediately send conversation_initiation_client_data with prompt
  + first message overrides and dynamic_variables (the agent must allow
  overrides in its dashboard security settings).
  After that:
    server -> client: audio | user_transcript | agent_response |
                      interruption | ping | internal_tentative_agent_response
    client -> server: {"user_audio_chunk": "<base64>"} | pong
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger("red.elevenlabs_trivia.bridge")

# Audio format constants
DISCORD_RATE = 48_000
DISCORD_CHANNELS = 2
ELEVENLABS_RATE_DEFAULT = 16_000
SAMPLE_WIDTH = 2  # 16-bit

# Send tick: how often we flush mixed user audio upstream.
SEND_TICK_SECONDS = 0.04
DISCORD_FRAME_BYTES_PER_TICK = (
    int(DISCORD_RATE * SEND_TICK_SECONDS) * DISCORD_CHANNELS * SAMPLE_WIDTH
)

# Maximum buffered upstream audio per user (drop oldest beyond this).
MAX_USER_BUFFER_BYTES = DISCORD_FRAME_BYTES_PER_TICK * 25  # ~1 s


@dataclass
class _UserBuffer:
    pcm_48k_stereo: bytearray = field(default_factory=bytearray)
    last_seen: float = 0.0


class ElevenLabsConversation:
    """One live ElevenLabs Conversational AI session.

    The cog drives this by pushing user PCM via `submit_user_pcm` (called from
    the voice-recv decoder thread) and registering a callback `on_agent_pcm`
    to receive 48 kHz stereo PCM ready for Discord playback.

    Lifecycle: `await start()`, then session runs until `await stop()` or the
    server closes the connection.
    """

    BASE_WS = "wss://api.elevenlabs.io/v1/convai/conversation"

    def __init__(
        self,
        *,
        api_key: str,
        agent_id: str,
        system_prompt_override: Optional[str] = None,
        first_message_override: Optional[str] = None,
        dynamic_variables: Optional[dict] = None,
        on_agent_pcm: Optional[Callable[[bytes], None]] = None,
        on_event: Optional[Callable[[dict], Awaitable[None]]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._api_key = api_key
        self._agent_id = agent_id
        self._system_prompt_override = system_prompt_override
        self._first_message_override = first_message_override
        self._dynamic_variables = dynamic_variables or {}
        self._on_agent_pcm = on_agent_pcm
        self._on_event = on_event
        self._loop = loop or asyncio.get_event_loop()

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._send_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

        # Per-user upstream buffers (48 kHz stereo, raw from voice-recv).
        self._user_bufs: dict[int, _UserBuffer] = defaultdict(_UserBuffer)
        self._user_bufs_lock = asyncio.Lock()

        # Resampler state (carried across calls, per direction).
        self._upstream_ratecv_state = None
        self._downstream_ratecv_state = None
        self._agent_audio_rate = ELEVENLABS_RATE_DEFAULT  # updated from init metadata

        # Stats / introspection.
        self.conversation_id: Optional[str] = None
        self.last_user_transcript: str = ""
        self.last_agent_response: str = ""

    # ----- public API -------------------------------------------------------

    async def start(self) -> None:
        url = f"{self.BASE_WS}?agent_id={quote(self._agent_id)}"
        headers = {"xi-api-key": self._api_key}
        self._session = aiohttp.ClientSession()
        log.info("Connecting to ElevenLabs ConvAI for agent %s", self._agent_id)
        self._ws = await self._session.ws_connect(
            url, headers=headers, heartbeat=None, max_msg_size=0
        )
        await self._send_initiation()
        self._recv_task = self._loop.create_task(self._recv_loop(), name="el-recv")
        self._send_task = self._loop.create_task(self._send_loop(), name="el-send")

    async def stop(self) -> None:
        self._stopped.set()
        for t in (self._recv_task, self._send_task):
            if t and not t.done():
                t.cancel()
        for t in (self._recv_task, self._send_task):
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    def submit_user_pcm(self, user_id: int, pcm_48k_stereo: bytes) -> None:
        """Thread-safe entry point called from the voice-recv decoder thread."""
        if self._stopped.is_set():
            return
        self._loop.call_soon_threadsafe(self._enqueue_user_pcm, user_id, pcm_48k_stereo)

    # ----- internals --------------------------------------------------------

    def _enqueue_user_pcm(self, user_id: int, pcm: bytes) -> None:
        buf = self._user_bufs[user_id]
        buf.pcm_48k_stereo.extend(pcm)
        buf.last_seen = time.monotonic()
        if len(buf.pcm_48k_stereo) > MAX_USER_BUFFER_BYTES:
            # Drop oldest to bound memory under network stalls.
            overflow = len(buf.pcm_48k_stereo) - MAX_USER_BUFFER_BYTES
            del buf.pcm_48k_stereo[:overflow]

    async def _send_initiation(self) -> None:
        msg: dict = {"type": "conversation_initiation_client_data"}
        override = {}
        if self._system_prompt_override or self._first_message_override:
            agent_override: dict = {}
            if self._system_prompt_override:
                agent_override["prompt"] = {"prompt": self._system_prompt_override}
            if self._first_message_override:
                agent_override["first_message"] = self._first_message_override
            override["agent"] = agent_override
        if override:
            msg["conversation_config_override"] = override
        if self._dynamic_variables:
            msg["dynamic_variables"] = self._dynamic_variables
        await self._ws.send_json(msg)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    try:
                        evt = json.loads(raw.data)
                    except json.JSONDecodeError:
                        log.warning("Non-JSON text frame from ElevenLabs: %r", raw.data[:200])
                        continue
                    await self._handle_event(evt)
                elif raw.type == aiohttp.WSMsgType.BINARY:
                    # ConvAI sends JSON over text by default; binary is unexpected.
                    log.debug("Unexpected binary frame: %d bytes", len(raw.data))
                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                    break
                elif raw.type == aiohttp.WSMsgType.ERROR:
                    log.warning("WS error frame: %s", self._ws.exception())
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ElevenLabs recv loop crashed")
        finally:
            self._stopped.set()

    async def _handle_event(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "conversation_initiation_metadata":
            meta = evt.get("conversation_initiation_metadata_event", {})
            self.conversation_id = meta.get("conversation_id")
            # Format strings look like "pcm_16000" or "pcm_22050".
            output_fmt = meta.get("agent_output_audio_format", "pcm_16000")
            self._agent_audio_rate = _parse_pcm_rate(output_fmt, ELEVENLABS_RATE_DEFAULT)
            log.info(
                "ElevenLabs session started: conversation_id=%s output_rate=%d",
                self.conversation_id,
                self._agent_audio_rate,
            )
        elif etype == "audio":
            audio_evt = evt.get("audio_event", {})
            b64 = audio_evt.get("audio_base_64", "")
            if b64 and self._on_agent_pcm:
                pcm_in = base64.b64decode(b64)
                pcm_out = self._upsample_for_discord(pcm_in)
                if pcm_out:
                    try:
                        self._on_agent_pcm(pcm_out)
                    except Exception:
                        log.exception("on_agent_pcm raised")
        elif etype == "ping":
            ping_evt = evt.get("ping_event", {})
            event_id = ping_evt.get("event_id")
            if event_id is not None and self._ws and not self._ws.closed:
                await self._ws.send_json({"type": "pong", "event_id": event_id})
        elif etype == "interruption":
            # The cog should drop any queued agent audio. We surface this via
            # on_event so the cog can call StreamingPCMSource.flush().
            pass
        elif etype == "user_transcript":
            ut = evt.get("user_transcription_event", {}).get("user_transcript", "")
            if ut:
                self.last_user_transcript = ut
                log.debug("user transcript: %s", ut)
        elif etype == "agent_response":
            ar = evt.get("agent_response_event", {}).get("agent_response", "")
            if ar:
                self.last_agent_response = ar
                log.debug("agent response: %s", ar)
        # internal_tentative_agent_response, agent_response_correction, etc.
        # are useful for UIs but we ignore them here.

        if self._on_event:
            try:
                await self._on_event(evt)
            except Exception:
                log.exception("on_event raised")

    async def _send_loop(self) -> None:
        try:
            while not self._stopped.is_set():
                await asyncio.sleep(SEND_TICK_SECONDS)
                chunk = await self._collect_and_resample_user_audio()
                if not chunk:
                    continue
                if self._ws is None or self._ws.closed:
                    break
                payload = {"user_audio_chunk": base64.b64encode(chunk).decode("ascii")}
                try:
                    await self._ws.send_json(payload)
                except (aiohttp.ClientError, ConnectionResetError):
                    log.warning("WS send failed; ending session")
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ElevenLabs send loop crashed")
        finally:
            self._stopped.set()

    async def _collect_and_resample_user_audio(self) -> bytes:
        # Pop up to one tick's worth from each user, mix, then resample
        # 48 kHz stereo -> 16 kHz mono.
        async with self._user_bufs_lock:
            stale_threshold = time.monotonic() - 5.0
            mixed_48k: Optional[bytes] = None
            for uid in list(self._user_bufs.keys()):
                ubuf = self._user_bufs[uid]
                if not ubuf.pcm_48k_stereo:
                    if ubuf.last_seen < stale_threshold:
                        del self._user_bufs[uid]
                    continue
                take = min(len(ubuf.pcm_48k_stereo), DISCORD_FRAME_BYTES_PER_TICK)
                # align to sample boundary (4 bytes per stereo s16 sample)
                take -= take % 4
                if take == 0:
                    continue
                frame = bytes(ubuf.pcm_48k_stereo[:take])
                del ubuf.pcm_48k_stereo[:take]
                if mixed_48k is None:
                    mixed_48k = frame
                else:
                    mixed_48k = _mix_pcm(mixed_48k, frame)
        if not mixed_48k:
            return b""
        # 48 kHz stereo s16 -> 48 kHz mono s16
        mono_48k = audioop.tomono(mixed_48k, SAMPLE_WIDTH, 0.5, 0.5)
        # 48 kHz -> 16 kHz, keeping resampler state across calls.
        resampled, self._upstream_ratecv_state = audioop.ratecv(
            mono_48k,
            SAMPLE_WIDTH,
            1,
            DISCORD_RATE,
            ELEVENLABS_RATE_DEFAULT,
            self._upstream_ratecv_state,
        )
        return resampled

    def _upsample_for_discord(self, pcm_mono_at_agent_rate: bytes) -> bytes:
        if not pcm_mono_at_agent_rate:
            return b""
        # agent rate mono -> 48 kHz mono
        upsampled, self._downstream_ratecv_state = audioop.ratecv(
            pcm_mono_at_agent_rate,
            SAMPLE_WIDTH,
            1,
            self._agent_audio_rate,
            DISCORD_RATE,
            self._downstream_ratecv_state,
        )
        # 48 kHz mono -> 48 kHz stereo
        return audioop.tostereo(upsampled, SAMPLE_WIDTH, 1.0, 1.0)


def _mix_pcm(a: bytes, b: bytes) -> bytes:
    """Mix two equal-length s16le PCM buffers with simple saturation."""
    if len(a) == len(b):
        return audioop.add(a, b, SAMPLE_WIDTH)
    # Pad the shorter one with zeros so add() doesn't drop samples.
    if len(a) < len(b):
        a = a + b"\x00" * (len(b) - len(a))
    else:
        b = b + b"\x00" * (len(a) - len(b))
    return audioop.add(a, b, SAMPLE_WIDTH)


def _parse_pcm_rate(fmt: str, default: int) -> int:
    # e.g. "pcm_16000", "pcm_22050", "pcm_44100"
    try:
        if fmt.startswith("pcm_"):
            return int(fmt.split("_", 1)[1])
    except ValueError:
        pass
    return default
