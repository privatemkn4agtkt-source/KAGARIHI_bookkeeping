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
    ("現金", "資産"),
    ("普通預金", "資産"),
    ("売掛金", "資産"),
    ("売上", "収益"),
    ("旅費交通費", "費用"),
    ("消耗品費", "費用"),
    ("スタジオレンタル代", "費用"),
    ("会場レンタル代", "費用"),
    ("買掛金", "負債"),
    ("未払金", "負債"),
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
            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                period TEXT NOT NULL,
                budget_amount INTEGER NOT NULL,
                UNIQUE(account_name, period)
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
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO journal_entries
               (entry_date, debit_account, credit_account, amount, description, event_tag)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entry_date, debit_account, credit_account, amount, description, event_tag),
        )
        await db.commit()
        return cursor.lastrowid


async def get_journal_entries(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, entry_date, debit_account, credit_account, amount, description, event_tag
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

    res = {k: 0 for k in (
        "operating_in", "operating_out",
        "investing_in", "investing_out",
        "financing_in", "financing_out",
    )}
    for e in entries:
        d_cash = e["debit_account"] in CASH_ACCOUNTS
        c_cash = e["credit_account"] in CASH_ACCOUNTS
        if d_cash and c_cash:
            continue  # 現金間移動は除外
        if d_cash:
            cat = classify(e["credit_account"], e["credit_type"] or "")
            res[f"{cat}_in"] += e["amount"]
        if c_cash:
            cat = classify(e["debit_account"], e["debit_type"] or "")
            res[f"{cat}_out"] += e["amount"]

    res["operating_net"] = res["operating_in"] - res["operating_out"]
    res["investing_net"] = res["investing_in"] - res["investing_out"]
    res["financing_net"] = res["financing_in"] - res["financing_out"]
    res["net_change"] = res["operating_net"] + res["investing_net"] + res["financing_net"]
    return res


# =============================================================================
# メンバー
# =============================================================================

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

async def set_budget(account_name: str, period: str, amount: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO budgets (account_name, period, budget_amount) VALUES (?, ?, ?)",
            (account_name, period, amount),
        )
        await conn.commit()


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
