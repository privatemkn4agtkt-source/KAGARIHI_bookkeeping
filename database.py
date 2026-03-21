import aiosqlite
import os
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
    ("受取利息", "収益"),
    ("仕入", "費用"),
    ("給料", "費用"),
    ("旅費交通費", "費用"),
    ("通信費", "費用"),
    ("消耗品費", "費用"),
    ("買掛金", "負債"),
    ("未払金", "負債"),
    ("資本金", "資本"),
    ("繰越利益剰余金", "資本"),
]


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
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # デフォルト勘定科目を投入
        for name, acc_type in DEFAULT_ACCOUNTS:
            await db.execute(
                "INSERT OR IGNORE INTO accounts (name, account_type) VALUES (?, ?)",
                (name, acc_type),
            )
        await db.commit()


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


async def add_journal_entry(
    entry_date: str,
    debit_account: str,
    credit_account: str,
    amount: int,
    description: str,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO journal_entries
               (entry_date, debit_account, credit_account, amount, description)
               VALUES (?, ?, ?, ?, ?)""",
            (entry_date, debit_account, credit_account, amount, description),
        )
        await db.commit()
        return cursor.lastrowid


async def get_journal_entries(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT id, entry_date, debit_account, credit_account, amount, description
               FROM journal_entries ORDER BY entry_date DESC, id DESC LIMIT ?""",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]


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
                # 残高の計算: 資産・費用は借方残, 負債・資本・収益は貸方残
                if acc_type in ("資産", "費用"):
                    balance = debit - credit
                else:
                    balance = credit - debit
                row["balance"] = balance
                result.append(row)
            return result


async def delete_journal_entry(entry_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM journal_entries WHERE id = ?", (entry_id,)
        )
        await db.commit()
        return cursor.rowcount > 0
