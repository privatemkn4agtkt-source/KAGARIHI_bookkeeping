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
import time as _time
import httpx
from collections import defaultdict as _defaultdict
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
        "label": "明細",
        "items": [
            {"path": "/journal",   "icon": "📒", "label": "仕訳帳"},
            {"path": "/events",    "icon": "🎸", "label": "ライブ収支"},
            {"path": "/advances",  "icon": "💳", "label": "立替精算表"},
            {"path": "/goods",     "icon": "📦", "label": "グッズ在庫"},
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
        "label": "税務",
        "items": [
            {"path": "/taxreturn", "icon": "📋", "label": "確定申告サマリー"},
        ],
    },
    {
        "label": "管理",
        "items": [
            {"path": "/members",      "icon": "👥", "label": "メンバー管理"},
            {"path": "/accounts",     "icon": "📂", "label": "勘定科目一覧"},
            {"path": "/storage",      "icon": "💾", "label": "ストレージ確認"},
            {"path": "/permissions",        "icon": "🔑", "label": "許可管理"},
            {"path": "/admin/email-users",  "icon": "✉️",  "label": "メールユーザー"},
            {"path": "/export/excel",       "icon": "📥", "label": "Excelダウンロード"},
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


async def get_accounts_grouped() -> tuple[list[dict], list[dict]]:
    """よく使う科目とその他に分けて返す"""
    all_accounts = await db.get_accounts_with_usage()
    common_set = dict.fromkeys(db.COMMON_ACCOUNTS)  # 順序付き集合として利用
    common = [a for a in sorted(all_accounts, key=lambda a: list(common_set).index(a["name"]) if a["name"] in common_set else 999) if a["name"] in common_set]
    others = [a for a in all_accounts if a["name"] not in common_set]
    return common, others


def fmt(n: int) -> str:
    if n < 0:
        return f"▲{abs(n):,}円"
    return f"{n:,}円"


# ─────────────────────────────────────────────────────────
# ログイン試行レート制限
# ─────────────────────────────────────────────────────────

_LOGIN_MAX_ATTEMPTS = 5    # 同一IPで失敗できる最大回数
_LOGIN_WINDOW_SEC   = 900  # 集計ウィンドウ（秒）= 15分

_login_failures: dict[str, list[float]] = _defaultdict(list)  # IP → タイムスタンプ


def _prune(ip: str) -> None:
    cutoff = _time.monotonic() - _LOGIN_WINDOW_SEC
    _login_failures[ip] = [t for t in _login_failures[ip] if t > cutoff]


def _is_rate_limited(ip: str) -> tuple[bool, int]:
    """(ブロック中か, 解除まで残り分) を返す"""
    _prune(ip)
    fails = _login_failures[ip]
    if len(fails) >= _LOGIN_MAX_ATTEMPTS:
        wait_sec = int(_LOGIN_WINDOW_SEC - (_time.monotonic() - fails[0])) + 1
        return True, max((wait_sec + 59) // 60, 1)
    return False, 0


def _record_failure(ip: str) -> None:
    _prune(ip)
    _login_failures[ip].append(_time.monotonic())


def _clear_failures(ip: str) -> None:
    _login_failures.pop(ip, None)


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
    # メール認証ユーザーは常に許可（管理者が追加した信頼済みアカウント）
    if user.get("auth_type") == "email":
        return user
    # Discord ユーザーは許可リストを参照
    if not await db.is_allowed_user(user["id"]):
        raise HTTPException(status_code=307, headers={"Location": "/auth/denied"})
    return user


# ─────────────────────────────────────────────────────────
# 認証ルート
# ─────────────────────────────────────────────────────────

@app.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = Query(default=""), wait: int = Query(default=0)):
    """ログインページを表示（Discord + メール両方のフォーム）"""
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "wait": wait,
        "discord_available": bool(CLIENT_ID and CLIENT_SECRET),
    })


@app.get("/auth/discord")
async def discord_login(request: Request):
    """Discord OAuth2 フローを開始"""
    if not CLIENT_ID or not CLIENT_SECRET:
        return HTMLResponse(
            "<h2>⚠️ DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET が未設定です。</h2>"
            "<p>.env ファイルを確認してください。</p>",
            status_code=500,
        )
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


