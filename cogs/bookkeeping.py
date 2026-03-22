import csv
import io
import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import date
import database as db

CRIT_PERCENT = 90   # 月初自動警告の閾値 (%)


def fmt_amount(n: int) -> str:
    return f"{n:,}円"


def _truncate(text: str, limit: int = 1000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…（省略）"


def _storage_embed(info: dict) -> discord.Embed:
    pct = info["disk_percent"]
    if pct >= CRIT_PERCENT:
        color = discord.Color.red()
        title = "🚨 ストレージ警告: 残り僅か"
    elif pct >= 70:
        color = discord.Color.orange()
        title = "⚠️ ストレージ警告: 残量少"
    else:
        color = discord.Color.green()
        title = "💾 ストレージ状況"

    def fmt_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="ディスク使用率", value=f"{pct:.1f}%", inline=True)
    embed.add_field(name="空き容量", value=fmt_bytes(info["disk_free"]), inline=True)
    embed.add_field(name="合計容量", value=fmt_bytes(info["disk_total"]), inline=True)
    embed.add_field(name="DBファイルサイズ", value=fmt_bytes(info["db_size"]), inline=True)
    embed.add_field(name="仕訳件数", value=f"{info['entry_count']:,} 件", inline=True)
    return embed


# =============================================================================
# 仕訳記録後のキャンセルボタン（60秒以内に取り消し可能）
# =============================================================================

