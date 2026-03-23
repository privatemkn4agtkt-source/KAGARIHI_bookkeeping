"""
KAGARIHI 会計ダッシュボード — Discord OAuth2 認証付き

環境変数 (.env):
  DISCORD_CLIENT_ID      : Discord Application の Client ID
  DISCORD_CLIENT_SECRET  : Discord Application の Client Secret
  DISCORD_REDIRECT_URI   : コールバックURL (例: https://example.com/auth/callback)
  ALLOWED_DISCORD_IDS    : 閲覧を許可する Discord User ID (カンマ区切り)
  SESSION_SECRET         : セッション署名用シークレット (長いランダム文字列)
  DB_PATH                : DB ファイルパス (省略時: bookkeeping.db)
  DASHBOARD_PORT         : ポート番号 (省略時: 8000)

起動:
  python dashboard.py

Discord Application の設定:
  1. https://discord.com/developers/applications でアプリを選択
  2. OAuth2 → Redirects に DISCORD_REDIRECT_URI を追加
"""
import os
import secrets
import httpx
from datetime import date
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request, Query, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import database as db

load_dotenv()

# ─────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────
DB_PATH        = os.getenv("DB_PATH", "bookkeeping.db")
CLIENT_ID      = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET  = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI   = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8000/auth/callback")
SESSION_SECRET = os.getenv("SESSION_SECRET") or secrets.token_hex(32)
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
# ALLOWED_DISCORD_IDS は init_db() で DB に自動シードされるので env は直接参照しない

DISCORD_API    = "https://discord.com/api/v10"
DISCORD_OAUTH2 = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN  = "https://discord.com/api/oauth2/token"

db.DB_PATH = DB_PATH

# ─────────────────────────────────────────────────────────
# ナビゲーション構造定義
# ─────────────────────────────────────────────────────────

NAV_SECTIONS = [
    {
        "label": "概要",
        "items": [
            {"path": "/",       "icon": "🏠", "label": "ダッシュボード"},
        ],
    },
    {
        "label": "財務諸表",
        "items": [
            {"path": "/pl",    "icon": "📊", "label": "損益計算書"},
            {"path": "/bs",    "icon": "🏦", "label": "貸借対照表"},
            {"path": "/trial", "icon": "📋", "label": "試算表"},
        ],
    },
    {
        "label": "明細",
        "items": [
            {"path": "/journal",   "icon": "📒", "label": "仕訳帳"},
            {"path": "/events",    "icon": "🎸", "label": "ライブ収支"},
            {"path": "/goods",     "icon": "📦", "label": "グッズ在庫"},
            {"path": "/advances",  "icon": "💴", "label": "立替払い"},
        ],
    },
    {
        "label": "税務",
        "items": [
            {"path": "/tax",       "icon": "🧾", "label": "消費税サマリー"},
            {"path": "/taxreturn", "icon": "📋", "label": "確定申告サマリー"},
        ],
    },
    {
        "label": "管理",
        "items": [
            {"path": "/users", "icon": "👥", "label": "ユーザー管理"},
        ],
    },
]


async def sorted_nav_sections() -> list:
    """訪問回数が多い順にセクション内のアイテムをソートして返す"""
    visits = await db.get_page_visits()
    result = []
    for section in NAV_SECTIONS:
        sorted_items = sorted(
            section["items"],
            key=lambda x: -visits.get(x["path"], 0),
        )
        result.append({**section, "items": sorted_items})
    return result


# ─────────────────────────────────────────────────────────
# アプリ初期化
# ─────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    yield


app = FastAPI(title="KAGARIHI 会計ダッシュボード", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 24 * 7)
templates = Jinja2Templates(directory="templates")


def fmt(n: int) -> str:
    if n < 0:
        return f"▲{abs(n):,}円"
    return f"{n:,}円"


# ─────────────────────────────────────────────────────────
# 認証ヘルパー
# ─────────────────────────────────────────────────────────

def get_current_user(request: Request) -> dict | None:
    """セッションからログインユーザーを返す。未ログインなら None。"""
    return request.session.get("user")


from fastapi import HTTPException


async def auth_guard(request: Request) -> dict:
    """全保護ページに付ける依存関係"""
    user = get_current_user(request)
    if user is None:
        request.session["next"] = str(request.url)
        raise HTTPException(status_code=307, headers={"Location": "/auth/login"})
    # DBをリクエストごとに参照（再起動なしで許可変更が反映される）
    if not await db.is_allowed_user(user["id"]):
        raise HTTPException(status_code=307, headers={"Location": "/auth/denied"})
    return user


