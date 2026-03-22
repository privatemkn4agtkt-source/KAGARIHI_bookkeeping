import aiosqlite
import os
import shutil
from datetime import date

DB_PATH = os.getenv("DB_PATH", "bookkeeping.db")

# 勘定科目の種別
ACCOUNT_TYPES = {
    "資産": "asset",
    "負債": "liability",
    "資本": "equity",
    "収益": "revenue",
    "費用": "expense",
}

DEFAULT_ACCOUNTS = [
    # 資産
    ("現金", "資産"),
    ("普通預金", "資産"),
    ("売掛金", "資産"),
    ("グッズ在庫", "資産"),
    ("仮払消費税", "資産"),
    # 収益
    ("売上", "収益"),
    ("グッズ売上", "収益"),
    ("Booth売上", "収益"),
    ("Fanbox売上", "収益"),
    ("ストリーミング収益", "収益"),
    ("出演料", "収益"),
    # 費用
    ("旅費交通費", "費用"),
    ("消耗品費", "費用"),
    ("スタジオレンタル代", "費用"),
    ("会場レンタル代", "費用"),
    ("グッズ仕入", "費用"),
    ("機材費", "費用"),
    ("音源制作費", "費用"),
    ("MV制作費", "費用"),
    ("宣伝広告費", "費用"),
    ("通信費", "費用"),
    ("配信サービス費", "費用"),
    # 負債
    ("買掛金", "負債"),
    ("未払金", "負債"),
    ("仮受消費税", "負債"),
    ("源泉徴収預り金", "負債"),
    # 資本
    ("繰越利益剰余金", "資本"),
]

# キャッシュフロー計算書で「現金」として扱う勘定科目
CASH_ACCOUNTS = {"現金", "普通預金"}
# 資産・負債の中でも営業活動に分類する科目（売掛金・買掛金など）
OPERATING_ACCOUNTS = {"売掛金", "未収金", "未収入金", "前払金", "買掛金", "未払金", "前受金", "預り金"}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                account_type TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS journal_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                debit_account TEXT NOT NULL,
                credit_account TEXT NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT NOT NULL,
                event_tag TEXT DEFAULT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS advances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paid_by TEXT NOT NULL,
                amount INTEGER NOT NULL,
                description TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                settled INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT UNIQUE NOT NULL,
                display_name TEXT,
                added_by TEXT,
                added_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                period TEXT NOT NULL,
                budget_amount INTEGER NOT NULL,
                UNIQUE(account_name, period)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                selling_price INTEGER NOT NULL,
                stock INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goods_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goods_name TEXT NOT NULL,
                tx_type TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                total_amount INTEGER NOT NULL,
                entry_date TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                journal_entry_id INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # デフォルト勘定科目を投入
        for name, acc_type in DEFAULT_ACCOUNTS:
            await db.execute(
                "INSERT OR IGNORE INTO accounts (name, account_type) VALUES (?, ?)",
                (name, acc_type),
            )
        # マイグレーション: event_tag 列の追加（既存DBへの対応）
        try:
            await db.execute("ALTER TABLE journal_entries ADD COLUMN event_tag TEXT DEFAULT NULL")
        except Exception:
            pass
        # マイグレーション: tax_rate 列の追加
        try:
            await db.execute("ALTER TABLE journal_entries ADD COLUMN tax_rate INTEGER DEFAULT 0")
        except Exception:
            pass
        # 環境変数 ALLOWED_DISCORD_IDS の初回シード（既存レコードは無視）
        import os as _os
        _raw = _os.getenv("ALLOWED_DISCORD_IDS", "")
        for _uid in (_raw.split(",") if _raw else []):
            _uid = _uid.strip()
            if _uid:
                await db.execute(
                    "INSERT OR IGNORE INTO allowed_users (discord_user_id, added_by) VALUES (?, 'env')",
                    (_uid,),
                )
        await db.commit()


# =============================================================================
# 勘定科目
# =============================================================================

async def add_account(name: str, account_type: str) -> bool:
    if account_type not in ACCOUNT_TYPES:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO accounts (name, account_type) VALUES (?, ?)",
                (name, account_type),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_accounts() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, account_type FROM accounts ORDER BY account_type, name"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def account_exists(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM accounts WHERE name = ?", (name,)
        ) as cursor:
            return await cursor.fetchone() is not None


# =============================================================================
# 仕訳
# =============================================================================

