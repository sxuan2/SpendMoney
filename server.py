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
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    
    # 核心总计指标
    cursor.execute("SELECT COUNT(*) FROM records WHERE owner_user_id=? AND status != 'confirmed'", (owner_user_id,))
    pending_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT SUM(amount), SUM(tax), SUM(subtotal), COUNT(*) FROM records WHERE owner_user_id=? AND status = 'confirmed'", (owner_user_id,))
    row = cursor.fetchone()
    total_amount = row[0] if row[0] else 0.0
    total_tax = row[1] if row[1] else 0.0
    total_subtotal = row[2] if row[2] else 0.0
    confirmed_count = row[3] if row[3] else 0
    
    # 计算近 15 天消费趋势所需数据
    now = datetime.datetime.now()
    days_15 = []
    for i in range(14, -1, -1):
        d = now - datetime.timedelta(days=i)
        days_15.append(d.strftime("%Y-%m-%d"))
        
    cutoff_day = days_15[0]
    cursor.execute("""
        SELECT date, SUM(amount) 
        FROM records 
        WHERE owner_user_id=? AND status = 'confirmed' AND date != '' AND date >= ?
        GROUP BY date 
        ORDER BY date ASC
    """, (owner_user_id, cutoff_day))
    
    day_trend_data = cursor.fetchall()
    day_trend_dict = {d: 0.0 for d in days_15}
    for r in day_trend_data:
        d_str, amt = r[0], r[1]
        if d_str in day_trend_dict:
            day_trend_dict[d_str] = amt
            
    amounts_15 = list(day_trend_dict.values())
    # ============================================================================

    # 1. 计算大图表所需的过去 6 个月时间轴
    last_6_months = []
    for i in range(5, -1, -1):
        m = now.month - i
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        last_6_months.append(f"{y:04d}-{m:02d}")
    
    cutoff_month = last_6_months[0]
    trend_dict = {m: 0.0 for m in last_6_months}
    cursor.execute("""
        SELECT date, amount, COALESCE(amortization_months, 1)
        FROM records
        WHERE owner_user_id=? AND status = 'confirmed' AND date != ''
    """, (owner_user_id,))
    confirmed_records = cursor.fetchall()

    def add_months(month_text, offset):
        year, month_number = map(int, month_text.split("-"))
        absolute_month = year * 12 + month_number - 1 + offset
        return f"{absolute_month // 12:04d}-{absolute_month % 12 + 1:02d}"

    for record_date, record_amount, amortization_months in confirmed_records:
        start_month = str(record_date)[:7]
        months = max(1, int(amortization_months or 1))
        monthly_amount = (record_amount or 0.0) / months
        for offset in range(months):
            target_month = add_months(start_month, offset)
            if target_month in trend_dict:
                trend_dict[target_month] += monthly_amount
            
    dates = list(trend_dict.keys())
    amounts = list(trend_dict.values())
    
    # 2. 动态生成供下拉选择的最近 6 个月历史轴
    dropdown_months = []
    for i in range(6):
        m = now.month - i
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        dropdown_months.append(f"{y:04d}-{m:02d}")
        
    selected_month = month if month else now.strftime("%Y-%m")
    
    month_options = ""
    for dm in dropdown_months:
        sel = "selected" if dm == selected_month else ""
        month_options += f"<option value='{dm}' {sel}>{dm}</option>"
    
    # 3. 抓取选定月份的聚合分类数据
    cursor.execute("""
        SELECT r.date, r.amount, COALESCE(r.amortization_months, 1),
               COALESCE(c.name, r.category)
        FROM records r
        LEFT JOIN categories c ON r.owner_user_id = c.owner_user_id AND r.category = c.code
        WHERE r.owner_user_id=? AND r.status = 'confirmed' AND r.date != ''
    """, (owner_user_id,))
    category_totals = {}
    for record_date, record_amount, amortization_months, cat_name in cursor.fetchall():
        start_month = str(record_date)[:7]
        months = max(1, int(amortization_months or 1))
        covered_months = (add_months(start_month, offset) for offset in range(months))
        if selected_month in covered_months:
            category_totals[cat_name] = category_totals.get(cat_name, 0.0) + (record_amount or 0.0) / months
    month_category_data = sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    category_pie = [{"name": r[0] if r[0] else "未知代码", "value": r[1]} for r in month_category_data]
    
    # 4. 组装月度分类汇总表 HTML
    table_rows = ""
    month_total = 0.0
    for r in month_category_data:
        cat_name = escape(r[0] if r[0] else "未知代码")
        amt = r[1]
        month_total += amt
        table_rows += f"""
            <tr style="border-bottom: 1px solid #f3f4f6;">
                <td style="padding: 12px 8px; font-weight: 600; color: var(--text-main);">{cat_name}</td>
                <td style="padding: 12px 8px; text-align: right; color: var(--success); font-weight: bold;">${amt:.2f}</td>
            </tr>
        """
    if not month_category_data:
        table_rows = "<tr><td colspan='2' style='padding: 24px; text-align: center; color: var(--text-muted); font-weight: 600;'>该月份暂无任何入库账单</td></tr>"
    else:
        table_rows += f"""
            <tr style="background: #fafafa;">
                <td style="padding: 14px 8px; font-weight: 800; color: var(--text-main);">本月统计支出（含摊销）</td>
                <td style="padding: 14px 8px; text-align: right; color: var(--text-main); font-weight: 800; font-size: 18px;">${month_total:.2f}</td>
            </tr>
        """

    # AI 引擎性能数据
    cursor.execute("SELECT COUNT(*) FROM records WHERE owner_user_id=? AND status = 'ocr_failed'", (owner_user_id,))
    failed_count = cursor.fetchone()[0] or 0
    total_processed = confirmed_count + pending_count
    success_rate = 100.0
    if total_processed + failed_count > 0:
        success_rate = (total_processed / (total_processed + failed_count)) * 100
        
    cursor.execute("SELECT COUNT(*) FROM records WHERE owner_user_id=? AND status = 'confirmed' AND (date = '' OR merchant = 'Unknown')", (owner_user_id,))
    incomplete_count = cursor.fetchone()[0] or 0
    completeness_rate = 100.0
    if confirmed_count > 0:
        completeness_rate = ((confirmed_count - incomplete_count) / confirmed_count) * 100
        
    aov = (total_amount / confirmed_count) if confirmed_count > 0 else 0.0
    tax_ratio = (total_tax / total_subtotal * 100) if total_subtotal > 0 else 0.0
    
    conn.close()

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>数据看板 - SpendMoney</title>
        <script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
        {COMMON_HEAD}
        <style>
            .chart-container {{
                width: 100% !important;
                min-width: 100% !important;
                height: 320px;
            }}
            .chart-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
                gap: 16px;
                width: 100%;
            }}
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
        
        <div class="ip-banner">
            <span>📲 快捷指令 POST 接口：<code>/spendmoney/api/iphone-upload</code></span>
        </div>

        <div class="kpi-grid">
            <div class="card">
                <div class="kpi-title">总消费金额</div>
                <div class="kpi-value green">${total_amount:.2f}</div>
            </div>
            <div class="card">
                <div class="kpi-title">累计贡献税费</div>
                <div class="kpi-value">${total_tax:.2f}</div>
            </div>
            <div class="card">
                <div class="kpi-title">待办复核任务</div>
                <div class="kpi-value blue">{pending_count}</div>
            </div>
            <div class="card">
                <div class="kpi-title">已入库总数</div>
                <div class="kpi-value">{confirmed_count}</div>
            </div>
        </div>

        <div class="chart-grid">
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="kpi-title">近 6 个月统计趋势（含摊销）</div>
                <div id="trendChart" class="chart-container"></div>
            </div>
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="kpi-title">近 15 天消费趋势</div>
                <div id="dayTrendChart" class="chart-container"></div>
            </div>
        </div>

        <div class="chart-grid">
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="row" style="align-items: center; margin-bottom: 0;">
                    <div class="kpi-title" style="margin: 0;">筛选账期</div>
                    <select id="monthSelector" onchange="location.href='/spendmoney/dashboard?month=' + this.value" style="width: auto; padding: 6px 12px; font-size: 14px; margin: 0; border-radius: 8px;">
                        {month_options}
                    </select>
                </div>
                <div id="categoryChart" class="chart-container"></div>
            </div>
            
            <div class="card">
                <div class="kpi-title">{selected_month} 分类汇总（含摊销）</div>
                <table style="width: 100%; border-collapse: collapse; text-align: left; margin-top: 12px;">
                    <thead>
                        <tr style="border-bottom: 2px solid var(--border);">
                            <th style="padding: 12px 8px; color: var(--text-muted); font-size: 14px; font-weight: 600;">标签名称</th>
                            <th style="padding: 12px 8px; color: var(--text-muted); font-size: 14px; font-weight: 600; text-align: right;">当月汇总金额</th>
                        </tr>
                    </thead>
                    <tbody>
                        {table_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        var trendChart = echarts.init(document.getElementById('trendChart'));
        var dayTrendChart = echarts.init(document.getElementById('dayTrendChart'));
        var categoryChart = echarts.init(document.getElementById('categoryChart'));

        // 近 6 个月消费趋势配置
        var trendOption = {{
            tooltip: {{ trigger: 'axis', formatter: function(params) {{ var item = params[0]; return item.marker + '月度消费: $' + Number(item.value).toFixed(2); }} }},
            grid: {{ left: '3%', right: '4%', bottom: '3%', containLabel: true }},
            xAxis: {{ type: 'category', data: {json.dumps(dates)} }},
            yAxis: {{ type: 'value' }},
            series: [{{
                name: '月度消费', type: 'bar', barMaxWidth: 42,
                itemStyle: {{ color: '#334155', borderRadius: [5, 5, 0, 0] }},
                data: {json.dumps(amounts)}
            }}, {{
                name: '趋势线', type: 'line', symbol: 'none', smooth: true,
                lineStyle: {{ color: '#64748b', width: 2, type: 'dashed' }},
                data: {json.dumps(amounts)}
            }}]
        }};
        trendChart.setOption(trendOption);

        // 近 15 天消费趋势配置
        var dayTrendOption = {{
            tooltip: {{ trigger: 'axis', formatter: function(params) {{ var item = params[0]; return item.marker + '日度消费: $' + Number(item.value).toFixed(2); }} }},
            grid: {{ left: '3%', right: '4%', bottom: '8%', containLabel: true }},
            xAxis: {{ type: 'category', data: {json.dumps(days_15)}, axisLabel: {{ formatter: function(value) {{ return value.slice(5); }}, rotate: 35 }} }},
            yAxis: {{ type: 'value' }},
            series: [{{
                name: '日度消费', type: 'bar', barMaxWidth: 28,
                itemStyle: {{ color: '#10b981', borderRadius: [4, 4, 0, 0] }},
                data: {json.dumps(amounts_15)}
            }}, {{
                name: '趋势线', type: 'line', symbol: 'none', smooth: true,
                lineStyle: {{ color: '#047857', width: 2, type: 'dashed' }},
                data: {json.dumps(amounts_15)}
            }}]
        }};
        dayTrendChart.setOption(dayTrendOption);

        // 分类饼图配置
        var categoryOption = {{
            tooltip: {{ trigger: 'item', formatter: function(item) {{ return item.marker + item.name + ': $' + Number(item.value).toFixed(2) + ' (' + Number(item.percent).toFixed(2) + '%)'; }} }},
            legend: {{ orient: 'horizontal', bottom: 'bottom' }},
            series: [{{
                name: '分类',
                type: 'pie',
                radius: ['45%', '75%'],
                avoidLabelOverlap: false,
                itemStyle: {{ borderRadius: 10, borderColor: '#fff', borderWidth: 2 }},
                label: {{ show: false, position: 'center' }},
                emphasis: {{ label: {{ show: true, fontSize: 16, fontWeight: 'bold' }} }},
                labelLine: {{ show: false }},
                data: {json.dumps(category_pie)}
            }}]
        }};
        categoryChart.setOption(categoryOption);
        
        // 终极修复：使用多个时间步长连续强刷重绘，确保容器彻底在浏览器定型后将图表完全撑开
        function forceResize() {{
            trendChart.resize();
            dayTrendChart.resize();
            categoryChart.resize();
        }}
        
        // 页面初始化及加载后全量刷新
        forceResize();
        setTimeout(forceResize, 50);
        setTimeout(forceResize, 200);
        
        // 监听窗口大小改变
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
    expense_options = "".join([f"<option value='{escape(code)}' {'selected' if code=='0' else ''}>{escape(name)}</option>" for code, name, _ in expense_rows])
    income_options = "".join([f"<option value='{escape(code)}'>{escape(name)}</option>" for code, name, _ in income_rows])
    category_options_by_type = json.dumps({
        "expense": [{"code": code, "name": name} for code, name, _ in expense_rows],
        "income": [{"code": code, "name": name} for code, name, _ in income_rows],
    }, ensure_ascii=False)
    
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, amount, merchant, date, raw_text, status, subtotal, tax, category, COALESCE(record_type, 'expense') FROM records WHERE owner_user_id=? AND status != 'confirmed'", (owner_user_id,))
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
                        <select id="manualCategory" name="category">
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
                <div class="form-group"><label><input type="checkbox" name="annual_expense" value="true"> 年度支出（统计按 12 个月均摊）</label></div>
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
        cat_code = escape(str(r[9] or "0"))
        amount_val = format_export_amount(r[2])
        current_record_type = normalize_record_type(r[10] if len(r) > 10 else "expense")
        record_type_options = "".join([f"<option value='{escape(type_code)}' {'selected' if type_code==current_record_type else ''}>{escape(type_label)}</option>" for type_code, type_label in RECORD_TYPE_LABELS.items()])
        
        cat_options = "".join([f"<option value='{escape(code)}' {'selected' if code==cat_code else ''}>{escape(name)} (代码: {escape(code)})</option>" for code, name in cmap.items()])
        
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
                f"<div class='form-group'><label>确认分类标签</label><select name='category'>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易日期</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' min='0' step='0.01' name='subtotal' inputmode='decimal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' min='0' step='0.01' name='tax' inputmode='decimal' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' min='0' step='0.01' name='amount' inputmode='decimal' value='{amount_val}' required></div>" \
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
        function refreshManualCategories() {{
            const items = manualCategoryOptions[manualRecordType.value] || [];
            manualCategory.innerHTML = items.map(function(item) {{
                return `<option value="${{item.code}}">${{item.name}}</option>`;
            }}).join('');
        }}
        if (manualRecordType && manualCategory) {{
            manualRecordType.addEventListener('change', refreshManualCategories);
        }}
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
):
    owner_user_id = get_owner_user_id(request)
    category_rows = get_category_rows(owner_user_id)
    cmap = {code: name for code, name, _ in category_rows}
    category_type_map = {code: category_type for code, _, category_type in category_rows}

    start_date = (start_date or "").strip()[:10]
    end_date = (end_date or "").strip()[:10]
    record_type = normalize_record_type(record_type) if record_type else ""
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

    active_filters = {"start_date": start_date, "end_date": end_date, "record_type": record_type}
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
            .filter-card {{ padding: 20px 24px 18px; margin-bottom: 12px; }}
            .filter-form {{ display: grid; grid-template-columns: 260px 150px 340px; column-gap: 22px; row-gap: 16px; align-items: end; }}
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
            @media (max-width: 680px) {{
                .container {{ padding-left: 8px; padding-right: 8px; }}
                .ip-banner {{ display: none; }}
                .filter-form {{ grid-template-columns: 1fr; }}
                .filter-footer {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
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
                    <p class="filter-summary">当前显示 {filtered_count} 条记录</p>
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
        annual_checked = "checked" if int(r[11] or 1) == 12 else ""
        annual_badge = " · 年摊" if annual_checked else ""
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
                f"<td><span class='compact-badge' title='{display_cat}{annual_badge}'>{display_cat}{annual_badge}</span></td>" \
                f"<td class='amount-cell'>${amount_val:.2f}</td></tr>" \
                f"<tr class='detail-row' id='detail-{r[0]}'><td colspan='7'>" \
                f"<div class='history-detail'>" \
                f"<div class='detail-meta'>录入时间：{created_at_val} · 源文件：{filename}</div>" \
                f"<form action='/spendmoney/update' method='post'>" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='{escape(source_url)}'>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>记录类型</label><select name='record_type'>{record_type_options}</select></div>" \
                f"<div class='form-group'><label>商户/来源</label><input type='text' name='merchant' value='{merchant_val}' required></div>" \
                f"<div class='form-group'><label>标签</label><select name='category' class='category-edit'>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易时间</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' min='0' step='0.01' name='subtotal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' min='0' step='0.01' name='tax' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' min='0' step='0.01' name='amount' value='{amount_form_val}' required></div>" \
                f"<div class='form-group'><label><input type='checkbox' name='annual_expense' value='true' {annual_checked}> 年度支出（统计按 12 个月均摊）</label></div>" \
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
    grouped = {"expense": [], "income": []}
    for code, name, category_type in rows:
        grouped.setdefault(normalize_category_type(category_type), []).append((code, name))

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
            .category-section-title {{ display:flex; align-items:center; gap:8px; }}
            .category-row {{ padding:16px 24px; }}
            .category-form {{ display:flex; flex:1; gap:12px; align-items:flex-end; }}
            @media (max-width: 680px) {{ .category-form {{ display:block; }} .category-row .form-row {{ display:block; }} }}
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

        <div class="card" style="background: var(--primary); color: #fff; border:none;">
            <h3 style="color: #fff; margin-top:0; border-bottom: 1px solid rgba(255,255,255,0.2);">新增标签</h3>
            <p style="font-size: 13px; color: #cbd5e1; margin-bottom: 16px;">标签分为支出和收入两类；代码仍是底层唯一标识，建议收入使用 100 以上编号。</p>
            <form action="/spendmoney/category_add" method="post">
                <div class="form-row" style="align-items: flex-end;">
                    <div class="form-group" style="margin-bottom:0;"><label style="color:#e2e8f0;">标签类型</label><select name="category_type"><option value="expense">支出</option><option value="income">收入</option></select></div>
                    <div class="form-group" style="margin-bottom:0;"><label style="color:#e2e8f0;">唯一代码</label><input type="number" name="code" required></div>
                    <div class="form-group" style="margin-bottom:0;"><label style="color:#e2e8f0;">显示名称</label><input type="text" name="name" required></div>
                    <button type="submit" style="width:auto; margin-bottom:0; background: var(--success);">新建标签</button>
                </div>
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
        for code, name in grouped[category_type]:
            type_options = "".join([
                f"<option value='{escape(type_code)}' {'selected' if type_code == category_type else ''}>{escape(type_label)}</option>"
                for type_code, type_label in CATEGORY_TYPE_LABELS.items()
            ])
            html += f"""
            <div class='card category-row'>
                <div class="form-row" style="align-items: flex-end; margin: 0;">
                    <form action="/spendmoney/category_update" method="post" class="category-form">
                        <div class="form-group" style="margin-bottom:0; flex:1;">
                            <label>底层代码 (不可改)</label>
                            <input type="text" name="code" value="{escape(code)}" readonly>
                        </div>
                        <div class="form-group" style="margin-bottom:0; flex:1;">
                            <label>标签类型</label>
                            <select name="category_type">{type_options}</select>
                        </div>
                        <div class="form-group" style="margin-bottom:0; flex:2;">
                            <label>显示名称</label>
                            <input type="text" name="name" value="{escape(name)}" required>
                        </div>
                        <button type="submit" class="btn-edit" style="width:auto; margin-bottom:0;">保存</button>
                    </form>
                    <form action="/spendmoney/category_delete" method="post" onsubmit="return confirm('删除标签后，旧数据将只显示底层数字代码。确定删除吗？');" style="margin:0;">
                        <input type="hidden" name="code" value="{escape(code)}">
                        <button type="submit" class="btn-danger" style="width:auto; margin-bottom:0;">删除</button>
                    </form>
                </div>
            </div>
            """

    return html + "</div></body></html>"

@app.post("/category_add", response_class=HTMLResponse)
async def category_add(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    category_type: str = Form("expense")
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
    category_type: str = Form("expense")
):
    owner_user_id = get_owner_user_id(request)
    category_type = normalize_category_type(category_type)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE categories SET name=?, category_type=? WHERE owner_user_id=? AND code=?",
        (name.strip(), category_type, owner_user_id, code)
    )
    conn.commit()
    conn.close()
    return """<script>window.location.href='/spendmoney/categories';</script>"""

@app.post("/category_delete", response_class=HTMLResponse)
async def category_delete(request: Request, code: str = Form(...)):
    owner_user_id = get_owner_user_id(request)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM categories WHERE owner_user_id=? AND code=?", (owner_user_id, code))
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
    category: str = Form("0"),
    record_type: str = Form("expense"),
    annual_expense: bool = Form(False)
):
    owner_user_id = get_owner_user_id(request)
    record_type = normalize_record_type(record_type)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    filename = f"manual_{uuid.uuid4().hex}"
    raw_text = "【手工录入】无原件"
    status = 'confirmed' 
    
    cursor.execute(
        "INSERT INTO records (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at, amortization_months, record_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, ?)",
        (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, 12 if annual_expense else 1, record_type)
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
    category: str = Form("0"),
    record_type: str = Form("expense"),
    annual_expense: bool = Form(False),
    source: str = Form("/spendmoney/")
):
    owner_user_id = get_owner_user_id(request)
    record_type = normalize_record_type(record_type)
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE records SET amount=?, merchant=?, date=?, subtotal=?, tax=?, category=?, record_type=?, amortization_months=?, status='confirmed' WHERE id=? AND owner_user_id=?",
                   (amount, merchant, date, subtotal, tax, category, record_type, 12 if annual_expense else 1, id, owner_user_id))
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
        
        category_code = "0"
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
