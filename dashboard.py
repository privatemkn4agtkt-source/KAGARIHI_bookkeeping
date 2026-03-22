"""
簡易ダッシュボード: bookkeeping.db の内容をブラウザで閲覧できます。
使い方: python dashboard.py  →  http://localhost:8000 を開く
"""
import os
import asyncio
from datetime import date
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database as db

# DB_PATH を環境変数または既定パスから取得
DB_PATH = os.getenv("DB_PATH", "bookkeeping.db")
db.DB_PATH = DB_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="KAGARIHI 会計ダッシュボード", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


def fmt(n: int) -> str:
    if n < 0:
        return f"▲{abs(n):,}円"
    return f"{n:,}円"


# ─────────────────────────────────────────────────────────
# ルーティング
# ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # トップ: 損益サマリー + 今月収支
    today = date.today()
    ym = today.strftime("%Y-%m")
    year = str(today.year)

    monthly = await db.get_monthly_summary(ym)
    yearly_rows = await db.get_yearly_summary(year)
    total_rev = sum(r["revenue"] for r in yearly_rows)
    total_exp = sum(r["expense"] for r in yearly_rows)
    net_year = total_rev - total_exp

    events = await db.get_events()
    goods_list = await db.get_goods()
    members = await db.get_members()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "monthly": monthly,
        "year": year,
        "total_rev": total_rev,
        "total_exp": total_exp,
        "net_year": net_year,
        "event_count": len(events),
        "goods_count": len(goods_list),
        "member_count": len(members),
        "fmt": fmt,
    })


@app.get("/pl", response_class=HTMLResponse)
async def profit_loss(request: Request, year: str = Query(default=None)):
    if year is None:
        year = str(date.today().year)
    rows = await db.get_yearly_summary(year)

    import aiosqlite
    period_filter = year + "%"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT je.*, a_d.account_type AS dt, a_c.account_type AS ct
            FROM journal_entries je
            LEFT JOIN accounts a_d ON je.debit_account  = a_d.name
            LEFT JOIN accounts a_c ON je.credit_account = a_c.name
            WHERE je.entry_date LIKE ?
        """, (period_filter,)) as cur:
            entries = [dict(r) for r in await cur.fetchall()]

    rev = {}; exp = {}
    for e in entries:
        if e["ct"] == "収益": rev[e["credit_account"]] = rev.get(e["credit_account"], 0) + e["amount"]
        if e["dt"] == "費用": exp[e["debit_account"]]  = exp.get(e["debit_account"],  0) + e["amount"]

    total_rev = sum(rev.values())
    total_exp = sum(exp.values())
    net = total_rev - total_exp

    return templates.TemplateResponse("pl.html", {
        "request": request,
        "year": year,
        "revenues": sorted(rev.items(), key=lambda x: -x[1]),
        "expenses": sorted(exp.items(), key=lambda x: -x[1]),
        "total_rev": total_rev,
        "total_exp": total_exp,
        "net": net,
        "monthly_rows": rows,
        "fmt": fmt,
    })


@app.get("/bs", response_class=HTMLResponse)
async def balance_sheet(request: Request):
    tb = await db.get_trial_balance()
    assets    = [r for r in tb if r["account_type"] == "資産"]
    liabs     = [r for r in tb if r["account_type"] == "負債"]
    equity    = [r for r in tb if r["account_type"] == "資本"]
    revenues  = [r for r in tb if r["account_type"] == "収益"]
    expenses  = [r for r in tb if r["account_type"] == "費用"]

    net_income = sum(r["balance"] for r in revenues) - sum(r["balance"] for r in expenses)
    total_asset = sum(r["balance"] for r in assets)
    total_liab  = sum(r["balance"] for r in liabs)
    total_eq    = sum(r["balance"] for r in equity) + net_income

    return templates.TemplateResponse("bs.html", {
        "request": request,
        "assets": assets,
        "liabs": liabs,
        "equity": equity,
        "net_income": net_income,
        "total_asset": total_asset,
        "total_liab": total_liab,
        "total_eq": total_eq,
        "fmt": fmt,
    })


@app.get("/journal", response_class=HTMLResponse)
async def journal(
    request: Request,
    start: str = Query(default=None),
    end: str = Query(default=None),
    account: str = Query(default=None),
    limit: int = Query(default=50),
):
    limit = min(max(limit, 1), 200)
    entries = await db.get_journal_entries_filtered(start, end, account, limit)
    accounts = await db.get_accounts()
    return templates.TemplateResponse("journal.html", {
        "request": request,
        "entries": entries,
        "accounts": accounts,
        "start": start or "",
        "end": end or "",
        "account": account or "",
        "limit": limit,
        "fmt": fmt,
    })


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request):
    event_names = await db.get_events()
    summaries = []
    for ev in event_names:
        s = await db.get_event_summary(ev)
        summaries.append(s)
    return templates.TemplateResponse("events.html", {
        "request": request,
        "summaries": summaries,
        "fmt": fmt,
    })


@app.get("/goods", response_class=HTMLResponse)
async def goods_page(request: Request):
    inv = await db.get_goods_inventory_summary()
    txs = await db.get_goods_transactions(limit=50)
    return templates.TemplateResponse("goods.html", {
        "request": request,
        "inventory": inv,
        "transactions": txs,
        "fmt": fmt,
    })


@app.get("/tax", response_class=HTMLResponse)
async def tax_page(request: Request, period: str = Query(default=None)):
    if period is None:
        period = str(date.today().year)
    result = await db.get_tax_summary(period)
    return templates.TemplateResponse("tax.html", {
        "request": request,
        "result": result,
        "period": period,
        "fmt": fmt,
    })


@app.get("/taxreturn", response_class=HTMLResponse)
async def taxreturn_page(request: Request, year: str = Query(default=None)):
    if year is None:
        year = str(date.today().year)
    result = await db.get_tax_return_summary(year)
    return templates.TemplateResponse("taxreturn.html", {
        "request": request,
        "result": result,
        "year": year,
        "fmt": fmt,
    })


@app.get("/trial", response_class=HTMLResponse)
async def trial_balance(request: Request):
    rows = await db.get_trial_balance()
    total_debit  = sum(r["debit_total"]  for r in rows)
    total_credit = sum(r["credit_total"] for r in rows)
    return templates.TemplateResponse("trial.html", {
        "request": request,
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "fmt": fmt,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000, reload=True)
