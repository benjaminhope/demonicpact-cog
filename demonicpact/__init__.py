from .demonicpact import DemonicPact


async def setup(bot):
    await bot.add_cog(DemonicPact(bot))