@app.post("/auth/email-login")
async def email_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    ip = request.client.host if request.client else "unknown"

    blocked, wait_min = _is_rate_limited(ip)
    if blocked:
        return RedirectResponse(f"/auth/login?error=locked&wait={wait_min}", status_code=303)

    local_user = await db.get_local_user_by_email(email)
    if not local_user or not db.verify_password(password, local_user["password_hash"]):
        _record_failure(ip)
        return RedirectResponse("/auth/login?error=invalid", status_code=303)

    _clear_failures(ip)
    request.session["user"] = {
        "id":          f"email:{local_user['email']}",
        "username":    local_user["email"],
        "global_name": local_user["display_name"],
        "avatar":      None,
        "auth_type":   "email",
    }
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


@app.get("/admin/email-users", response_class=HTMLResponse)
async def email_users_page(request: Request, msg: str = Query(default=""), ok: int = Query(default=1), user: dict = Depends(auth_guard)):
    users = await db.get_local_users()
    return templates.TemplateResponse("email_users.html", {
        "request": request, "user": user,
        "local_users": users,
        "message": msg, "message_ok": ok == 1,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/admin/email-users/add")
async def email_user_add(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    user: dict = Depends(auth_guard),
):
    ok, err = await db.create_local_user(email, password, display_name)
    if ok:
        return RedirectResponse(f"/admin/email-users?msg={display_name}+を追加しました&ok=1", status_code=303)
    return RedirectResponse(f"/admin/email-users?msg={err}&ok=0", status_code=303)


@app.post("/admin/email-users/{user_id}/delete")
async def email_user_delete(
    user_id: int,
    request: Request,
    user: dict = Depends(auth_guard),
):
    await db.delete_local_user(user_id)
    return RedirectResponse("/admin/email-users?msg=削除しました&ok=1", status_code=303)


# ─────────────────────────────────────────────────────────
# 保護されたページ（全ルートに user=Depends(auth_guard) を追加）
# ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: dict = Depends(auth_guard), saved: str = Query(default=None), added: int = Query(0), error_msg: str = Query(default="")):
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

    accounts = await db.get_accounts()
    accounts_common, accounts_other = await get_accounts_grouped()

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
        "accounts": accounts,
        "accounts_common": accounts_common,
        "accounts_other": accounts_other,
        "events": events,
        "members": members,
        "today": today.isoformat(),
        "saved": saved,
        "added": added,
        "error_msg": error_msg,
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/journal/add")
async def journal_add(
    request: Request,
    user: dict = Depends(auth_guard),
    entry_date: str = Form(...),
    debit_account: str = Form(...),
    credit_account: str = Form(...),
    amount: int = Form(...),
    description: str = Form(default=""),
    event_tag: str = Form(default=""),
    tax_rate: int = Form(default=0),
):
    if amount < 0 or (amount == 0 and debit_account != "グッズ在庫"):
        return RedirectResponse("/?error_msg=金額は1円以上で入力してください", status_code=303)
    if credit_account in db.CASH_ACCOUNTS:
        balance = await db.get_account_balance(credit_account)
        if balance - amount < 0:
            from urllib.parse import quote
            msg = quote(f"{credit_account}の残高が不足しています（現在残高: {balance:,}円、引落予定: {amount:,}円）")
            return RedirectResponse(f"/?error_msg={msg}", status_code=303)
    await db.add_journal_entry(
        entry_date=entry_date,
        debit_account=debit_account,
        credit_account=credit_account,
        amount=amount,
        description=description,
        event_tag=event_tag or None,
        tax_rate=tax_rate,
    )
    return RedirectResponse("/?saved=1", status_code=303)


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
    deleted: int = Query(default=0), updated: int = Query(default=0),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/journal")
    limit = min(max(limit, 1), 200)
    entries = await db.get_journal_entries_filtered(start, end, account, limit)
    accounts = await db.get_accounts()
    accounts_common, accounts_other = await get_accounts_grouped()
    return templates.TemplateResponse("journal.html", {
        "request": request, "user": user,
        "entries": entries, "accounts": accounts,
        "accounts_common": accounts_common,
        "accounts_other": accounts_other,
        "start": start or "", "end": end or "",
        "account": account or "", "limit": limit, "fmt": fmt,
        "deleted": deleted, "updated": updated,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/journal/{entry_id}/delete")
async def journal_delete(
    entry_id: int,
    request: Request,
    user: dict = Depends(auth_guard),
):
    await db.delete_journal_entry(entry_id)
    return RedirectResponse("/journal?deleted=1", status_code=303)


@app.post("/journal/{entry_id}/update")
async def journal_update(
    entry_id: int,
    request: Request,
    entry_date: str = Form(...),
    debit_account: str = Form(...),
    credit_account: str = Form(...),
    amount: int = Form(...),
    description: str = Form(default=""),
    event_tag: str = Form(default=""),
    user: dict = Depends(auth_guard),
):
    await db.update_journal_entry(
        entry_id, entry_date, debit_account, credit_account,
        amount, description, event_tag or None,
    )
    return RedirectResponse("/journal?updated=1", status_code=303)


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
async def advances_page(
    request: Request,
    settled: int = Query(0),
    error_msg: str = Query(default=""),
    user: dict = Depends(auth_guard),
):
    await db.record_page_visit("/advances")
    advances = await db.get_advances(settled=False)
    totals: dict[str, int] = {}
    for a in advances:
        totals[a["paid_by"]] = totals.get(a["paid_by"], 0) + a["amount"]
    accounts = await db.get_accounts()
    flash_msg = ""
    flash_ok = True
    if settled:
        flash_msg = "精算済みにしました"
    elif error_msg:
        flash_msg = error_msg
        flash_ok = False
    return templates.TemplateResponse("advances.html", {
        "request": request, "user": user,
        "advances": advances,
        "totals": sorted(totals.items(), key=lambda x: -x[1]),
        "flash_msg": flash_msg,
        "flash_ok": flash_ok,
        "accounts": accounts,
        "fmt": fmt,
        "nav_sections": await sorted_nav_sections(),
    })


@app.post("/advances/add")
async def advance_add(
    request: Request,
    paid_by: str = Form(...),
    amount: int = Form(...),
    description: str = Form(...),
    entry_date: str = Form(...),
    user: dict = Depends(auth_guard),
):
    from urllib.parse import quote
    if amount <= 0:
        msg = quote("金額は1円以上で入力してください")
        return RedirectResponse(f"/?error_msg={msg}", status_code=303)
    await db.add_advance(paid_by=paid_by, amount=amount, description=description, entry_date=entry_date)
    return RedirectResponse("/?added=1", status_code=303)


@app.post("/advances/{advance_id}/settle")
async def settle_advance(
    advance_id: int,
    request: Request,
    redirect_to: str = Form("/advances"),
    settle_date: str = Form(...),
    debit_account: str = Form("未払金"),
    credit_account: str = Form("現金"),
    user: dict = Depends(auth_guard),
):
    advance = await db.get_advance_by_id(advance_id)
    if not advance:
        return RedirectResponse(f"{redirect_to}?error_msg=立替が見つかりません", status_code=303)
    if credit_account in db.CASH_ACCOUNTS:
        balance = await db.get_account_balance(credit_account)
        if balance - advance["amount"] < 0:
            from urllib.parse import quote
            msg = quote(f"{credit_account}の残高が不足しています（現在残高: {balance:,}円）")
            sep = "&" if "?" in redirect_to else "?"
            return RedirectResponse(f"{redirect_to}{sep}error_msg={msg}", status_code=303)
    success = await db.settle_advance(advance_id)
    if not success:
        sep = "&" if "?" in redirect_to else "?"
        return RedirectResponse(f"{redirect_to}{sep}error_msg=精算処理に失敗しました", status_code=303)
    await db.add_journal_entry(
        settle_date, debit_account, credit_account,
        advance["amount"],
        f"【立替#{advance_id:04d}精算】{advance['description']}",
    )
    sep = "&" if "?" in redirect_to else "?"
    return RedirectResponse(f"{redirect_to}{sep}settled=1", status_code=303)


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

    today_str = datetime.now().strftime("%Y-%m-%d")
    year_str  = datetime.now().strftime("%Y")

    # ── 財務諸表データを事前取得（既存DBモジュールを再利用） ─────
    tb_rows   = await db.get_trial_balance()
    yearly    = await db.get_yearly_summary(year_str)
    cf        = await db.get_cash_flow(year_str)

    # ── 財務諸表シート共通スタイル補助関数 ──────────────────────
    def _fill(c): return PatternFill("solid", fgColor=c)
    def _border():
        s = Side(style="thin", color="BDBDBD")
        return Border(left=s, right=s, top=s, bottom=s)
    def _thick_bottom():
        thin = Side(style="thin",   color="BDBDBD")
        thk  = Side(style="medium", color="1F3864")
        return Border(left=thin, right=thin, top=thin, bottom=thk)
    F_HEAD  = Font(name="Meiryo UI", bold=True, color="FFFFFF", size=10)
    F_TITLE = Font(name="Meiryo UI", bold=True, size=10)
    F_BODY  = Font(name="Meiryo UI", size=9)
    F_TOTAL = Font(name="Meiryo UI", bold=True, size=9)
    A_C = Alignment(horizontal="center", vertical="center")
    A_L = Alignment(horizontal="left",   vertical="center")
    A_R = Alignment(horizontal="right",  vertical="center")

    def write_headers(ws, headers):
        for col, (label, width) in enumerate(headers, 1):
            c = ws.cell(row=1, column=col, value=label)
            c.font = F_HEAD; c.fill = _fill("1F3864")
            c.alignment = A_C; c.border = _border()
            ws.column_dimensions[get_column_letter(col)].width = width

    def style(ws, row, col, val, *, num=False, center=False, fill_color=None, bold=False, thick_bottom=False):
        c = ws.cell(row=row, column=col, value=val)
        c.font = F_TOTAL if bold else F_BODY
        c.border = _thick_bottom() if thick_bottom else _border()
        if fill_color: c.fill = _fill(fill_color)
        if num:    c.number_format = '#,##0'; c.alignment = A_R
        elif center: c.alignment = A_C
        else:        c.alignment = A_L

    def section_title(ws, row, text, n_cols):
        """セクション見出し行（濃紺背景）"""
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols)
        c = ws.cell(row=row, column=1, value=text)
        c.font = F_HEAD; c.fill = _fill("1F3864"); c.alignment = A_L; c.border = _border()

    def total_row(ws, row, label, amount, n_cols, fc="D6E4F0"):
        """合計行（薄青背景・太字）"""
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=n_cols-1)
        c = ws.cell(row=row, column=1, value=label)
        c.font = F_TOTAL; c.fill = _fill(fc); c.alignment = A_L; c.border = _border()
        c = ws.cell(row=row, column=n_cols, value=amount)
        c.font = F_TOTAL; c.fill = _fill(fc); c.number_format = '#,##0'; c.alignment = A_R; c.border = _border()

    # ── ① 損益計算書 ────────────────────────────────────────────
    ws = wb.create_sheet("損益計算書")
    ws.freeze_panes = "A3"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 16
    ws.merge_cells("A1:B1")
    c = ws.cell(row=1, column=1, value=f"損益計算書　{year_str}年度　（出力日：{today_str}）")
    c.font = Font(name="Meiryo UI", bold=True, size=12); c.alignment = A_L

    assets   = [r for r in tb_rows if r["account_type"] == "資産"]
    liabs    = [r for r in tb_rows if r["account_type"] == "負債"]
    equity   = [r for r in tb_rows if r["account_type"] == "資本"]
    revenues = [r for r in tb_rows if r["account_type"] == "収益"]
    expenses = [r for r in tb_rows if r["account_type"] == "費用"]
    total_rev = sum(r["balance"] for r in revenues)
    total_exp = sum(r["balance"] for r in expenses)
    net_income = total_rev - total_exp

    r = 2
    section_title(ws, r, "【収益】", 2); r += 1
    for row in sorted(revenues, key=lambda x: -x["balance"]):
        style(ws, r, 1, row["name"], fill_color="E8F5E9")
        style(ws, r, 2, row["balance"], num=True, fill_color="E8F5E9"); r += 1
    total_row(ws, r, "収益合計", total_rev, 2, "C8E6C9"); r += 1
    r += 1
    section_title(ws, r, "【費用】", 2); r += 1
    for row in sorted(expenses, key=lambda x: -x["balance"]):
        style(ws, r, 1, row["name"], fill_color="FFF3E0")
        style(ws, r, 2, row["balance"], num=True, fill_color="FFF3E0"); r += 1
    total_row(ws, r, "費用合計", total_exp, 2, "FFE0B2"); r += 1
    r += 1
    fc_net = "C8E6C9" if net_income >= 0 else "FFCDD2"
    total_row(ws, r, "当期純利益（損失）", net_income, 2, fc_net)

    # ── ② 貸借対照表 ────────────────────────────────────────────
    ws = wb.create_sheet("貸借対照表")
    ws.freeze_panes = "A3"
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 16
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["D"].width = 16
    ws.merge_cells("A1:D1")
    c = ws.cell(row=1, column=1, value=f"貸借対照表　{today_str} 現在")
    c.font = Font(name="Meiryo UI", bold=True, size=12); c.alignment = A_L

    total_asset  = sum(r["balance"] for r in assets)
    total_liab   = sum(r["balance"] for r in liabs)
    total_equity = sum(r["balance"] for r in equity) + net_income

    # 左列: 資産、右列: 負債＋資本 を同じ行に並べる
    left  = [("【資産】", None, "E3F2FD")] + [(r["name"], r["balance"], "E3F2FD") for r in assets] \
           + [("資産合計", total_asset, "BBDEFB")]
    right = [("【負債】", None, "FCE4EC")] + [(r["name"], r["balance"], "FCE4EC") for r in liabs] \
           + [("負債合計", total_liab, "FFCDD2")] \
           + [("【資本】", None, "F3E5F5")] + [(r["name"], r["balance"], "F3E5F5") for r in equity] \
           + [("当期純利益", net_income, "F3E5F5")] \
           + [("負債・資本合計", total_equity + total_liab, "CE93D8")]

    for i, ((lname, lval, lfc), (rname, rval, rfc)) in enumerate(
        zip(left + [("", None, None)] * max(0, len(right)-len(left)),
            right + [("", None, None)] * max(0, len(left)-len(right))), 2):
        def _bs(col, name, val, fc):
            if name is None: return
            is_header = name.startswith("【") or name.endswith("合計") or name in ("当期純利益",)
            c1 = ws.cell(row=i, column=col, value=name)
            c1.font = F_TOTAL if is_header else F_BODY
            if fc: c1.fill = _fill(fc)
            c1.alignment = A_L; c1.border = _border()
            c2 = ws.cell(row=i, column=col+1, value=val)
            c2.font = F_TOTAL if is_header else F_BODY
            if fc: c2.fill = _fill(fc)
            if val is not None: c2.number_format = '#,##0'; c2.alignment = A_R
            c2.border = _border()
        _bs(1, lname, lval, lfc)
        _bs(3, rname, rval, rfc)

    # ── ③ 試算表 ────────────────────────────────────────────────
    ws = wb.create_sheet("試算表")
    ws.freeze_panes = "A3"
    ws.merge_cells("A1:E1")
    c = ws.cell(row=1, column=1, value=f"試算表　（出力日：{today_str}）")
    c.font = Font(name="Meiryo UI", bold=True, size=12); c.alignment = A_L
    write_headers(ws, [("勘定科目",20),("種別",10),("借方合計",14),("貸方合計",14),("残高",14)])
    type_colors = {"資産":"E3F2FD","負債":"FCE4EC","資本":"F3E5F5","収益":"E8F5E9","費用":"FFF3E0"}
    for r, row in enumerate(tb_rows, 3):
        fc = type_colors.get(row["account_type"])
        style(ws, r, 1, row["name"],         fill_color=fc)
        style(ws, r, 2, row["account_type"], center=True, fill_color=fc)
        style(ws, r, 3, row["debit_total"],  num=True,    fill_color=fc)
        style(ws, r, 4, row["credit_total"], num=True,    fill_color=fc)
        style(ws, r, 5, row["balance"],      num=True,    fill_color=fc)
    tr = len(tb_rows) + 3
    total_row(ws, tr, "合計", sum(r["debit_total"] for r in tb_rows), 3, "D6E4F0")
    style(ws, tr, 4, sum(r["credit_total"] for r in tb_rows), num=True, fill_color="D6E4F0", bold=True)
    style(ws, tr, 5, None, fill_color="D6E4F0")

    # ── ④ キャッシュフロー計算書 ────────────────────────────────
    ws = wb.create_sheet("キャッシュフロー")
    ws.freeze_panes = "A3"
    ws.column_dimensions["A"].width = 30; ws.column_dimensions["B"].width = 16
    ws.merge_cells("A1:B1")
    c = ws.cell(row=1, column=1, value=f"キャッシュフロー計算書　{year_str}年度　（出力日：{today_str}）")
    c.font = Font(name="Meiryo UI", bold=True, size=12); c.alignment = A_L

    cf_sections = [
        ("営業活動によるキャッシュフロー", [
            ("営業収入",    cf["operating_in"],  "E8F5E9"),
            ("営業支出",   -cf["operating_out"], "FFF3E0"),
        ], cf["operating_net"], "C8E6C9"),
        ("投資活動によるキャッシュフロー", [
            ("投資収入",    cf["investing_in"],  "E8F5E9"),
            ("投資支出",   -cf["investing_out"], "FFF3E0"),
        ], cf["investing_net"], "C8E6C9"),
        ("財務活動によるキャッシュフロー", [
            ("財務収入",    cf["financing_in"],  "E8F5E9"),
            ("財務支出",   -cf["financing_out"], "FFF3E0"),
        ], cf["financing_net"], "C8E6C9"),
    ]
    r = 2
    for title, items, net, net_fc in cf_sections:
        section_title(ws, r, f"【{title}】", 2); r += 1
        for label, val, fc in items:
            style(ws, r, 1, label, fill_color=fc)
            style(ws, r, 2, val,   num=True, fill_color=fc); r += 1
        total_row(ws, r, f"{title} 小計", net, 2, net_fc); r += 1
        r += 1
    fc_net = "C8E6C9" if cf["net_change"] >= 0 else "FFCDD2"
    total_row(ws, r, "現金増減合計", cf["net_change"], 2, fc_net); r += 2

    section_title(ws, r, "【収入源別内訳】", 2); r += 1
    for src, vals in cf["source_breakdown"].items():
        style(ws, r, 1, src)
        style(ws, r, 2, vals["in"] - vals["out"], num=True); r += 1

    # ── ⑤ 月次収支 ──────────────────────────────────────────────
    ws = wb.create_sheet("月次収支")
    ws.freeze_panes = "A3"
    ws.merge_cells("A1:D1")
    c = ws.cell(row=1, column=1, value=f"月次収支　{year_str}年度　（出力日：{today_str}）")
    c.font = Font(name="Meiryo UI", bold=True, size=12); c.alignment = A_L
    write_headers(ws, [("月",10),("収益",14),("費用",14),("純利益",14)])
    for r, row in enumerate(yearly, 3):
        fc = "E8F5E9" if row["net"] >= 0 else "FFCDD2"
        style(ws, r, 1, row["month"],   center=True)
        style(ws, r, 2, row["revenue"], num=True)
        style(ws, r, 3, row["expense"], num=True)
        style(ws, r, 4, row["net"],     num=True, fill_color=fc)
    tr = len(yearly) + 3
    total_row(ws, tr, "合計", sum(r["revenue"] for r in yearly), 2)
    style(ws, tr, 3, sum(r["expense"] for r in yearly), num=True, fill_color="D6E4F0", bold=True)
    fc_t = "C8E6C9" if sum(r["net"] for r in yearly) >= 0 else "FFCDD2"
    style(ws, tr, 4, sum(r["net"] for r in yearly), num=True, fill_color=fc_t, bold=True)

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
