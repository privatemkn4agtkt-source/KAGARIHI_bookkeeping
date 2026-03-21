import asyncio
import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import database as db

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")


class BookkeepingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await db.init_db()
        await self.load_extension("cogs.bookkeeping")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"スラッシュコマンドをギルド {GUILD_ID} に同期しました")
        else:
            await self.tree.sync()
            print("スラッシュコマンドをグローバルに同期しました（反映まで最大1時間かかります）")

    async def on_ready(self):
        print(f"✅ {self.user} としてログインしました")
        print("簿記Botが起動しました。")


def main():
    if not TOKEN:
        print("エラー: DISCORD_TOKEN が設定されていません。.env ファイルを確認してください。")
        return

    bot = BookkeepingBot()
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
