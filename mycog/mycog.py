from redbot.core import commands


class MyCog(commands.Cog):
    """My first cog."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def hello(self, ctx):
        """Say hi."""
        await ctx.send("Hello from MyCog!")
