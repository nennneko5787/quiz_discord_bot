import os

import discord
import dotenv
from discord.ext import commands

dotenv.load_dotenv()


class WebhookCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.webhook = discord.Webhook.from_url(
            os.getenv("webhook") or "", client=self.bot
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id != 1491704146544300094:
            return

        if message.flags.ephemeral:
            return

        await self.webhook.send(
            message.content,
            embeds=message.embeds,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(WebhookCog(bot))
