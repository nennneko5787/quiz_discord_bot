import asyncio
import io
import random
import re
import traceback
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Union

import discord
import dotenv
import httpx
import jaconv
import numpy
from discord import app_commands
from discord.ext import commands, tasks
from openai import AsyncOpenAI
from pydantic import BaseModel

dotenv.load_dotenv()

openaiClient = AsyncOpenAI(
    api_key="PAICHA_TAIHO_OMEDETO",
    base_url="https://capi.voids.top/v2",
)

CHOICE_LABELS = list("ABCDEFGHIJKLMNOPQRST")  # 最大20択
CHOICE_STYLES = [
    discord.ButtonStyle.primary,
    discord.ButtonStyle.secondary,
    discord.ButtonStyle.success,
    discord.ButtonStyle.danger,
    discord.ButtonStyle.blurple,
]


# ---- モデル ----


class Question(BaseModel):
    genre: str
    question: str
    answer: bool
    explanation: str


class QuestionEx(BaseModel):
    genre: str
    question: str
    choices: List[str]  # 2〜20個
    answerIndex: int  # 0-indexed
    explanation: str


# ---- ◯✕クイズ View ----


class AnswerButtons(discord.ui.ActionRow):
    def __init__(self, view: "QuizView") -> None:
        self.__view = view

        self.answers: Dict[int, bool] = {}
        self.correctLog: List[Union[discord.Member, discord.User]] = []

        super().__init__()

    def getAnswerPercent(self):
        all = len(self.answers)
        correct = len(
            [a for a in self.answers.values() if a is self.__view.question.answer]
        )
        incorrect = all - correct
        return all, correct / all, incorrect / all

    def recordPress(self, user: Union[discord.Member, discord.User], answer: bool):
        if answer is self.__view.question.answer:
            if user not in self.correctLog:
                self.correctLog.append(user)
        else:
            if user in self.correctLog:
                self.correctLog.remove(user)

        self.answers[user.id] = answer

    @discord.ui.button(
        label="⭕", style=discord.ButtonStyle.success, custom_id="quiz_circle"
    )
    async def circleButton(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.recordPress(interaction.user, True)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"{button.label} に回答しました。", color=discord.Color.green()
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="❌", style=discord.ButtonStyle.danger, custom_id="quiz_cross"
    )
    async def crossButton(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        self.recordPress(interaction.user, False)
        await interaction.response.send_message(
            embed=discord.Embed(
                title=f"{button.label} に回答しました。", color=discord.Color.red()
            ),
            ephemeral=True,
        )


class QuizView(discord.ui.LayoutView):
    def __init__(self, question: Question):
        super().__init__(timeout=20)
        self.timeoutEvent = asyncio.Event()
        self.message: discord.Message | None = None
        self.question = question

        self.header = discord.ui.TextDisplay(
            f"### ジャンル「{question.genre}」からの問題！"
        )
        self.body = discord.ui.TextDisplay(question.question)
        self.limit = discord.ui.TextDisplay(
            f"回答期限{discord.utils.format_dt(datetime.now() + timedelta(seconds=30), 'R')}"
        )
        self.buttons = AnswerButtons(self)
        container = discord.ui.Container(
            self.header,
            self.body,
            self.limit,
            self.buttons,
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)


# ---- 選択肢クイズ View ----


class AnswerButtonsEx:
    """選択肢数に応じて ActionRow を動的生成する（1行5個まで、最大20択）"""

    def __init__(self, view: "QuizViewEx") -> None:
        self._view = view
        # user_id -> choiceIndex
        self.answers: Dict[int, int] = {}
        self.correctLog: List[Union[discord.Member, discord.User]] = []

        self.rows: List[discord.ui.ActionRow] = []
        self.buttons: List[discord.ui.Button] = []

        choices = view.question.choices
        for rowStart in range(0, len(choices), 5):
            row = discord.ui.ActionRow()
            for i in range(rowStart, min(rowStart + 5, len(choices))):
                btn = discord.ui.Button(
                    label=CHOICE_LABELS[i],
                    style=CHOICE_STYLES[i % len(CHOICE_STYLES)],
                    custom_id=f"quiz_ex_{i}",
                )
                btn.callback = self._makeCallback(i)
                row.add_item(btn)
                self.buttons.append(btn)
            self.rows.append(row)

    def _makeCallback(self, index: int):
        async def callback(interaction: discord.Interaction):
            self._recordPress(interaction.user, index)
            isCorrect = index == self._view.question.answerIndex
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=f"{CHOICE_LABELS[index]} に回答しました。",
                    color=discord.Color.green() if isCorrect else discord.Color.red(),
                ),
                ephemeral=True,
            )

        return callback

    def _recordPress(self, user: Union[discord.Member, discord.User], index: int):
        isCorrect = index == self._view.question.answerIndex
        if isCorrect:
            if user not in self.correctLog:
                self.correctLog.append(user)
        else:
            if user in self.correctLog:
                self.correctLog.remove(user)
        self.answers[user.id] = index

    def getAnswerStats(self):
        total = len(self.answers)
        counts = [0] * len(self._view.question.choices)
        for choiceIndex in self.answers.values():
            if 0 <= choiceIndex < len(counts):
                counts[choiceIndex] += 1
        percents = [c / total if total > 0 else 0.0 for c in counts]
        return total, counts, percents

    def setDisabled(self, disabled: bool):
        for btn in self.buttons:
            btn.disabled = disabled


