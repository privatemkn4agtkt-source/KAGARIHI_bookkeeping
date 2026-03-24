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
import io
import os
import secrets
import httpx
from datetime import date, datetime
from contextlib import asynccontextmanager
from urllib.parse import urlencode

import aiosqlite as _aiosqlite
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Query, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
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
            {"path": "/pl",        "icon": "📊", "label": "損益計算書"},
            {"path": "/bs",        "icon": "🏦", "label": "貸借対照表"},
            {"path": "/trial",     "icon": "📋", "label": "試算表"},
            {"path": "/cashflow",  "icon": "💰", "label": "キャッシュフロー"},
            {"path": "/ledger",    "icon": "📖", "label": "総勘定元帳"},
            {"path": "/budget",    "icon": "🎯", "label": "予算実績対比"},
        ],
    },
    {
        "label": "集計",
        "items": [
            {"path": "/monthly", "icon": "📅", "label": "月次収支"},
            {"path": "/yearly",  "icon": "📆", "label": "年次集計"},
        ],
    },
    {
        "label": "明細",
        "items": [
            {"path": "/journal",   "icon": "📒", "label": "仕訳帳"},
            {"path": "/events",    "icon": "🎸", "label": "ライブ収支"},
            {"path": "/advances",  "icon": "💳", "label": "立替精算表"},
            {"path": "/goods",     "icon": "📦", "label": "グッズ在庫"},
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
            {"path": "/members",      "icon": "👥", "label": "メンバー管理"},
            {"path": "/accounts",     "icon": "📂", "label": "勘定科目一覧"},
            {"path": "/storage",      "icon": "💾", "label": "ストレージ確認"},
            {"path": "/permissions",  "icon": "🔑", "label": "許可管理"},
            {"path": "/export/excel", "icon": "📥", "label": "Excelダウンロード"},
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
    unsettled_advances = await db.get_advances(settled=False)
    advance_totals: dict[str, int] = {}
    for a in unsettled_advances:
        advance_totals[a["paid_by"]] = advance_totals.get(a["paid_by"], 0) + a["amount"]

    # 税関連残高（源泉徴収預かり金・仮受消費税・仮払消費税）
    _TAX_ACCOUNTS = {"源泉徴収預かり金", "仮受消費税", "仮払消費税"}
    tb = await db.get_trial_balance()
    tax_balances = {r["name"]: r["balance"] for r in tb if r["name"] in _TAX_ACCOUNTS}

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
        "unsettled_advances": unsettled_advances,
        "advance_totals": sorted(advance_totals.items(), key=lambda x: -x[1]),
        "tax_balances": tax_balances,
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
async def goods_page(request: Request, goods_name: str = Query(default=None), user: dict = Depends(auth_guard)):
    await db.record_page_visit("/goods")
    goods_list = await db.get_goods()
    return templates.TemplateResponse("goods.html", {
        "request": request, "user": user,
        "inventory": await db.get_goods_inventory_summary(),
        "transactions": await db.get_goods_transactions(goods_name=goods_name, limit=100),
        "goods_list": goods_list,
        "selected_goods": goods_name or "",
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


@app.get("/permissions", response_class=HTMLResponse)
async def permissions_page(
    request: Request,
    msg: str = Query(default=None),
    ok: int = Query(default=1),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/permissions")
    users = await db.get_allowed_users()
    return templates.TemplateResponse("permissions.html", {
        "request": request, "user": user,
        "users": users,
        "current_user_id": user["id"],
        "message": msg,
        "message_ok": ok == 1,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/permissions/add")
async def permissions_add(
    request: Request,
    discord_id: str = Form(...),
    display_name: str = Form(default=""),
    user: dict = Depends(auth_guard),
):
    discord_id = discord_id.strip()
    if not discord_id.isdigit():
        return RedirectResponse("/permissions?msg=Discord+IDは数字のみで入力してください&ok=0", status_code=303)
    name = display_name.strip() or discord_id
    added_by = f"{user['global_name']}（{user['id']}）"
    success = await db.add_allowed_user(discord_id, name, added_by)
    if success:
        return RedirectResponse(f"/permissions?msg=Discord+ID+{discord_id}+を追加しました&ok=1", status_code=303)
    return RedirectResponse(f"/permissions?msg=Discord+ID+{discord_id}+はすでに登録されています&ok=0", status_code=303)


@app.post("/permissions/remove")
async def permissions_remove(
    request: Request,
    discord_id: str = Form(...),
    user: dict = Depends(auth_guard),
):
    discord_id = discord_id.strip()
    if discord_id == user["id"]:
        return RedirectResponse("/permissions?msg=自分自身の権限は削除できません&ok=0", status_code=303)
    success = await db.remove_allowed_user(discord_id)
    if success:
        return RedirectResponse(f"/permissions?msg=Discord+ID+{discord_id}+を削除しました&ok=1", status_code=303)
    return RedirectResponse(f"/permissions?msg=Discord+ID+{discord_id}+が見つかりません&ok=0", status_code=303)


@app.get("/storage", response_class=HTMLResponse)
async def storage_page(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/storage")
    storage = await db.get_storage_info()
    return templates.TemplateResponse("storage.html", {
        "request": request, "user": user,
        "storage": storage,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/members", response_class=HTMLResponse)
async def members_page(
    request: Request,
    msg: str = Query(default=None),
    ok: int = Query(default=1),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/members")
    members = await db.get_members()
    unsettled_advances = await db.get_advances(settled=False)
    advance_totals: dict[str, int] = {}
    for a in unsettled_advances:
        advance_totals[a["paid_by"]] = advance_totals.get(a["paid_by"], 0) + a["amount"]
    return templates.TemplateResponse("members.html", {
        "request": request, "user": user,
        "members": members,
        "advance_totals": advance_totals,
        "message": msg,
        "message_ok": ok == 1,
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/members/add")
async def members_add(
    request: Request,
    name: str = Form(...),
    user: dict = Depends(auth_guard),
):
    name = name.strip()
    if not name:
        return RedirectResponse("/members?msg=名前を入力してください&ok=0", status_code=303)
    success = await db.add_member(name)
    if success:
        return RedirectResponse(f"/members?msg={name}+を追加しました&ok=1", status_code=303)
    return RedirectResponse(f"/members?msg={name}+はすでに登録されています&ok=0", status_code=303)


@app.post("/members/remove")
async def members_remove(
    request: Request,
    name: str = Form(...),
    user: dict = Depends(auth_guard),
):
    name = name.strip()
    success, warning = await db.delete_member(name)
    if success:
        msg = f"{name}+を削除しました{warning}"
        return RedirectResponse(f"/members?msg={msg}&ok=1", status_code=303)
    return RedirectResponse(f"/members?msg={name}+が見つかりません&ok=0", status_code=303)


@app.get("/monthly", response_class=HTMLResponse)
async def monthly_page(
    request: Request,
    ym: str = Query(default=None),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/monthly")
    if ym is None:
        ym = date.today().strftime("%Y-%m")
    result = await db.get_monthly_summary(ym)
    return templates.TemplateResponse("monthly.html", {
        "request": request, "user": user,
        "result": result, "ym": ym, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/ledger", response_class=HTMLResponse)
async def ledger_page(
    request: Request,
    account: str = Query(default=None),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/ledger")
    accounts = await db.get_accounts()
    rows = []
    if account:
        rows = await db.get_general_ledger(account)
    return templates.TemplateResponse("ledger.html", {
        "request": request, "user": user,
        "accounts": accounts, "account": account or "",
        "rows": rows, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/yearly", response_class=HTMLResponse)
async def yearly_page(
    request: Request,
    year: str = Query(default=None),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/yearly")
    if year is None:
        year = str(date.today().year)
    rows = await db.get_yearly_summary(year)
    total_rev = sum(r["revenue"] for r in rows)
    total_exp = sum(r["expense"] for r in rows)
    return templates.TemplateResponse("yearly.html", {
        "request": request, "user": user,
        "year": year, "rows": rows,
        "total_rev": total_rev, "total_exp": total_exp,
        "net": total_rev - total_exp,
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/budget", response_class=HTMLResponse)
async def budget_page(
    request: Request,
    period: str = Query(default=None),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/budget")
    if period is None:
        period = date.today().strftime("%Y-%m")
    rows = await db.get_budget_vs_actual(period)
    accounts = await db.get_accounts()
    return templates.TemplateResponse("budget.html", {
        "request": request, "user": user,
        "rows": rows, "period": period, "accounts": accounts, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/budget/set")
async def budget_set(
    request: Request,
    account_name: str = Form(...),
    period: str = Form(...),
    amount: int = Form(...),
    user: dict = Depends(auth_guard),
):
    if not await db.account_exists(account_name):
        return RedirectResponse(f"/budget?period={period}", status_code=303)
    await db.set_budget(account_name, period, amount)
    return RedirectResponse(f"/budget?period={period}", status_code=303)


@app.get("/cashflow", response_class=HTMLResponse)
async def cashflow_page(
    request: Request,
    period: str = Query(default=None),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/cashflow")
    if period is None:
        period = str(date.today().year)
    cf = await db.get_cash_flow(period)
    return templates.TemplateResponse("cashflow.html", {
        "request": request, "user": user,
        "cf": cf, "period": period, "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/advances", response_class=HTMLResponse)
async def advances_page(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/advances")
    advances = await db.get_advances(settled=False)
    totals: dict[str, int] = {}
    for a in advances:
        totals[a["paid_by"]] = totals.get(a["paid_by"], 0) + a["amount"]
    return templates.TemplateResponse("advances.html", {
        "request": request, "user": user,
        "advances": advances,
        "totals": sorted(totals.items(), key=lambda x: -x[1]),
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, user: dict = Depends(auth_guard)):
    await db.record_page_visit("/accounts")
    accounts = await db.get_accounts()
    return templates.TemplateResponse("accounts.html", {
        "request": request, "user": user,
        "accounts": accounts,
        "nav_sections": await sorted_nav_sections(),
    })


@app.get("/export/excel")
async def export_excel(request: Request, user: dict = Depends(auth_guard)):
    """全取引データを書式付き Excel ファイルとしてダウンロード"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return HTMLResponse("<h2>⚠️ openpyxl が未インストールです。update.sh を実行してください。</h2>", status_code=500)

    # ── スタイル定義 ──────────────────────────────────────────────
    def _fill(c): return PatternFill("solid", fgColor=c)
    def _border():
        s = Side(style="thin", color="BDBDBD")
        return Border(left=s, right=s, top=s, bottom=s)
    F_HEAD = Font(name="Meiryo UI", bold=True, color="FFFFFF", size=10)
    F_BODY = Font(name="Meiryo UI", size=9)
    A_C = Alignment(horizontal="center", vertical="center")
    A_L = Alignment(horizontal="left",   vertical="center")
    A_R = Alignment(horizontal="right",  vertical="center")

    def write_headers(ws, headers):
        for col, (label, width) in enumerate(headers, 1):
            c = ws.cell(row=1, column=col, value=label)
            c.font = F_HEAD; c.fill = _fill("1F3864")
            c.alignment = A_C; c.border = _border()
            ws.column_dimensions[get_column_letter(col)].width = width

    def style(ws, row, col, val, *, num=False, center=False, fill_color=None):
        c = ws.cell(row=row, column=col, value=val)
        c.font = F_BODY; c.border = _border()
        if fill_color: c.fill = _fill(fill_color)
        if num:    c.number_format = '#,##0'; c.alignment = A_R
        elif center: c.alignment = A_C
        else:        c.alignment = A_L

    wb = Workbook()
    wb.remove(wb.active)

    async with _aiosqlite.connect(DB_PATH) as con:
        con.row_factory = _aiosqlite.Row

        # ── 仕訳帳 ──────────────────────────────────────────────
        ws = wb.create_sheet("仕訳帳")
        ws.freeze_panes = "A2"
        write_headers(ws, [
            ("ID",5),("日付",12),("借方科目",16),("貸方科目",16),
            ("金額（税込）",14),("消費税率",10),("摘要",40),("イベント",16),("登録日時",18),
        ])
        async with con.execute("SELECT name, account_type FROM accounts") as cur:
            acct_type = {r["name"]: r["account_type"] for r in await cur.fetchall()}
        async with con.execute(
            "SELECT id,entry_date,debit_account,credit_account,amount,tax_rate,description,event_tag,created_at "
            "FROM journal_entries ORDER BY entry_date,id"
        ) as cur:
            for r, row in enumerate(await cur.fetchall(), 2):
                dt = acct_type.get(row["debit_account"],""); ct = acct_type.get(row["credit_account"],"")
                fc = "E8F5E9" if "収益" in (dt,ct) else "FFF3E0" if "費用" in (dt,ct) else None
                style(ws,r,1,row["id"],      center=True,  fill_color=fc)
                style(ws,r,2,row["entry_date"],            fill_color=fc)
                style(ws,r,3,row["debit_account"],         fill_color=fc)
                style(ws,r,4,row["credit_account"],        fill_color=fc)
                style(ws,r,5,row["amount"],  num=True,     fill_color=fc)
                style(ws,r,6,f'{row["tax_rate"]}%' if row["tax_rate"] else "0%", center=True, fill_color=fc)
                style(ws,r,7,row["description"],           fill_color=fc)
                style(ws,r,8,row["event_tag"] or "",       fill_color=fc)
                style(ws,r,9,row["created_at"],            fill_color=fc)
        ws.auto_filter.ref = "A1:I1"

        # ── 立替精算 ─────────────────────────────────────────────
        ws = wb.create_sheet("立替精算")
        ws.freeze_panes = "A2"
        write_headers(ws, [("ID",5),("日付",12),("立替者",14),("金額",12),("内容",40),("精算状況",10),("登録日時",18)])
        async with con.execute(
            "SELECT id,entry_date,paid_by,amount,description,settled,created_at FROM advances ORDER BY entry_date,id"
        ) as cur:
            for r, row in enumerate(await cur.fetchall(), 2):
                fc = "F5F5F5" if row["settled"] else None
                style(ws,r,1,row["id"],      center=True, fill_color=fc)
                style(ws,r,2,row["entry_date"],           fill_color=fc)
                style(ws,r,3,row["paid_by"],              fill_color=fc)
                style(ws,r,4,row["amount"],  num=True,    fill_color=fc)
                style(ws,r,5,row["description"],          fill_color=fc)
                style(ws,r,6,"精算済" if row["settled"] else "未精算", center=True, fill_color=fc)
                style(ws,r,7,row["created_at"],           fill_color=fc)

        # ── グッズ取引 ───────────────────────────────────────────
        ws = wb.create_sheet("グッズ取引")
        ws.freeze_panes = "A2"
        write_headers(ws, [("ID",5),("日付",12),("グッズ名",20),("種別",8),("数量",8),("単価",12),("合計金額",14),("摘要",30),("登録日時",18)])
        async with con.execute(
            "SELECT id,entry_date,goods_name,tx_type,quantity,unit_price,total_amount,description,created_at "
            "FROM goods_transactions ORDER BY entry_date,id"
        ) as cur:
            for r, row in enumerate(await cur.fetchall(), 2):
                fc = "E8F5E9" if row["tx_type"] == "販売" else "FFF3E0"
                style(ws,r,1,row["id"],          center=True, fill_color=fc)
                style(ws,r,2,row["entry_date"],              fill_color=fc)
                style(ws,r,3,row["goods_name"],              fill_color=fc)
                style(ws,r,4,row["tx_type"],     center=True, fill_color=fc)
                style(ws,r,5,row["quantity"],    center=True, fill_color=fc)
                style(ws,r,6,row["unit_price"],  num=True,    fill_color=fc)
                style(ws,r,7,row["total_amount"],num=True,    fill_color=fc)
                style(ws,r,8,row["description"],             fill_color=fc)
                style(ws,r,9,row["created_at"],              fill_color=fc)

        # ── グッズ在庫 ───────────────────────────────────────────
        ws = wb.create_sheet("グッズ在庫")
        ws.freeze_panes = "A2"
        write_headers(ws, [("グッズ名",24),("販売単価",12),("現在庫数",10),("在庫評価額",14),("登録日時",18)])
        async with con.execute("SELECT name,selling_price,stock,created_at FROM goods ORDER BY name") as cur:
            for r, row in enumerate(await cur.fetchall(), 2):
                style(ws,r,1,row["name"])
                style(ws,r,2,row["selling_price"], num=True)
                style(ws,r,3,row["stock"],         center=True)
                style(ws,r,4,row["selling_price"]*row["stock"], num=True)
                style(ws,r,5,row["created_at"])

        # ── 勘定科目 ─────────────────────────────────────────────
        ws = wb.create_sheet("勘定科目")
        ws.freeze_panes = "A2"
        write_headers(ws, [("ID",5),("勘定科目名",20),("種別",10)])
        type_colors = {"資産":"E3F2FD","負債":"FCE4EC","資本":"F3E5F5","収益":"E8F5E9","費用":"FFF3E0"}
        async with con.execute("SELECT id,name,account_type FROM accounts ORDER BY account_type,id") as cur:
            for r, row in enumerate(await cur.fetchall(), 2):
                fc = type_colors.get(row["account_type"])
                style(ws,r,1,row["id"],           center=True, fill_color=fc)
                style(ws,r,2,row["name"],                      fill_color=fc)
                style(ws,r,3,row["account_type"], center=True, fill_color=fc)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from urllib.parse import quote
    today = datetime.now().strftime("%Y%m%d")
    filename = f"会計データ_{today}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("dashboard:app", host="0.0.0.0", port=DASHBOARD_PORT, reload=False, proxy_headers=True, forwarded_allow_ips="*")