# ─────────────────────────────────────────────────────────
# 認証ルート
# ─────────────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
async def login(request: Request):
    """Discord の OAuth2 認証画面へリダイレクト"""
    if not CLIENT_ID or not CLIENT_SECRET:
        return HTMLResponse(
            "<h2>⚠️ DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET が未設定です。</h2>"
            "<p>.env ファイルを確認してください。</p>",
            status_code=500,
        )
    # CSRF 対策: state トークンをセッションに保存
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state

    params = urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    return RedirectResponse(f"{DISCORD_OAUTH2}?{params}", status_code=302)


@app.get("/auth/callback")
async def oauth_callback(request: Request, code: str = Query(None), state: str = Query(None), error: str = Query(None)):
    """Discord からのコールバック処理"""
    if error:
        return RedirectResponse("/auth/login?error=cancelled", status_code=302)

    # state 検証
    saved_state = request.session.pop("oauth_state", None)
    if not state or state != saved_state:
        return HTMLResponse("<h2>❌ 不正なリクエストです（state 不一致）</h2>", status_code=400)

    # アクセストークンを取得
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            DISCORD_TOKEN,
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if token_resp.status_code != 200:
        return HTMLResponse(f"<h2>❌ トークン取得失敗: {token_resp.text}</h2>", status_code=500)

    token_data = token_resp.json()
    access_token = token_data["access_token"]

    # ユーザー情報を取得
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if user_resp.status_code != 200:
        return HTMLResponse("<h2>❌ ユーザー情報取得失敗</h2>", status_code=500)

    user = user_resp.json()
    display_name = user.get("global_name") or user["username"]
    # セッションに必要な情報だけ保存
    request.session["user"] = {
        "id":          user["id"],
        "username":    user["username"],
        "global_name": display_name,
        "avatar":      user.get("avatar"),
    }

    # 許可チェック（DB参照）
    if not await db.is_allowed_user(user["id"]):
        return RedirectResponse("/auth/denied", status_code=302)

    # 許可済みなら表示名をDBに記録（名前変更対応）
    await db.update_allowed_user_name(user["id"], display_name)

    next_url = request.session.pop("next", "/")
    return RedirectResponse(next_url, status_code=302)


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=302)


@app.get("/auth/denied", response_class=HTMLResponse)
async def denied(request: Request):
    user = get_current_user(request)
    return templates.TemplateResponse("denied.html", {"request": request, "user": user})


