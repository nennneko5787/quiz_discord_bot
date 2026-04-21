import os

import discord
import dotenv
from discord.ext import commands

dotenv.load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot("quiz#", help_command=None, intents=intents)


@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    await bot.tree.sync()
    await ctx.reply("OK")


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.quiz")
    await bot.load_extension("cogs.webhook")


if __name__ == "__main__":
    bot.run(os.getenv("discord") or "")
