"""Discord audio sink (capture) and source (playback) helpers.

Discord voice runs at 48 kHz, 16-bit, stereo, in 20 ms frames (3840 bytes).
ElevenLabs Conversational AI uses 16 kHz, 16-bit, mono PCM. Resampling and
channel conversion live here in `bridge.py`; this module just plumbs frames
between Discord and a thread-safe queue.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable

import discord

try:
    from discord.ext import voice_recv  # type: ignore
    _HAS_VOICE_RECV = True
except ImportError:
    voice_recv = None  # type: ignore
    _HAS_VOICE_RECV = False

log = logging.getLogger("red.elevenlabs_trivia.sources")


def _voice_recv_base():
    """Return voice_recv.AudioSink, or object if voice-recv isn't installed.

    This lets the module import cleanly so the main cog can surface a helpful
    error to the user instead of failing at load time.
    """
    if _HAS_VOICE_RECV:
        return voice_recv.AudioSink
    return object

FRAME_BYTES_48K_STEREO = 3840  # 20 ms * 48000 Hz * 2 ch * 2 bytes
SILENCE_FRAME = b"\x00" * FRAME_BYTES_48K_STEREO


class TriviaCaptureSink(_voice_recv_base()):  # type: ignore[misc]
    """Voice-recv sink that pushes decoded PCM into a thread-safe callback.

    Voice-recv invokes `write` on its own decoder thread, so the callback
    must be cheap and not call into asyncio directly.
    """

    def __init__(
        self,
        on_pcm: Callable[[int, str, bytes], None],
    ):
        if not _HAS_VOICE_RECV:
            raise RuntimeError(
                "discord-ext-voice-recv is not installed. "
                "Run: pip install discord-ext-voice-recv"
            )
        super().__init__()
        self._on_pcm = on_pcm

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data) -> None:  # noqa: ANN001 - voice_recv types
        if user is None or getattr(user, "bot", False):
            return
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return
        try:
            self._on_pcm(user.id, user.display_name, pcm)
        except Exception:
            log.exception("on_pcm callback raised")

    def cleanup(self) -> None:
        pass


class StreamingPCMSource(discord.AudioSource):
    """A Discord audio source fed by an external producer.

    Producers call `feed(pcm)` with arbitrary-length 48 kHz stereo s16le
    bytes. The source slices into 20 ms frames on `read()` and returns
    silence when starved so playback stays alive across pauses.

    Call `close()` to make `read()` return b"" and let the voice client
    stop the player cleanly.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._buf = bytearray()
        self._frames: deque[bytes] = deque()
        self._closed = False
        self._lock = asyncio.Lock()

    def is_opus(self) -> bool:
        return False

    # --- producer side (asyncio) ---

    def feed(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        self._buf.extend(pcm)
        while len(self._buf) >= FRAME_BYTES_48K_STEREO:
            frame = bytes(self._buf[:FRAME_BYTES_48K_STEREO])
            del self._buf[:FRAME_BYTES_48K_STEREO]
            self._frames.append(frame)

    def flush(self) -> None:
        """Drop any buffered audio. Used on interruption events."""
        self._buf.clear()
        self._frames.clear()

    def close(self) -> None:
        self._closed = True
        self._buf.clear()
        self._frames.clear()

    # --- consumer side (discord voice thread) ---

    def read(self) -> bytes:
        if self._closed:
            return b""
        if self._frames:
            return self._frames.popleft()
        return SILENCE_FRAME

    def cleanup(self) -> None:
        self.close()
