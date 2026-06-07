from .elevenlabs_voice import ElevenLabsVoice


async def setup(bot):
    await bot.add_cog(ElevenLabsVoice(bot))
