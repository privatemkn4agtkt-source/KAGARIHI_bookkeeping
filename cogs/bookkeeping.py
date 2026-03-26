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
        accounts = await db.get_accounts_with_usage()
        common_set = dict.fromkeys(db.COMMON_ACCOUNTS)
        common  = [a for a in sorted(accounts, key=lambda a: list(common_set).index(a["name"]) if a["name"] in common_set else 999) if a["name"] in common_set]
        others  = [a for a in accounts if a["name"] not in common_set]
        ordered = common + others
        q = current.lower()
        return [
            app_commands.Choice(name=a["name"], value=a["name"])
            for a in ordered if q in a["name"].lower()
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
    # ダッシュボード
    # =========================================================================

    async def _is_dashboard_allowed(self, discord_user_id: str) -> bool:
        """コマンド実行者がダッシュボード許可リストに含まれているか確認"""
        return await db.is_allowed_user(discord_user_id)

    @app_commands.command(name="ダッシュボード", description="会計ダッシュボードのURLを表示します（許可ユーザーのみ）")
    async def show_dashboard(self, interaction: discord.Interaction):
        if not await self._is_dashboard_allowed(str(interaction.user.id)):
            await interaction.response.send_message(
                "❌ 閲覧権限がありません。ダッシュボードの許可管理ページで権限を付与してもらってください。",
                ephemeral=True,
            )
            return
        # .envファイルから最新のURLを動的に読み込む
        dashboard_url = os.getenv("DASHBOARD_URL", "")
        env_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_file):
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DASHBOARD_URL="):
                        dashboard_url = line.split("=", 1)[1].strip()
                        break
        if not dashboard_url:
            await interaction.response.send_message(
                "⚠️ ダッシュボードURLが設定されていません（環境変数 `DASHBOARD_URL`）。",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title="📊 会計ダッシュボード",
            description=f"[ダッシュボードを開く]({dashboard_url})\n\n許可管理はダッシュボードの「許可管理」ページから行えます。",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # =========================================================================
    # 仕訳
    # =========================================================================

    @app_commands.command(name="仕訳", description="仕訳を記録します（複式簿記）")
    @app_commands.describe(
        借方="借方勘定科目（候補から選ぶか直接入力）",
        貸方="貸方勘定科目（候補から選ぶか直接入力）",
        金額="金額（円、整数・税込で入力）",
        摘要="取引の説明",
        日付="取引日 YYYY-MM-DD（省略時は今日）",
        イベント="紐づけるイベント名（/イベント作成 で事前登録が必要）",
        消費税率="消費税率 0 or 10（%）。指定すると税額を自動計算して表示します",
    )
    @app_commands.autocomplete(借方=_account_autocomplete, 貸方=_account_autocomplete, イベント=_event_autocomplete)
    @app_commands.choices(消費税率=[
        app_commands.Choice(name="非課税 (0%)", value=0),
        app_commands.Choice(name="課税 (10%)", value=10),
    ])
    async def add_entry(
        self,
        interaction: discord.Interaction,
        借方: str,
        貸方: str,
        金額: int,
        摘要: str,
        日付: str | None = None,
        イベント: str | None = None,
        消費税率: int = 0,
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
        if 貸方 in db.CASH_ACCOUNTS:
            balance = await db.get_account_balance(貸方)
            if balance - 金額 < 0:
                await interaction.response.send_message(
                    f"❌ {貸方}の残高が不足しています。\n現在残高: {fmt_amount(balance)}\n引落予定: {fmt_amount(金額)}",
                    ephemeral=True,
                )
                return

        entry_id = await db.add_journal_entry(entry_date, 借方, 貸方, 金額, 摘要, イベント, 消費税率)

        embed = discord.Embed(
            title=f"✅ 仕訳 #{entry_id:04d} を記録しました",
            color=discord.Color.green(),
        )
        embed.add_field(name="日付", value=entry_date, inline=True)
        embed.add_field(name="金額（税込）", value=fmt_amount(金額), inline=True)
        if 消費税率 > 0:
            tax_amt = int(金額 * 消費税率 / (100 + 消費税率))
            embed.add_field(name=f"消費税({消費税率}%)", value=fmt_amount(tax_amt), inline=True)
        else:
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

    @app_commands.command(name="仕訳削除", description="指定したIDの仕訳を削除します（確認あり）")
    @app_commands.describe(仕訳id="削除する仕訳のID（/仕訳帳 で確認できます）")
    async def delete_entry(self, interaction: discord.Interaction, 仕訳id: int):
        entry = await db.get_journal_entry(仕訳id)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳id:04d} が見つかりません。", ephemeral=True)
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

    @app_commands.command(name="仕訳編集", description="既存の仕訳を編集します")
    @app_commands.describe(
        仕訳id="編集する仕訳のID",
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
        仕訳id: int,
        借方: str | None = None,
        貸方: str | None = None,
        金額: int | None = None,
        摘要: str | None = None,
        日付: str | None = None,
        イベント: str | None = None,
    ):
        entry = await db.get_journal_entry(仕訳id)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳id:04d} が見つかりません。", ephemeral=True)
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

        await db.update_journal_entry(仕訳id, new_date, new_debit, new_credit, new_amount, new_desc, new_tag)

        embed = discord.Embed(title=f"✏️ 仕訳 #{仕訳id:04d} を編集しました", color=discord.Color.orange())
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
    # 予算
    # =========================================================================

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
        embed.set_footer(text=f"精算時は /立替精算 {advance_id} を使用してください")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="立替精算", description="指定した立替を精算済みにします")
    @app_commands.describe(
        立替id="精算済みにする立替のID（/立替精算表 で確認）",
        仕訳作成="Trueにすると「未払金 / 現金」の精算仕訳も自動作成します",
    )
    async def settle_advance(
        self,
        interaction: discord.Interaction,
        立替id: int,
        仕訳作成: bool = False,
    ):
        advances = await db.get_advances(settled=False)
        target = next((a for a in advances if a["id"] == 立替id), None)
        if not target:
            await interaction.response.send_message(
                f"❌ 立替 #{立替id:04d} が見つからないか、すでに精算済みです。", ephemeral=True
            )
            return

        success = await db.settle_advance(立替id)
        if not success:
            await interaction.response.send_message(f"❌ 精算処理に失敗しました。", ephemeral=True)
            return

        msg = f"✅ 立替 #{立替id:04d}（{target['paid_by']} / {fmt_amount(target['amount'])} / {target['description']}）を精算済みにしました。"
        if 仕訳作成:
            entry_id = await db.add_journal_entry(
                str(date.today()), "未払金", "現金",
                target["amount"], f"【立替#{立替id:04d}精算】{target['description']}",
            )
            msg += f"\n仕訳 #{entry_id:04d}　未払金 / 現金　{fmt_amount(target['amount'])} も作成しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    # =========================================================================
    # 勘定科目管理
    # =========================================================================

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

    @app_commands.command(name="仕訳タグ変更", description="仕訳のイベントタグを後から変更・解除します")
    @app_commands.describe(
        仕訳id="変更する仕訳のID",
        イベント="新しいイベント名（空欄で解除）",
    )
    @app_commands.autocomplete(イベント=_event_autocomplete)
    async def change_entry_tag(
        self,
        interaction: discord.Interaction,
        仕訳id: int,
        イベント: str | None = None,
    ):
        entry = await db.get_journal_entry(仕訳id)
        if not entry:
            await interaction.response.send_message(f"❌ 仕訳 #{仕訳id:04d} が見つかりません。", ephemeral=True)
            return
        new_tag = イベント or None
        if new_tag and not await db.event_exists(new_tag):
            await interaction.response.send_message(f"イベント「{new_tag}」は登録されていません。", ephemeral=True)
            return
        await db.update_journal_entry_tag(仕訳id, new_tag)
        msg = f"✅ 仕訳 #{仕訳id:04d} のイベントタグを「{new_tag}」に変更しました。" if new_tag else f"✅ 仕訳 #{仕訳id:04d} のイベントタグを解除しました。"
        await interaction.response.send_message(msg, ephemeral=True)

    # =========================================================================
    # グッズ在庫管理
    # =========================================================================

    async def _goods_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        goods = await db.get_goods()
        return [
            app_commands.Choice(name=f"{g['name']} (在庫:{g['stock']})", value=g["name"])
            for g in goods if current.lower() in g["name"].lower()
        ][:25]

    @app_commands.command(name="グッズ登録", description="新しいグッズを在庫管理に追加します")
    @app_commands.describe(
        グッズ名="グッズの名称（例: Tシャツ、クリアファイル）",
        販売単価="販売価格（円）",
    )
    async def goods_register(self, interaction: discord.Interaction, グッズ名: str, 販売単価: int):
        if 販売単価 <= 0:
            await interaction.response.send_message("販売単価は1以上で入力してください。", ephemeral=True)
            return
        ok = await db.add_goods(グッズ名, 販売単価)
        if not ok:
            await interaction.response.send_message(f"❌ 「{グッズ名}」はすでに登録されています。", ephemeral=True)
            return
        embed = discord.Embed(title="📦 グッズを登録しました", color=discord.Color.green())
        embed.add_field(name="グッズ名", value=グッズ名, inline=True)
        embed.add_field(name="販売単価", value=fmt_amount(販売単価), inline=True)
        embed.add_field(name="初期在庫", value="0 個", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="グッズ仕入", description="グッズの仕入を記録し在庫を増やします（仕訳も自動生成）")
    @app_commands.describe(
        グッズ名="仕入れるグッズ名",
        数量="仕入数量（個）",
        仕入単価="1個あたりの仕入単価（円）",
        日付="仕入日 YYYY-MM-DD（省略時は今日）",
        摘要="備考",
    )
    @app_commands.autocomplete(グッズ名=_goods_autocomplete)
    async def goods_purchase(
        self,
        interaction: discord.Interaction,
        グッズ名: str,
        数量: int,
        仕入単価: int,
        日付: str | None = None,
        摘要: str = "",
    ):
        if 数量 <= 0 or 仕入単価 < 0:
            await interaction.response.send_message("数量は1以上、単価は0以上を指定してください。", ephemeral=True)
            return
        if not await db.goods_exists(グッズ名):
            await interaction.response.send_message(f"❌ 「{グッズ名}」は未登録です。`/グッズ登録` で先に登録してください。", ephemeral=True)
            return
        entry_date = 日付 or str(date.today())
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return

        total = 数量 * 仕入単価
        desc = 摘要 or f"{グッズ名} 仕入 {数量}個"
        # 仕訳: グッズ在庫 / 現金（無償仕入の場合は仕訳不要）
        jid = await db.add_journal_entry(entry_date, "グッズ在庫", "現金", total, desc, None, 10) if total > 0 else None
        tx_id = await db.record_goods_purchase(グッズ名, 数量, 仕入単価, entry_date, desc, jid)

        embed = discord.Embed(title="📥 グッズ仕入を記録しました", color=discord.Color.blue())
        embed.add_field(name="グッズ名", value=グッズ名, inline=True)
        embed.add_field(name="数量", value=f"{数量} 個", inline=True)
        embed.add_field(name="仕入単価", value=fmt_amount(仕入単価), inline=True)
        embed.add_field(name="仕入総額", value=fmt_amount(total), inline=True)
        embed.add_field(name="連携仕訳", value=f"#{jid:04d}" if jid else "なし（無償仕入）", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="グッズ販売", description="グッズの販売を記録し在庫を減らします（仕訳も自動生成）")
    @app_commands.describe(
        グッズ名="販売するグッズ名",
        数量="販売数量（個）",
        販売単価="1個あたりの販売単価（省略時はグッズ登録時の単価）",
        日付="販売日 YYYY-MM-DD（省略時は今日）",
        イベント="紐づけるイベント名",
        摘要="備考",
    )
    @app_commands.autocomplete(グッズ名=_goods_autocomplete, イベント=_event_autocomplete)
    async def goods_sale(
        self,
        interaction: discord.Interaction,
        グッズ名: str,
        数量: int,
        販売単価: int | None = None,
        日付: str | None = None,
        イベント: str | None = None,
        摘要: str = "",
    ):
        if 数量 <= 0:
            await interaction.response.send_message("数量は1以上を指定してください。", ephemeral=True)
            return
        goods_list = await db.get_goods()
        goods_info = next((g for g in goods_list if g["name"] == グッズ名), None)
        if goods_info is None:
            await interaction.response.send_message(f"❌ 「{グッズ名}」は未登録です。", ephemeral=True)
            return
        unit_price = 販売単価 if 販売単価 is not None else goods_info["selling_price"]
        if unit_price <= 0:
            await interaction.response.send_message("販売単価は1以上を指定してください。", ephemeral=True)
            return
        entry_date = 日付 or str(date.today())
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            await interaction.response.send_message("日付は YYYY-MM-DD 形式で入力してください。", ephemeral=True)
            return
        if イベント and not await db.event_exists(イベント):
            await interaction.response.send_message(f"イベント「{イベント}」は登録されていません。", ephemeral=True)
            return

        total = 数量 * unit_price
        desc = 摘要 or f"{グッズ名} 販売 {数量}個"
        # 仕訳: 現金 / グッズ売上
        jid = await db.add_journal_entry(entry_date, "現金", "グッズ売上", total, desc, イベント, 10)
        tx_id, err = await db.record_goods_sale(グッズ名, 数量, unit_price, entry_date, desc, jid)
        if err:
            # 仕訳を取り消し
            await db.delete_journal_entry(jid)
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        embed = discord.Embed(title="💰 グッズ販売を記録しました", color=discord.Color.green())
        embed.add_field(name="グッズ名", value=グッズ名, inline=True)
        embed.add_field(name="数量", value=f"{数量} 個", inline=True)
        embed.add_field(name="販売単価", value=fmt_amount(unit_price), inline=True)
        embed.add_field(name="売上合計", value=fmt_amount(total), inline=True)
        embed.add_field(name="連携仕訳", value=f"#{jid:04d}", inline=True)
        if イベント:
            embed.add_field(name="イベント", value=イベント, inline=True)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="グッズ削除", description="グッズを在庫管理から削除します（取引履歴がある場合は削除不可）")
    @app_commands.describe(グッズ名="削除するグッズ名")
    @app_commands.autocomplete(グッズ名=_goods_autocomplete)
    async def goods_delete(self, interaction: discord.Interaction, グッズ名: str):
        ok, msg = await db.delete_goods(グッズ名)
        if ok:
            await interaction.response.send_message(f"✅ 「{グッズ名}」を削除しました。")
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    @app_commands.command(name="ダミーデータ挿入", description="デモ用のダミーデータを一括挿入します（開発・テスト用）")
    async def insert_dummy_data(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        from datetime import date, timedelta
        import random

        today = date.today()

        # ── イベント ──────────────────────────────────────────
        events = ["春ライブ2025", "夏コミ2025", "秋ワンマン2025", "冬フェス2025"]
        for ev in events:
            await db.create_event(ev)

        # ── メンバー ──────────────────────────────────────────
        members = ["yuzpi-", "ゆずは", "ディスオーダー", "もっちゃん"]
        for m in members:
            await db.add_member(m)

        # ── 仕訳データ（全勘定科目を網羅）────────────────────
        journal_seeds = [
            # (借方, 貸方, 金額, 摘要, 日付オフセット, イベントタグ)

            # 資本：初期資金
            ("普通預金",         "繰越利益剰余金",  300000, "初期資金（繰越利益）",         -180, None),
            # 現金・預金間移動
            ("現金",             "普通預金",         30000, "ATM引き出し",                -170, None),

            # 収益：出演料＋源泉徴収
            ("普通預金",         "出演料",          150000, "春ライブ出演料入金",            -90, "春ライブ2025"),
            ("出演料",           "源泉徴収預り金",    15000, "源泉徴収預り（出演料）",         -90, "春ライブ2025"),
            # 収益：チケット（掛け売り含む）
            ("普通預金",         "売上",             80000, "春ライブチケット売上",           -90, "春ライブ2025"),
            ("売掛金",           "売上",             50000, "チケット掛け売り",              -88, "春ライブ2025"),
            ("普通預金",         "売掛金",            50000, "売掛金回収",                  -75, None),
            # 収益：グッズ物販（仮受消費税含む）
            ("現金",             "グッズ売上",        45000, "春ライブグッズ物販",            -89, "春ライブ2025"),
            # 収益：デジタル
            ("普通預金",         "Booth売上",         41000, "Booth同人誌売上",              -60, None),
            ("普通預金",         "Fanbox売上",        32000, "Fanbox月額収入",               -55, None),
            ("普通預金",         "ストリーミング収益",   8500, "Spotify等配信収益",            -50, None),

            # 費用：会場・スタジオ
            ("旅費交通費",       "現金",              12400, "会場下見交通費",               -85, "春ライブ2025"),
            ("スタジオレンタル代","普通預金",           28000, "リハーサルスタジオ代",          -83, "春ライブ2025"),
            ("会場レンタル代",   "普通預金",           65000, "春ライブ会場レンタル",          -82, "春ライブ2025"),
            # 費用：グッズ仕入（買掛金経由）
            ("グッズ在庫",       "現金",              35000, "缶バッジ仕入（現金）",           -78, None),
            ("グッズ仕入",       "買掛金",            28000, "Tシャツ仕入（後払い）",          -72, None),
            ("買掛金",           "普通預金",           28000, "Tシャツ代金支払い",             -45, None),
            # 費用：制作（未払金経由）
            ("音源制作費",       "未払金",            120000, "ミニアルバム録音費",             -70, "秋ワンマン2025"),
            ("MV制作費",         "未払金",             80000, "MV制作費",                    -68, "秋ワンマン2025"),
            ("未払金",           "普通預金",           200000, "録音・MV制作費支払い",          -40, "秋ワンマン2025"),
            # 費用：機材（仮払消費税含む）
            ("機材費",           "未払金",             45000, "マイクスタンド購入",             -58, None),
            ("未払金",           "普通預金",            45000, "機材代金支払い",               -35, None),
            # 費用：宣伝・通信・配信
            ("宣伝広告費",       "普通預金",            15000, "SNS広告費",                   -65, "夏コミ2025"),
            ("通信費",           "普通預金",             5500, "携帯通信費",                   -30, None),
            ("配信サービス費",   "普通預金",              3000, "配信ツール月額",                -25, None),
            ("消耗品費",         "現金",                 3200, "文房具・雑費",                  -15, None),
            # 源泉徴収納付
            ("源泉徴収預り金",   "普通預金",             15000, "源泉徴収納付",                 -20, None),

            # 冬フェス
            ("普通預金",         "出演料",              50000, "冬フェス出演料",                -18, "冬フェス2025"),
            ("旅費交通費",       "現金",                 8600, "遠征交通費",                   -16, "冬フェス2025"),
            ("普通預金",         "売上",                60000, "冬フェスチケット売上",           -18, "冬フェス2025"),
        ]
        for debit, credit, amount, desc, offset, tag in journal_seeds:
            entry_date = (today + timedelta(days=offset)).isoformat()
            await db.add_journal_entry(
                entry_date=entry_date,
                debit_account=debit,
                credit_account=credit,
                amount=amount,
                description=desc,
                event_tag=tag,
                tax_rate=10 if "売上" in credit or "売上" in desc else 0,
            )

        # ── 立替データ ────────────────────────────────────────
        advances_seeds = [
            ("yuzpi-",       3500, "会場下見の電車代",      -86),
            ("もっちゃん",   12000, "スタジオ延長代 立替",    -84),
            ("ゆずは",        5400, "衣装クリーニング代",      -60),
            ("ディスオーダー", 2800, "印刷費（セットリスト）",  -22),
            ("yuzpi-",       2000, "スタジオ代 立替",         -10),
        ]
        for member, amount, desc, offset in advances_seeds:
            entry_date = (today + timedelta(days=offset)).isoformat()
            await db.add_advance(paid_by=member, amount=amount, description=desc, entry_date=entry_date)

        # ── グッズ ────────────────────────────────────────────
        goods_seeds = [
            ("缶バッジセット", 800),
            ("アクリルキーホルダー", 1200),
            ("Tシャツ", 3500),
            ("クリアファイル", 600),
        ]
        for gname, price in goods_seeds:
            await db.add_goods(name=gname, selling_price=price)

        # ── 予算 ──────────────────────────────────────────────
        period = today.strftime("%Y-%m")
        budget_seeds = [
            ("旅費交通費", 30000),
            ("スタジオレンタル代", 50000),
            ("宣伝広告費", 20000),
            ("消耗品費", 10000),
            ("配信サービス費", 5000),
            ("機材費", 100000),
        ]
        for acc, amount in budget_seeds:
            await db.set_budget(account_name=acc, period=period, amount=amount)

        embed = discord.Embed(
            title="✅ ダミーデータ挿入完了",
            color=discord.Color.green(),
        )
        embed.add_field(name="イベント", value=f"{len(events)} 件", inline=True)
        embed.add_field(name="メンバー", value=f"{len(members)} 件", inline=True)
        embed.add_field(name="仕訳", value=f"{len(journal_seeds)} 件", inline=True)
        embed.add_field(name="立替", value=f"{len(advances_seeds)} 件", inline=True)
        embed.add_field(name="グッズ", value=f"{len(goods_seeds)} 件", inline=True)
        embed.add_field(name="予算", value=f"{len(budget_seeds)} 件", inline=True)
        embed.set_footer(text="ダッシュボードを開いて確認してください")
        await interaction.followup.send(embed=embed, ephemeral=True)


    @app_commands.command(name="ダミーデータ削除", description="ダミーデータ挿入で追加したデータを一括削除します（開発・テスト用）")
    async def delete_dummy_data(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        import aiosqlite

        DUMMY_EVENTS = ["春ライブ2025", "夏コミ2025", "秋ワンマン2025", "冬フェス2025"]
        DUMMY_MEMBERS = ["yuzpi-", "ゆずは", "ディスオーダー", "もっちゃん"]
        DUMMY_GOODS = ["缶バッジセット", "アクリルキーホルダー", "Tシャツ", "クリアファイル"]
        DUMMY_DESCRIPTIONS = [
            "初期資金（繰越利益）", "ATM引き出し",
            "春ライブ出演料入金", "源泉徴収預り（出演料）",
            "春ライブチケット売上", "チケット掛け売り", "売掛金回収",
            "春ライブグッズ物販",
            "Booth同人誌売上", "Fanbox月額収入", "Spotify等配信収益",
            "会場下見交通費", "リハーサルスタジオ代", "春ライブ会場レンタル",
            "缶バッジ仕入（現金）", "Tシャツ仕入（後払い）", "Tシャツ代金支払い",
            "ミニアルバム録音費", "MV制作費", "録音・MV制作費支払い",
            "マイクスタンド購入", "機材代金支払い",
            "SNS広告費", "携帯通信費", "配信ツール月額", "文房具・雑費",
            "源泉徴収納付",
            "冬フェス出演料", "遠征交通費", "冬フェスチケット売上",
        ]
        DUMMY_ADVANCE_DESCS = [
            "会場下見の電車代", "スタジオ延長代 立替",
            "衣装クリーニング代", "印刷費（セットリスト）", "スタジオ代 立替",
        ]
        DUMMY_BUDGET_ACCOUNTS = [
            "旅費交通費", "スタジオレンタル代", "宣伝広告費",
            "消耗品費", "配信サービス費", "機材費",
        ]

        async with aiosqlite.connect(db.DB_PATH) as conn:
            # 仕訳削除
            placeholders = ",".join("?" * len(DUMMY_DESCRIPTIONS))
            cur = await conn.execute(
                f"DELETE FROM journal_entries WHERE description IN ({placeholders})",
                DUMMY_DESCRIPTIONS,
            )
            deleted_journals = cur.rowcount

            # グッズ取引削除
            placeholders = ",".join("?" * len(DUMMY_GOODS))
            await conn.execute(
                f"DELETE FROM goods_transactions WHERE goods_name IN ({placeholders})",
                DUMMY_GOODS,
            )

            # グッズ削除
            cur = await conn.execute(
                f"DELETE FROM goods WHERE name IN ({placeholders})",
                DUMMY_GOODS,
            )
            deleted_goods = cur.rowcount

            # 立替削除
            placeholders = ",".join("?" * len(DUMMY_ADVANCE_DESCS))
            cur = await conn.execute(
                f"DELETE FROM advances WHERE description IN ({placeholders})",
                DUMMY_ADVANCE_DESCS,
            )
            deleted_advances = cur.rowcount

            # イベント削除
            placeholders = ",".join("?" * len(DUMMY_EVENTS))
            cur = await conn.execute(
                f"DELETE FROM events WHERE name IN ({placeholders})",
                DUMMY_EVENTS,
            )
            deleted_events = cur.rowcount

            # メンバー削除
            placeholders = ",".join("?" * len(DUMMY_MEMBERS))
            cur = await conn.execute(
                f"DELETE FROM members WHERE name IN ({placeholders})",
                DUMMY_MEMBERS,
            )
            deleted_members = cur.rowcount

            # 予算削除（当月分のみ）
            from datetime import date as _date
            period = _date.today().strftime("%Y-%m")
            placeholders = ",".join("?" * len(DUMMY_BUDGET_ACCOUNTS))
            cur = await conn.execute(
                f"DELETE FROM budgets WHERE period = ? AND account_name IN ({placeholders})",
                [period, *DUMMY_BUDGET_ACCOUNTS],
            )
            deleted_budgets = cur.rowcount

            await conn.commit()

        embed = discord.Embed(
            title="🗑️ ダミーデータ削除完了",
            color=discord.Color.orange(),
        )
        embed.add_field(name="イベント", value=f"{deleted_events} 件", inline=True)
        embed.add_field(name="メンバー", value=f"{deleted_members} 件", inline=True)
        embed.add_field(name="仕訳", value=f"{deleted_journals} 件", inline=True)
        embed.add_field(name="立替", value=f"{deleted_advances} 件", inline=True)
        embed.add_field(name="グッズ", value=f"{deleted_goods} 件", inline=True)
        embed.add_field(name="予算", value=f"{deleted_budgets} 件", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Bookkeeping(bot))
