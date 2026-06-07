from .elevenlabs_trivia import ElevenLabsTrivia


async def setup(bot):
    await bot.add_cog(ElevenLabsTrivia(bot))