# ─────────────────────────────────────────────────────────
# 保護されたページ（全ルートに user=Depends(auth_guard) を追加）
# ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/")
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
        "user": user,
        "monthly": monthly,
        "year": year,
        "total_rev": total_rev,
        "total_exp": total_exp,
        "net_year": net_year,
        "event_count": len(events),
        "goods_count": len(goods_list),
        "member_count": len(members),
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/pl", response_class=HTMLResponse)
async def profit_loss(request: Request, year: str = Query(default=None), user: dict = Depends(auth_guard)):
    await db.record_page_visit("/pl")
    if year is None:
        year = str(date.today().year)
    rows = await db.get_yearly_summary(year)

    import aiosqlite
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("""
            SELECT je.*, a_d.account_type AS dt, a_c.account_type AS ct
            FROM journal_entries je
            LEFT JOIN accounts a_d ON je.debit_account  = a_d.name
            LEFT JOIN accounts a_c ON je.credit_account = a_c.name
            WHERE je.entry_date LIKE ?
        """, (year + "%",)) as cur:
            entries = [dict(r) for r in await cur.fetchall()]

    rev = {}; exp = {}
    for e in entries:
        if e["ct"] == "収益": rev[e["credit_account"]] = rev.get(e["credit_account"], 0) + e["amount"]
        if e["dt"] == "費用": exp[e["debit_account"]]  = exp.get(e["debit_account"],  0) + e["amount"]

    total_rev = sum(rev.values())
    total_exp = sum(exp.values())

    return templates.TemplateResponse("pl.html", {
        "request": request, "user": user,
        "year": year,
        "revenues": sorted(rev.items(), key=lambda x: -x[1]),
        "expenses": sorted(exp.items(), key=lambda x: -x[1]),
        "total_rev": total_rev, "total_exp": total_exp,
        "net": total_rev - total_exp,
        "monthly_rows": rows, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/bs", response_class=HTMLResponse)
async def balance_sheet(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/bs")
    tb = await db.get_trial_balance()
    assets   = [r for r in tb if r["account_type"] == "資産"]
    liabs    = [r for r in tb if r["account_type"] == "負債"]
    equity   = [r for r in tb if r["account_type"] == "資本"]
    revenues = [r for r in tb if r["account_type"] == "収益"]
    expenses = [r for r in tb if r["account_type"] == "費用"]
    net_income  = sum(r["balance"] for r in revenues) - sum(r["balance"] for r in expenses)
    total_asset = sum(r["balance"] for r in assets)
    total_liab  = sum(r["balance"] for r in liabs)
    total_eq    = sum(r["balance"] for r in equity) + net_income

    return templates.TemplateResponse("bs.html", {
        "request": request, "user": user,
        "assets": assets, "liabs": liabs, "equity": equity,
        "net_income": net_income, "total_asset": total_asset,
        "total_liab": total_liab, "total_eq": total_eq, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/trial", response_class=HTMLResponse)
async def trial_balance(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/trial")
    rows = await db.get_trial_balance()
    return templates.TemplateResponse("trial.html", {
        "request": request, "user": user,
        "rows": rows,
        "total_debit":  sum(r["debit_total"]  for r in rows),
        "total_credit": sum(r["credit_total"] for r in rows),
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/journal", response_class=HTMLResponse)
async def journal(
    request: Request,
    start: str = Query(default=None), end: str = Query(default=None),
    account: str = Query(default=None), limit: int = Query(default=50),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/journal")
    limit = min(max(limit, 1), 200)
    entries = await db.get_journal_entries_filtered(start, end, account, limit)
    accounts = await db.get_accounts()
    return templates.TemplateResponse("journal.html", {
        "request": request, "user": user,
        "entries": entries, "accounts": accounts,
        "start": start or "", "end": end or "",
        "account": account or "", "limit": limit, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/events")
    event_names = await db.get_events()
    summaries = [await db.get_event_summary(ev) for ev in event_names]
    return templates.TemplateResponse("events.html", {
        "request": request, "user": user, "summaries": summaries, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/goods", response_class=HTMLResponse)
async def goods_page(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/goods")
    return templates.TemplateResponse("goods.html", {
        "request": request, "user": user,
        "inventory": await db.get_goods_inventory_summary(),
        "transactions": await db.get_goods_transactions(limit=50),
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/tax", response_class=HTMLResponse)
async def tax_page(request: Request, period: str = Query(default=None), user: dict = Depends(auth_guard)):
    await db.record_page_visit("/tax")
    if period is None:
        period = str(date.today().year)
    return templates.TemplateResponse("tax.html", {
        "request": request, "user": user,
        "result": await db.get_tax_summary(period),
        "period": period, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/taxreturn", response_class=HTMLResponse)
async def taxreturn_page(request: Request, year: str = Query(default=None), user: dict = Depends(auth_guard)):
    await db.record_page_visit("/taxreturn")
    if year is None:
        year = str(date.today().year)
    return templates.TemplateResponse("taxreturn.html", {
        "request": request, "user": user,
        "result": await db.get_tax_return_summary(year),
        "year": year, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/advances", response_class=HTMLResponse)
async def advances_page(
    request: Request,
    show_settled: str = Query(default="0"),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/advances")
    settled_flag = show_settled == "1"
    unsettled = await db.get_advances(settled=False)
    settled = await db.get_advances(settled=True) if settled_flag else []
    totals: dict[str, int] = {}
    for a in unsettled:
        totals[a["paid_by"]] = totals.get(a["paid_by"], 0) + a["amount"]
    return templates.TemplateResponse("advances.html", {
        "request": request, "user": user,
        "unsettled": unsettled,
        "settled": settled,
        "show_settled": settled_flag,
        "totals": sorted(totals.items(), key=lambda x: -x[1]),
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    msg: str = Query(default=""),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/users")
    allowed = await db.get_allowed_users()
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user,
        "allowed_users": allowed,
        "msg": msg,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/users/add")
async def users_add(
    request: Request,
    discord_id: str = Form(...),
    display_name: str = Form(default=""),
    user: dict = Depends(auth_guard),
):
    discord_id = discord_id.strip()
    if not discord_id.isdigit():
        return RedirectResponse("/users?msg=error_invalid_id", status_code=302)
    name = display_name.strip() or discord_id
    added_by = user.get("global_name", user.get("username", "dashboard"))
    ok = await db.add_allowed_user(discord_id, name, added_by)
    msg = "added" if ok else "already_exists"
    return RedirectResponse(f"/users?msg={msg}", status_code=302)


@app.post("/users/remove")
async def users_remove(
    request: Request,
    discord_id: str = Form(...),
    user: dict = Depends(auth_guard),
):
    discord_id = discord_id.strip()
    if discord_id == user["id"]:
        return RedirectResponse("/users?msg=error_self", status_code=302)
    ok = await db.remove_allowed_user(discord_id)
    msg = "removed" if ok else "not_found"
    return RedirectResponse(f"/users?msg={msg}", status_code=302)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=DASHBOARD_PORT, reload=False)
