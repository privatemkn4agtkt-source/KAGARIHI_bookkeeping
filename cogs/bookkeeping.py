import discord
from discord import app_commands
from discord.ext import commands
from datetime import date
import database as db


def fmt_amount(n: int) -> str:
    return f"{n:,}円"


class Bookkeeping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -------------------------------------------------------------------------
    # /仕訳  借方 貸方 金額 摘要 [日付]
    # -------------------------------------------------------------------------
    @app_commands.command(name="仕訳", description="仕訳を記録します（複式簿記）")
    @app_commands.describe(
        借方="借方勘定科目",
        貸方="貸方勘定科目",
        金額="金額（円、整数）",
        摘要="取引の説明",
        日付="取引日 YYYY-MM-DD（省略時は今日）",
    )
    async def add_entry(
        self,
        interaction: discord.Interaction,
        借方: str,
        貸方: str,
        金額: int,
        摘要: str,
        日付: str | None = None,
    ):
        if 金額 <= 0:
            await interaction.response.send_message("金額は1以上の整数を指定してください。", ephemeral=True)
            return

        entry_date = 日付 or str(date.today())

        # 日付フォーマット検証
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            await interaction.response.send_message(
                "日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True
            )
            return

        # 勘定科目の存在確認
        if not await db.account_exists(借方):
            await interaction.response.send_message(
                f"勘定科目「{借方}」は登録されていません。`/勘定科目追加` で追加してください。",
                ephemeral=True,
            )
            return
        if not await db.account_exists(貸方):
            await interaction.response.send_message(
                f"勘定科目「{貸方}」は登録されていません。`/勘定科目追加` で追加してください。",
                ephemeral=True,
            )
            return

        entry_id = await db.add_journal_entry(entry_date, 借方, 貸方, 金額, 摘要)

        embed = discord.Embed(title="✅ 仕訳を記録しました", color=discord.Color.green())
        embed.add_field(name="ID", value=str(entry_id), inline=True)
        embed.add_field(name="日付", value=entry_date, inline=True)
        embed.add_field(name="金額", value=fmt_amount(金額), inline=True)
        embed.add_field(name="借方", value=借方, inline=True)
        embed.add_field(name="貸方", value=貸方, inline=True)
        embed.add_field(name="摘要", value=摘要, inline=False)
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /仕訳帳  [件数]
    # -------------------------------------------------------------------------
    @app_commands.command(name="仕訳帳", description="最近の仕訳を一覧表示します")
    @app_commands.describe(件数="表示件数（デフォルト: 10、最大: 50）")
    async def journal(self, interaction: discord.Interaction, 件数: int = 10):
        件数 = min(max(件数, 1), 50)
        entries = await db.get_journal_entries(件数)

        if not entries:
            await interaction.response.send_message("仕訳がまだありません。", ephemeral=True)
            return

        lines = []
        for e in entries:
            lines.append(
                f"`#{e['id']:04d}` {e['entry_date']}　"
                f"**{e['debit_account']}** / **{e['credit_account']}**　"
                f"{fmt_amount(e['amount'])}　{e['description']}"
            )

        embed = discord.Embed(
            title=f"📒 仕訳帳（直近 {len(entries)} 件）",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /試算表
    # -------------------------------------------------------------------------
    @app_commands.command(name="試算表", description="残高試算表を表示します")
    async def trial_balance(self, interaction: discord.Interaction):
        rows = await db.get_trial_balance()

        if not rows:
            await interaction.response.send_message("仕訳がまだありません。", ephemeral=True)
            return

        type_order = ["資産", "負債", "資本", "収益", "費用"]
        sections: dict[str, list] = {t: [] for t in type_order}
        for r in rows:
            t = r["account_type"]
            if t in sections:
                sections[t].append(r)

        embed = discord.Embed(title="📊 残高試算表", color=discord.Color.gold())

        total_debit = 0
        total_credit = 0

        for acc_type in type_order:
            items = sections[acc_type]
            if not items:
                continue
            lines = []
            for r in items:
                lines.append(
                    f"　{r['name']}: {fmt_amount(r['balance'])}"
                    f"（借方 {fmt_amount(r['debit_total'])} / 貸方 {fmt_amount(r['credit_total'])}）"
                )
                total_debit += r["debit_total"]
                total_credit += r["credit_total"]
            embed.add_field(name=f"【{acc_type}】", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"借方合計: {fmt_amount(total_debit)}　貸方合計: {fmt_amount(total_credit)}")
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /損益計算書
    # -------------------------------------------------------------------------
    @app_commands.command(name="損益計算書", description="損益計算書（PL）を表示します")
    async def income_statement(self, interaction: discord.Interaction):
        rows = await db.get_trial_balance()

        revenues = [r for r in rows if r["account_type"] == "収益"]
        expenses = [r for r in rows if r["account_type"] == "費用"]

        total_rev = sum(r["balance"] for r in revenues)
        total_exp = sum(r["balance"] for r in expenses)
        net = total_rev - total_exp

        embed = discord.Embed(title="📈 損益計算書", color=discord.Color.green() if net >= 0 else discord.Color.red())

        rev_lines = [f"　{r['name']}: {fmt_amount(r['balance'])}" for r in revenues] or ["　（なし）"]
        exp_lines = [f"　{r['name']}: {fmt_amount(r['balance'])}" for r in expenses] or ["　（なし）"]

        embed.add_field(name="【収益】", value="\n".join(rev_lines), inline=False)
        embed.add_field(name="収益合計", value=fmt_amount(total_rev), inline=True)
        embed.add_field(name="【費用】", value="\n".join(exp_lines), inline=False)
        embed.add_field(name="費用合計", value=fmt_amount(total_exp), inline=True)
        embed.add_field(
            name="当期純利益" if net >= 0 else "当期純損失",
            value=fmt_amount(abs(net)),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /貸借対照表
    # -------------------------------------------------------------------------
    @app_commands.command(name="貸借対照表", description="貸借対照表（BS）を表示します")
    async def balance_sheet(self, interaction: discord.Interaction):
        rows = await db.get_trial_balance()

        assets = [r for r in rows if r["account_type"] == "資産"]
        liabilities = [r for r in rows if r["account_type"] == "負債"]
        equities = [r for r in rows if r["account_type"] == "資本"]
        # 当期純利益を資本に含める
        revenues = [r for r in rows if r["account_type"] == "収益"]
        expenses = [r for r in rows if r["account_type"] == "費用"]
        net_income = sum(r["balance"] for r in revenues) - sum(r["balance"] for r in expenses)

        total_assets = sum(r["balance"] for r in assets)
        total_liab = sum(r["balance"] for r in liabilities)
        total_equity = sum(r["balance"] for r in equities) + net_income

        embed = discord.Embed(title="📋 貸借対照表", color=discord.Color.blurple())

        def fmt_lines(items):
            return "\n".join(f"　{r['name']}: {fmt_amount(r['balance'])}" for r in items) or "　（なし）"

        embed.add_field(name="【資産】", value=fmt_lines(assets), inline=True)
        embed.add_field(
            name="【負債・資本】",
            value=fmt_lines(liabilities) + f"\n　当期純利益: {fmt_amount(net_income)}\n" + fmt_lines(equities),
            inline=True,
        )
        embed.add_field(name="資産合計", value=fmt_amount(total_assets), inline=True)
        embed.add_field(name="負債・資本合計", value=fmt_amount(total_liab + total_equity), inline=True)
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /勘定科目一覧
    # -------------------------------------------------------------------------
    @app_commands.command(name="勘定科目一覧", description="登録されている勘定科目を一覧表示します")
    async def list_accounts(self, interaction: discord.Interaction):
        accounts = await db.get_accounts()

        type_order = ["資産", "負債", "資本", "収益", "費用"]
        sections: dict[str, list[str]] = {t: [] for t in type_order}
        for a in accounts:
            t = a["account_type"]
            if t in sections:
                sections[t].append(a["name"])

        embed = discord.Embed(title="📂 勘定科目一覧", color=discord.Color.teal())
        for t in type_order:
            names = sections[t]
            if names:
                embed.add_field(name=f"【{t}】", value="、".join(names), inline=False)
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /勘定科目追加
    # -------------------------------------------------------------------------
    @app_commands.command(name="勘定科目追加", description="新しい勘定科目を追加します")
    @app_commands.describe(
        名前="勘定科目名",
        種別="勘定科目の種別",
    )
    @app_commands.choices(
        種別=[
            app_commands.Choice(name="資産", value="資産"),
            app_commands.Choice(name="負債", value="負債"),
            app_commands.Choice(name="資本", value="資本"),
            app_commands.Choice(name="収益", value="収益"),
            app_commands.Choice(name="費用", value="費用"),
        ]
    )
    async def add_account(
        self,
        interaction: discord.Interaction,
        名前: str,
        種別: str,
    ):
        success = await db.add_account(名前, 種別)
        if success:
            await interaction.response.send_message(
                f"✅ 勘定科目「{名前}」（{種別}）を追加しました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ 「{名前}」はすでに登録されています。", ephemeral=True
            )

    # -------------------------------------------------------------------------
    # /仕訳削除
    # -------------------------------------------------------------------------
    @app_commands.command(name="仕訳削除", description="指定したIDの仕訳を削除します")
    @app_commands.describe(仕訳ID="削除する仕訳のID（/仕訳帳 で確認できます）")
    async def delete_entry(self, interaction: discord.Interaction, 仕訳ID: int):
        deleted = await db.delete_journal_entry(仕訳ID)
        if deleted:
            await interaction.response.send_message(
                f"✅ 仕訳 #{仕訳ID:04d} を削除しました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ 仕訳 #{仕訳ID:04d} が見つかりません。", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Bookkeeping(bot))