class CancelEntryView(discord.ui.View):
    def __init__(self, entry_id: int, author_id: int):
        super().__init__(timeout=60)
        self.entry_id = entry_id
        self.author_id = author_id

    @discord.ui.button(label="🗑️ 取り消す", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("取り消しは記録した本人のみ行えます。", ephemeral=True)
            return
        success = await db.delete_journal_entry(self.entry_id)
        for item in self.children:
            item.disabled = True
        if success:
            await interaction.response.edit_message(
                content=f"🗑️ 仕訳 **#{self.entry_id:04d}** を取り消しました。",
                embed=None,
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"❌ 仕訳 #{self.entry_id:04d} の取り消しに失敗しました（すでに削除済みかもしれません）。",
                embed=None,
                view=None,
            )
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# =============================================================================
# 仕訳削除確認ビュー
# =============================================================================

class ConfirmDeleteEntryView(discord.ui.View):
    def __init__(self, entry: dict):
        super().__init__(timeout=30)
        self.entry = entry

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await db.delete_journal_entry(self.entry["id"])
        if success:
            await interaction.response.edit_message(
                content=f"✅ 仕訳 **#{self.entry['id']:04d}** を削除しました。", view=None
            )
        else:
            await interaction.response.edit_message(content="❌ 削除に失敗しました。", view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# =============================================================================
# イベント削除確認ビュー
# =============================================================================

class ConfirmDeleteEventView(discord.ui.View):
    def __init__(self, event_name: str):
        super().__init__(timeout=30)
        self.event_name = event_name

    @discord.ui.button(label="削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        success = await db.delete_event(self.event_name)
        if success:
            await interaction.response.edit_message(
                content=f"✅ イベント「{self.event_name}」を削除しました。関連仕訳のイベントタグもクリアされました。",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content=f"❌ 削除に失敗しました。", view=None
            )
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# =============================================================================
# Cog
# =============================================================================

class Bookkeeping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._alerted_month: tuple[int, int] | None = None

    def cog_load(self):
        self.monthly_storage_check.start()

    def cog_unload(self):
        self.monthly_storage_check.cancel()

    # -------------------------------------------------------------------------
    # 月初ストレージ監視（90%超で全ギルドに通知）
    # -------------------------------------------------------------------------
    @tasks.loop(hours=24)
    async def monthly_storage_check(self):
        today = date.today()
        if today.day != 1:
            return
        ym = (today.year, today.month)
        if self._alerted_month == ym:
            return
        info = await db.get_storage_info()
        if info["disk_percent"] < CRIT_PERCENT:
            return
        embed = _storage_embed(info)
        for guild in self.bot.guilds:
            channel = guild.system_channel or next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
            if channel:
                await channel.send(embed=embed)
        self._alerted_month = ym

    @monthly_storage_check.before_loop
    async def before_monthly_storage_check(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------------------
    # オートコンプリートヘルパー
    # -------------------------------------------------------------------------
    async def _account_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        accounts = await db.get_accounts()
        return [
            app_commands.Choice(name=a["name"], value=a["name"])
            for a in accounts if current.lower() in a["name"].lower()
        ][:25]

    async def _event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        events = await db.get_events()
        return [
            app_commands.Choice(name=e, value=e)
            for e in events if current.lower() in e.lower()
        ][:25]

    async def _member_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        members = await db.get_members()
        return [
            app_commands.Choice(name=m, value=m)
            for m in members if current.lower() in m.lower()
        ][:25]

    # =========================================================================
    # 仕訳
    # =========================================================================

    @app_commands.command(name="仕訳", description="仕訳を記録します（複式簿記）")
    @app_commands.describe(
        借方="借方勘定科目（候補から選ぶか直接入力）",
        貸方="貸方勘定科目（候補から選ぶか直接入力）",
        金額="金額（円、整数）",
        摘要="取引の説明",
        日付="取引日 YYYY-MM-DD（省略時は今日）",
        イベント="紐づけるイベント名（/イベント作成 で事前登録が必要）",
    )
    @app_commands.autocomplete(借方=_account_autocomplete, 貸方=_account_autocomplete, イベント=_event_autocomplete)
    async def add_entry(
        self,
        interaction: discord.Interaction,
        借方: str,
        貸方: str,
        金額: int,
        摘要: str,
        日付: str | None = None,
        イベント: str | None = None,
    ):
        if 金額 <= 0:
            await interaction.response.send_message("金額は1以上の整数を指定してください。", ephemeral=True)
            return

        entry_date = 日付 or str(date.today())
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return

        if not await db.account_exists(借方):
            await interaction.response.send_message(
                f"勘定科目「{借方}」は登録されていません。`/勘定科目追加` で追加してください。", ephemeral=True
            )
            return
        if not await db.account_exists(貸方):
            await interaction.response.send_message(
                f"勘定科目「{貸方}」は登録されていません。`/勘定科目追加` で追加してください。", ephemeral=True
            )
            return
        if イベント and not await db.event_exists(イベント):
            await interaction.response.send_message(
                f"イベント「{イベント}」は登録されていません。`/イベント作成` で先に作成してください。", ephemeral=True
            )
            return

        entry_id = await db.add_journal_entry(entry_date, 借方, 貸方, 金額, 摘要, イベント)

        embed = discord.Embed(
            title=f"✅ 仕訳 #{entry_id:04d} を記録しました",
            color=discord.Color.green(),
        )
        embed.add_field(name="日付", value=entry_date, inline=True)
        embed.add_field(name="金額", value=fmt_amount(金額), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="借方", value=借方, inline=True)
        embed.add_field(name="貸方", value=貸方, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="摘要", value=摘要, inline=False)
        if イベント:
            embed.add_field(name="イベント", value=イベント, inline=True)
        embed.set_footer(text=f"入力ミスは「取り消す」ボタン、または /仕訳削除 {entry_id} で取り消せます（60秒以内はボタンで即時取り消し）")
        view = CancelEntryView(entry_id, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view)

    # -------------------------------------------------------------------------
    # /仕訳帳
    # -------------------------------------------------------------------------
    @app_commands.command(name="仕訳帳", description="仕訳を一覧表示します（日付・科目・件数で絞り込み可）")
    @app_commands.describe(
        件数="表示件数（デフォルト: 10、最大: 50）",
        開始日="絞り込み開始日 YYYY-MM-DD",
        終了日="絞り込み終了日 YYYY-MM-DD",
        勘定科目="この科目が借方または貸方の仕訳だけ表示",
    )
    @app_commands.autocomplete(勘定科目=_account_autocomplete)
    async def journal(
        self,
        interaction: discord.Interaction,
        件数: int = 10,
        開始日: str | None = None,
        終了日: str | None = None,
        勘定科目: str | None = None,
    ):
        件数 = min(max(件数, 1), 50)

        for d in [開始日, 終了日]:
            if d:
                try:
                    date.fromisoformat(d)
                except ValueError:
                    await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
                    return

        if 開始日 or 終了日 or 勘定科目:
            entries = await db.get_journal_entries_filtered(開始日, 終了日, 勘定科目, 件数)
        else:
            entries = await db.get_journal_entries(件数)

        if not entries:
            await interaction.response.send_message("該当する仕訳がありません。", ephemeral=True)
            return

        lines = []
        for e in entries:
            event_str = f" 🎸{e['event_tag']}" if e.get("event_tag") else ""
            lines.append(
                f"`#{e['id']:04d}` {e['entry_date']}　"
                f"**{e['debit_account']}** / **{e['credit_account']}**　"
                f"{fmt_amount(e['amount'])}　{e['description']}{event_str}"
            )

        filter_desc = []
        if 開始日:
            filter_desc.append(f"{開始日}〜")
        if 終了日:
            filter_desc.append(f"〜{終了日}")
        if 勘定科目:
            filter_desc.append(勘定科目)
        title_suffix = f"（{' '.join(filter_desc)}）" if filter_desc else f"（直近 {len(entries)} 件）"

        embed = discord.Embed(
            title=f"📒 仕訳帳{title_suffix}",
            description=_truncate("\n".join(lines), 4000),
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
            if r["account_type"] in sections:
                sections[r["account_type"]].append(r)

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
            embed.add_field(name=f"【{acc_type}】", value=_truncate("\n".join(lines)), inline=False)

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

        embed = discord.Embed(
            title="📈 損益計算書",
            color=discord.Color.green() if net >= 0 else discord.Color.red(),
        )
        rev_lines = [f"　{r['name']}: {fmt_amount(r['balance'])}" for r in revenues] or ["　（なし）"]
        exp_lines = [f"　{r['name']}: {fmt_amount(r['balance'])}" for r in expenses] or ["　（なし）"]

        embed.add_field(name="【収益】", value=_truncate("\n".join(rev_lines)), inline=False)
        embed.add_field(name="収益合計", value=fmt_amount(total_rev), inline=True)
        embed.add_field(name="【費用】", value=_truncate("\n".join(exp_lines)), inline=False)
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
        revenues = [r for r in rows if r["account_type"] == "収益"]
        expenses = [r for r in rows if r["account_type"] == "費用"]
        net_income = sum(r["balance"] for r in revenues) - sum(r["balance"] for r in expenses)

        total_assets = sum(r["balance"] for r in assets)
        total_liab = sum(r["balance"] for r in liabilities)
        total_equity = sum(r["balance"] for r in equities) + net_income

        embed = discord.Embed(title="📋 貸借対照表", color=discord.Color.blurple())

        def fmt_lines(items):
            return "\n".join(f"　{r['name']}: {fmt_amount(r['balance'])}" for r in items) or "　（なし）"

        embed.add_field(name="【資産】", value=_truncate(fmt_lines(assets)), inline=True)
        embed.add_field(
            name="【負債・資本】",
            value=_truncate(fmt_lines(liabilities) + f"\n　当期純利益: {fmt_amount(net_income)}\n" + fmt_lines(equities)),
            inline=True,
        )
        embed.add_field(name="資産合計", value=fmt_amount(total_assets), inline=True)
        embed.add_field(name="負債・資本合計", value=fmt_amount(total_liab + total_equity), inline=True)
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------------------
    # /総勘定元帳
    # -------------------------------------------------------------------------
    @app_commands.command(name="総勘定元帳", description="指定した勘定科目の元帳（全取引・累積残高）を表示します")
    @app_commands.describe(勘定科目="表示する勘定科目名")
    @app_commands.autocomplete(勘定科目=_account_autocomplete)
    async def general_ledger(self, interaction: discord.Interaction, 勘定科目: str):
        if not await db.account_exists(勘定科目):
            await interaction.response.send_message(f"勘定科目「{勘定科目}」は登録されていません。", ephemeral=True)
            return

        entries = await db.get_general_ledger(勘定科目)
        if not entries:
            await interaction.response.send_message(f"「{勘定科目}」の仕訳がまだありません。", ephemeral=True)
            return

        acc_type = entries[0]["account_type"]
        balance_side = "借方" if acc_type in ("資産", "費用") else "貸方"

        lines = []
        for e in entries:
            debit_str  = fmt_amount(e["debit"])  if e["debit"]  else "　　　　"
            credit_str = fmt_amount(e["credit"]) if e["credit"] else "　　　　"
            lines.append(
                f"`{e['entry_date']}` {e['counterpart']}\n"
                f"　借方: {debit_str}　貸方: {credit_str}　残高: {fmt_amount(e['balance'])}\n"
                f"　摘要: {e['description']}"
            )

        CHUNK = 10
        total = len(entries)
        shown = lines[:CHUNK]

        embed = discord.Embed(
            title=f"📖 総勘定元帳 ／ {勘定科目}（{acc_type}・{balance_side}残）",
            description=_truncate("\n".join(shown), 4000),
            color=discord.Color.dark_gold(),
        )
        if total > CHUNK:
            embed.set_footer(text=f"全 {total} 件中 最初の {CHUNK} 件を表示")
        else:
            embed.set_footer(text=f"全 {total} 件　期末残高: {fmt_amount(entries[-1]['balance'])}")
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # キャッシュフロー計算書
    # =========================================================================

    @app_commands.command(name="キャッシュフロー計算書", description="キャッシュフロー計算書（直接法）を表示します")
    @app_commands.describe(期間="対象期間 YYYY または YYYY-MM（省略時: 今年）")
    async def cash_flow(self, interaction: discord.Interaction, 期間: str | None = None):
        period = 期間 or str(date.today())[:4]
        cf = await db.get_cash_flow(period)

        def cf_field(in_: int, out_: int, net: int) -> str:
            sign = "+" if net >= 0 else ""
            return (
                f"　収入: {fmt_amount(in_)}\n"
                f"　支出: {fmt_amount(out_)}\n"
                f"　**小計: {sign}{fmt_amount(net)}**"
            )

        net_color = discord.Color.blue() if cf["net_change"] >= 0 else discord.Color.red()
        embed = discord.Embed(title=f"💰 キャッシュフロー計算書（{period}）", color=net_color)
        embed.add_field(
            name="【営業活動によるCF】",
            value=cf_field(cf["operating_in"], cf["operating_out"], cf["operating_net"]),
            inline=False,
        )
        embed.add_field(
            name="【投資活動によるCF】",
            value=cf_field(cf["investing_in"], cf["investing_out"], cf["investing_net"]),
            inline=False,
        )
        embed.add_field(
            name="【財務活動によるCF】",
            value=cf_field(cf["financing_in"], cf["financing_out"], cf["financing_net"]),
            inline=False,
        )
        sign = "+" if cf["net_change"] >= 0 else ""
        embed.add_field(name="現金増減額", value=f"**{sign}{fmt_amount(cf['net_change'])}**", inline=False)

        # ソース別内訳
        SOURCE_ICONS = {"ライブ": "🎸", "グッズ": "👕", "Booth": "🛒", "Fanbox": "💛"}
        breakdown_lines = []
        for src, vals in cf["source_breakdown"].items():
            net = vals["in"] - vals["out"]
            if vals["in"] == 0 and vals["out"] == 0:
                continue
            sign_s = "+" if net >= 0 else ""
            icon = SOURCE_ICONS.get(src, "")
            breakdown_lines.append(
                f"{icon} **{src}**: 収入 {fmt_amount(vals['in'])} / 支出 {fmt_amount(vals['out'])}　→ **{sign_s}{fmt_amount(net)}**"
            )
        if breakdown_lines:
            embed.add_field(
                name="【収益源別内訳】",
                value=_truncate("\n".join(breakdown_lines)),
                inline=False,
            )

        embed.set_footer(text="現金・普通預金を対象に直接法で集計")
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # イベント管理
    # =========================================================================

    @app_commands.command(name="イベント作成", description="ライブ等のイベントタグを作成します")
    @app_commands.describe(名前="イベント名（/仕訳 で紐づけ可能になります）")
    async def create_event(self, interaction: discord.Interaction, 名前: str):
        success = await db.create_event(名前)
        if success:
            await interaction.response.send_message(
                f"✅ イベント「{名前}」を作成しました。`/仕訳` の「イベント」欄で選択できます。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(f"❌ 「{名前}」はすでに登録されています。", ephemeral=True)

    @app_commands.command(name="イベント一覧", description="登録済みのイベント一覧を表示します")
    async def list_events(self, interaction: discord.Interaction):
        events = await db.get_events()
        if not events:
            await interaction.response.send_message(
                "イベントがまだ登録されていません。`/イベント作成` で追加してください。", ephemeral=True
            )
            return
        embed = discord.Embed(title="🎸 イベント一覧", color=discord.Color.purple())
        embed.description = "\n".join(f"　{e}" for e in events)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="イベント削除", description="イベントタグを削除します（確認あり）")
    @app_commands.describe(名前="削除するイベント名")
    @app_commands.autocomplete(名前=_event_autocomplete)
    async def delete_event(self, interaction: discord.Interaction, 名前: str):
        if not await db.event_exists(名前):
            await interaction.response.send_message(f"❌ イベント「{名前}」が見つかりません。", ephemeral=True)
            return

        count = await db.get_event_entry_count(名前)
        warning = f"\n⚠️ このイベントには **{count} 件**の仕訳が紐づいています。削除すると仕訳のイベントタグがクリアされます。" if count > 0 else ""

        view = ConfirmDeleteEventView(名前)
        await interaction.response.send_message(
            f"イベント「**{名前}**」を削除しますか？{warning}",
            view=view,
            ephemeral=True,
        )

    # =========================================================================
    # ライブ収支
    # =========================================================================

    @app_commands.command(name="ライブ収支", description="イベント別の収支を表示します")
    @app_commands.describe(イベント名="対象のイベント名")
    @app_commands.autocomplete(イベント名=_event_autocomplete)
    async def event_summary(self, interaction: discord.Interaction, イベント名: str):
        summary = await db.get_event_summary(イベント名)
        if summary["entry_count"] == 0:
            await interaction.response.send_message(f"「{イベント名}」に紐づいた仕訳がありません。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🎸 ライブ収支: {イベント名}",
            color=discord.Color.green() if summary["net"] >= 0 else discord.Color.red(),
        )
        rev_lines = [f"　{k}: {fmt_amount(v)}" for k, v in summary["revenues"].items()] or ["　（なし）"]
        exp_lines = [f"　{k}: {fmt_amount(v)}" for k, v in summary["expenses"].items()] or ["　（なし）"]

        embed.add_field(name="【収益】", value=_truncate("\n".join(rev_lines)), inline=False)
        embed.add_field(name="収益合計", value=fmt_amount(summary["total_revenue"]), inline=True)
        embed.add_field(name="【費用】", value=_truncate("\n".join(exp_lines)), inline=False)
        embed.add_field(name="費用合計", value=fmt_amount(summary["total_expense"]), inline=True)
        net = summary["net"]
        embed.add_field(
            name="当期純利益" if net >= 0 else "当期純損失",
            value=fmt_amount(abs(net)),
            inline=False,
        )
        embed.set_footer(text=f"関連仕訳数: {summary['entry_count']} 件")
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # 月次収支
    # =========================================================================

    @app_commands.command(name="月次収支", description="指定月の収支サマリーを表示します")
    @app_commands.describe(年月="対象年月 YYYY-MM（省略時: 今月）")
    async def monthly_summary(self, interaction: discord.Interaction, 年月: str | None = None):
        year_month = 年月 or str(date.today())[:7]
        try:
            date.fromisoformat(year_month + "-01")
        except ValueError:
            await interaction.response.send_message("年月は YYYY-MM 形式で入力してください。", ephemeral=True)
            return

        summary = await db.get_monthly_summary(year_month)

        embed = discord.Embed(
            title=f"📅 月次収支: {year_month}",
            color=discord.Color.green() if summary["net"] >= 0 else discord.Color.red(),
        )
        rev_lines = [f"　{k}: {fmt_amount(v)}" for k, v in summary["revenues"].items()] or ["　（なし）"]
        exp_lines = [f"　{k}: {fmt_amount(v)}" for k, v in summary["expenses"].items()] or ["　（なし）"]

        embed.add_field(name="【収益】", value=_truncate("\n".join(rev_lines)), inline=False)
        embed.add_field(name="収益合計", value=fmt_amount(summary["total_revenue"]), inline=True)
        embed.add_field(name="【費用】", value=_truncate("\n".join(exp_lines)), inline=False)
        embed.add_field(name="費用合計", value=fmt_amount(summary["total_expense"]), inline=True)
        net = summary["net"]
        embed.add_field(
            name="当月純利益" if net >= 0 else "当月純損失",
            value=fmt_amount(abs(net)),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # 予算
    # =========================================================================

    @app_commands.command(name="予算設定", description="勘定科目ごとの予算を設定します")
    @app_commands.describe(
        勘定科目="予算を設定する科目",
        期間="対象期間 YYYY-MM または YYYY",
        金額="予算金額（円）",
    )
    @app_commands.autocomplete(勘定科目=_account_autocomplete)
    async def set_budget(self, interaction: discord.Interaction, 勘定科目: str, 期間: str, 金額: int):
        if not await db.account_exists(勘定科目):
            await interaction.response.send_message(f"勘定科目「{勘定科目}」が見つかりません。", ephemeral=True)
            return
        if 金額 < 0:
            await interaction.response.send_message("金額は0以上を指定してください。", ephemeral=True)
            return
        await db.set_budget(勘定科目, 期間, 金額)
        await interaction.response.send_message(
            f"✅ 「{勘定科目}」の {期間} 予算を {fmt_amount(金額)} に設定しました。", ephemeral=True
        )

    @app_commands.command(name="予算実績対比表", description="予算と実績を比較します")
    @app_commands.describe(期間="対象期間 YYYY-MM または YYYY（省略時: 今月）")
    async def budget_vs_actual(self, interaction: discord.Interaction, 期間: str | None = None):
        period = 期間 or str(date.today())[:7]
        rows = await db.get_budget_vs_actual(period)

        if not rows:
            await interaction.response.send_message(
                f"{period} の予算が設定されていません。`/予算設定` で設定してください。", ephemeral=True
            )
            return

        lines = []
        for r in rows:
            over = r["actual"] > r["budget"]
            icon = "🔴" if over else ("🟡" if r["ratio"] >= 80 else "🟢")
            lines.append(
                f"{icon} **{r['account_name']}**\n"
                f"　予算: {fmt_amount(r['budget'])}　実績: {fmt_amount(r['actual'])}　"
                f"差異: {fmt_amount(r['diff'])}（{r['ratio']:.1f}%使用）"
            )

        embed = discord.Embed(
            title=f"📊 予算実績対比表: {period}",
            description=_truncate("\n".join(lines), 4000),
            color=discord.Color.purple(),
        )
        embed.set_footer(text="🟢 80%未満 🟡 80%以上 🔴 予算超過")
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # メンバー管理
    # =========================================================================

    @app_commands.command(name="メンバー追加", description="バンドメンバーを追加します")
    @app_commands.describe(名前="メンバー名")
    async def add_member(self, interaction: discord.Interaction, 名前: str):
        success = await db.add_member(名前)
        if success:
            await interaction.response.send_message(f"✅ メンバー「{名前}」を追加しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 「{名前}」はすでに登録されています。", ephemeral=True)

    @app_commands.command(name="メンバー削除", description="バンドメンバーを削除します")
    @app_commands.describe(名前="削除するメンバー名")
    @app_commands.autocomplete(名前=_member_autocomplete)
    async def delete_member(self, interaction: discord.Interaction, 名前: str):
        success, warning = await db.delete_member(名前)
        if success:
            msg = f"✅ メンバー「{名前}」を削除しました。{warning}"
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {warning}", ephemeral=True)

    @app_commands.command(name="メンバー一覧", description="バンドメンバーを一覧表示します")
    async def list_members(self, interaction: discord.Interaction):
        members = await db.get_members()
        if not members:
            await interaction.response.send_message(
                "メンバーがまだ登録されていません。`/メンバー追加` で追加してください。", ephemeral=True
            )
            return
        embed = discord.Embed(title="👥 メンバー一覧", color=discord.Color.teal())
        embed.description = "\n".join(f"　{m}" for m in members)
        await interaction.response.send_message(embed=embed)

    # =========================================================================
    # 立替管理
    # =========================================================================

    @app_commands.command(name="立替記録", description="メンバーの立替費用を記録します")
    @app_commands.describe(
        メンバー="立替したメンバー名",
        金額="立替金額（円）",
        摘要="内容（例: スタジオ代）",
        日付="立替日 YYYY-MM-DD（省略時は今日）",
        費用科目="指定すると「費用科目 / 未払金」の仕訳も自動作成します",
    )
    @app_commands.autocomplete(メンバー=_member_autocomplete, 費用科目=_account_autocomplete)
    async def record_advance(
        self,
        interaction: discord.Interaction,
        メンバー: str,
        金額: int,
        摘要: str,
        日付: str | None = None,
        費用科目: str | None = None,
    ):
        if 金額 <= 0:
            await interaction.response.send_message("金額は1以上の整数を指定してください。", ephemeral=True)
            return
        entry_date = 日付 or str(date.today())
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return
        if 費用科目 and not await db.account_exists(費用科目):
            await interaction.response.send_message(f"勘定科目「{費用科目}」は登録されていません。", ephemeral=True)
            return

        advance_id = await db.add_advance(メンバー, 金額, 摘要, entry_date)
        embed = discord.Embed(title=f"✅ 立替 #{advance_id:04d} を記録しました", color=discord.Color.green())
        embed.add_field(name="立替者", value=メンバー, inline=True)
        embed.add_field(name="金額", value=fmt_amount(金額), inline=True)
        embed.add_field(name="日付", value=entry_date, inline=True)
        embed.add_field(name="内容", value=摘要, inline=False)

        if 費用科目:
            entry_id = await db.add_journal_entry(entry_date, 費用科目, "未払金", 金額, f"【立替#{advance_id:04d}】{摘要}")
            embed.add_field(
                name="仕訳も自動作成",
                value=f"仕訳 #{entry_id:04d}　{費用科目} / 未払金　{fmt_amount(金額)}",
                inline=False,
            )
        embed.set_footer(text=f"精算時は /立替精算済み {advance_id} を使用してください")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="立替精算表", description="未精算の立替一覧を表示します")
    async def advance_report(self, interaction: discord.Interaction):
        advances = await db.get_advances(settled=False)
        if not advances:
            await interaction.response.send_message("未精算の立替はありません。", ephemeral=True)
            return

        totals: dict[str, int] = {}
        for a in advances:
            totals[a["paid_by"]] = totals.get(a["paid_by"], 0) + a["amount"]

        lines = [
            f"`#{a['id']:04d}` {a['entry_date']} **{a['paid_by']}** {fmt_amount(a['amount'])} {a['description']}"
            for a in advances[:20]
        ]
        total_lines = [f"　{name}: {fmt_amount(amt)}" for name, amt in totals.items()]

        embed = discord.Embed(
            title=f"💳 立替精算表（未精算: {len(advances)} 件）",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.add_field(name="メンバー別合計", value="\n".join(total_lines), inline=False)
        embed.set_footer(text="`/立替精算済み 立替ID` で精算済みにできます")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="立替精算済み", description="指定した立替を精算済みにします")
    @app_commands.describe(
        立替ID="精算済みにする立替のID（/立替精算表 で確認）",
        仕訳作成="Trueにすると「未払金 / 現金」の精算仕訳も自動作成します",
    )
    async def settle_advance(
        self,
        interaction: discord.Interaction,
        立替ID: int,
        仕訳作成: bool = False,
    ):
        advances = await db.get_advances(settled=False)
        target = next((a for a in advances if a["id"] == 立替ID), None)
        if not target:
            await interaction.response.send_message(
                f"❌ 立替 #{立替ID:04d} が見つからないか、すでに精算済みです。", ephemeral=True
            )
            return

        success = await db.settle_advance(立替ID)
        if not success:
            await interaction.response.send_message(f"❌ 精算処理に失敗しました。", ephemeral=True)
            return

        msg = f"✅ 立替 #{立替ID:04d}（{target['paid_by']} / {fmt_amount(target['amount'])} / {target['description']}）を精算済みにしました。"
        if 仕訳作成:
            entry_id = await db.add_journal_entry(
                str(date.today()), "未払金", "現金",
                target["amount"], f"【立替#{立替ID:04d}精算】{target['description']}",
            )
            msg += f"\n仕訳 #{entry_id:04d}　未払金 / 現金　{fmt_amount(target['amount'])} も作成しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    # =========================================================================
    # 勘定科目管理
    # =========================================================================

    @app_commands.command(name="年次集計", description="指定年の月別収支を一覧表示します")
    @app_commands.describe(年="対象年 YYYY（省略時: 今年）")
    async def yearly_summary(self, interaction: discord.Interaction, 年: str | None = None):
        year = 年 or str(date.today().year)
        if not year.isdigit() or len(year) != 4:
            await interaction.response.send_message("年は YYYY 形式で入力してください。", ephemeral=True)
            return

        rows = await db.get_yearly_summary(year)
        total_rev = sum(r["revenue"] for r in rows)
        total_exp = sum(r["expense"] for r in rows)

        lines = []
        for r in rows:
            if r["revenue"] == 0 and r["expense"] == 0:
                continue
            sign = "+" if r["net"] >= 0 else ""
            lines.append(
                f"`{r['month']}` 収益 {fmt_amount(r['revenue'])}　費用 {fmt_amount(r['expense'])}　"
                f"**{sign}{fmt_amount(r['net'])}**"
            )

        if not lines:
            await interaction.response.send_message(f"{year} 年の仕訳がありません。", ephemeral=True)
            return

        net = total_rev - total_exp
        embed = discord.Embed(
            title=f"📅 {year}年 年次集計",
            description=_truncate("\n".join(lines), 4000),
            color=discord.Color.green() if net >= 0 else discord.Color.red(),
        )
        embed.add_field(name="年間収益合計", value=fmt_amount(total_rev), inline=True)
        embed.add_field(name="年間費用合計", value=fmt_amount(total_exp), inline=True)
        sign = "+" if net >= 0 else ""
        embed.add_field(name="年間純利益" if net >= 0 else "年間純損失", value=f"**{sign}{fmt_amount(net)}**", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="収支比較", description="2つの期間の収支を比較します")
    @app_commands.describe(
        期間1="比較する期間1 YYYY-MM または YYYY",
        期間2="比較する期間2 YYYY-MM または YYYY",
    )
    async def period_comparison(self, interaction: discord.Interaction, 期間1: str, 期間2: str):
        result = await db.get_period_comparison(期間1, 期間2)
        s1 = result["period1"]
        s2 = result["period2"]

        all_rev_keys = sorted(set(list(s1["revenues"].keys()) + list(s2["revenues"].keys())))
        all_exp_keys = sorted(set(list(s1["expenses"].keys()) + list(s2["expenses"].keys())))

        def diff_str(v1: int, v2: int) -> str:
            diff = v2 - v1
            sign = "+" if diff >= 0 else ""
            return f"({sign}{fmt_amount(diff)})"

        rev_lines = []
        for k in all_rev_keys:
            v1, v2 = s1["revenues"].get(k, 0), s2["revenues"].get(k, 0)
            rev_lines.append(f"　{k}: {fmt_amount(v1)} → {fmt_amount(v2)} {diff_str(v1, v2)}")
        exp_lines = []
        for k in all_exp_keys:
            v1, v2 = s1["expenses"].get(k, 0), s2["expenses"].get(k, 0)
            exp_lines.append(f"　{k}: {fmt_amount(v1)} → {fmt_amount(v2)} {diff_str(v1, v2)}")

        embed = discord.Embed(
            title=f"📊 収支比較: {期間1} vs {期間2}",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="【収益】",
            value=_truncate("\n".join(rev_lines) or "　（なし）"),
            inline=False,
        )
        embed.add_field(
            name=f"収益合計: {fmt_amount(s1['total_revenue'])} → {fmt_amount(s2['total_revenue'])} {diff_str(s1['total_revenue'], s2['total_revenue'])}",
            value="\u200b",
            inline=False,
        )
        embed.add_field(
            name="【費用】",
            value=_truncate("\n".join(exp_lines) or "　（なし）"),
            inline=False,
        )
        embed.add_field(
            name=f"費用合計: {fmt_amount(s1['total_expense'])} → {fmt_amount(s2['total_expense'])} {diff_str(s1['total_expense'], s2['total_expense'])}",
            value="\u200b",
            inline=False,
        )
        embed.add_field(
            name=f"純利益: {fmt_amount(s1['net'])} → {fmt_amount(s2['net'])} {diff_str(s1['net'], s2['net'])}",
            value="\u200b",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="勘定科目一覧", description="登録されている勘定科目を一覧表示します")
    async def list_accounts(self, interaction: discord.Interaction):
        accounts = await db.get_accounts()

        type_order = ["資産", "負債", "資本", "収益", "費用"]
        sections: dict[str, list[str]] = {t: [] for t in type_order}
        for a in accounts:
            if a["account_type"] in sections:
                sections[a["account_type"]].append(a["name"])

        embed = discord.Embed(title="📂 勘定科目一覧", color=discord.Color.teal())
        for t in type_order:
            names = sections[t]
            if names:
                embed.add_field(name=f"【{t}】", value="、".join(names), inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="勘定科目削除", description="勘定科目を削除します（仕訳で使用中の科目は削除不可）")
    @app_commands.describe(名前="削除する勘定科目名")
    @app_commands.autocomplete(名前=_account_autocomplete)
    async def delete_account(self, interaction: discord.Interaction, 名前: str):
        success, reason = await db.delete_account(名前)
        if success:
            await interaction.response.send_message(f"✅ 勘定科目「{名前}」を削除しました。", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 削除できません: {reason}", ephemeral=True)

    @app_commands.command(name="勘定科目追加", description="新しい勘定科目を追加します")
    @app_commands.describe(名前="勘定科目名", 種別="勘定科目の種別")
    @app_commands.choices(
        種別=[
            app_commands.Choice(name="資産", value="資産"),
            app_commands.Choice(name="負債", value="負債"),
            app_commands.Choice(name="資本", value="資本"),
            app_commands.Choice(name="収益", value="収益"),
            app_commands.Choice(name="費用", value="費用"),
        ]
    )
    async def add_account(self, interaction: discord.Interaction, 名前: str, 種別: str):
        success = await db.add_account(名前, 種別)
        if success:
            await interaction.response.send_message(
                f"✅ 勘定科目「{名前}」（{種別}）を追加しました。", ephemeral=True
            )
        else:
            await interaction.response.send_message(f"❌ 「{名前}」はすでに登録されています。", ephemeral=True)

    # =========================================================================
    # 仕訳削除・ストレージ確認
    # =========================================================================

    @app_commands.command(name="仕訳削除", description="指定したIDの仕訳を削除します（確認あり）")
    @app_commands.describe(仕訳ID="削除する仕訳のID（/仕訳帳 で確認できます）")
    async def delete_entry(self, interaction: discord.Interaction, 仕訳ID: int):
        entry = await db.get_journal_entry(仕訳ID)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳ID:04d} が見つかりません。", ephemeral=True)
            return

        event_str = f"\nイベント: {entry['event_tag']}" if entry.get("event_tag") else ""
        content = (
            f"以下の仕訳を削除しますか？\n"
            f"**#{entry['id']:04d}** {entry['entry_date']}　"
            f"**{entry['debit_account']}** / **{entry['credit_account']}**　"
            f"{fmt_amount(entry['amount'])}　{entry['description']}{event_str}"
        )
        view = ConfirmDeleteEntryView(entry)
        await interaction.response.send_message(content, view=view, ephemeral=True)

    @app_commands.command(name="エクスポート", description="仕訳帳をCSVファイルとして出力します")
    @app_commands.describe(
        開始日="出力開始日 YYYY-MM-DD（省略時: 全期間）",
        終了日="出力終了日 YYYY-MM-DD（省略時: 全期間）",
    )
    async def export_csv(
        self,
        interaction: discord.Interaction,
        開始日: str | None = None,
        終了日: str | None = None,
    ):
        for d in [開始日, 終了日]:
            if d:
                try:
                    date.fromisoformat(d)
                except ValueError:
                    await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
                    return

        entries = await db.get_journal_entries_filtered(開始日, 終了日, None, limit=10000)
        if not entries:
            await interaction.response.send_message("出力する仕訳がありません。", ephemeral=True)
            return

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", "日付", "借方", "貸方", "金額", "摘要", "イベント"])
        for e in sorted(entries, key=lambda x: (x["entry_date"], x["id"])):
            writer.writerow([
                e["id"], e["entry_date"], e["debit_account"],
                e["credit_account"], e["amount"], e["description"],
                e.get("event_tag") or "",
            ])

        buf.seek(0)
        period_str = ""
        if 開始日 or 終了日:
            period_str = f"_{開始日 or ''}_{終了日 or ''}"
        filename = f"仕訳帳{period_str}_{date.today()}.csv"
        file = discord.File(fp=io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=filename)
        await interaction.response.send_message(
            f"✅ {len(entries)} 件をCSVで出力しました。",
            file=file,
            ephemeral=True,
        )

    @app_commands.command(name="バックアップ", description="DBファイルをこのチャンネルに送信してバックアップします")
    async def backup(self, interaction: discord.Interaction):
        import database as dbmod
        db_path = os.path.abspath(dbmod.DB_PATH)
        if not os.path.exists(db_path):
            await interaction.response.send_message("DBファイルが見つかりません。", ephemeral=True)
            return
        file = discord.File(fp=db_path, filename=f"bookkeeping_backup_{date.today()}.db")
        await interaction.response.send_message(
            f"💾 DBバックアップ（{date.today()}）",
            file=file,
            ephemeral=True,
        )

    @app_commands.command(name="仕訳編集", description="既存の仕訳を編集します")
    @app_commands.describe(
        仕訳ID="編集する仕訳のID",
        借方="新しい借方勘定科目",
        貸方="新しい貸方勘定科目",
        金額="新しい金額",
        摘要="新しい摘要",
        日付="新しい日付 YYYY-MM-DD",
        イベント="新しいイベントタグ（空文字で解除）",
    )
    @app_commands.autocomplete(借方=_account_autocomplete, 貸方=_account_autocomplete, イベント=_event_autocomplete)
    async def edit_entry(
        self,
        interaction: discord.Interaction,
        仕訳ID: int,
        借方: str | None = None,
        貸方: str | None = None,
        金額: int | None = None,
        摘要: str | None = None,
        日付: str | None = None,
        イベント: str | None = None,
    ):
        entry = await db.get_journal_entry(仕訳ID)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳ID:04d} が見つかりません。", ephemeral=True)
            return

        new_debit   = 借方 or entry["debit_account"]
        new_credit  = 貸方 or entry["credit_account"]
        new_amount  = 金額 if 金額 is not None else entry["amount"]
        new_desc    = 摘要 or entry["description"]
        new_date    = 日付 or entry["entry_date"]
        new_tag     = (イベント if イベント != "" else None) if イベント is not None else entry["event_tag"]

        if new_amount <= 0:
            await interaction.response.send_message("金額は1以上の整数を指定してください。", ephemeral=True)
            return
        try:
            date.fromisoformat(new_date)
        except ValueError:
            await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return
        if not await db.account_exists(new_debit):
            await interaction.response.send_message(f"勘定科目「{new_debit}」は登録されていません。", ephemeral=True)
            return
        if not await db.account_exists(new_credit):
            await interaction.response.send_message(f"勘定科目「{new_credit}」は登録されていません。", ephemeral=True)
            return
        if new_tag and not await db.event_exists(new_tag):
            await interaction.response.send_message(f"イベント「{new_tag}」は登録されていません。", ephemeral=True)
            return

        await db.update_journal_entry(仕訳ID, new_date, new_debit, new_credit, new_amount, new_desc, new_tag)

        embed = discord.Embed(title=f"✏️ 仕訳 #{仕訳ID:04d} を編集しました", color=discord.Color.orange())
        embed.add_field(name="日付", value=new_date, inline=True)
        embed.add_field(name="金額", value=fmt_amount(new_amount), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="借方", value=new_debit, inline=True)
        embed.add_field(name="貸方", value=new_credit, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="摘要", value=new_desc, inline=False)
        if new_tag:
            embed.add_field(name="イベント", value=new_tag, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="仕訳タグ変更", description="仕訳のイベントタグを後から変更・解除します")
    @app_commands.describe(
        仕訳ID="変更する仕訳のID",
        イベント="新しいイベント名（空欄で解除）",
    )
    @app_commands.autocomplete(イベント=_event_autocomplete)
    async def change_entry_tag(
        self,
        interaction: discord.Interaction,
        仕訳ID: int,
        イベント: str | None = None,
    ):
        entry = await db.get_journal_entry(仕訳ID)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳ID:04d} が見つかりません。", ephemeral=True)
            return
        new_tag = イベント or None
        if new_tag and not await db.event_exists(new_tag):
            await interaction.response.send_message(f"イベント「{new_tag}」は登録されていません。", ephemeral=True)
            return
        await db.update_journal_entry_tag(仕訳ID, new_tag)
        msg = f"✅ 仕訳 #{仕訳ID:04d} のイベントタグを「{new_tag}」に変更しました。" if new_tag else f"✅ 仕訳 #{仕訳ID:04d} のイベントタグを解除しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="ストレージ確認", description="ディスク使用量とDB情報を表示します")
    async def storage_status(self, interaction: discord.Interaction):
        info = await db.get_storage_info()
        embed = _storage_embed(info)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bookkeeping(bot))
