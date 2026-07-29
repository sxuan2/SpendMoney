import asyncio
import os
import socket
import sqlite3
import uuid
import json
import datetime
import csv
import io
from html import escape
from urllib.parse import urlencode, quote
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from dashboard_stats import build_dashboard_stats
from database import DEFAULT_OWNER_USER_ID, init_db
from processor import process_receipt_file, UPLOAD_DIR, PROCESSED_DIR
from PIL import Image, ImageOps

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/processed", StaticFiles(directory=PROCESSED_DIR), name="processed")

CATEGORY_TYPE_LABELS = {"expense": "支出", "income": "收入"}
RECORD_TYPE_LABELS = {"expense": "支出", "income": "收入"}

def normalize_record_type(value: str) -> str:
    value = (value or "expense").strip()
    return value if value in RECORD_TYPE_LABELS else "expense"

def record_type_label(value: str) -> str:
    return RECORD_TYPE_LABELS.get(value, "支出")

def normalize_category_type(value: str) -> str:
    value = (value or "expense").strip()
    return value if value in CATEGORY_TYPE_LABELS else "expense"

def category_type_label(value: str) -> str:
    return CATEGORY_TYPE_LABELS.get(value, "支出")

def spendmoney_return_url(source: str) -> str:
    """Only allow post-action redirects back into SpendMoney."""
    if source == "/spendmoney/" or source.startswith("/spendmoney/history?"):
        return source
    return "/spendmoney/"

def get_owner_user_id(request: Request) -> str:
    """Nginx injects this after Django auth_request succeeds."""
    owner_user_id = (request.headers.get("X-Authenticated-User-Id") or "").strip()
    return owner_user_id or DEFAULT_OWNER_USER_ID

def get_api_key_owner(api_key: str):
    api_key = (api_key or "").strip()
    if not api_key:
        return None

    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT owner_user_id FROM api_keys WHERE api_key=? AND is_active=1", (api_key,))
    row = cursor.fetchone()
    conn.close()
    return str(row[0]) if row else None

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def compress_and_fix_image(image_path, max_dimension=1920):
    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.save(image_path, "JPEG", quality=85, optimize=True)
    except Exception as e:
        print(f"[!] 图像压缩失败，保留原图继续处理: {e}")

@app.on_event("startup")
async def startup_event():
    init_db()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(PROCESSED_DIR, exist_ok=True)

def ensure_owner_default_categories(cursor, owner_user_id):
    expense_defaults = [
        ('1', '餐饮美食'), ('2', '服饰美容'),
        ('3', '交通汽车'), ('4', '居家生活'),
        ('5', '休闲娱乐'), ('6', '数码电器'),
        ('7', '医疗健康'), ('0', '未分类/其他')
    ]
    income_defaults = [
        ('100', '工资收入'), ('101', '奖金'),
        ('102', '报销'), ('103', '利息/投资'),
        ('104', '其他收入')
    ]
    cursor.executemany(
        "INSERT OR IGNORE INTO categories (owner_user_id, code, name, category_type) VALUES (?, ?, ?, 'expense')",
        [(owner_user_id, code, name) for code, name in expense_defaults]
    )
    cursor.executemany(
        "INSERT OR IGNORE INTO categories (owner_user_id, code, name, category_type) VALUES (?, ?, ?, 'income')",
        [(owner_user_id, code, name) for code, name in income_defaults]
    )


def normalize_amortization_months(value, record_type="expense"):
    if normalize_record_type(record_type) == "income":
        return 1
    try:
        months = int(value or 1)
    except (TypeError, ValueError):
        months = 1
    return max(1, min(months, 120))