class QuizViewEx(discord.ui.LayoutView):
    def __init__(self, question: QuestionEx):
        super().__init__(timeout=30)
        self.message: discord.Message | None = None
        self.question = question

        choicesText = "\n".join(
            f"**{CHOICE_LABELS[i]}.** {choice}"
            for i, choice in enumerate(question.choices)
        )

        self.header = discord.ui.TextDisplay(
            f"### ジャンル「{question.genre}」からの問題！"
        )
        self.body = discord.ui.TextDisplay(f"{question.question}\n\n{choicesText}")
        self.limit = discord.ui.TextDisplay(
            f"回答期限{discord.utils.format_dt(datetime.now() + timedelta(seconds=30), 'R')}"
        )
        self.buttonRows = AnswerButtonsEx(self)

        container = discord.ui.Container(
            self.header,
            self.body,
            self.limit,
            *self.buttonRows.rows,
            accent_color=discord.Color.blurple(),
        )
        self.add_item(container)


# ---- Cog ----


class QuizCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.http = httpx.AsyncClient()
        self.inGame: bool = False
        self.task: Optional[asyncio.Task] = None
        self.queue: asyncio.Queue[Callable[[], Awaitable[None]]] = asyncio.Queue()

    group = app_commands.Group(name="quiz", description="クイズ関連のコマンド。")

    @group.command(name="pokemon", description="ポケモンクイズの練習をします")
    async def pokemonCommand(self, interaction: discord.Interaction):
        await interaction.response.send_message("練習を始めます", ephemeral=True)
        await self.queue.put(lambda: self.pokemon())

    @group.command(name="quiz", description="クイズの練習をします")
    @app_commands.rename(genre="ジャンル", extras="追加情報", difficulty="難しさ")
    @app_commands.describe(
        genre="ジャンルを指定できます（省略可）",
        extras="絶対に見えないであろう追加情報",
        difficulty="難しさ(0~1000)",
    )
    async def quizCommand(
        self,
        interaction: discord.Interaction,
        genre: str = "",
        extras: str = "",
        difficulty: Optional[app_commands.Range[int, 0, 1000]] = None,
    ):
        await interaction.response.send_message("練習を始めます", ephemeral=True)
        await self.queue.put(
            lambda: self.quiz(
                genre=genre,
                extras=extras,
                difficulty=difficulty,
            )
        )

    @group.command(name="quizex", description="選択肢形式のクイズの練習をします")
    @app_commands.rename(genre="ジャンル", extras="追加情報", difficulty="難しさ")
    @app_commands.describe(
        genre="ジャンルを指定できます（省略可）",
        extras="追加情報",
        difficulty="難しさ(0~1000)",
    )
    async def quizExCommand(
        self,
        interaction: discord.Interaction,
        genre: str = "",
        extras: str = "",
        difficulty: Optional[app_commands.Range[int, 0, 1000]] = None,
    ):
        await interaction.response.send_message("練習を始めます", ephemeral=True)
        await self.queue.put(
            lambda: self.quizEx(
                genre=genre,
                extras=extras,
                difficulty=difficulty,
            )
        )

    @tasks.loop(minutes=30)
    async def quizLoop(self):
        if not self.bot.is_ready():
            return
        func = random.choice(
            [
                lambda: self.quiz(),
                lambda: self.pokemon(),
                lambda: self.quizEx(),
            ]
        )
        await self.queue.put(func)

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.quizLoop.is_running():
            self.quizLoop.start()

    async def cog_load(self):
        self.task = asyncio.create_task(self.quizQueue())

    async def cog_unload(self):
        self.quizLoop.stop()
        if self.task:
            self.task.cancel()

    async def quizQueue(self):
        while True:
            try:
                print("start")
                func = await self.queue.get()
                print("omg")
                await func()
                print("yeah")
            except Exception:
                traceback.print_exc()
            finally:
                await asyncio.sleep(0)
                print("end")

    async def pokemon(self):
        channel = self.bot.get_channel(1491704146544300094)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        self.inGame = True
        async with channel.typing():
            response = await self.http.get(
                "https://pokeapi.co/api/v2/pokemon-species/?limit=0"
            )
            count = response.json()["count"]
            id = numpy.random.randint(1, count)

            response = await self.http.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
            jsonData = response.json()
            imageUrl = jsonData["sprites"]["front_default"]

            response = await self.http.get(
                f"https://pokeapi.co/api/v2/pokemon-species/{id}/"
            )
            jsonData = response.json()
            name = next(
                entry["name"]
                for entry in jsonData["names"]
                if entry["language"]["name"] == "ja-hrkt"
            )

            response = await self.http.get(imageUrl)
            questionMessage = await channel.send(
                embed=discord.Embed(
                    title="問題",
                    description="このポケモンは何？\n**20秒以内に回答してください**",
                    color=discord.Color.blurple(),
                ).set_image(url="attachment://image.png"),
                file=discord.File(io.BytesIO(response.content), "image.png"),
            )

        def check(message: discord.Message):
            if message.channel.id == channel.id:
                if name in jaconv.hira2kata(message.content):
                    return True
            return False

        try:
            message: discord.Message = await self.bot.wait_for(
                "message", timeout=20, check=check
            )
            await message.reply(
                embed=discord.Embed(
                    title="正解",
                    description=f"正解は**{name}**！",
                    color=discord.Color.blurple(),
                ).add_field(name="正解者", value=message.author.mention)
            )
        except asyncio.TimeoutError:
            await questionMessage.reply(
                embed=discord.Embed(
                    title="正解",
                    description=f"正解は**{name}**！",
                    color=discord.Color.red(),
                ).add_field(name="正解者", value="なし")
            )
        finally:
            self.inGame = False

    async def quiz(
        self,
        *,
        genre: str = "",
        extras: str = "",
        difficulty: Optional[int] = None,
    ):
        channel = self.bot.get_channel(1491704146544300094)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        if difficulty is None:
            difficulty = random.randint(1, 1000)
        self.inGame = True

        async with channel.typing():
            prompt = (
                "適当に◯✕クイズ1問だけ出してください。"
                f"難しさ指数: {difficulty} / 500 で問題を作ってください。"
                f"ジャンル指定: {genre if genre != '' else 'なし'} (ジャンル指定は無視しないでください。)"
                f"追加情報: {extras}"
                "色んなジャンルから問題を出してください。"
                "日常で使うクイズの他に「ボカロ」「ネットカルチャー」「ツイ廃」「アニメ」「日本史」「世界史」「性癖」「VTuber」など様々なジャンルで出題してください。(ぜひこれ以外のジャンルを出してほしい)"
                '{"genre":"ジャンル","question":"問題文","answer":true/false,"explanation":"解説"}'
                "json以外のデータを出力しないでください。(メッセージも)"
            )

            response = await openaiClient.responses.create(
                model="gemini-3-pro-preview",
                instructions="あなたはクイズ出題AIです。JSONのみ返してください。",
                input=prompt,
            )

            rawText = (response.output_text or "").strip()
            rawText = re.sub(r"^```json\s*", "", rawText, flags=re.I)
            rawText = re.sub(r"```$", "", rawText).strip()
            rawText = re.sub(r"<thought>.*?</thought>", "", rawText, flags=re.S).strip()

            match = re.search(r"\{.*\}", rawText, re.S)
            if not match:
                raise ValueError("JSON not found")

            question = Question.model_validate_json(match.group(0))

        view = QuizView(question=question)
        quizMessage = await channel.send(view=view)
        view.message = quizMessage

        await asyncio.sleep(30)

        for item in view.buttons.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await quizMessage.edit(view=view)
        except discord.NotFound:
            pass

        winner = (
            view.buttons.correctLog[0] if len(view.buttons.correctLog) > 0 else None
        )
        answerCount, correct, incorrect = view.buttons.getAnswerPercent()

        await channel.send(
            embeds=[
                discord.Embed(
                    title="正解発表",
                    description=f"正解は {':o:' if question.answer else ':x:'}\n\n",
                    color=discord.Color.blurple(),
                )
                .add_field(
                    name="最速正解者",
                    value=f"{winner.mention if winner else '正解者なし'}",
                )
                .add_field(
                    name="正解率",
                    value=f"回答者{answerCount}人のうち 正解 {correct * 100}% 不正解 {incorrect * 100}%",
                )
                .set_footer(text=f"難しさ: {difficulty}"),
                discord.Embed(
                    title="解説",
                    description=question.explanation,
                    color=discord.Color.blurple(),
                ),
            ]
        )
        self.inGame = False

    async def quizEx(
        self,
        *,
        genre: str = "",
        extras: str = "",
        difficulty: Optional[int] = None,
    ):
        channel = self.bot.get_channel(1491704146544300094)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        if difficulty is None:
            difficulty = random.randint(1, 1000)
        self.inGame = True

        async with channel.typing():
            prompt = (
                "選択肢形式のクイズを1問だけ出してください。"
                f"難しさ指数: {difficulty} / 1000 で問題を作ってください。"
                f"ジャンル指定: {genre if genre != '' else 'なし'} (ジャンル指定は無視しないでください。)"
                f"追加情報: {extras}"
                "色んなジャンルから問題を出してください。"
                "日常で使うクイズの他に「ボカロ」「ネットカルチャー」「ツイ廃」「アニメ」「日本史」「世界史」「性癖」「VTuber」など様々なジャンルで出題してください。(ぜひこれ以外のジャンルを出してほしい)"
                "選択肢の数は問題の性質に合わせて2〜20個の間で自由に決めてください。"
                '{"genre":"ジャンル","question":"問題文","choices":["選択肢A","選択肢B",...],"answerIndex":0,"explanation":"解説"}'
                "answerIndexは0始まりのインデックスです。"
                "json以外のデータを出力しないでください。(メッセージも)"
            )

            response = await openaiClient.responses.create(
                model="gemini-3-pro-preview",
                instructions="あなたはクイズ出題AIです。JSONのみ返してください。",
                input=prompt,
            )

            rawText = (response.output_text or "").strip()
            rawText = re.sub(r"^```json\s*", "", rawText, flags=re.I)
            rawText = re.sub(r"```$", "", rawText).strip()
            rawText = re.sub(r"<thought>.*?</thought>", "", rawText, flags=re.S).strip()

            match = re.search(r"\{.*\}", rawText, re.S)
            if not match:
                raise ValueError("JSON not found")

            question = QuestionEx.model_validate_json(match.group(0))
            # 念のため上限チェック
            question.choices = question.choices[:20]
            if not (0 <= question.answerIndex < len(question.choices)):
                question.answerIndex = 0

        view = QuizViewEx(question=question)
        quizMessage = await channel.send(view=view)
        view.message = quizMessage

        await asyncio.sleep(30)

        view.buttonRows.setDisabled(True)
        try:
            await quizMessage.edit(view=view)
        except discord.NotFound:
            pass

        winner = (
            view.buttonRows.correctLog[0]
            if len(view.buttonRows.correctLog) > 0
            else None
        )
        total, counts, percents = view.buttonRows.getAnswerStats()
        correctLabel = CHOICE_LABELS[question.answerIndex]
        correctChoice = question.choices[question.answerIndex]

        statsLines = "\n".join(
            f"**{CHOICE_LABELS[i]}.** {choice}　{round(p * 100)}%（{c}人）"
            + (" ✅" if i == question.answerIndex else "")
            for i, (choice, c, p) in enumerate(zip(question.choices, counts, percents))
        )

        await channel.send(
            embeds=[
                discord.Embed(
                    title="正解発表",
                    description=f"正解は **{correctLabel}. {correctChoice}**",
                    color=discord.Color.blurple(),
                )
                .add_field(
                    name="最速正解者",
                    value=winner.mention if winner else "正解者なし",
                )
                .add_field(name="回答者数", value=f"{total}人", inline=True)
                .add_field(name="回答内訳", value=statsLines, inline=False)
                .set_footer(text=f"難しさ: {difficulty}"),
                discord.Embed(
                    title="解説",
                    description=question.explanation,
                    color=discord.Color.blurple(),
                ),
            ]
        )
        self.inGame = False


async def setup(bot: commands.Bot):
    await bot.add_cog(QuizCog(bot))