async def add_journal_entry(
    entry_date: str,
    debit_account: str,
    credit_account: str,
    amount: int,
    description: str,
    event_tag: str | None = None,
    tax_rate: int = 0,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO journal_entries
               (entry_date, debit_account, credit_account, amount, description, event_tag, tax_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (entry_date, debit_account, credit_account, amount, description, event_tag, tax_rate),
        )
        await db.commit()
        return cursor.lastrowid


async def get_journal_entries(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, entry_date, debit_account, credit_account, amount, description, event_tag,
                      COALESCE(tax_rate, 0) AS tax_rate
               FROM journal_entries ORDER BY entry_date DESC, id DESC LIMIT ?""",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


async def delete_journal_entry(entry_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM journal_entries WHERE id = ?", (entry_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_journal_entry(entry_id: int) -> dict | None:
    """指定IDの仕訳を1件返す"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, entry_date, debit_account, credit_account, amount, description, event_tag,
                      COALESCE(tax_rate, 0) AS tax_rate
               FROM journal_entries WHERE id = ?""",
            (entry_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def update_journal_entry(
    entry_id: int,
    entry_date: str,
    debit_account: str,
    credit_account: str,
    amount: int,
    description: str,
    event_tag: str | None,
    tax_rate: int = 0,
) -> bool:
    """仕訳を上書き編集する"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """UPDATE journal_entries
               SET entry_date=?, debit_account=?, credit_account=?, amount=?, description=?, event_tag=?, tax_rate=?
               WHERE id=?""",
            (entry_date, debit_account, credit_account, amount, description, event_tag, tax_rate, entry_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def update_journal_entry_tag(entry_id: int, event_tag: str | None) -> bool:
    """仕訳のイベントタグだけを変更する"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE journal_entries SET event_tag=? WHERE id=?",
            (event_tag, entry_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_journal_entries_filtered(
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """日付範囲・勘定科目でフィルタした仕訳一覧"""
    conditions = []
    params: list = []
    if start_date:
        conditions.append("entry_date >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("entry_date <= ?")
        params.append(end_date)
    if account:
        conditions.append("(debit_account = ? OR credit_account = ?)")
        params.extend([account, account])
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT id, entry_date, debit_account, credit_account, amount, description, event_tag,
                       COALESCE(tax_rate, 0) AS tax_rate
               FROM journal_entries {where}
               ORDER BY entry_date DESC, id DESC LIMIT ?""",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


# =============================================================================
# 試算表
# =============================================================================

async def get_trial_balance() -> list[dict]:
    """試算表: 各勘定科目の借方合計・貸方合計・残高を返す"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT
                a.name,
                a.account_type,
                COALESCE(d.total, 0) AS debit_total,
                COALESCE(c.total, 0) AS credit_total
            FROM accounts a
            LEFT JOIN (
                SELECT debit_account AS acct, SUM(amount) AS total
                FROM journal_entries GROUP BY debit_account
            ) d ON a.name = d.acct
            LEFT JOIN (
                SELECT credit_account AS acct, SUM(amount) AS total
                FROM journal_entries GROUP BY credit_account
            ) c ON a.name = c.acct
            WHERE COALESCE(d.total, 0) != 0 OR COALESCE(c.total, 0) != 0
            ORDER BY a.account_type, a.name
            """
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                row = dict(r)
                acc_type = row["account_type"]
                debit = row["debit_total"]
                credit = row["credit_total"]
                if acc_type in ("資産", "費用"):
                    balance = debit - credit
                else:
                    balance = credit - debit
                row["balance"] = balance
                result.append(row)
            return result


# =============================================================================
# 総勘定元帳
# =============================================================================

async def get_general_ledger(account_name: str) -> list[dict]:
    """指定勘定科目の全仕訳を時系列で返す（借方額・貸方額・相手科目・累積残高付き）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT account_type FROM accounts WHERE name = ?", (account_name,)
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return []
            account_type = row["account_type"]

        async with conn.execute(
            """
            SELECT
                id,
                entry_date,
                CASE WHEN debit_account  = ? THEN amount ELSE 0 END AS debit,
                CASE WHEN credit_account = ? THEN amount ELSE 0 END AS credit,
                CASE WHEN debit_account  = ? THEN credit_account ELSE debit_account END AS counterpart,
                description
            FROM journal_entries
            WHERE debit_account = ? OR credit_account = ?
            ORDER BY entry_date, id
            """,
            (account_name, account_name, account_name, account_name, account_name),
        ) as cursor:
            rows = await cursor.fetchall()

        result = []
        balance = 0
        for r in rows:
            row = dict(r)
            if account_type in ("資産", "費用"):
                balance += row["debit"] - row["credit"]
            else:
                balance += row["credit"] - row["debit"]
            row["balance"] = balance
            row["account_type"] = account_type
            result.append(row)
        return result


# =============================================================================
# ストレージ情報
# =============================================================================

async def get_storage_info() -> dict:
    """ディスク使用量・DBファイルサイズ・仕訳件数を返す"""
    db_path_abs = os.path.abspath(DB_PATH)
    db_size = os.path.getsize(db_path_abs) if os.path.exists(db_path_abs) else 0
    disk = shutil.disk_usage(os.path.dirname(db_path_abs))
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT COUNT(*) FROM journal_entries") as cursor:
            entry_count = (await cursor.fetchone())[0]
    return {
        "db_size": db_size,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_percent": disk.used / disk.total * 100,
        "entry_count": entry_count,
    }


# =============================================================================
# イベント（ライブ）管理
# =============================================================================

async def create_event(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute("INSERT INTO events (name) VALUES (?)", (name,))
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_events() -> list[str]:
    """登録済みイベント名の一覧"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT name FROM events ORDER BY name") as cursor:
            return [r[0] for r in await cursor.fetchall()]


async def event_exists(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT 1 FROM events WHERE name = ?", (name,)) as cursor:
            return await cursor.fetchone() is not None


async def get_event_entry_count(name: str) -> int:
    """イベントタグを持つ仕訳件数を返す"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE event_tag = ?", (name,)
        ) as cursor:
            return (await cursor.fetchone())[0]


async def delete_event(name: str) -> bool:
    """イベントを削除する（関連仕訳のevent_tagはNULLにクリア）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE journal_entries SET event_tag = NULL WHERE event_tag = ?", (name,)
        )
        cursor = await conn.execute("DELETE FROM events WHERE name = ?", (name,))
        await conn.commit()
        return cursor.rowcount > 0


async def get_event_summary(event_tag: str) -> dict:
    """イベント別収支サマリー"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT je.*,
                   a_d.account_type AS debit_type,
                   a_c.account_type AS credit_type
            FROM journal_entries je
            LEFT JOIN accounts a_d ON je.debit_account = a_d.name
            LEFT JOIN accounts a_c ON je.credit_account = a_c.name
            WHERE je.event_tag = ?
            ORDER BY je.entry_date, je.id
            """,
            (event_tag,),
        ) as cursor:
            entries = [dict(r) for r in await cursor.fetchall()]

    revenues: dict[str, int] = {}
    expenses: dict[str, int] = {}
    for e in entries:
        if e["credit_type"] == "収益":
            revenues[e["credit_account"]] = revenues.get(e["credit_account"], 0) + e["amount"]
        if e["debit_type"] == "費用":
            expenses[e["debit_account"]] = expenses.get(e["debit_account"], 0) + e["amount"]

    total_rev = sum(revenues.values())
    total_exp = sum(expenses.values())
    return {
        "event_tag": event_tag,
        "revenues": revenues,
        "expenses": expenses,
        "total_revenue": total_rev,
        "total_expense": total_exp,
        "net": total_rev - total_exp,
        "entry_count": len(entries),
    }


# =============================================================================
# 月次収支
# =============================================================================

async def get_monthly_summary(year_month: str) -> dict:
    """月次収支サマリー。year_month: 'YYYY-MM'"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT je.*,
                   a_d.account_type AS debit_type,
                   a_c.account_type AS credit_type
            FROM journal_entries je
            LEFT JOIN accounts a_d ON je.debit_account = a_d.name
            LEFT JOIN accounts a_c ON je.credit_account = a_c.name
            WHERE je.entry_date LIKE ?
            ORDER BY je.entry_date, je.id
            """,
            (year_month + "%",),
        ) as cursor:
            entries = [dict(r) for r in await cursor.fetchall()]

    revenues: dict[str, int] = {}
    expenses: dict[str, int] = {}
    for e in entries:
        if e["credit_type"] == "収益":
            revenues[e["credit_account"]] = revenues.get(e["credit_account"], 0) + e["amount"]
        if e["debit_type"] == "費用":
            expenses[e["debit_account"]] = expenses.get(e["debit_account"], 0) + e["amount"]

    total_rev = sum(revenues.values())
    total_exp = sum(expenses.values())
    return {
        "period": year_month,
        "revenues": revenues,
        "expenses": expenses,
        "total_revenue": total_rev,
        "total_expense": total_exp,
        "net": total_rev - total_exp,
    }


# =============================================================================
# キャッシュフロー計算書（直接法）
# =============================================================================

async def get_cash_flow(period: str | None = None) -> dict:
    """
    直接法キャッシュフロー計算書。
    period: 'YYYY' または 'YYYY-MM'（None で全期間）
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        where = "WHERE 1=1"
        params: list = []
        if period:
            where += " AND je.entry_date LIKE ?"
            params.append(period + "%")
        async with conn.execute(
            f"""
            SELECT je.*,
                   a_d.account_type AS debit_type,
                   a_c.account_type AS credit_type
            FROM journal_entries je
            LEFT JOIN accounts a_d ON je.debit_account = a_d.name
            LEFT JOIN accounts a_c ON je.credit_account = a_c.name
            {where}
            """,
            params,
        ) as cursor:
            entries = [dict(r) for r in await cursor.fetchall()]

    def classify(name: str, acc_type: str) -> str:
        if acc_type in ("収益", "費用"):
            return "operating"
        if name in OPERATING_ACCOUNTS:
            return "operating"
        if acc_type == "資産":
            return "investing"
        return "financing"  # 負債, 資本

    SOURCE_LABELS = ["ライブ", "グッズ", "Booth", "Fanbox"]

    def get_source(entry: dict, non_cash_account: str) -> str | None:
        """収支源を判定する。現金増減に直結する勘定科目とevent_tagから判定。"""
        if entry.get("event_tag"):
            return "ライブ"
        acc_lower = non_cash_account.lower()
        if "グッズ" in non_cash_account:
            return "グッズ"
        if "booth" in acc_lower:
            return "Booth"
        if "fanbox" in acc_lower:
            return "Fanbox"
        return None

    res = {k: 0 for k in (
        "operating_in", "operating_out",
        "investing_in", "investing_out",
        "financing_in", "financing_out",
    )}
    source_breakdown: dict[str, dict[str, int]] = {
        s: {"in": 0, "out": 0} for s in SOURCE_LABELS
    }
    for e in entries:
        d_cash = e["debit_account"] in CASH_ACCOUNTS
        c_cash = e["credit_account"] in CASH_ACCOUNTS
        if d_cash and c_cash:
            continue  # 現金間移動は除外
        if d_cash:
            cat = classify(e["credit_account"], e["credit_type"] or "")
            res[f"{cat}_in"] += e["amount"]
            src = get_source(e, e["credit_account"])
            if src:
                source_breakdown[src]["in"] += e["amount"]
        if c_cash:
            cat = classify(e["debit_account"], e["debit_type"] or "")
            res[f"{cat}_out"] += e["amount"]
            src = get_source(e, e["debit_account"])
            if src:
                source_breakdown[src]["out"] += e["amount"]

    res["operating_net"] = res["operating_in"] - res["operating_out"]
    res["investing_net"] = res["investing_in"] - res["investing_out"]
    res["financing_net"] = res["financing_in"] - res["financing_out"]
    res["net_change"] = res["operating_net"] + res["investing_net"] + res["financing_net"]
    res["source_breakdown"] = source_breakdown
    return res


# =============================================================================
# メンバー
# =============================================================================

async def delete_account(name: str) -> tuple[bool, str]:
    """勘定科目を削除する。仕訳で使用中の場合は失敗する"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE debit_account=? OR credit_account=?",
            (name, name),
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if count > 0:
            return False, f"この科目は {count} 件の仕訳で使用中のため削除できません"
        cursor = await conn.execute("DELETE FROM accounts WHERE name=?", (name,))
        await conn.commit()
        if cursor.rowcount == 0:
            return False, "科目が見つかりません"
        return True, ""


async def add_member(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute("INSERT INTO members (name) VALUES (?)", (name,))
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_members() -> list[str]:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT name FROM members ORDER BY name") as cursor:
            return [r[0] for r in await cursor.fetchall()]


async def delete_member(name: str) -> tuple[bool, str]:
    """メンバーを削除する。未精算の立替がある場合は警告を返す（削除は続行）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM advances WHERE paid_by=? AND settled=0", (name,)
        ) as cursor:
            unsettled = (await cursor.fetchone())[0]
        cursor = await conn.execute("DELETE FROM members WHERE name=?", (name,))
        await conn.commit()
        if cursor.rowcount == 0:
            return False, "メンバーが見つかりません"
        warning = f"（未精算立替 {unsettled} 件あり）" if unsettled > 0 else ""
        return True, warning


async def member_exists(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT 1 FROM members WHERE name = ?", (name,)) as cursor:
            return await cursor.fetchone() is not None


# =============================================================================
# 立替
# =============================================================================

async def add_advance(paid_by: str, amount: int, description: str, entry_date: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "INSERT INTO advances (paid_by, amount, description, entry_date) VALUES (?, ?, ?, ?)",
            (paid_by, amount, description, entry_date),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_advances(settled: bool = False) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, paid_by, amount, description, entry_date FROM advances WHERE settled = ? ORDER BY entry_date, id",
            (1 if settled else 0,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def settle_advance(advance_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "UPDATE advances SET settled = 1 WHERE id = ? AND settled = 0", (advance_id,)
        )
        await conn.commit()
        return cursor.rowcount > 0


# =============================================================================
# 予算
# =============================================================================

async def get_yearly_summary(year: str) -> list[dict]:
    """年次集計: 各月の収益・費用・純利益を返す"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        months = [f"{year}-{m:02d}" for m in range(1, 13)]
        result = []
        for ym in months:
            async with conn.execute(
                """
                SELECT je.*, a_d.account_type AS debit_type, a_c.account_type AS credit_type
                FROM journal_entries je
                LEFT JOIN accounts a_d ON je.debit_account = a_d.name
                LEFT JOIN accounts a_c ON je.credit_account = a_c.name
                WHERE je.entry_date LIKE ?
                """,
                (ym + "%",),
            ) as cursor:
                entries = [dict(r) for r in await cursor.fetchall()]
            rev = sum(e["amount"] for e in entries if e["credit_type"] == "収益")
            exp = sum(e["amount"] for e in entries if e["debit_type"] == "費用")
            result.append({"month": ym, "revenue": rev, "expense": exp, "net": rev - exp})
        return result


async def get_period_comparison(period1: str, period2: str) -> dict:
    """2期間の収支比較。period: 'YYYY-MM' または 'YYYY'"""
    async def _summary(period: str) -> dict:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute(
                """
                SELECT je.*, a_d.account_type AS debit_type, a_c.account_type AS credit_type
                FROM journal_entries je
                LEFT JOIN accounts a_d ON je.debit_account = a_d.name
                LEFT JOIN accounts a_c ON je.credit_account = a_c.name
                WHERE je.entry_date LIKE ?
                """,
                (period + "%",),
            ) as cursor:
                entries = [dict(r) for r in await cursor.fetchall()]
        revenues: dict[str, int] = {}
        expenses: dict[str, int] = {}
        for e in entries:
            if e["credit_type"] == "収益":
                revenues[e["credit_account"]] = revenues.get(e["credit_account"], 0) + e["amount"]
            if e["debit_type"] == "費用":
                expenses[e["debit_account"]] = expenses.get(e["debit_account"], 0) + e["amount"]
        total_rev = sum(revenues.values())
        total_exp = sum(expenses.values())
        return {"period": period, "revenues": revenues, "expenses": expenses,
                "total_revenue": total_rev, "total_expense": total_exp, "net": total_rev - total_exp}

    s1 = await _summary(period1)
    s2 = await _summary(period2)
    return {"period1": s1, "period2": s2}


async def set_budget(account_name: str, period: str, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO budgets (account_name, period, budget_amount) VALUES (?, ?, ?)",
            (account_name, period, amount),
        )
        await conn.commit()


# =============================================================================
# ダッシュボード許可ユーザー管理
# =============================================================================

async def get_allowed_users() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT discord_user_id, display_name, added_by, added_at FROM allowed_users ORDER BY added_at"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def is_allowed_user(discord_user_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT 1 FROM allowed_users WHERE discord_user_id = ?", (discord_user_id,)
        ) as cursor:
            return await cursor.fetchone() is not None


async def add_allowed_user(discord_user_id: str, display_name: str, added_by: str) -> bool:
    """許可ユーザーを追加。すでに存在する場合は False を返す。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                "INSERT INTO allowed_users (discord_user_id, display_name, added_by) VALUES (?, ?, ?)",
                (discord_user_id, display_name, added_by),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_allowed_user(discord_user_id: str) -> bool:
    """許可ユーザーを削除。見つからない場合は False を返す。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "DELETE FROM allowed_users WHERE discord_user_id = ?", (discord_user_id,)
        )
        await conn.commit()
        return cursor.rowcount > 0


async def update_allowed_user_name(discord_user_id: str, display_name: str) -> None:
    """表示名を更新（ログイン時に呼ぶ）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE allowed_users SET display_name = ? WHERE discord_user_id = ?",
            (display_name, discord_user_id),
        )
        await conn.commit()


# =============================================================================
# グッズ在庫管理
# =============================================================================

async def add_goods(name: str, selling_price: int) -> bool:
    """グッズを登録する"""
    async with aiosqlite.connect(DB_PATH) as conn:
        try:
            await conn.execute(
                "INSERT INTO goods (name, selling_price) VALUES (?, ?)",
                (name, selling_price),
            )
            await conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_goods() -> list[dict]:
    """全グッズ一覧（在庫数・販売単価付き）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT id, name, selling_price, stock FROM goods ORDER BY name"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def goods_exists(name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT 1 FROM goods WHERE name = ?", (name,)) as cursor:
            return await cursor.fetchone() is not None


async def delete_goods(name: str) -> tuple[bool, str]:
    """グッズを削除する（取引履歴がある場合は拒否）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT COUNT(*) FROM goods_transactions WHERE goods_name = ?", (name,)
        ) as cursor:
            count = (await cursor.fetchone())[0]
        if count > 0:
            return False, f"取引履歴が {count} 件あるため削除できません"
        cursor = await conn.execute("DELETE FROM goods WHERE name = ?", (name,))
        await conn.commit()
        if cursor.rowcount == 0:
            return False, "グッズが見つかりません"
        return True, ""


async def record_goods_purchase(
    goods_name: str,
    quantity: int,
    unit_price: int,
    entry_date: str,
    description: str,
    journal_entry_id: int | None = None,
) -> int:
    """グッズ仕入を記録し在庫を増やす"""
    total = quantity * unit_price
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            """INSERT INTO goods_transactions
               (goods_name, tx_type, quantity, unit_price, total_amount, entry_date, description, journal_entry_id)
               VALUES (?, '仕入', ?, ?, ?, ?, ?, ?)""",
            (goods_name, quantity, unit_price, total, entry_date, description, journal_entry_id),
        )
        await conn.execute(
            "UPDATE goods SET stock = stock + ? WHERE name = ?", (quantity, goods_name)
        )
        await conn.commit()
        return cursor.lastrowid


async def record_goods_sale(
    goods_name: str,
    quantity: int,
    unit_price: int,
    entry_date: str,
    description: str,
    journal_entry_id: int | None = None,
) -> tuple[int, str]:
    """グッズ販売を記録し在庫を減らす。在庫不足なら (0, error_msg) を返す"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT stock FROM goods WHERE name = ?", (goods_name,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return 0, "グッズが見つかりません"
        if row["stock"] < quantity:
            return 0, f"在庫不足（現在庫: {row['stock']} 個）"

        total = quantity * unit_price
        cursor = await conn.execute(
            """INSERT INTO goods_transactions
               (goods_name, tx_type, quantity, unit_price, total_amount, entry_date, description, journal_entry_id)
               VALUES (?, '販売', ?, ?, ?, ?, ?, ?)""",
            (goods_name, quantity, unit_price, total, entry_date, description, journal_entry_id),
        )
        await conn.execute(
            "UPDATE goods SET stock = stock - ? WHERE name = ?", (quantity, goods_name)
        )
        await conn.commit()
        return cursor.lastrowid, ""


async def get_goods_transactions(goods_name: str | None = None, limit: int = 30) -> list[dict]:
    """グッズ取引履歴"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if goods_name:
            async with conn.execute(
                """SELECT * FROM goods_transactions WHERE goods_name = ?
                   ORDER BY entry_date DESC, id DESC LIMIT ?""",
                (goods_name, limit),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]
        else:
            async with conn.execute(
                "SELECT * FROM goods_transactions ORDER BY entry_date DESC, id DESC LIMIT ?",
                (limit,),
            ) as cursor:
                return [dict(r) for r in await cursor.fetchall()]


async def get_goods_inventory_summary() -> list[dict]:
    """全グッズの在庫状況サマリー（仕入総額・売上総額・在庫評価額）"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT name, selling_price, stock FROM goods ORDER BY name") as cursor:
            goods_list = [dict(r) for r in await cursor.fetchall()]

        result = []
        for g in goods_list:
            async with conn.execute(
                """SELECT
                     COALESCE(SUM(CASE WHEN tx_type='仕入' THEN total_amount ELSE 0 END), 0) AS total_purchase,
                     COALESCE(SUM(CASE WHEN tx_type='販売' THEN total_amount ELSE 0 END), 0) AS total_sales,
                     COALESCE(SUM(CASE WHEN tx_type='仕入' THEN quantity ELSE 0 END), 0) AS total_purchased,
                     COALESCE(SUM(CASE WHEN tx_type='販売' THEN quantity ELSE 0 END), 0) AS total_sold
                   FROM goods_transactions WHERE goods_name = ?""",
                (g["name"],),
            ) as cursor:
                stats = dict(await cursor.fetchone())
            avg_cost = (stats["total_purchase"] // stats["total_purchased"]) if stats["total_purchased"] > 0 else 0
            result.append({
                "name": g["name"],
                "selling_price": g["selling_price"],
                "stock": g["stock"],
                "total_purchased": stats["total_purchased"],
                "total_sold": stats["total_sold"],
                "total_purchase_amount": stats["total_purchase"],
                "total_sales_amount": stats["total_sales"],
                "inventory_value": avg_cost * g["stock"],
                "gross_profit": stats["total_sales"] - stats["total_purchase"],
            })
        return result


# =============================================================================
# 消費税サマリー
# =============================================================================

async def get_tax_summary(period: str | None = None) -> dict:
    """
    消費税対応仕訳の集計。
    period: 'YYYY' または 'YYYY-MM'（None で全期間）
    返す値はすべて税込金額ベース。tax_amount = amount * rate / (100 + rate)
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        where = "WHERE COALESCE(tax_rate, 0) > 0"
        params: list = []
        if period:
            where += " AND entry_date LIKE ?"
            params.append(period + "%")
        async with conn.execute(
            f"""SELECT je.*, a_d.account_type AS debit_type, a_c.account_type AS credit_type
                FROM journal_entries je
                LEFT JOIN accounts a_d ON je.debit_account = a_d.name
                LEFT JOIN accounts a_c ON je.credit_account = a_c.name
                {where} ORDER BY je.entry_date, je.id""",
            params,
        ) as cursor:
            entries = [dict(r) for r in await cursor.fetchall()]

    taxable_revenue = 0
    taxable_expense = 0
    collected_tax = 0   # 仮受消費税（売上に係る）
    paid_tax = 0        # 仮払消費税（仕入・経費に係る）
    details: list[dict] = []

    for e in entries:
        rate = e.get("tax_rate") or 0
        if rate == 0:
            continue
        tax_amt = int(e["amount"] * rate / (100 + rate))
        excl_amt = e["amount"] - tax_amt
        is_revenue = e["credit_type"] == "収益"
        is_expense = e["debit_type"] == "費用"
        if is_revenue:
            taxable_revenue += excl_amt
            collected_tax += tax_amt
        if is_expense:
            taxable_expense += excl_amt
            paid_tax += tax_amt
        details.append({
            "id": e["id"],
            "entry_date": e["entry_date"],
            "description": e["description"],
            "amount_incl": e["amount"],
            "tax_rate": rate,
            "tax_amount": tax_amt,
            "amount_excl": excl_amt,
            "type": "収益" if is_revenue else ("費用" if is_expense else "その他"),
        })

    return {
        "period": period or "全期間",
        "taxable_revenue": taxable_revenue,
        "taxable_expense": taxable_expense,
        "collected_tax": collected_tax,
        "paid_tax": paid_tax,
        "tax_payable": collected_tax - paid_tax,
        "details": details,
    }


# =============================================================================
# 確定申告サマリー
# =============================================================================

async def get_tax_return_summary(year: str) -> dict:
    """
    確定申告用年間サマリー。
    収入・経費を科目別に集計し、所得・推定納税額を返す。
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """SELECT je.*, a_d.account_type AS debit_type, a_c.account_type AS credit_type
               FROM journal_entries je
               LEFT JOIN accounts a_d ON je.debit_account = a_d.name
               LEFT JOIN accounts a_c ON je.credit_account = a_c.name
               WHERE je.entry_date LIKE ?""",
            (year + "%",),
        ) as cursor:
            entries = [dict(r) for r in await cursor.fetchall()]

    revenues: dict[str, int] = {}
    expenses: dict[str, int] = {}
    for e in entries:
        if e["credit_type"] == "収益":
            revenues[e["credit_account"]] = revenues.get(e["credit_account"], 0) + e["amount"]
        if e["debit_type"] == "費用":
            expenses[e["debit_account"]] = expenses.get(e["debit_account"], 0) + e["amount"]

    total_revenue = sum(revenues.values())
    total_expense = sum(expenses.values())
    gross_income = total_revenue - total_expense
    # 青色申告特別控除（簡易的に65万円を適用）
    blue_return_deduction = min(650000, max(0, gross_income))
    taxable_income = max(0, gross_income - blue_return_deduction)
    # 基礎控除 48万円
    basic_deduction = 480000
    taxable_after_deductions = max(0, taxable_income - basic_deduction)
    # 簡易累進税率（所得税）
    def calc_income_tax(income: int) -> int:
        brackets = [
            (1950000, 0.05, 0),
            (3300000, 0.10, 97500),
            (6950000, 0.20, 427500),
            (9000000, 0.23, 636000),
            (18000000, 0.33, 1536000),
            (40000000, 0.40, 2796000),
            (float("inf"), 0.45, 4796000),
        ]
        for limit, rate, deduction in brackets:
            if income <= limit:
                return int(income * rate - deduction)
        return 0
    income_tax = calc_income_tax(taxable_after_deductions)
    # 復興特別所得税 2.1%
    surtax = int(income_tax * 0.021)

    return {
        "year": year,
        "revenues": revenues,
        "expenses": expenses,
        "total_revenue": total_revenue,
        "total_expense": total_expense,
        "gross_income": gross_income,
        "blue_return_deduction": blue_return_deduction,
        "taxable_income": taxable_income,
        "basic_deduction": basic_deduction,
        "taxable_after_deductions": taxable_after_deductions,
        "income_tax": income_tax,
        "surtax": surtax,
        "total_tax": income_tax + surtax,
    }


# =============================================================================
# デモデータ投入
# =============================================================================

async def seed_demo_data() -> dict:
    """デモ用サンプルデータを投入する。重複は無視。"""
    from datetime import date as _date

    summary = {"events": [], "members": [], "goods": [], "journal_entries": 0}

    # イベント
    for ev in ["2025春ライブ@渋谷", "2025夏フェス出演", "2025冬ワンマン@新宿"]:
        ok = await create_event(ev)
        if ok:
            summary["events"].append(ev)

    # メンバー
    for m in ["田中（Vo）", "佐藤（Gt）", "鈴木（Ba）", "高橋（Dr）"]:
        ok = await add_member(m)
        if ok:
            summary["members"].append(m)

    # グッズ
    for name, price in [("Tシャツ", 3300), ("クリアファイル", 550), ("ステッカー", 330), ("CD", 1100)]:
        ok = await add_goods(name, price)
        if ok:
            summary["goods"].append(name)

    # 仕訳データ（消費税込みのものも含む）
    demo_entries = [
        # ライブ収益（現金の増加 ← 収益）
        ("2025-04-15", "現金",      "売上",          85000,  "2025春ライブ チケット売上",       "2025春ライブ@渋谷",  0),
        ("2025-04-15", "現金",      "グッズ売上",     42000,  "2025春ライブ グッズ販売",         "2025春ライブ@渋谷",  10),
        # 費用（費用科目 ← 現金）
        ("2025-04-10", "会場レンタル代", "現金",       30000,  "2025春ライブ 会場費",             "2025春ライブ@渋谷",  10),
        ("2025-04-10", "宣伝広告費",    "現金",        8000,  "フライヤー印刷代",                "2025春ライブ@渋谷",  10),
        # デジタル収益
        ("2025-05-01", "普通預金",  "Booth売上",      28600,  "Booth 4月分売上入金",             None, 10),
        ("2025-05-01", "普通預金",  "Fanbox売上",     15400,  "Fanbox 4月分支援入金",            None, 10),
        ("2025-05-20", "普通預金",  "ストリーミング収益", 3200, "Spotify/Apple 4月分",            None, 0),
        # スタジオ費用
        ("2025-05-10", "スタジオレンタル代", "現金",  12000,  "月例練習 スタジオ代",             None, 10),
        ("2025-06-10", "スタジオレンタル代", "現金",  12000,  "月例練習 スタジオ代",             None, 10),
        # 機材費用
        ("2025-06-01", "機材費",    "現金",           55000,  "ギターエフェクター購入",          None, 10),
        # 夏フェス
        ("2025-07-20", "普通預金",  "出演料",        100000,  "夏フェス 出演料",                 "2025夏フェス出演", 10),
        ("2025-07-15", "旅費交通費","現金",           22000,  "夏フェス 交通費（4名分）",        "2025夏フェス出演",  0),
        # 音源・映像制作費
        ("2025-08-01", "音源制作費","普通預金",      150000,  "1stミニアルバム レコーディング費", None, 10),
        ("2025-09-01", "MV制作費",  "普通預金",      200000,  "MV制作依頼",                      None, 10),
        # 冬ワンマン
        ("2025-12-20", "現金",      "売上",          120000,  "2025冬ワンマン チケット売上",     "2025冬ワンマン@新宿", 0),
        ("2025-12-20", "現金",      "グッズ売上",     63000,  "2025冬ワンマン グッズ販売",       "2025冬ワンマン@新宿", 10),
        ("2025-12-18", "会場レンタル代", "現金",      50000,  "2025冬ワンマン 会場費",           "2025冬ワンマン@新宿", 10),
        ("2025-12-10", "宣伝広告費","現金",           15000,  "SNS広告費",                       "2025冬ワンマン@新宿", 10),
        # グッズ仕入（資産増加 ← 現金）
        ("2025-03-01", "グッズ在庫","現金",           40000,  "Tシャツ 20枚 仕入",               None, 10),
        ("2025-03-01", "グッズ在庫","現金",            5500,  "ステッカー 50枚 仕入",             None, 10),
    ]

    count = 0
    async with aiosqlite.connect(DB_PATH) as conn:
        for entry_date, debit, credit, amount, desc, event_tag, tax_rate in demo_entries:
            await conn.execute(
                """INSERT INTO journal_entries
                   (entry_date, debit_account, credit_account, amount, description, event_tag, tax_rate)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entry_date, debit, credit, amount, desc, event_tag, tax_rate),
            )
            count += 1
        await conn.commit()
    summary["journal_entries"] = count

    # グッズ取引履歴
    async with aiosqlite.connect(DB_PATH) as conn:
        goods_txs = [
            ("Tシャツ", "仕入", 20, 2000, 40000, "2025-03-01", "初回仕入"),
            ("ステッカー", "仕入", 50, 110, 5500, "2025-03-01", "初回仕入"),
            ("Tシャツ", "販売", 8, 3300, 26400, "2025-04-15", "春ライブ販売"),
            ("ステッカー", "販売", 20, 330, 6600, "2025-04-15", "春ライブ販売"),
            ("Tシャツ", "販売", 5, 3300, 16500, "2025-12-20", "冬ワンマン販売"),
            ("ステッカー", "販売", 15, 330, 4950, "2025-12-20", "冬ワンマン販売"),
        ]
        for gname, tx_type, qty, unit, total, edate, desc in goods_txs:
            exists = await goods_exists(gname)
            if not exists:
                continue
            await conn.execute(
                """INSERT INTO goods_transactions
                   (goods_name, tx_type, quantity, unit_price, total_amount, entry_date, description)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (gname, tx_type, qty, unit, total, edate, desc),
            )
        # 在庫数を更新
        await conn.execute("UPDATE goods SET stock = 7 WHERE name = 'Tシャツ'")
        await conn.execute("UPDATE goods SET stock = 15 WHERE name = 'ステッカー'")
        await conn.commit()

    return summary


async def get_budget_vs_actual(period: str) -> list[dict]:
    """予算実績対比。period: 'YYYY-MM' または 'YYYY'"""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            """
            SELECT b.account_name, b.budget_amount, a.account_type
            FROM budgets b
            LEFT JOIN accounts a ON b.account_name = a.name
            WHERE b.period = ?
            ORDER BY a.account_type, b.account_name
            """,
            (period,),
        ) as cursor:
            budgets = [dict(r) for r in await cursor.fetchall()]

        like_pattern = period + "%"
        result = []
        for b in budgets:
            async with conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN debit_account  = ? THEN amount ELSE 0 END), 0) AS total_debit,
                    COALESCE(SUM(CASE WHEN credit_account = ? THEN amount ELSE 0 END), 0) AS total_credit
                FROM journal_entries
                WHERE entry_date LIKE ? AND (debit_account = ? OR credit_account = ?)
                """,
                (b["account_name"], b["account_name"], like_pattern, b["account_name"], b["account_name"]),
            ) as cursor:
                row = await cursor.fetchone()

            acc_type = b.get("account_type") or "費用"
            if acc_type in ("資産", "費用"):
                actual = (row["total_debit"] if row else 0) - (row["total_credit"] if row else 0)
            else:
                actual = (row["total_credit"] if row else 0) - (row["total_debit"] if row else 0)

            diff = b["budget_amount"] - actual
            ratio = actual / b["budget_amount"] * 100 if b["budget_amount"] > 0 else 0
            result.append({
                "account_name": b["account_name"],
                "account_type": acc_type,
                "budget": b["budget_amount"],
                "actual": actual,
                "diff": diff,
                "ratio": ratio,
            })
        return result