def validation_error_page(title: str, message: str, back_url: str = "/spendmoney/") -> str:
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>{escape(title)}</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">!</div>
                <h2 class="msg-title" style="color: var(--danger);">{escape(title)}</h2>
                <p class="hint">{escape(message)}</p>
                <a class="btn-link btn-primary" href="{escape(back_url)}">返回修改</a>
            </div>
        </body>
        </html>
    """


def get_category_rows(owner_user_id=DEFAULT_OWNER_USER_ID, category_type=None):
    owner_user_id = str(owner_user_id)
    category_type = normalize_category_type(category_type) if category_type else None
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    ensure_owner_default_categories(cursor, owner_user_id)
    conn.commit()
    params = [owner_user_id]
    where_type = ""
    if category_type:
        where_type = " AND category_type=?"
        params.append(category_type)
    sql = (
        "SELECT code, name, COALESCE(category_type, 'expense') "
        "FROM categories "
        "WHERE owner_user_id=?" + where_type + " "
        "ORDER BY CASE COALESCE(category_type, 'expense') WHEN 'expense' THEN 0 ELSE 1 END, "
        "cast(code as integer), code"
    )
    cursor.execute(sql, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


def get_category_map(owner_user_id=DEFAULT_OWNER_USER_ID, category_type=None):
    return {row[0]: row[1] for row in get_category_rows(owner_user_id, category_type)}

def format_export_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        return datetime.date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return value[:10]

def format_export_amount(value):
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"

COMMON_HEAD = """
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <style>
        :root {
            --bg-color: #f9fafb;
            --card-bg: #ffffff;
            --text-main: #111827;
            --text-muted: #6b7280;
            --primary: #0f172a; 
            --primary-hover: #334155;
            --success: #10b981;
            --danger: #ef4444;
            --border: #e5e7eb;
            --radius: 16px;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        body { margin: 0; font-family: var(--font-family); background: var(--bg-color); color: var(--text-main); -webkit-font-smoothing: antialiased; }
        .container { max-width: 900px; margin: 0 auto; padding: 24px 16px; padding-bottom: 60px; }
        h2 { font-size: 28px; font-weight: 800; letter-spacing: -0.025em; margin: 0 0 24px 0; color: var(--text-main); }
        h3 { font-size: 18px; font-weight: 700; border-bottom: 2px solid var(--border); padding-bottom: 8px; margin: 32px 0 16px 0; color: var(--text-main); }
        
        .nav-bar { display: flex; gap: 8px; margin-bottom: 16px; background: rgba(255, 255, 255, 0.75); padding: 8px; border-radius: 14px; border: 1px solid var(--border); box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); position: sticky; top: 16px; z-index: 100; overflow-x: auto; }
        .nav-bar a { text-decoration: none; color: var(--text-muted); font-weight: 600; font-size: 14px; padding: 10px 16px; border-radius: 10px; transition: all 0.2s ease; white-space: nowrap; }
        .nav-bar a:hover { background: #f3f4f6; color: var(--text-main); }
        .nav-bar a.active { background: var(--primary); color: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        
        .ip-banner { background: #e0f2fe; color: #0369a1; padding: 12px 16px; border-radius: 12px; margin-bottom: 24px; font-size: 14px; font-weight: 600; border: 1px solid #bae6fd; display: flex; align-items: center; justify-content: space-between; }
        .ip-banner a { color: #0284c7; text-decoration: underline; font-weight: 800; }
        
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; margin-bottom: 16px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.02), 0 2px 4px -1px rgba(0, 0, 0, 0.02); transition: transform 0.2s ease, box-shadow 0.2s ease; }
        .card:hover { transform: translateY(-2px); box-shadow: 0 12px 20px -4px rgba(0, 0, 0, 0.06), 0 4px 6px -2px rgba(0, 0, 0, 0.03); }
        
        .row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
        .merchant { font-size: 20px; font-weight: 800; color: var(--text-main); }
        .amount { font-size: 24px; font-weight: 800; color: var(--success); }
        .filename, .date, .hint { font-size: 13px; color: var(--text-muted); margin-bottom: 12px; }
        .category-badge { display: inline-block; background: var(--primary); color: #fff; padding: 4px 10px; border-radius: 8px; font-size: 12px; font-weight: 700; margin-bottom: 12px; border: 1px solid var(--primary-hover);}
        .ocr-details { font-size: 13px; line-height: 1.6; background: #f3f4f6; border-radius: 12px; padding: 12px; margin-top: 12px; max-height: 200px; overflow-y: auto; color: var(--text-muted); border: 1px solid var(--border); font-family: monospace; }
        summary { cursor: pointer; color: var(--primary); font-size: 14px; font-weight: 600; outline: none; margin-bottom: 8px; user-select: none; }
        
        .form-row { display: flex; gap: 12px; }
        .form-group { margin-bottom: 16px; flex: 1; }
        .form-group label { display: block; margin-bottom: 6px; font-size: 13px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .amortization-field.is-hidden { display: none; }
        input[type="text"], input[type="number"], input[type="date"], input[type="file"], select {{ width: 100%; box-sizing: border-box; padding: 12px 16px; border-radius: 10px; border: 1px solid var(--border); font-size: 15px; font-family: inherit; background: var(--bg-color); transition: all 0.2s; font-weight: 600; color: var(--text-main); }}
        input[readonly] { background: #f3f4f6; color: #9ca3af; cursor: not-allowed; }
        input:focus:not([readonly]), select:focus { outline: none; border-color: var(--primary); background: #fff; box-shadow: 0 0 0 3px rgba(15, 23, 42, 0.1); }
        
        button, .btn-link { border: none; border-radius: 10px; padding: 12px 20px; font-size: 15px; font-weight: 600; cursor: pointer; transition: all 0.2s ease; display: block; text-align: center; box-sizing: border-box; font-family: inherit; width: 100%; text-decoration: none; margin-bottom: 8px;}
        .btn-primary { background: var(--primary); color: #fff; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn-primary:hover { background: var(--primary-hover); transform: translateY(-1px); }
        .btn-edit { background: var(--success); color: #fff; }
        .btn-edit:hover { background: #059669; }
        .btn-danger { background: #fff; color: var(--danger); border: 1px solid #fca5a5; }
        .btn-danger:hover { background: #fef2f2; border-color: var(--danger); }
        .btn-cancel { background: #f3f4f6; color: var(--text-main); border: 1px solid var(--border); }
        .btn-cancel:hover { background: #e5e7eb; }
        
        #loading-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(255,255,255,0.85); backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); align-items: center; justify-content: center; z-index: 9999; flex-direction: column; text-align: center; padding: 20px; box-sizing: border-box;}
        .spinner { border: 4px solid #e5e7eb; border-top: 4px solid var(--primary); border-radius: 50%; width: 44px; height: 44px; animation: spin 1s linear infinite; margin-bottom: 20px;}
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        
        .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .kpi-grid .card { margin-bottom: 0; padding: 20px; }
        .kpi-title { font-size: 13px; color: var(--text-muted); margin-bottom: 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
        .kpi-value { font-size: 32px; font-weight: 800; color: var(--text-main); letter-spacing: -0.025em; }
        .kpi-value.green { color: var(--success); }
        .kpi-value.blue { color: #3b82f6; }
        .chart-grid { display: grid; grid-template-columns: 1fr; gap: 16px; margin-bottom: 24px; }
        @media (min-width: 768px) { .chart-grid { grid-template-columns: 1.5fr 1fr; } }
        .chart-container { height: 320px; width: 100%; }
        .health-item { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid var(--border); font-size: 15px;}
        .health-item:last-child { border-bottom: none; padding-bottom: 0; }
        .health-item strong { color: var(--text-main); font-weight: 700; }
        
        .img-container { width: 100%; max-height: 50vh; margin: 16px 0; border-radius: var(--radius); overflow: hidden; background: var(--bg-color); border: 1px solid var(--border);}
        .img-container img { max-width: 100%; display: block; }
        
        .msg-page { display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }
        .msg-card { text-align: center; max-width: 400px; width: 100%; padding: 40px 24px; margin: 24px;}
        .msg-title { margin-top: 0; font-size: 24px; color: var(--text-main); font-weight: 800;}

        @media (max-width: 600px) {
            body { overflow-x: hidden; }
            .container { width: 100%; padding: 14px 12px 40px; box-sizing: border-box; }
            h2 { margin-bottom: 14px; font-size: 23px; }
            h3 { margin: 24px 0 14px; font-size: 17px; }
            .card h3:first-child { margin-top: 0; }
            .nav-bar { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; top: 6px; margin-bottom: 14px; padding: 6px; overflow: visible; }
            .nav-bar a { display: flex; min-width: 0; min-height: 40px; align-items: center; justify-content: center; padding: 7px 5px; font-size: 12px; line-height: 1.25; text-align: center; white-space: normal; }
            .nav-bar a:last-child { grid-column: 1 / -1; }
            .ip-banner { display: block; margin-bottom: 14px; padding: 10px 12px; font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
            .card { padding: 16px; margin-bottom: 12px; border-radius: 13px; }
            .card:hover { transform: none; }
            .form-row { display: block; }
            .form-group { width: 100%; margin-bottom: 13px; }
            input[type="text"], input[type="number"], input[type="date"], input[type="file"], select { min-height: 46px; padding: 11px 12px; font-size: 16px; }
            input[type="file"] { padding: 9px; }
            button, .btn-link { min-height: 44px; padding: 11px 14px; }
            .row { gap: 8px; flex-wrap: wrap; }
            .merchant { font-size: 18px; }
            .amount { font-size: 21px; }
            .ocr-details { max-height: 150px; overflow-wrap: anywhere; }
            .kpi-grid { grid-template-columns: 1fr 1fr; gap: 10px; }
            .kpi-grid .card { padding: 14px; }
            .kpi-value { font-size: 25px; }
            .chart-container { height: 260px; }
        }
    </style>
    <script>
        function showLoading(msg) {
            const overlay = document.getElementById('loading-overlay');
            if (msg) document.getElementById('loading-text').innerHTML = msg;
            overlay.style.display = 'flex';
        }
    </script>
"""

@app.get("/dashboard", response_class=HTMLResponse)
async def render_dashboard(request: Request, month: Optional[str] = None):
    owner_user_id = get_owner_user_id(request)
    stats = build_dashboard_stats(owner_user_id, month)
    selected_month = stats.selected_month

    month_options = ""
    for dm in stats.dropdown_months:
        sel = "selected" if dm == selected_month else ""
        month_options += f"<option value='{dm}' {sel}>{dm}</option>"

    savings_rate = (stats.selected_month_net / stats.selected_month_income * 100) if stats.selected_month_income else 0.0
    kpi_net_class = "amount-net-positive" if stats.selected_month_net >= 0 else "amount-net-negative"
    budget_gap = stats.budget_remaining
    budget_gap_class = "amount-net-positive" if budget_gap >= 0 else "amount-net-negative"
    budget_label = "预算内剩余" if budget_gap >= 0 else "预算内超出"
    projected_gap_class = "amount-net-positive" if stats.projected_budget_gap >= 0 else "amount-net-negative"
    projected_gap_label = "预计剩余" if stats.projected_budget_gap >= 0 else "预计超出"
    daily_budget_class = "amount-net-positive" if stats.daily_budget_remaining >= 0 else "amount-net-negative"
    monthly_average = round(stats.avg_monthly_expense, 2)
    avg_expense_amounts = [monthly_average for _ in stats.dates]

    def build_expense_rank_items(rows):
        if not rows:
            return "<div class='rank-empty'>暂无支出</div>"
        total = sum(amount for _, amount in rows) or 0.0
        max_amount = max(amount for _, amount in rows) or 1.0
        items_html = ""
        for name, amount in rows:
            width = max(4, min(100, amount / max_amount * 100))
            percent = (amount / total * 100) if total else 0.0
            safe_name = escape(name if name else "未知代码")
            items_html += f"""
                <div class="rank-row">
                    <div class="rank-name" title="{safe_name}">{safe_name}</div>
                    <div class="rank-meta amount-expense">${amount:.2f} / {percent:.0f}%</div>
                    <div class="rank-track"><div class="rank-fill expense" style="width: {width:.1f}%;"></div></div>
                </div>
            """
        return f"<div class='rank-list'>{items_html}</div>"

    def build_budget_analysis():
        if stats.total_budget <= 0:
            return """
                <div class='budget-analysis-grid'>
                    <div class='budget-analysis-item budget-analysis-wide'>
                        <div class='budget-analysis-label'>预算状态</div>
                        <div class='budget-analysis-value'>未设置</div>
                        <div class='budget-analysis-note'>去标签管理为主要支出标签设置月预算后，这里会显示预计月底是否超支。</div>
                    </div>
                </div>
            """
        return f"""
            <div class="budget-analysis-grid">
                <div class="budget-analysis-item">
                    <div class="budget-analysis-label">预算内已用</div>
                    <div class="budget-analysis-value">{stats.budget_used_percent:.0f}%</div>
                    <div class="budget-analysis-note">${stats.budgeted_expense:.2f} / ${stats.total_budget:.2f}</div>
                </div>
                <div class="budget-analysis-item">
                    <div class="budget-analysis-label">{projected_gap_label}</div>
                    <div class="budget-analysis-value {projected_gap_class}">${abs(stats.projected_budget_gap):.2f}</div>
                    <div class="budget-analysis-note">预计预算内支出 ${stats.projected_budgeted_expense:.2f}</div>
                </div>
                <div class="budget-analysis-item">
                    <div class="budget-analysis-label">剩余日均可花</div>
                    <div class="budget-analysis-value {daily_budget_class}">${stats.daily_budget_remaining:.2f}</div>
                    <div class="budget-analysis-note">剩余 {stats.month_days_remaining} 天</div>
                </div>
                <div class="budget-analysis-item">
                    <div class="budget-analysis-label">非预算支出</div>
                    <div class="budget-analysis-value">${stats.unbudgeted_expense:.2f}</div>
                    <div class="budget-analysis-note">一次性/未设预算，不计入预算已用</div>
                </div>
            </div>
        """

    def build_budget_items(statuses, suggestions):
        if statuses:
            items_html = ""
            for item in statuses:
                safe_name = escape(item.name)
                if item.state == "unset":
                    meta = f"已用 ${item.amount:.2f} · 未设预算"
                    state_class = "budget-unset"
                    width = 0
                elif item.state == "over":
                    meta = f"超出 ${abs(item.remaining):.2f}"
                    state_class = "budget-over"
                    width = 100
                else:
                    meta = f"已用 {item.percent:.0f}% · 剩余 ${item.remaining:.2f}"
                    state_class = "budget-warning" if item.state == "warning" else "budget-ok"
                    width = max(3, min(100, item.percent))
                items_html += f"""
                    <div class="budget-row">
                        <div class="budget-head">
                            <span class="budget-name" title="{safe_name}">{safe_name}</span>
                            <span class="budget-meta {state_class}">{meta}</span>
                        </div>
                        <div class="budget-track"><div class="budget-fill {state_class}" style="width: {width:.1f}%;"></div></div>
                    </div>
                """
            return f"<div class='budget-list'>{items_html}</div>"
        if suggestions:
            suggestion_items = "".join(f"<li>{escape(name)}</li>" for name in suggestions)
            return f"<div class='empty-panel'><div>建议先给这些高频支出设月预算：</div><ul>{suggestion_items}</ul></div>"
        return "<div class='empty-panel'>暂无支出数据，暂不需要设置预算。</div>"

    def build_task_items():
        tasks = []
        if stats.pending_count:
            tasks.append(("待复核账单", f"{stats.pending_count} 笔", "/spendmoney/"))
        if stats.uncategorized_count:
            tasks.append(("未分类支出", f"{stats.uncategorized_count} 笔", f"/spendmoney/history?month={selected_month}"))
        if stats.amortized_item_count:
            tasks.append(("年度摊销中", f"{stats.amortized_item_count} 项", f"/spendmoney/history?amortized=1&record_type=expense"))
        over_count = sum(1 for item in stats.budget_statuses if item.state == "over")
        if over_count:
            tasks.append(("预算超支标签", f"{over_count} 个", "/spendmoney/categories"))
        if not tasks:
            return "<div class='empty-panel'>本月没有需要优先处理的事项。</div>"
        rows = ""
        for title, value, href in tasks:
            rows += f"""
                <a class="task-row" href="{href}">
                    <span>{escape(title)}</span>
                    <strong>{escape(value)}</strong>
                </a>
            """
        return f"<div class='task-list'>{rows}</div>"

    def build_recent_rows(items):
        if not items:
            return "<tr><td colspan='4' class='empty-cell'>本月暂无确认支出</td></tr>"
        rows = ""
        for item in items:
            badge = "<span class='mini-badge'>摊销</span>" if item.is_amortized else ""
            rows += f"""
                <tr>
                    <td>{escape(item.date[5:] if len(item.date) >= 10 else item.date)}</td>
                    <td title="{escape(item.merchant)}">{escape(item.merchant)} {badge}</td>
                    <td title="{escape(item.category)}">{escape(item.category)}</td>
                    <td class="amount-expense" style="text-align:right; font-weight:800; white-space:nowrap;">${item.amount:.2f}</td>
                </tr>
            """
        return rows

    expense_rank_items = build_expense_rank_items(stats.expense_category_data)
    budget_analysis = build_budget_analysis()
    budget_items = build_budget_items(stats.budget_statuses, stats.budget_suggestions)
    task_items = build_task_items()
    recent_rows = build_recent_rows(stats.recent_expenses)

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>数据看板 - SpendMoney</title>
        <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
        {COMMON_HEAD}
        <style>
            .workbench-filter {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:16px; }}
            .workbench-note {{ color:#64748b; font-size:13px; font-weight:800; }}
            .chart-container {{ width:100% !important; min-width:100% !important; height:320px; }}
            .control-grid {{ display:grid; grid-template-columns:minmax(0, 1.45fr) minmax(300px, .85fr); gap:16px; }}
            .analysis-grid {{ display:grid; grid-template-columns:minmax(0, 1fr) minmax(300px, 1fr); gap:16px; }}
            .amount-income {{ color:#059669; }}
            .amount-expense {{ color:#dc2626; }}
            .amount-net-positive {{ color:#047857; }}
            .amount-net-negative {{ color:#b91c1c; }}
            .split-list {{ display:flex; flex-direction:column; gap:12px; margin-top:18px; }}
            .split-row {{ display:flex; align-items:center; justify-content:space-between; padding:14px; border:1px solid #eef2f7; background:#f8fafc; border-radius:8px; }}
            .split-label {{ color:#64748b; font-size:13px; font-weight:800; }}
            .split-value {{ color:#111827; font-size:20px; font-weight:900; }}
            .rank-list {{ display:flex; flex-direction:column; gap:13px; margin-top:14px; }}
            .rank-row {{ display:grid; grid-template-columns:minmax(92px, 1fr) auto; gap:10px 12px; align-items:baseline; }}
            .rank-name {{ color:#111827; font-size:14px; font-weight:900; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
            .rank-meta {{ font-size:13px; font-weight:900; white-space:nowrap; }}
            .rank-track, .budget-track {{ grid-column:1 / -1; height:9px; border-radius:999px; background:#eef2f7; overflow:hidden; }}
            .rank-fill, .budget-fill {{ height:100%; min-width:5px; border-radius:inherit; }}
            .rank-fill.expense {{ background:#ef4444; }}
            .budget-analysis-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:14px; }}
            .budget-analysis-item {{ padding:13px; border:1px solid #eef2f7; background:#f8fafc; border-radius:8px; }}
            .budget-analysis-wide {{ grid-column:1 / -1; }}
            .budget-analysis-label {{ color:#64748b; font-size:12px; font-weight:900; margin-bottom:7px; }}
            .budget-analysis-value {{ color:#111827; font-size:20px; font-weight:900; line-height:1.15; }}
            .budget-analysis-note {{ color:#64748b; font-size:12px; font-weight:800; margin-top:7px; line-height:1.4; }}
            .budget-list {{ display:flex; flex-direction:column; gap:14px; margin-top:16px; padding-top:16px; border-top:1px solid #eef2f7; }}
            .budget-head {{ display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:8px; }}
            .budget-name {{ color:#111827; font-size:14px; font-weight:900; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
            .budget-meta {{ font-size:13px; font-weight:900; white-space:nowrap; }}
            .budget-fill.budget-ok {{ background:#10b981; }}
            .budget-fill.budget-warning {{ background:#f59e0b; }}
            .budget-fill.budget-over {{ background:#ef4444; }}
            .budget-ok {{ color:#047857; }}
            .budget-warning {{ color:#b45309; }}
            .budget-over {{ color:#dc2626; }}
            .budget-unset {{ color:#64748b; }}
            .empty-panel {{ padding:18px; background:#f8fafc; border:1px solid #eef2f7; border-radius:8px; color:#64748b; font-weight:800; line-height:1.6; }}
            .empty-panel ul {{ margin:8px 0 0 18px; padding:0; }}
            .task-list {{ display:flex; flex-direction:column; gap:10px; margin-top:14px; }}
            .task-row {{ display:flex; justify-content:space-between; align-items:center; padding:13px 14px; border:1px solid #eef2f7; border-radius:8px; color:#111827; text-decoration:none; font-weight:900; background:#f8fafc; }}
            .task-row strong {{ color:#2563eb; }}
            .data-table {{ width:100%; border-collapse:collapse; table-layout:fixed; margin-top:12px; }}
            .data-table th {{ text-align:left; color:#64748b; font-size:12px; padding:9px 8px; border-bottom:2px solid var(--border); }}
            .data-table td {{ padding:9px 8px; border-bottom:1px solid #f1f5f9; font-size:13px; line-height:1.35; font-weight:600; color:#111827; vertical-align:middle; }}
            .data-table th:nth-child(1), .data-table td:nth-child(1) {{ width:48px; }}
            .data-table th:nth-child(3), .data-table td:nth-child(3) {{ width:94px; }}
            .data-table th:nth-child(4), .data-table td:nth-child(4) {{ width:82px; }}
            .data-table td:nth-child(2), .data-table td:nth-child(3) {{ font-weight:700; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
            .empty-cell {{ text-align:center; color:#64748b !important; padding:24px 8px !important; }}
            .mini-badge {{ display:inline-flex; margin-left:5px; padding:1px 5px; border-radius:999px; background:#e0f2fe; color:#0369a1; font-size:10px; font-weight:800; }}
            @media (max-width: 880px) {{ .control-grid, .analysis-grid {{ grid-template-columns:1fr; }} .workbench-filter {{ align-items:flex-start; flex-direction:column; }} }}
        </style>
    </head>
    <body>
    <div class="container">
        <h2>记账中心</h2>
        <div class="nav-bar">
            <a href="/spendmoney/dashboard" class="active">📊 数据看板</a>
            <a href="/spendmoney/">🧾 上传与待办</a>
            <a href="/spendmoney/history">🗄️ 历史台账</a>
            <a href="/spendmoney/categories">🏷️ 标签管理</a>
            <a href="/nav/">🏠 返回主页</a>
        </div>

        <div class="workbench-filter">
            <div class="workbench-note">支出控制工作台 · 支出口径含年度摊销</div>
            <select id="monthSelector" onchange="location.href='/spendmoney/dashboard?month=' + this.value" style="width:auto; padding:7px 12px; font-size:14px; margin:0; border-radius:8px;">
                {month_options}
            </select>
        </div>

        <div class="kpi-grid">
            <div class="card">
                <div class="kpi-title">本月支出（含摊销）</div>
                <div class="kpi-value amount-expense">${stats.selected_month_expense:.2f}</div>
            </div>
            <div class="card">
                <div class="kpi-title">本月收入</div>
                <div class="kpi-value green">${stats.selected_month_income:.2f}</div>
            </div>
            <div class="card">
                <div class="kpi-title">本月结余</div>
                <div class="kpi-value {kpi_net_class}">${stats.selected_month_net:.2f}</div>
                <div class="workbench-note">结余率 {savings_rate:.0f}%</div>
            </div>
            <div class="card">
                <div class="kpi-title">待处理</div>
                <div class="kpi-value blue">{stats.pending_count + stats.uncategorized_count}</div>
            </div>
        </div>

        <div class="control-grid">
            <div class="card" style="display:flex; flex-direction:column;">
                <div class="kpi-title">近 6 个月支出趋势</div>
                <div id="expenseTrendChart" class="chart-container"></div>
            </div>
            <div class="card">
                <div class="kpi-title">{selected_month} 支出拆解</div>
                <div class="split-list">
                    <div class="split-row"><span class="split-label">实际账单支出</span><span class="split-value amount-expense">${stats.selected_direct_expense:.2f}</span></div>
                    <div class="split-row"><span class="split-label">年度摊销支出</span><span class="split-value amount-expense">${stats.selected_amortized_expense:.2f}</span></div>
                    <div class="split-row"><span class="split-label">预算总额</span><span class="split-value">${stats.total_budget:.2f}</span></div>
                    <div class="split-row"><span class="split-label">{budget_label}</span><span class="split-value {budget_gap_class}">${abs(budget_gap):.2f}</span></div>
                    <div class="split-row"><span class="split-label">非预算支出</span><span class="split-value">${stats.unbudgeted_expense:.2f}</span></div>
                </div>
            </div>
        </div>

        <div class="analysis-grid">
            <div class="card">
                <div class="kpi-title">支出分类排行</div>
                {expense_rank_items}
            </div>
            <div class="card">
                <div class="kpi-title">预算分析</div>
                {budget_analysis}
                {budget_items}
            </div>
        </div>

        <div class="analysis-grid">
            <div class="card">
                <div class="kpi-title">需要处理</div>
                {task_items}
            </div>
            <div class="card">
                <div class="kpi-title">最近支出明细</div>
                <table class="data-table">
                    <thead><tr><th>日期</th><th>商户</th><th>标签</th><th style="text-align:right;">金额</th></tr></thead>
                    <tbody>{recent_rows}</tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        var expenseTrendChart = echarts.init(document.getElementById('expenseTrendChart'));
        var formatMoney = function(value) {{ return '$' + Number(value || 0).toFixed(2); }};
        expenseTrendChart.setOption({{
            tooltip: {{ trigger: 'axis', formatter: function(params) {{ return params.map(function(item) {{ return item.marker + item.seriesName + ': ' + formatMoney(item.value); }}).join('<br>'); }} }},
            legend: {{ data: ['支出', '近 6 月均线'], top: 0, right: 0 }},
            grid: {{ left: '3%', right: '4%', top: 46, bottom: '8%', containLabel: true }},
            xAxis: {{ type: 'category', data: {json.dumps(stats.dates)}, axisTick: {{ alignWithLabel: true }} }},
            yAxis: {{ type: 'value', axisLabel: {{ formatter: function(value) {{ return '$' + Number(value || 0).toLocaleString(); }} }}, splitLine: {{ lineStyle: {{ color: '#e5e7eb' }} }} }},
            series: [
                {{ name: '支出', type: 'bar', barMaxWidth: 34, itemStyle: {{ color: '#ef4444', borderRadius: [4, 4, 0, 0] }}, data: {json.dumps(stats.expense_amounts)} }},
                {{ name: '近 6 月均线', type: 'line', symbol: 'none', lineStyle: {{ color: '#64748b', width: 2, type: 'dashed' }}, data: {json.dumps(avg_expense_amounts)} }}
            ]
        }});
        function forceResize() {{ expenseTrendChart.resize(); }}
        forceResize();
        setTimeout(forceResize, 50);
        setTimeout(forceResize, 200);
        window.addEventListener('resize', forceResize);
    </script>
    </body></html>
    """
    return html

@app.get("/", response_class=HTMLResponse)
async def dashboard_main(request: Request):
    owner_user_id = get_owner_user_id(request)
    expense_rows = get_category_rows(owner_user_id, "expense")
    income_rows = get_category_rows(owner_user_id, "income")
    cmap = {code: name for code, name, _ in expense_rows}
    expense_options = "<option value='' selected disabled>请选择支出标签</option>" + "".join([f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name, _ in expense_rows])
    income_options = "<option value='' selected disabled>请选择收入标签</option>" + "".join([f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name, _ in income_rows])
    category_options_by_type = json.dumps({
        "expense": [{"code": code, "name": name} for code, name, _ in expense_rows],
        "income": [{"code": code, "name": name} for code, name, _ in income_rows],
    }, ensure_ascii=False)
    
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, amount, merchant, date, raw_text, status, subtotal, tax, category, COALESCE(record_type, 'expense'), COALESCE(amortization_months, 1) FROM records WHERE owner_user_id=? AND status != 'confirmed'", (owner_user_id,))
    records = cursor.fetchall()
    conn.close()
    
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>上传与待办 - SpendMoney</title>
        {COMMON_HEAD}
    </head>
    <body>
    <div id="loading-overlay">
        <div class="spinner"></div>
        <div id="loading-text" style="font-weight:bold; font-size: 16px; color:var(--text-main);">正在处理上传...</div>
    </div>
    <div class="container">
        <h2>记账中心</h2>
        <div class="nav-bar">
            <a href="/spendmoney/dashboard">📊 数据看板</a>
            <a href="/spendmoney/" class="active">🧾 上传与待办</a>
            <a href="/spendmoney/history">🗄️ 历史台账</a>
            <a href="/spendmoney/categories">🏷️ 标签管理</a>
            <a href="/nav/">🏠 返回主页</a>
        </div>
        
        <div class="ip-banner">
            <span>📲 快捷指令 POST 接口：<code>/spendmoney/api/iphone-upload</code></span>
        </div>
        
        <div class="card" style="padding-bottom: 16px;">
            <h3>📸 上传并识别</h3>
            <p class="hint" style="margin-top:0;">上传图片预览裁切，AI 将自动提取字段。</p>
            <form action="/spendmoney/upload" method="post" enctype="multipart/form-data" onsubmit="showLoading()">
                <input type="file" name="file" required>
                <button type="submit" class="btn-primary">安全上传并预览</button>
            </form>
        </div>

        <div class="card" style="padding-bottom: 16px;">
            <h3>✍️ 手工记账</h3>
            <p class="hint" style="margin-top:0;">没有小票？直接在此手动录入消费记录，直达台账。</p>
            <form action="/spendmoney/manual_add" method="post" onsubmit="showLoading('正在保存手工记录...')">
                <div class="form-row">
                    <div class="form-group"><label>记录类型</label><select id="manualRecordType" name="record_type"><option value="expense">支出</option><option value="income">收入</option></select></div>
                    <div class="form-group"><label>商户/来源</label><input type="text" name="merchant" required></div>
                    <div class="form-group"><label>分类标签</label>
                        <select id="manualCategory" name="category" required>
                            {expense_options}
                        </select>
                    </div>
                </div>
                <div class="form-group"><label>交易日期</label><input type="date" name="date" required value="{today_str}"></div>
                <div class="form-row">
                    <div class="form-group"><label>税前金额</label><input type="number" min="0" step="0.01" name="subtotal" inputmode="decimal" value="0.00"></div>
                    <div class="form-group"><label>税费</label><input type="number" min="0" step="0.01" name="tax" inputmode="decimal" value="0.00"></div>
                </div>
                <div class="form-group"><label>总金额 (Total)</label><input type="number" min="0" step="0.01" name="amount" inputmode="decimal" required></div>
                <div class="form-group amortization-field"><label>摊销月数</label><input type="number" min="1" max="120" step="1" name="amortization_months" inputmode="numeric" value="1"><div class="hint">普通消费填 1；两月一交填 2；年费填 12。</div></div>
                <button type="submit" class="btn-primary" style="background: var(--success); width: 100%;">快速确认保存</button>
            </form>
        </div>

        <h3>待核对入库草稿</h3>
    """

    for r in records:
        merchant_val = escape(str(r[3] or ""))
        date_val = escape(str(r[4] or ""))
        raw_text = escape(str(r[5] or ""))
        subtotal_val = format_export_amount(r[7])
        tax_val = format_export_amount(r[8])
        cat_code = escape(str(r[9] or ""))
        amount_val = format_export_amount(r[2])
        current_record_type = normalize_record_type(r[10] if len(r) > 10 else "expense")
        record_type_options = "".join([f"<option value='{escape(type_code)}' {'selected' if type_code==current_record_type else ''}>{escape(type_label)}</option>" for type_code, type_label in RECORD_TYPE_LABELS.items()])
        amortization_months_val = normalize_amortization_months(r[11] if len(r) > 11 else 1, current_record_type)
        
        cat_placeholder_selected = "selected" if not cat_code or cat_code not in cmap else ""
        cat_options = f"<option value='' disabled {cat_placeholder_selected}>请选择标签</option>" + "".join([f"<option value='{escape(code)}' {'selected' if code==cat_code else ''}>{escape(name)} (代码: {escape(code)})</option>" for code, name in cmap.items()])
        
        html += f"<div class='card'>" \
                f"<div class='filename'>源文件: {escape(str(r[1]))}</div>" \
                f"<div class='category-badge'>预设代码: {cat_code}</div>" \
                f"<div class='ocr-details' style='margin-bottom:16px;'>{raw_text}</div>" \
                f"<form action='/spendmoney/update' method='post'>" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='/spendmoney/'>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>记录类型</label><select name='record_type' class='record-type-edit'>{record_type_options}</select></div>" \
                f"<div class='form-group'><label>商户/来源</label><input type='text' name='merchant' value='{merchant_val}' required></div>" \
                f"<div class='form-group'><label>确认分类标签</label><select name='category' required>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易日期</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' min='0' step='0.01' name='subtotal' inputmode='decimal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' min='0' step='0.01' name='tax' inputmode='decimal' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' min='0' step='0.01' name='amount' inputmode='decimal' value='{amount_val}' required></div>" \
                f"<div class='form-group amortization-field'><label>摊销月数</label><input type='number' min='1' max='120' step='1' name='amortization_months' inputmode='numeric' value='{amortization_months_val}'><div class='hint'>普通消费填 1；两月一交填 2；年费填 12。</div></div>" \
                f"<button type='submit' class='btn-edit'>确认无误并入库</button></form>" \
                f"<form action='/spendmoney/delete' method='post' onsubmit=\"return confirm('确定要彻底删除这条记录吗？');\">" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='/spendmoney/'>" \
                f"<button type='submit' class='btn-danger'>删除此草稿</button></form></div>"

    if not records:
        html += "<div class='card'><div style='text-align:center; color:var(--text-muted); padding:20px 0;'>当前所有小票均已确认入库，享受清空收件箱的快感吧。</div></div>"

    return html + f"""
    <script>
        const manualCategoryOptions = {category_options_by_type};
        const manualRecordType = document.getElementById('manualRecordType');
        const manualCategory = document.getElementById('manualCategory');
        function syncAmortizationField(form, typeSelect) {{
            const field = form.querySelector('.amortization-field');
            if (!field || !typeSelect) return;
            const input = field.querySelector('input[name="amortization_months"]');
            const isIncome = typeSelect.value === 'income';
            field.classList.toggle('is-hidden', isIncome);
            if (input) {{
                input.disabled = isIncome;
                if (isIncome) input.value = '1';
            }}
        }}
        function refreshManualCategories() {{
            const items = manualCategoryOptions[manualRecordType.value] || [];
            const label = manualRecordType.value === 'income' ? '请选择收入标签' : '请选择支出标签';
            manualCategory.innerHTML = `<option value="" selected disabled>${{label}}</option>` + items.map(function(item) {{
                return `<option value="${{item.code}}">${{item.name}}</option>`;
            }}).join('');
            syncAmortizationField(manualRecordType.form, manualRecordType);
        }}
        if (manualRecordType && manualCategory) {{
            manualRecordType.addEventListener('change', refreshManualCategories);
            syncAmortizationField(manualRecordType.form, manualRecordType);
        }}
        document.querySelectorAll('.record-type-edit').forEach(function(typeSelect) {{
            syncAmortizationField(typeSelect.form, typeSelect);
            typeSelect.addEventListener('change', function() {{ syncAmortizationField(typeSelect.form, typeSelect); }});
        }});
    </script>
    </div></body></html>"""

@app.get("/history", response_class=HTMLResponse)
async def history(
    request: Request,
    sort: str = "date",
    order: str = "desc",
    start_date: str = "",
    end_date: str = "",
    record_type: str = "",
    category: list[str] = Query(default=[]),
    amortized: str = "",
):
    owner_user_id = get_owner_user_id(request)
    category_rows = get_category_rows(owner_user_id)
    cmap = {code: name for code, name, _ in category_rows}
    category_type_map = {code: category_type for code, _, category_type in category_rows}

    start_date = (start_date or "").strip()[:10]
    end_date = (end_date or "").strip()[:10]
    record_type = normalize_record_type(record_type) if record_type else ""
    amortized = "1" if str(amortized).strip() in ("1", "true", "yes") else ""
    selected_categories = []
    for code in category:
        code = (code or "").strip()
        if code and code in cmap and code not in selected_categories:
            selected_categories.append(code)

    sort_columns = {
        "id": "id",
        "merchant": "merchant COLLATE NOCASE",
        "created_at": "created_at",
        "date": "date",
        "category": "(SELECT name FROM categories WHERE owner_user_id = records.owner_user_id AND code = records.category) COLLATE NOCASE",
        "record_type": "record_type",
        "amount": "amount",
    }
    if sort not in sort_columns:
        sort = "date"
    order = "asc" if order.lower() == "asc" else "desc"
    direction = "ASC" if order == "asc" else "DESC"
    empty_dates_last = ""
    if sort in ("date", "created_at"):
        empty_dates_last = f"CASE WHEN {sort_columns[sort]} IS NULL OR {sort_columns[sort]} = '' THEN 1 ELSE 0 END ASC, "

    where_clauses = ["owner_user_id=?", "status = 'confirmed'"]
    query_params = [owner_user_id]
    if start_date:
        where_clauses.append("date >= ?")
        query_params.append(start_date)
    if end_date:
        where_clauses.append("date <= ?")
        query_params.append(end_date)
    if record_type:
        where_clauses.append("COALESCE(record_type, 'expense') = ?")
        query_params.append(record_type)
    if selected_categories:
        placeholders = ", ".join(["?"] * len(selected_categories))
        where_clauses.append(f"category IN ({placeholders})")
        query_params.extend(selected_categories)
    if amortized:
        where_clauses.append("COALESCE(amortization_months, 1) > 1")
    where_sql = " AND ".join(where_clauses)

    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT id, filename, amount, merchant, date, raw_text, status,
               subtotal, tax, category, created_at, COALESCE(amortization_months, 1),
               COALESCE(record_type, 'expense')
        FROM records
        WHERE {where_sql}
        ORDER BY {empty_dates_last}{sort_columns[sort]} {direction}, id DESC
    """, query_params)
    records = cursor.fetchall()
    conn.close()

    active_filters = {"start_date": start_date, "end_date": end_date, "record_type": record_type, "amortized": amortized}
    active_filters = {key: value for key, value in active_filters.items() if value}
    if selected_categories:
        active_filters["category"] = selected_categories
    source_params = {"sort": sort, "order": order, **active_filters}
    source_url = f"/spendmoney/history?{urlencode(source_params, doseq=True)}"

    def history_url(params):
        clean_params = {key: value for key, value in params.items() if value}
        return f"/spendmoney/history?{urlencode(clean_params, doseq=True)}" if clean_params else "/spendmoney/history"

    def sort_link(column, label):
        next_order = "asc" if column != sort or order == "desc" else "desc"
        indicator = ""
        if column == sort:
            indicator = " ↑" if order == "asc" else " ↓"
        url = history_url({"sort": column, "order": next_order, **active_filters})
        return f"<a href='{url}'>{label}{indicator}</a>"

    record_type_filter_options = "<option value=''>全部类型</option>" + "".join([
        f"<option value='{escape(type_code)}' {'selected' if type_code == record_type else ''}>{escape(type_label)}</option>"
        for type_code, type_label in RECORD_TYPE_LABELS.items()
    ])
    selected_category_set = set(selected_categories)
    category_filter_options = "".join([
        f"<option value='{escape(code)}' data-record-type='{escape(category_type)}' {'selected' if code in selected_category_set else ''}>{escape(category_type_label(category_type))} · {escape(name)}</option>"
        for code, name, category_type in category_rows
    ])
    clear_filter_url = history_url({"sort": sort, "order": order})
    amortized_checked = "checked" if amortized else ""
    filtered_count = len(records)
    date_range_value = " 至 ".join([value for value in (start_date, end_date) if value])

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>历史台账 - SpendMoney</title>
        <link href="https://cdn.jsdelivr.net/npm/tom-select@2.4.3/dist/css/tom-select.css" rel="stylesheet">
        <link href="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css" rel="stylesheet">
        {COMMON_HEAD}
        <style>
            .history-shell {{
                overflow-x: auto; background: var(--card-bg); border: 1px solid var(--border);
                border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.035);
            }}
            .history-table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
            .history-table th {{
                padding: 9px 10px; text-align: left; background: #f8fafc;
                border-bottom: 1px solid var(--border); font-size: 12px;
                color: var(--text-muted); white-space: nowrap;
            }}
            .history-table th a {{ color: inherit; text-decoration: none; display: block; }}
            .history-table th a:hover {{ color: var(--text-main); }}
            .history-row {{ cursor: pointer; transition: background .15s ease; }}
            .history-row:hover, .history-row.open {{ background: #f8fafc; }}
            .history-row td {{
                padding: 9px 10px; border-bottom: 1px solid #f1f5f9; font-size: 13px;
                vertical-align: middle; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
            }}
            .merchant-cell {{ font-weight: 700; }}
            .amount-cell {{ text-align: right; font-weight: 800; color: var(--success); }}
            .date-cell {{ color: var(--text-muted); font-variant-numeric: tabular-nums; }}
            .compact-badge {{
                display: inline-block; max-width: 100%; overflow: hidden; text-overflow: ellipsis;
                vertical-align: middle; background: #e2e8f0; color: #334155;
                padding: 3px 7px; border-radius: 999px; font-size: 11px; font-weight: 700;
            }}
            .detail-row {{ display: none; }}
            .detail-row.open {{ display: table-row; }}
            .detail-row > td {{ padding: 0; border-bottom: 1px solid var(--border); }}
            .history-detail {{ padding: 16px; background: #fff; }}
            .detail-meta {{ font-size: 12px; color: var(--text-muted); margin-bottom: 12px; }}
            .history-detail .form-group {{ margin-bottom: 10px; }}
            .history-detail input, .history-detail select {{ padding: 9px 11px; font-size: 13px; }}
            .history-help {{ margin: 0 0 10px; font-size: 12px; color: var(--text-muted); }}
            .filter-card {{ padding: 18px 24px 16px; margin-bottom: 12px; }}
            .filter-form {{ display: grid; grid-template-columns: minmax(220px, 1.1fr) minmax(130px, .55fr) minmax(260px, 1.35fr); column-gap: 22px; row-gap: 14px; align-items: end; }}
            .filter-form > * {{ min-width: 0; }}
            .filter-form .form-group {{ margin-bottom: 0; min-width: 0; }}
            .filter-form label {{ margin-bottom: 7px; font-size: 13px; font-weight: 800; color: var(--text-muted); }}
            .filter-form input[type="text"], .filter-form select {{ height: 46px; min-height: 46px; max-height: 46px; width: 100%; box-sizing: border-box; padding: 0 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg-color); color: var(--text-main); font-size: 14px; line-height: 46px; box-shadow: none; outline: none; }}
            .filter-form input[type="text"]:focus, .filter-form select:focus {{ border-color: var(--primary); background: #fff; box-shadow: 0 0 0 3px rgba(15, 23, 42, 0.1); }}
            .filter-footer {{ grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 0; }}
            .filter-actions {{ display: flex; gap: 12px; align-items: center; justify-content: flex-end; min-width: 0; }}
            .filter-action-group {{ display: flex; gap: 12px; align-items: center; }}
            .filter-actions button, .filter-actions .btn-link {{ height: 40px; width: auto; min-width: 92px; margin-bottom: 0; padding: 0 18px; white-space: nowrap; display: inline-flex; align-items: center; justify-content: center; }}
            .filter-actions .btn-primary {{ min-width: 108px; }}
            .filter-actions .btn-cancel {{ background: #f8fafc; color: var(--text-muted); border: 1px solid var(--border); box-shadow: none; }}
            .filter-actions .btn-export {{ min-width: 120px; background: #fff; color: #047857; border: 1px solid #a7f3d0; box-shadow: none; }}
            .filter-actions .btn-export:hover {{ background: #ecfdf5; }}
            .filter-summary {{ margin: 0; font-size: 13px; color: var(--text-muted); }}
            .filter-form .ts-wrapper {{ width: 100%; height: 46px; box-sizing: border-box; }}
            .filter-form .ts-wrapper.single .ts-control, .filter-form .ts-wrapper.multi .ts-control {{ height: 46px; min-height: 46px; max-height: 46px; box-sizing: border-box; padding: 0 12px; border-radius: 10px; border-color: var(--border); background: var(--bg-color); box-shadow: none; display: flex; align-items: center; overflow: hidden; }}
            .filter-form .ts-wrapper.single .ts-control {{ padding: 0 12px; }}
            .filter-form .ts-wrapper.single .ts-control::after {{ right: 12px; }}
            .filter-form .ts-wrapper.focus .ts-control {{ border-color: var(--primary); background: #fff; box-shadow: 0 0 0 3px rgba(15, 23, 42, 0.1); }}
            .filter-form .ts-control > input {{ min-height: 0; height: 28px; font-size: 14px; line-height: 28px; }}
            .filter-form .ts-control .item {{ background: #e2e8f0; border: 0; border-radius: 999px; color: #334155; font-weight: 700; padding: 3px 9px; line-height: 18px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .filter-form .ts-wrapper.single .ts-control .item {{ background: transparent; border-radius: 0; padding: 0; color: var(--text-main); font-weight: 700; }}
            .filter-form .ts-dropdown {{ border-radius: 12px; border-color: var(--border); box-shadow: 0 14px 32px rgba(15, 23, 42, .14); overflow: hidden; }}
            .filter-form .ts-dropdown .option {{ padding: 10px 12px; font-weight: 600; }}
            .filter-form .ts-dropdown .active {{ background: #f1f5f9; color: var(--text-main); }}
            .flatpickr-calendar {{ border-radius: 14px; box-shadow: 0 18px 40px rgba(15, 23, 42, .18); }}
            .record-type-badge {{ display:inline-flex; align-items:center; justify-content:center; min-width:42px; padding:3px 8px; border-radius:999px; font-size:11px; font-weight:800; }}
            .record-type-expense {{ background:#fee2e2; color:#991b1b; }}
            .record-type-income {{ background:#dcfce7; color:#166534; }}
            .export-form {{ margin: 0; }}
            .export-form button {{ width: auto; min-width: 108px; margin-bottom: 0; }}
            .filter-meta {{ display:flex; align-items:center; gap:18px; min-width:0; }}
            .checkbox-filter {{ min-height: 40px; display:inline-flex; align-items:center; gap:8px; color:var(--text-main); font-size:13px; font-weight:800; white-space:nowrap; }}
            .checkbox-filter input {{ width:14px; height:14px; margin:0; accent-color:var(--primary); }}
            @media (max-width: 680px) {{
                .container {{ padding-left: 8px; padding-right: 8px; }}
                .ip-banner {{ display: none; }}
                .filter-form {{ grid-template-columns: 1fr; }}
                .filter-footer {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
                .filter-meta {{ align-items:flex-start; flex-direction:column; gap:8px; }}
                .filter-actions {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
                .filter-action-group {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
                .history-table {{ min-width: 680px; }}
                .history-table th, .history-row td {{ padding: 8px 6px; font-size: 11px; }}
                .form-row {{ display: block; }}
            }}
        </style>
    </head>
    <body>
    <div class="container">
        <h2>记账中心</h2>
        <div class="nav-bar">
            <a href="/spendmoney/dashboard">📊 数据看板</a>
            <a href="/spendmoney/">🧾 上传与待办</a>
            <a href="/spendmoney/history" class="active">🗄️ 历史台账</a>
            <a href="/spendmoney/categories">🏷️ 标签管理</a>
            <a href="/nav/">🏠 返回主页</a>
        </div>
        
        <div class="ip-banner">
            <span>📲 快捷指令 POST 接口：<code>/spendmoney/api/iphone-upload</code></span>
        </div>
        <p class="history-help">点击表头排序；点击任意记录展开详情。</p>
        <div class="card filter-card">
            <form class="filter-form" action="/spendmoney/history" method="get">
                <input type="hidden" name="sort" value="{escape(sort)}">
                <input type="hidden" name="order" value="{escape(order)}">
                <input type="hidden" id="startDateFilter" name="start_date" value="{escape(start_date)}">
                <input type="hidden" id="endDateFilter" name="end_date" value="{escape(end_date)}">
                <div class="form-group">
                    <label>交易时间</label>
                    <input type="text" id="dateRangeFilter" value="{escape(date_range_value)}" placeholder="选择日期区间" autocomplete="off">
                </div>
                <div class="form-group">
                    <label>类型</label>
                    <select id="recordTypeFilter" name="record_type">{record_type_filter_options}</select>
                </div>
                <div class="form-group">
                    <label>标签</label>
                    <select id="categoryFilter" name="category" multiple placeholder="选择标签">{category_filter_options}</select>
                </div>
                <div class="filter-footer">
                    <div class="filter-meta">
                        <p class="filter-summary">当前显示 {filtered_count} 条记录</p>
                        <label class="checkbox-filter"><input type="checkbox" name="amortized" value="1" {amortized_checked}> 只看摊销</label>
                    </div>
                    <div class="filter-actions">
                        <a class="btn-link btn-cancel" href="{clear_filter_url}">清空</a>
                        <button type="submit" class="btn-primary">查询</button>
                        <button type="button" class="btn-export" onclick="window.location.href='/spendmoney/export.csv'">导出 CSV</button>
                    </div>
                </div>
            </form>
        </div>
        <div class="history-shell">
        <table class="history-table">
            <thead><tr>
                <th style="width:7%">{sort_link("id", "ID")}</th>
                <th style="width:18%">{sort_link("merchant", "商户")}</th>
                <th style="width:10%">{sort_link("record_type", "类型")}</th>
                <th style="width:17%">{sort_link("created_at", "录入时间")}</th>
                <th style="width:15%">{sort_link("date", "交易时间")}</th>
                <th style="width:18%">{sort_link("category", "标签")}</th>
                <th style="width:15%; text-align:right">{sort_link("amount", "金额")}</th>
            </tr></thead>
            <tbody>
    """

    for r in records:
        filename = escape(str(r[1]))
        merchant_val = escape(str(r[3] or ""))
        date_val = escape(str(r[4] or ""))
        raw_text = escape(str(r[5] or ""))
        subtotal_val = format_export_amount(r[7])
        tax_val = format_export_amount(r[8])
        cat_code = escape(str(r[9] or "0"))
        amount_val = float(r[2] or 0.0)
        amount_form_val = format_export_amount(r[2])
        created_at_val = escape(str(r[10] or "未知"))
        amortization_months_val = normalize_amortization_months(r[11] or 1, r[12] if len(r) > 12 else "expense")
        amortization_badge = f" · {amortization_months_val}月摊" if amortization_months_val > 1 else ""
        current_record_type = normalize_record_type(r[12] if len(r) > 12 else "expense")
        record_type_text = escape(record_type_label(current_record_type))
        record_type_badge_class = "record-type-income" if current_record_type == "income" else "record-type-expense"
        record_type_options = "".join([f"<option value='{escape(type_code)}' {'selected' if type_code==current_record_type else ''}>{escape(type_label)}</option>" for type_code, type_label in RECORD_TYPE_LABELS.items()])
        
        display_cat_name = cmap.get(cat_code, cat_code)
        display_cat_type = category_type_label(category_type_map.get(cat_code, "expense")) if cat_code in cmap else "未知"
        display_cat = escape(f"{display_cat_type} · {display_cat_name}")
        cat_options = "".join([
            f"<option value='{escape(code)}' data-record-type='{escape(category_type)}' {'selected' if code==cat_code else ''}>{escape(category_type_label(category_type))} · {escape(name)} ({escape(code)})</option>"
            for code, name, category_type in category_rows
        ])
        
        html += f"<tr class='history-row' data-detail='detail-{r[0]}' tabindex='0'>" \
                f"<td class='date-cell'>#{r[0]}</td>" \
                f"<td class='merchant-cell' title='{merchant_val}'>{merchant_val or '未知商户'}</td>" \
                f"<td><span class='record-type-badge {record_type_badge_class}'>{record_type_text}</span></td>" \
                f"<td class='date-cell'>{created_at_val}</td>" \
                f"<td class='date-cell'>{date_val or '未知'}</td>" \
                f"<td><span class='compact-badge' title='{display_cat}{amortization_badge}'>{display_cat}{amortization_badge}</span></td>" \
                f"<td class='amount-cell'>${amount_val:.2f}</td></tr>" \
                f"<tr class='detail-row' id='detail-{r[0]}'><td colspan='7'>" \
                f"<div class='history-detail'>" \
                f"<div class='detail-meta'>录入时间：{created_at_val} · 源文件：{filename}</div>" \
                f"<form action='/spendmoney/update' method='post'>" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='{escape(source_url)}'>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>记录类型</label><select name='record_type' class='record-type-edit'>{record_type_options}</select></div>" \
                f"<div class='form-group'><label>商户/来源</label><input type='text' name='merchant' value='{merchant_val}' required></div>" \
                f"<div class='form-group'><label>标签</label><select name='category' class='category-edit'>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易时间</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' min='0' step='0.01' name='subtotal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' min='0' step='0.01' name='tax' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' min='0' step='0.01' name='amount' value='{amount_form_val}' required></div>" \
                f"<div class='form-group amortization-field'><label>摊销月数</label><input type='number' min='1' max='120' step='1' name='amortization_months' inputmode='numeric' value='{amortization_months_val}'><div class='hint'>普通消费填 1；两月一交填 2；年费填 12。</div></div>" \
                f"<button type='submit' class='btn-edit'>保存修改</button></form>" \
                f"<form action='/spendmoney/delete' method='post' onsubmit=\"return confirm('确定要删除这条历史记录吗？');\">" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='{escape(source_url)}'>" \
                f"<button type='submit' class='btn-danger'>删除记录</button></form>" \
                f"<div class='ocr-details'><strong>OCR 原始信息</strong><br>{raw_text}</div>" \
                f"</div></td></tr>"

    if not records:
        html += "<tr><td colspan='7' style='text-align:center;color:var(--text-muted);padding:28px;'>暂无历史台账记录</td></tr>"

    return html + """
            </tbody>
        </table>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/tom-select@2.4.3/dist/js/tom-select.complete.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js"></script>
        <script>
            const recordTypeFilter = document.getElementById('recordTypeFilter');
            const recordTypeSelect = new TomSelect('#recordTypeFilter', {
                maxOptions: 20,
                allowEmptyOption: true,
                searchField: ['text']
            });
            const categorySelect = new TomSelect('#categoryFilter', {
                plugins: ['remove_button'],
                maxOptions: 500,
                hideSelected: true,
                closeAfterSelect: false,
                placeholder: '选择标签',
                score: function(search) {
                    const baseScore = this.getScoreFunction(search);
                    return function(item) {
                        const selectedType = recordTypeFilter.value;
                        if (selectedType && item.recordType !== selectedType) return 0;
                        return baseScore(item);
                    };
                },
                render: {
                    option: function(data, escape) {
                        return '<div data-record-type="' + escape(data.recordType || '') + '">' + escape(data.text) + '</div>';
                    }
                }
            });

            const allCategoryOptions = Object.values(categorySelect.options).map(function(option) {
                const sourceOption = categorySelect.input.querySelector('option[value="' + CSS.escape(option.value) + '"]');
                return Object.assign({}, option, {
                    recordType: sourceOption ? sourceOption.dataset.recordType : ''
                });
            });

            function syncCategoryFilterToType() {
                const selectedType = recordTypeFilter.value;
                categorySelect.items.slice().forEach(function(value) {
                    const option = allCategoryOptions.find(function(item) { return item.value === value; });
                    if (selectedType && option && option.recordType !== selectedType) {
                        categorySelect.removeItem(value, true);
                    }
                });
                categorySelect.clearOptions();
                allCategoryOptions.forEach(function(option) {
                    if (!selectedType || option.recordType === selectedType) {
                        categorySelect.addOption(option);
                    }
                });
                categorySelect.refreshOptions(false);
                categorySelect.refreshItems();
            }
            recordTypeFilter.addEventListener('change', syncCategoryFilterToType);
            recordTypeSelect.on('change', syncCategoryFilterToType);
            syncCategoryFilterToType();

            function syncEditCategoryOptions(form) {
                const typeSelect = form.querySelector('.record-type-edit');
                const categorySelect = form.querySelector('.category-edit');
                if (!typeSelect || !categorySelect) return;
                const amortizationField = form.querySelector('.amortization-field');
                const amortizationInput = amortizationField ? amortizationField.querySelector('input[name="amortization_months"]') : null;
                const isIncome = typeSelect.value === 'income';
                if (amortizationField) amortizationField.classList.toggle('is-hidden', isIncome);
                if (amortizationInput) {
                    amortizationInput.disabled = isIncome;
                    if (isIncome) amortizationInput.value = '1';
                }
                const selectedType = typeSelect.value;
                let firstAllowedValue = '';
                Array.from(categorySelect.options).forEach(function(option) {
                    const allowed = !selectedType || option.dataset.recordType === selectedType;
                    option.hidden = !allowed;
                    option.disabled = !allowed;
                    if (allowed && !firstAllowedValue) firstAllowedValue = option.value;
                });
                const currentOption = categorySelect.options[categorySelect.selectedIndex];
                if (currentOption && currentOption.disabled && firstAllowedValue) {
                    categorySelect.value = firstAllowedValue;
                }
            }
            document.querySelectorAll('.history-detail form').forEach(function(form) {
                const typeSelect = form.querySelector('.record-type-edit');
                if (!typeSelect) return;
                syncEditCategoryOptions(form);
                typeSelect.addEventListener('change', function() { syncEditCategoryOptions(form); });
            });

            const startDateInput = document.getElementById('startDateFilter');
            const endDateInput = document.getElementById('endDateFilter');
            const defaultDates = [startDateInput.value, endDateInput.value].filter(Boolean);
            flatpickr('#dateRangeFilter', {
                mode: 'range',
                dateFormat: 'Y-m-d',
                defaultDate: defaultDates,
                locale: { rangeSeparator: ' 至 ' },
                onChange: function(selectedDates, dateStr, instance) {
                    startDateInput.value = selectedDates[0] ? instance.formatDate(selectedDates[0], 'Y-m-d') : '';
                    endDateInput.value = selectedDates[1] ? instance.formatDate(selectedDates[1], 'Y-m-d') : (selectedDates[0] ? instance.formatDate(selectedDates[0], 'Y-m-d') : '');
                },
                onReady: function(selectedDates, dateStr, instance) {
                    if (defaultDates.length === 2) {
                        instance.input.value = defaultDates.join(' 至 ');
                    }
                }
            });

            document.querySelectorAll('.history-row').forEach(function(row) {
                function toggleDetail() {
                    const detail = document.getElementById(row.dataset.detail);
                    const willOpen = !detail.classList.contains('open');
                    document.querySelectorAll('.detail-row.open').forEach(item => item.classList.remove('open'));
                    document.querySelectorAll('.history-row.open').forEach(item => item.classList.remove('open'));
                    if (willOpen) {
                        detail.classList.add('open');
                        row.classList.add('open');
                    }
                }
                row.addEventListener('click', toggleDetail);
                row.addEventListener('keydown', function(event) {
                    if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        toggleDetail();
                    }
                });
            });
        </script>
    </div></body></html>"""

@app.get("/export.csv")
async def export_csv(request: Request):
    owner_user_id = get_owner_user_id(request)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT r.id, r.date, r.merchant, COALESCE(r.record_type, 'expense'),
               COALESCE(c.name, r.category), r.subtotal, r.tax, r.amount, r.raw_text
        FROM records r
        LEFT JOIN categories c ON r.owner_user_id = c.owner_user_id AND r.category = c.code
        WHERE r.owner_user_id=? AND r.status = 'confirmed'
        ORDER BY CASE WHEN r.date IS NULL OR r.date = '' THEN 1 ELSE 0 END ASC,
                 r.date DESC, r.id DESC
    """, (owner_user_id,))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["external_id", "date", "merchant", "type", "category", "subtotal", "tax", "amount", "raw_text"])
    for row in rows:
        writer.writerow([
            f"spendmoney-{row[0]}",
            format_export_date(row[1]),
            row[2] or "",
            normalize_record_type(row[3]),
            row[4] or "",
            format_export_amount(row[5]),
            format_export_amount(row[6]),
            format_export_amount(row[7]),
            row[8] or "",
        ])

    filename = f"spendmoney-export-{datetime.date.today().isoformat()}.csv"
    return Response(
        content=output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/categories", response_class=HTMLResponse)
async def manage_categories(request: Request):
    owner_user_id = get_owner_user_id(request)
    rows = get_category_rows(owner_user_id)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS category_budgets
                      (owner_user_id TEXT NOT NULL,
                       category_code TEXT NOT NULL,
                       monthly_budget REAL NOT NULL DEFAULT 0,
                       PRIMARY KEY (owner_user_id, category_code))""")
    cursor.execute("SELECT category_code, monthly_budget FROM category_budgets WHERE owner_user_id=?", (owner_user_id,))
    budget_by_code = {str(code): float(amount or 0.0) for code, amount in cursor.fetchall()}
    conn.commit()
    conn.close()
    grouped = {"expense": [], "income": []}
    for code, name, category_type in rows:
        grouped.setdefault(normalize_category_type(category_type), []).append((code, name, budget_by_code.get(str(code), 0.0)))

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>标签管理 - SpendMoney</title>
        {COMMON_HEAD}
        <style>
            .type-badge {{ display:inline-flex; align-items:center; justify-content:center; min-width:42px; padding:4px 9px; border-radius:999px; font-size:12px; font-weight:800; }}
            .type-expense {{ background:#fee2e2; color:#991b1b; }}
            .type-income {{ background:#dcfce7; color:#166534; }}
            .category-section-title {{ display:flex; align-items:center; gap:8px; margin:18px 0 8px; }}
            .category-table-wrap {{ overflow-x:auto; background:#fff; border:1px solid var(--border); border-radius:10px; box-shadow:0 2px 8px rgba(15,23,42,.04); margin-bottom:14px; }}
            .category-grid {{ min-width:720px; }}
            .category-grid-head, .category-line {{ display:grid; grid-template-columns:100px 110px minmax(180px, 1fr) 130px 150px; gap:10px; align-items:center; }}
            .category-grid-head {{ padding:9px 12px; font-size:12px; color:var(--text-muted); background:#f8fafc; border-bottom:1px solid var(--border); font-weight:800; }}
            .category-line {{ padding:8px 12px; border-bottom:1px solid #f1f5f9; margin:0; }}
            .category-line:last-child {{ border-bottom:0; }}
            .category-line input, .category-line select {{ min-height:32px; height:32px; padding:4px 8px; margin:0; font-size:13px; border-radius:6px; }}
            .category-code {{ color:#64748b; background:#f8fafc; }}
            .category-actions {{ display:flex; gap:8px; justify-content:flex-end; }}
            .category-actions button {{ width:auto; min-height:32px; height:32px; padding:0 12px; margin:0; border-radius:8px; font-size:13px; }}
            @media (max-width: 680px) {{ .category-grid {{ min-width:680px; }} }}
        </style>
    </head>
    <body>
    <div class="container">
        <h2>记账中心</h2>
        <div class="nav-bar">
            <a href="/spendmoney/dashboard">📊 数据看板</a>
            <a href="/spendmoney/">🧾 上传与待办</a>
            <a href="/spendmoney/history">🗄️ 历史台账</a>
            <a href="/spendmoney/categories" class="active">🏷️ 标签管理</a>
            <a href="/nav/">🏠 返回主页</a>
        </div>

        <div class="card" style="padding:14px 16px;">
            <form action="/spendmoney/category_add" method="post" class="form-row" style="align-items:flex-end; margin:0; gap:10px;">
                <div class="form-group" style="margin-bottom:0;"><label>类型</label><select name="category_type"><option value="expense">支出</option><option value="income">收入</option></select></div>
                <div class="form-group" style="margin-bottom:0;"><label>代码</label><input type="number" name="code" required></div>
                <div class="form-group" style="margin-bottom:0; flex:2;"><label>名称</label><input type="text" name="name" required></div>
                <div class="form-group" style="margin-bottom:0;"><label>月预算</label><input type="number" step="0.01" min="0" name="monthly_budget" value="0"></div>
                <button type="submit" style="width:auto; margin-bottom:0;">新增</button>
            </form>
        </div>
    """

    for category_type in ("expense", "income"):
        label = category_type_label(category_type)
        badge_class = "type-expense" if category_type == "expense" else "type-income"
        html += f"""
        <h3 class="category-section-title"><span class="type-badge {badge_class}">{label}</span><span>{label}标签</span></h3>
        """
        if not grouped.get(category_type):
            html += "<div class='card'><div style='text-align:center;color:var(--text-muted);padding:18px;'>暂无标签</div></div>"
            continue
        html += """
        <div class='category-table-wrap'>
            <div class='category-grid'>
                <div class='category-grid-head'><span>代码</span><span>类型</span><span>名称</span><span>月预算</span><span style='text-align:right;'>操作</span></div>
        """
        for code, name, monthly_budget in grouped[category_type]:
            type_options = "".join([
                f"<option value='{escape(type_code)}' {'selected' if type_code == category_type else ''}>{escape(type_label)}</option>"
                for type_code, type_label in CATEGORY_TYPE_LABELS.items()
            ])
            budget_disabled = "disabled" if category_type == "income" else ""
            safe_code = escape(code)
            html += f"""
                <form action="/spendmoney/category_update" method="post" class="category-line">
                    <input class="category-code" type="text" name="code" value="{safe_code}" readonly>
                    <select name="category_type">{type_options}</select>
                    <input type="text" name="name" value="{escape(name)}" required>
                    <input type="number" step="0.01" min="0" name="monthly_budget" value="{monthly_budget:.2f}" {budget_disabled}>
                    <div class="category-actions">
                        <button type="submit" class="btn-edit">保存</button>
                        <button type="submit" class="btn-danger" form="delete-category-{safe_code}">删除</button>
                    </div>
                </form>
                <form id="delete-category-{safe_code}" action="/spendmoney/category_delete" method="post" onsubmit="return confirm('删除标签后，旧数据将只显示底层数字代码。确定删除吗？');" style="display:none;">
                    <input type="hidden" name="code" value="{safe_code}">
                </form>
            """
        html += """
            </div>
        </div>
        """

    return html + "</div></body></html>"

@app.post("/category_add", response_class=HTMLResponse)
async def category_add(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    category_type: str = Form("expense"),
    monthly_budget: float = Form(0.0)
):
    owner_user_id = get_owner_user_id(request)
    category_type = normalize_category_type(category_type)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO categories (owner_user_id, code, name, category_type) VALUES (?, ?, ?, ?)",
            (owner_user_id, code.strip(), name.strip(), category_type)
        )
        if category_type == "expense" and monthly_budget > 0:
            cursor.execute(
                "INSERT OR REPLACE INTO category_budgets (owner_user_id, category_code, monthly_budget) VALUES (?, ?, ?)",
                (owner_user_id, code.strip(), monthly_budget)
            )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()
    return """<script>window.location.href='/spendmoney/categories';</script>"""

@app.post("/category_update", response_class=HTMLResponse)
async def category_update(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    category_type: str = Form("expense"),
    monthly_budget: float = Form(0.0)
):
    owner_user_id = get_owner_user_id(request)
    category_type = normalize_category_type(category_type)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE categories SET name=?, category_type=? WHERE owner_user_id=? AND code=?",
        (name.strip(), category_type, owner_user_id, code)
    )
    if category_type == "expense" and monthly_budget > 0:
        cursor.execute(
            "INSERT OR REPLACE INTO category_budgets (owner_user_id, category_code, monthly_budget) VALUES (?, ?, ?)",
            (owner_user_id, code, monthly_budget)
        )
    else:
        cursor.execute("DELETE FROM category_budgets WHERE owner_user_id=? AND category_code=?", (owner_user_id, code))
    conn.commit()
    conn.close()
    return """<script>window.location.href='/spendmoney/categories';</script>"""

@app.post("/category_delete", response_class=HTMLResponse)
async def category_delete(request: Request, code: str = Form(...)):
    owner_user_id = get_owner_user_id(request)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM categories WHERE owner_user_id=? AND code=?", (owner_user_id, code))
    cursor.execute("DELETE FROM category_budgets WHERE owner_user_id=? AND category_code=?", (owner_user_id, code))
    conn.commit()
    conn.close()
    return """<script>window.location.href='/spendmoney/categories';</script>"""

@app.post("/upload", response_class=HTMLResponse)
async def upload_receipt(request: Request, file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    original_name = os.path.basename(file.filename or "receipt.jpg")
    safe_name = f"{uuid.uuid4().hex}_{original_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            buffer.write(content)

    await asyncio.to_thread(compress_and_fix_image, file_path)

    preview_url = f"/spendmoney/uploads/{quote(safe_name)}"
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head>
            <title>确认并裁切</title>
            <link href="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.css" rel="stylesheet">
            <script src="https://cdnjs.cloudflare.com/ajax/libs/cropperjs/1.5.13/cropper.min.js"></script>
            {COMMON_HEAD}
        </head>
        <body>
        <div id="loading-overlay">
            <div class="spinner"></div>
            <div id="loading-text" style="font-weight:bold; font-size: 16px; color:var(--text-main);">AI 引擎正在提取数据...</div>
        </div>
        <div class="container"><div class="card">
    <h2>框选识别区域</h2>
    <p class="hint">如不框选则默认识别全图。框选所需区域后点击提取。</p>
    
    <div class="img-container">
        <img id="preview-image" src="{preview_url}">
    </div>
    
    <form action="/spendmoney/confirm" method="post" onsubmit="showLoading()">
        <input type="hidden" name="filename" value="{escape(safe_name)}">
        <input type="hidden" id="crop_x" name="crop_x" value="0">
        <input type="hidden" id="crop_y" name="crop_y" value="0">
        <input type="hidden" id="crop_w" name="crop_w" value="0">
        <input type="hidden" id="crop_h" name="crop_h" value="0">
        <button type="submit" class="btn-primary">开始提取数据</button>
    </form>
    
    <form action="/spendmoney/cancel" method="post">
        <input type="hidden" name="filename" value="{escape(safe_name)}">
        <button type="submit" class="btn-cancel">拍模糊了，取消删除</button>
    </form>
    </div></div>
    
    <script>
        window.onload = function() {{
            const image = document.getElementById('preview-image');
            const cropper = new Cropper(image, {{
                viewMode: 1,
                dragMode: 'crop',
                autoCrop: false,
                zoomable: false,
                crop(event) {{
                    document.getElementById('crop_x').value = Math.round(event.detail.x);
                    document.getElementById('crop_y').value = Math.round(event.detail.y);
                    document.getElementById('crop_w').value = Math.round(event.detail.width);
                    document.getElementById('crop_h').value = Math.round(event.detail.height);
                }}
            }});
        }};
    </script>
    </body></html>
    """

@app.post("/cancel", response_class=HTMLResponse)
async def cancel_upload(filename: str = Form(...)):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(UPLOAD_DIR, safe_name)
    if os.path.exists(file_path):
        os.remove(file_path)
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>已取消</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">🗑️</div>
                <h2 class="msg-title">操作已取消</h2>
                <p class="hint">文件已安全清理，正在返回主页...</p>
            </div>
            <script>setTimeout(function(){{ window.location.href='/spendmoney/'; }}, 1000);</script>
        </body>
        </html>
    """

@app.post("/confirm", response_class=HTMLResponse)
async def confirm_receipt(
    request: Request,
    filename: str = Form(...), 
    crop_x: int = Form(0), 
    crop_y: int = Form(0), 
    crop_w: int = Form(0), 
    crop_h: int = Form(0)
):
    file_path = os.path.join(UPLOAD_DIR, os.path.basename(filename))

    if not os.path.exists(file_path):
        return "File not found. <a href='/spendmoney/'>Return</a>"

    owner_user_id = get_owner_user_id(request)
    result = await asyncio.to_thread(process_receipt_file, file_path, os.path.basename(filename), crop_x, crop_y, crop_w, crop_h, owner_user_id)
    status = escape(result.get("status", "unknown"))

    if status == "ocr_failed":
        return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>提取失败</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card" style="border-color: var(--danger);">
                <div class="msg-icon">⚠️</div>
                <h2 class="msg-title" style="color: var(--danger);">提取失败</h2>
                <p class="hint">OCR 引擎发生错误，请检查后台日志。</p>
                <a href='/spendmoney/' class="btn-link btn-cancel">返回记账主页</a>
            </div>
        </body>
        </html>
        """

    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>提取成功</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">✨</div>
                <h2 class="msg-title" style="color: var(--success);">数据提取成功！</h2>
                <p class="hint">系统已生成草稿，正在跳转核对页面...</p>
            </div>
            <script>setTimeout(function(){{ window.location.href='/spendmoney/'; }}, 1000);</script>
        </body>
        </html>
    """

@app.post("/manual_add", response_class=HTMLResponse)
async def manual_add(
    request: Request,
    merchant: str = Form(...),
    date: str = Form(""),
    subtotal: float = Form(0.0),
    tax: float = Form(0.0),
    amount: float = Form(...),
    category: str = Form(""),
    record_type: str = Form("expense"),
    amortization_months: int = Form(1)
):
    owner_user_id = get_owner_user_id(request)
    record_type = normalize_record_type(record_type)
    category = str(category or "").strip()
    if not category:
        return validation_error_page("请选择分类标签", "保存前需要明确选择一个标签，避免误存到默认分类。", "/spendmoney/")
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    filename = f"manual_{uuid.uuid4().hex}"
    raw_text = "【手工录入】无原件"
    status = 'confirmed' 
    
    cursor.execute(
        "INSERT INTO records (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at, amortization_months, record_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)",
        (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, normalize_amortization_months(amortization_months, record_type), record_type)
    )
    conn.commit()
    conn.close()
    
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>保存成功</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">✓</div>
                <h2 class="msg-title" style="color: var(--success);">手工记账成功</h2>
                <p class="hint">数据已安全更新并直接归档。</p>
            </div>
            <script>setTimeout(function(){{ window.location.href='/spendmoney/'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/update", response_class=HTMLResponse)
async def update(
    request: Request,
    id: int = Form(...), 
    amount: float = Form(...), 
    merchant: str = Form(...), 
    date: str = Form(""),
    subtotal: float = Form(0.0),
    tax: float = Form(0.0),
    category: str = Form(""),
    record_type: str = Form("expense"),
    amortization_months: int = Form(1),
    source: str = Form("/spendmoney/")
):
    owner_user_id = get_owner_user_id(request)
    record_type = normalize_record_type(record_type)
    category = str(category or "").strip()
    if not category:
        return validation_error_page("请选择分类标签", "保存前需要明确选择一个标签，避免误存到默认分类。", source)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE records SET amount=?, merchant=?, date=?, subtotal=?, tax=?, category=?, record_type=?, amortization_months=?, status='confirmed' WHERE id=? AND owner_user_id=?",
                   (amount, merchant, date, subtotal, tax, category, record_type, normalize_amortization_months(amortization_months, record_type), id, owner_user_id))
    conn.commit()
    conn.close()
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>保存成功</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">✓</div>
                <h2 class="msg-title" style="color: var(--success);">保存成功</h2>
                <p class="hint">数据已安全更新并归档。</p>
            </div>
            <script>setTimeout(function(){{ window.location.href='{escape(spendmoney_return_url(source))}'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/delete", response_class=HTMLResponse)
async def delete_record(request: Request, id: int = Form(...), source: str = Form("/spendmoney/")):
    owner_user_id = get_owner_user_id(request)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM records WHERE id=? AND owner_user_id=?", (id, owner_user_id))
    conn.commit()
    conn.close()
    return f"""
        <!doctype html>
        <html lang="zh-CN">
        <head><title>删除成功</title>{COMMON_HEAD}</head>
        <body class="msg-page">
            <div class="card msg-card">
                <div class="msg-icon">🗑️</div>
                <h2 class="msg-title">记录已彻底删除</h2>
                <p class="hint">正在返回上级页面...</p>
            </div>
            <script>setTimeout(function(){{ window.location.href='{escape(spendmoney_return_url(source))}'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/api/iphone-upload")
async def receive_iphone_receipt(request: Request):
    api_key = request.headers.get("X-API-Key", "")
    owner_user_id = get_api_key_owner(api_key)
    if not owner_user_id:
        return JSONResponse(status_code=401, content={
            "status": "error",
            "message": "Unauthorized"
        })

    """
    专供 iPhone 快捷指令使用的 API 接口。
    接收由端侧 AI 提取出的 JSON 文本，并作为草稿入库。
    """
    try:
        # 1. 抓取快捷指令发来的标准外层 JSON
        body_bytes = await request.body()
        outer_data = json.loads(body_bytes.decode('utf-8'))
        
        print("\n================ [IPHONE DEBUG START] ================")
        print("1. Outer JSON Received from Shortcut:")
        print(json.dumps(outer_data, indent=2, ensure_ascii=False))
        print("------------------------------------------------------")
        
        # 2. 从键值对中提取出端侧 AI 真正生成的内层小票文本
        inner_json_text = outer_data.get('raw_text', '').strip()
        
        if not inner_json_text:
            # 容错：如果手机端不小心还是把整个文本作为 body 发过来了，尝试直接解析
            inner_json_text = body_bytes.decode('utf-8').strip()

        # 3. 容错清洗（剥离端侧 AI 偶尔夹带的 markdown 标记）
        if inner_json_text.startswith("```"):
            inner_json_text = inner_json_text.replace("```json", "").replace("```", "").strip()
            
        # 4. 解析真正的小票数据
        receipt_info = json.loads(inner_json_text)
        
        print("2. Parsed Inner Receipt JSON Dict:")
        print(json.dumps(receipt_info, indent=4, ensure_ascii=False))
        print("------------------------------------------------------")
        
        # 5. 提取核心字段
        merchant = receipt_info.get('merchant', 'Unknown Store')
        date_str = receipt_info.get('date', "")
        if date_str is None:
            date_str = ""
            
        subtotal = float(receipt_info.get('subtotal', 0.00))
        tax = float(receipt_info.get('tax', 0.00))
        total = float(receipt_info.get('total', 0.00))
        
        category_code = ""
        status = "processed" 
        virtual_filename = f"iphone_ai_{uuid.uuid4().hex[:8]}"
        raw_text_for_db = f"【iPhone 端侧 AI 原始解析】\n{json.dumps(receipt_info, indent=2, ensure_ascii=False)}"
        
        # 6. 写入数据库
        conn = sqlite3.connect('finance.db')
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO records 
            (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """,
            (owner_user_id, virtual_filename, total, merchant, date_str, subtotal, tax, category_code, raw_text_for_db, status)
        )
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        print(f"3. Success! Draft saved to DB with ID: {record_id}")
        print("================= [IPHONE DEBUG END] =================\n")
        
        return JSONResponse(status_code=201, content={
            "status": "success",
            "message": "Draft saved successfully.",
            "data": {"id": record_id, "merchant": merchant, "total": total}
        })
        
    except json.JSONDecodeError as e:
        print(f"[-] JSON Decode Error: {str(e)}")
        print("================= [IPHONE DEBUG END] =================\n")
        return JSONResponse(status_code=400, content={
            "status": "error", 
            "message": f"Invalid JSON payload: {str(e)}"
        })
    except Exception as e:
        print(f"[-] Unexpected Error: {str(e)}")
        print("================= [IPHONE DEBUG END] =================\n")
        return JSONResponse(status_code=500, content={
            "status": "error", 
            "message": f"Server error: {str(e)}"
        })
