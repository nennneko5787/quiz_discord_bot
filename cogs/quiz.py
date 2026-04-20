import asyncio
import io
import json
import os
import random
import re
import time
from typing import Dict, List

import discord
import dotenv
import httpx
import jaconv
import numpy
import openai
from discord.ext import commands, tasks

dotenv.load_dotenv()


class QuizView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=20)
        self.reactionLogs: Dict[int, List[str]] = {}
        self.firstTimes: Dict[int, Dict[str, float]] = {}
        self.startTime = time.monotonic()
        self.message: discord.Message | None = None

    def recordPress(self, userId: int, emoji: str):
        if userId not in self.reactionLogs:
            self.reactionLogs[userId] = []
        if emoji not in self.reactionLogs[userId]:
            self.reactionLogs[userId].append(emoji)
            if userId not in self.firstTimes:
                self.firstTimes[userId] = {}
            if emoji not in self.firstTimes[userId]:
                self.firstTimes[userId][emoji] = time.monotonic()

    @discord.ui.button(label="⭕", style=discord.ButtonStyle.success, custom_id="quiz_circle")
    async def circleButton(self, interaction: discord.Interaction, button: discord.ui.Button):
        assert interaction.user is not None
        self.recordPress(interaction.user.id, "⭕")
        await interaction.response.defer()

    @discord.ui.button(label="❌", style=discord.ButtonStyle.danger, custom_id="quiz_cross")
    async def crossButton(self, interaction: discord.Interaction, button: discord.ui.Button):
        assert interaction.user is not None
        self.recordPress(interaction.user.id, "❌")
        await interaction.response.defer()

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


class QuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http = httpx.AsyncClient()
        self.inGame: bool = False

        self.openai = openai.AsyncOpenAI(
            api_key=os.getenv("openai_api_key"),
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )

    @commands.command("ポケモン練習")
    @commands.cooldown(1, 20, commands.BucketType.guild)
    async def pokemonCommand(self, ctx: commands.Context):
        if self.inGame:
            return
        await ctx.reply("練習を始めます")
        await self.pokemon(practice=True)

    @discord.app_commands.command(name="クイズ練習", description="クイズの練習をします")
    @discord.app_commands.describe(genre="ジャンルを指定できます（省略可）")
    @discord.app_commands.checks.cooldown(1, 20, key=lambda i: i.guild_id)
    async def quizCommand(self, interaction: discord.Interaction, genre: str = ""):
        if self.inGame:
            await interaction.response.send_message("現在ゲーム中です", ephemeral=True)
            return
        await interaction.response.send_message("練習を始めます")
        await self.quiz(practice=True, genre=genre)

    @tasks.loop(minutes=30)
    async def quizLoop(self):
        await random.choice([self.quiz, self.pokemon])()

    @commands.Cog.listener()
    async def on_ready(self):
        self.quizLoop.start()

    async def pokemon(self, *, practice: bool = False):
        channel = self.bot.get_channel(1491704146544300094)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        self.inGame = True
        async with channel.typing():
            response = await self.http.get("https://pokeapi.co/api/v2/pokemon-species/?limit=0")
            count = response.json()["count"]
            id = numpy.random.randint(1, count)

            response = await self.http.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
            jsonData = response.json()
            imageUrl = jsonData["sprites"]["front_default"]

            response = await self.http.get(f"https://pokeapi.co/api/v2/pokemon-species/{id}/")
            jsonData = response.json()
            name = next(
                entry["name"]
                for entry in jsonData["names"]
                if entry["language"]["name"] == "ja-hrkt"
            )

            response = await self.http.get(imageUrl)
            questionMessage = await channel.send(
                "**問題！**\n\nこのポケモンは何？\n**20秒以内に回答してください**",
                file=discord.File(io.BytesIO(response.content), f"{id}.png"),
            )

        def check(message: discord.Message):
            if message.channel.id == channel.id:
                if message.content == name or jaconv.hira2kata(message.content) == name:
                    return True
            return False

        try:
            message: discord.Message = await self.bot.wait_for("message", timeout=20, check=check)
            await questionMessage.reply(f"正解は**{name}**！\n正解者: {message.author.mention}")
        except asyncio.TimeoutError:
            await questionMessage.reply(f"正解は**{name}**！\n正解者: **なし！**")
        finally:
            self.inGame = False

    async def quiz(self, *, practice: bool = False, genre: str = ""):
        channel = self.bot.get_channel(1491704146544300094)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        difficulty = random.randint(1, 1000)
        self.inGame = True

        async with channel.typing():
            response = await self.openai.chat.completions.create(
                model="gemma-4-31b-it",
                messages=[
                    {
                        "role": "system",
                        "content": "あなたはクイズ出題AIです。JSONのみ返してください。",
                    },
                    {
                        "role": "user",
                        "content": (
                            "適当に◯✕クイズ1問だけ出してください。"
                            f"難しさ指数: {difficulty} / 500 で問題を作ってください。"
                            f"ジャンル指定: {genre if genre != '' else 'なし'} (ジャンル指定は無視しないでください。)"
                            "色んなジャンルから問題を出してください。"
                            "日常で使うクイズの他に「ボカロ」「ネットカルチャー」「ツイ廃」「アニメ」「日本史」「世界史」「性癖」「VTuber」など様々なジャンルで出題してください。(ぜひこれ以外のジャンルを出してほしい)"
                            '{"genre":"ジャンル","question":"問題文","answer":true/false,"explanation":"解説"}'
                            "json以外のデータを出力しないでください。(メッセージも)"
                        ),
                    },
                ],
            )

            rawText = (response.choices[0].message.content or "").strip()
            rawText = re.sub(r"^```json\s*", "", rawText, flags=re.I)
            rawText = re.sub(r"```$", "", rawText).strip()
            rawText = re.sub(r"<thought>.*?</thought>", "", rawText, flags=re.S).strip()

            match = re.search(r"\{.*\}", rawText, re.S)
            if not match:
                raise ValueError("JSON not found")

            data = json.loads(match.group(0))

        view = QuizView()
        quizMessage = await channel.send(
            f"**ジャンル『{data['genre']}』からの問題！**\n\n{data['question']}\n\n"
            f"⭕ = ◯　❌ = ✕\n"
            f"制限時間: 20秒\n",
            view=view,
        )
        # on_timeout内でeditできるようmessageを渡す
        view.message = quizMessage

        # Viewのon_timeoutに任せる（asyncio.sleepと競合しないよう少し長めに待つ）
        await asyncio.sleep(20)

        guild = channel.guild
        bothUsers = []
        validUsers = []
        circleUsers = []
        crossUsers = []

        correctEmoji = "⭕" if data["answer"] else "❌"

        for userId, emojis in view.reactionLogs.items():
            member = guild.get_member(userId)
            if (
                member is None
                or member.bot
                or (guild.me is not None and member.id == guild.me.id)
            ):
                continue

            if "⭕" in emojis:
                circleUsers.append(member)
            if "❌" in emojis:
                crossUsers.append(member)

            if "⭕" in emojis and "❌" in emojis:
                bothUsers.append(member)
                continue

            if correctEmoji in emojis:
                validUsers.append(member)

        validUsers.sort(key=lambda x: view.firstTimes[x.id][correctEmoji])

        winner = validUsers[0] if validUsers else None

        totalVotes = len(circleUsers) + len(crossUsers) - len(bothUsers)  # 両押しは1票として計算しない
        circleCount = len(circleUsers) - len(bothUsers)
        crossCount = len(crossUsers) - len(bothUsers)

        def pct(n: int) -> str:
            if totalVotes == 0:
                return "0.0"
            return f"{n / totalVotes * 100:.1f}"

        messageText = (
            f"**時間終了！**\n"
            f"正解は {':o:' if data['answer'] else ':x:'}\n\n"
            f"⭕ {circleCount}票 ({pct(circleCount)}%)　❌ {crossCount}票 ({pct(crossCount)}%)\n\n"
            f"**最速正解者**\n"
            f"{winner.mention if winner else '正解者なし'}\n\n"
            f"-# 難しさ: {difficulty}"
        )

        if bothUsers:
            exposeText = "\n".join(user.mention for user in bothUsers)
            messageText += f"\n\n**両方押し不正者晒し**\n{exposeText}"

        messageText += f"\n\n**解説**\n{data['explanation']}"

        await channel.send(messageText)
        self.inGame = False


async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))
