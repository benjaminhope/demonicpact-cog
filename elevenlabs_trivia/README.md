# ElevenLabsTrivia

A Red-DiscordBot cog that joins a voice channel and lets an ElevenLabs
Conversational AI agent host a live trivia game. Players answer out loud;
the agent asks questions, judges answers, and tracks the round.

## How it works

```
Discord VC users  ──opus──►  voice-recv decoder  ──48k stereo PCM──►
   ──downmix + 16k mono──►  ElevenLabs ConvAI WebSocket  ─►  agent
                                                          ◄─  agent audio
   ◄──48k stereo PCM──  upsample + duplicate to stereo  ◄──
   ◄──opus──  Discord voice client
```

The cog opens its own `VoiceRecvClient` per guild, runs an aiohttp WebSocket
to ElevenLabs (`/v1/convai/conversation`), and resamples PCM in both
directions using the stdlib `audioop` module. Multiple speakers in the
voice channel get mixed together before being sent up to the agent.

## One-time setup

### 1. Install the optional dependency

`discord-ext-voice-recv` is the missing piece — discord.py does not ship
voice receive support. Install it into Red's venv:

```bash
"C:/redbot/red-env/Scripts/pip.exe" install discord-ext-voice-recv
```

(Or, from inside Red as bot owner: `[p]pipinstall discord-ext-voice-recv`.)

### 2. Create the ElevenLabs agent

In the ElevenLabs dashboard:

1. **Conversational AI → Agents → Create Agent.**
2. Pick a voice you like (a warm, energetic host voice works well).
3. Leave the system prompt fairly generic — the cog overrides it per
   topic at the start of each conversation.
4. **Security tab:** enable
   - *Allow client to override agent prompt*
   - *Allow client to override first message*
   - *Allow client to set dynamic variables*
   Without these, topic switching won't take effect.
5. Note the **agent ID** (looks like `agent_01abc...`).
6. Generate an **API key** under your account profile.

### 3. Configure the cog

```text
[p]load elevenlabs_trivia
[p]eltrivia setapikey YOUR_ELEVENLABS_KEY     # bot owner only, global
[p]eltrivia setagent  AGENT_ID                # per-guild
[p]eltrivia settopic  osrs                    # optional default topic
```

The `setapikey` command deletes your message after running so the key
doesn't sit in chat history. Run it in DM if your bot can't delete messages.

## Playing

1. Join a voice channel.
2. `[p]eltrivia start` — uses the server's default topic.
3. `[p]eltrivia start videogames` — picks a different topic for this round.
4. `[p]eltrivia stop` to end. The bot also leaves cleanly if it gets
   disconnected from the channel.

Available topics: `osrs`, `general`, `videogames`, `movies`, `history`,
`science`. See `[p]eltrivia topics` or edit `topics.py` to add more.

## Caveats

- **One voice connection per guild.** If Red's built-in `Audio` cog (or
  any other voice cog) is currently connected in the same server, this
  cog cannot connect — Discord allows only one bot voice client per guild.
  Disconnect the other one first.
- **Lavalink not used.** This cog deliberately bypasses Red-Lavalink and
  uses native discord.py voice. It does not interact with the playlist
  or now-playing state of the Audio cog.
- **Latency.** End-to-end you'll typically see ~700 ms–1.2 s between a
  player finishing a sentence and the agent responding. That's
  ElevenLabs round-trip plus Discord's jitter buffer; it's not something
  the cog can shave down.
- **Multiple speakers.** PCM from all non-bot speakers is mixed before
  being sent upstream. ElevenLabs' built-in turn-taking handles VAD on
  the mixed stream. Two people talking at once is going to confuse the
  agent the same way it would confuse a human host.
- **Costs.** Conversational AI minutes and TTS characters are billed by
  ElevenLabs. Don't leave a session idle.
