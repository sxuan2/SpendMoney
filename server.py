import asyncio
import os
import socket
import sqlite3
import uuid
import json
import datetime
from html import escape
from urllib.parse import quote
from typing import Optional

from fastapi import FastAPI, File, Form, UploadFile, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from database import init_db
from processor import process_receipt_file, UPLOAD_DIR, PROCESSED_DIR
from PIL import Image, ImageOps

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)
app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/processed", StaticFiles(directory=PROCESSED_DIR), name="processed")

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

def get_category_map():
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT code, name FROM categories ORDER BY cast(code as integer)")
    cmap = {row[0]: row[1] for row in cursor.fetchall()}
    conn.close()
    return cmap

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
async def render_dashboard(month: Optional[str] = None):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    
    # 核心总计指标
    cursor.execute("SELECT COUNT(*) FROM records WHERE status != 'confirmed'")
    pending_count = cursor.fetchone()[0] or 0
    
    cursor.execute("SELECT SUM(amount), SUM(tax), SUM(subtotal), COUNT(*) FROM records WHERE status = 'confirmed'")
    row = cursor.fetchone()
    total_amount = row[0] if row[0] else 0.0
    total_tax = row[1] if row[1] else 0.0
    total_subtotal = row[2] if row[2] else 0.0
    confirmed_count = row[3] if row[3] else 0
    
    # ==================== 【新增：计算近 7 天消费趋势所需数据】 ====================
    now = datetime.datetime.now()
    days_7 = []
    for i in range(6, -1, -1):
        d = now - datetime.timedelta(days=i)
        days_7.append(d.strftime("%Y-%m-%d"))
        
    cutoff_day = days_7[0]
    cursor.execute("""
        SELECT date, SUM(amount) 
        FROM records 
        WHERE status = 'confirmed' AND date != '' AND date >= ?
        GROUP BY date 
        ORDER BY date ASC
    """, (cutoff_day,))
    
    day_trend_data = cursor.fetchall()
    day_trend_dict = {d: 0.0 for d in days_7}
    for r in day_trend_data:
        d_str, amt = r[0], r[1]
        if d_str in day_trend_dict:
            day_trend_dict[d_str] = amt
            
    amounts_7 = list(day_trend_dict.values())
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
    cursor.execute("""
        SELECT substr(date, 1, 7) as month, SUM(amount) 
        FROM records 
        WHERE status = 'confirmed' AND date != '' AND substr(date, 1, 7) >= ?
        GROUP BY month 
        ORDER BY month ASC
    """, (cutoff_month,))
    
    trend_data = cursor.fetchall()
    trend_dict = {m: 0.0 for m in last_6_months}
    for r in trend_data:
        m_str, amt = r[0], r[1]
        if m_str in trend_dict:
            trend_dict[m_str] = amt
            
    dates = list(trend_dict.keys())
    amounts = list(trend_dict.values())
    
    # 2. 动态生成供下拉选择的过去 12 个月历史轴
    dropdown_months = []
    for i in range(12):
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
        SELECT COALESCE(c.name, r.category) as cat_name, SUM(r.amount) as t 
        FROM records r 
        LEFT JOIN categories c ON r.category = c.code 
        WHERE r.status = 'confirmed' AND r.date LIKE ? 
        GROUP BY cat_name 
        ORDER BY t DESC
    """, (f"{selected_month}%",))
    month_category_data = cursor.fetchall()
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
                <td style="padding: 14px 8px; font-weight: 800; color: var(--text-main);">本月合计支出</td>
                <td style="padding: 14px 8px; text-align: right; color: var(--text-main); font-weight: 800; font-size: 18px;">${month_total:.2f}</td>
            </tr>
        """

    # AI 引擎性能数据
    cursor.execute("SELECT COUNT(*) FROM records WHERE status = 'ocr_failed'")
    failed_count = cursor.fetchone()[0] or 0
    total_processed = confirmed_count + pending_count
    success_rate = 100.0
    if total_processed + failed_count > 0:
        success_rate = (total_processed / (total_processed + failed_count)) * 100
        
    cursor.execute("SELECT COUNT(*) FROM records WHERE status = 'confirmed' AND (date = '' OR merchant = 'Unknown')")
    incomplete_count = cursor.fetchone()[0] or 0
    completeness_rate = 100.0
    if confirmed_count > 0:
        completeness_rate = ((confirmed_count - incomplete_count) / confirmed_count) * 100
        
    aov = (total_amount / confirmed_count) if confirmed_count > 0 else 0.0
    tax_ratio = (total_tax / total_subtotal * 100) if total_subtotal > 0 else 0.0
    
    conn.close()
    local_ip = get_local_ip()

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
        <h2>SpendMoney 控制台</h2>
        <div class="nav-bar">
            <a href="/dashboard" class="active">📊 数据看板</a>
            <a href="/">🧾 上传与待办</a>
            <a href="/history">🗄️ 历史台账</a>
            <a href="/categories">🏷️ 标签管理</a>
        </div>
        
        <div class="ip-banner">
            <span>📱 手机扫码或访问：<a href="http://{local_ip}:8000">http://{local_ip}:8000</a> (需在同一 Wi-Fi 下)</span>
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
                <div class="kpi-title">近 6 个月消费趋势</div>
                <div id="trendChart" class="chart-container"></div>
            </div>
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="kpi-title">近 7 天消费趋势</div>
                <div id="dayTrendChart" class="chart-container"></div>
            </div>
        </div>

        <div class="chart-grid">
            <div class="card" style="display: flex; flex-direction: column;">
                <div class="row" style="align-items: center; margin-bottom: 0;">
                    <div class="kpi-title" style="margin: 0;">筛选账期</div>
                    <select id="monthSelector" onchange="location.href='/dashboard?month=' + this.value" style="width: auto; padding: 6px 12px; font-size: 14px; margin: 0; border-radius: 8px;">
                        {month_options}
                    </select>
                </div>
                <div id="categoryChart" class="chart-container"></div>
            </div>
            
            <div class="card">
                <div class="kpi-title">{selected_month} 分类明细汇总表</div>
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
            tooltip: {{ trigger: 'axis' }},
            grid: {{ left: '3%', right: '4%', bottom: '3%', containLabel: true }},
            xAxis: {{ type: 'category', boundaryGap: false, data: {json.dumps(dates)} }},
            yAxis: {{ type: 'value' }},
            series: [{{
                name: '月度消费',
                type: 'line',
                smooth: true,
                areaStyle: {{ color: 'rgba(15, 23, 42, 0.1)' }},
                lineStyle: {{ color: '#0f172a', width: 3 }},
                itemStyle: {{ color: '#0f172a' }},
                data: {json.dumps(amounts)}
            }}]
        }};
        trendChart.setOption(trendOption);

        // 近 7 天消费趋势配置
        var dayTrendOption = {{
            tooltip: {{ trigger: 'axis' }},
            grid: {{ left: '3%', right: '4%', bottom: '3%', containLabel: true }},
            xAxis: {{ type: 'category', boundaryGap: false, data: {json.dumps(days_7)} }},
            yAxis: {{ type: 'value' }},
            series: [{{
                name: '日度消费',
                type: 'line',
                smooth: true,
                areaStyle: {{ color: 'rgba(16, 185, 129, 0.1)' }},
                lineStyle: {{ color: '#10b981', width: 3 }},
                itemStyle: {{ color: '#10b981' }},
                data: {json.dumps(amounts_7)}
            }}]
        }};
        dayTrendChart.setOption(dayTrendOption);

        // 分类饼图配置
        var categoryOption = {{
            tooltip: {{ trigger: 'item', formatter: '{{b}}: ${{c}} ({{d}}%)' }},
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
async def dashboard_main():
    cmap = get_category_map()
    
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, filename, amount, merchant, date, raw_text, status, subtotal, tax, category FROM records WHERE status != 'confirmed'")
    records = cursor.fetchall()
    conn.close()
    
    local_ip = get_local_ip()
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
        <h2>SpendMoney 控制台</h2>
        <div class="nav-bar">
            <a href="/dashboard">📊 数据看板</a>
            <a href="/" class="active">🧾 上传与待办</a>
            <a href="/history">🗄️ 历史台账</a>
            <a href="/categories">🏷️ 标签管理</a>
        </div>
        
        <div class="ip-banner">
            <span>📱 手机扫码或访问：<a href="http://{local_ip}:8000">http://{local_ip}:8000</a> (需在同一 Wi-Fi 下)</span>
        </div>
        
        <div class="card" style="padding-bottom: 16px;">
            <h3>📸 上传并识别</h3>
            <p class="hint" style="margin-top:0;">上传图片预览裁切，AI 将自动提取字段。</p>
            <form action="/upload" method="post" enctype="multipart/form-data" onsubmit="showLoading()">
                <input type="file" name="file" required>
                <button type="submit" class="btn-primary">安全上传并预览</button>
            </form>
        </div>

        <div class="card" style="padding-bottom: 16px;">
            <h3>✍️ 手工记账</h3>
            <p class="hint" style="margin-top:0;">没有小票？直接在此手动录入消费记录，直达台账。</p>
            <form action="/manual_add" method="post" onsubmit="showLoading('正在保存手工记录...')">
                <div class="form-row">
                    <div class="form-group"><label>商户名称</label><input type="text" name="merchant" required></div>
                    <div class="form-group"><label>消费分类 (标签)</label>
                        <select name="category">
                            {''.join([f"<option value='{escape(code)}' {'selected' if code=='0' else ''}>{escape(name)}</option>" for code, name in cmap.items()])}
                        </select>
                    </div>
                </div>
                <div class="form-group"><label>交易日期</label><input type="date" name="date" required value="{today_str}"></div>
                <div class="form-row">
                    <div class="form-group"><label>税前金额</label><input type="number" step="0.01" name="subtotal" inputmode="decimal" value="0.00"></div>
                    <div class="form-group"><label>税费</label><input type="number" step="0.01" name="tax" inputmode="decimal" value="0.00"></div>
                </div>
                <div class="form-group"><label>总金额 (Total)</label><input type="number" step="0.01" name="amount" inputmode="decimal" required></div>
                <button type="submit" class="btn-primary" style="background: var(--success); width: 100%;">快速确认保存</button>
            </form>
        </div>

        <h3>待核对入库草稿</h3>
    """

    for r in records:
        merchant_val = escape(str(r[3] or ""))
        date_val = escape(str(r[4] or ""))
        raw_text = escape(str(r[5] or ""))
        subtotal_val = r[7] if r[7] else 0.0
        tax_val = r[8] if r[8] else 0.0
        cat_code = escape(str(r[9] or "0"))
        amount_val = r[2] if r[2] else 0.0
        
        cat_options = "".join([f"<option value='{escape(code)}' {'selected' if code==cat_code else ''}>{escape(name)} (代码: {escape(code)})</option>" for code, name in cmap.items()])
        
        html += f"<div class='card'>" \
                f"<div class='filename'>源文件: {escape(str(r[1]))}</div>" \
                f"<div class='category-badge'>预设代码: {cat_code}</div>" \
                f"<div class='ocr-details' style='margin-bottom:16px;'>{raw_text}</div>" \
                f"<form action='/update' method='post'>" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='/'>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>商户名称</label><input type='text' name='merchant' value='{merchant_val}' required></div>" \
                f"<div class='form-group'><label>确认分类标签</label><select name='category'>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易日期</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' step='0.01' name='subtotal' inputmode='decimal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' step='0.01' name='tax' inputmode='decimal' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' step='0.01' name='amount' inputmode='decimal' value='{amount_val}' required></div>" \
                f"<button type='submit' class='btn-edit'>确认无误并入库</button></form>" \
                f"<form action='/delete' method='post' onsubmit=\"return confirm('确定要彻底删除这条记录吗？');\">" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='/'>" \
                f"<button type='submit' class='btn-danger'>删除此草稿</button></form></div>"

    if not records:
        html += "<div class='card'><div style='text-align:center; color:var(--text-muted); padding:20px 0;'>当前所有小票均已确认入库，享受清空收件箱的快感吧。</div></div>"

    return html + "</div></body></html>"

@app.get("/history", response_class=HTMLResponse)
async def history(sort: str = "date", order: str = "desc"):
    cmap = get_category_map()

    sort_columns = {
        "id": "id",
        "merchant": "merchant COLLATE NOCASE",
        "created_at": "created_at",
        "date": "date",
        "category": "(SELECT name FROM categories WHERE code = records.category) COLLATE NOCASE",
        "amount": "amount",
    }
    if sort not in sort_columns:
        sort = "date"
    order = "asc" if order.lower() == "asc" else "desc"
    direction = "ASC" if order == "asc" else "DESC"
    empty_dates_last = ""
    if sort in ("date", "created_at"):
        empty_dates_last = f"CASE WHEN {sort_columns[sort]} IS NULL OR {sort_columns[sort]} = '' THEN 1 ELSE 0 END ASC, "

    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT id, filename, amount, merchant, date, raw_text, status,
               subtotal, tax, category, created_at
        FROM records
        WHERE status = 'confirmed'
        ORDER BY {empty_dates_last}{sort_columns[sort]} {direction}, id DESC
    """)
    records = cursor.fetchall()
    conn.close()

    local_ip = get_local_ip()
    source_url = f"/history?sort={quote(sort)}&order={quote(order)}"

    def sort_link(column, label):
        next_order = "asc" if column != sort or order == "desc" else "desc"
        indicator = ""
        if column == sort:
            indicator = " ↑" if order == "asc" else " ↓"
        return f"<a href='/history?sort={column}&order={next_order}'>{label}{indicator}</a>"

    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>历史台账 - SpendMoney</title>
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
            @media (max-width: 680px) {{
                .container {{ padding-left: 8px; padding-right: 8px; }}
                .ip-banner {{ display: none; }}
                .history-table {{ min-width: 680px; }}
                .history-table th, .history-row td {{ padding: 8px 6px; font-size: 11px; }}
                .form-row {{ display: block; }}
            }}
        </style>
    </head>
    <body>
    <div class="container">
        <h2>SpendMoney 控制台</h2>
        <div class="nav-bar">
            <a href="/dashboard">📊 数据看板</a>
            <a href="/">🧾 上传与待办</a>
            <a href="/history" class="active">🗄️ 历史台账</a>
            <a href="/categories">🏷️ 标签管理</a>
        </div>
        
        <div class="ip-banner">
            <span>📱 手机扫码或访问：<a href="http://{local_ip}:8000">http://{local_ip}:8000</a> (需在同一 Wi-Fi 下)</span>
        </div>
        <p class="history-help">点击表头排序；点击任意记录展开详情。</p>
        <div class="history-shell">
        <table class="history-table">
            <thead><tr>
                <th style="width:8%">{sort_link("id", "ID")}</th>
                <th style="width:21%">{sort_link("merchant", "商户")}</th>
                <th style="width:18%">{sort_link("created_at", "录入时间")}</th>
                <th style="width:16%">{sort_link("date", "交易时间")}</th>
                <th style="width:20%">{sort_link("category", "标签")}</th>
                <th style="width:17%; text-align:right">{sort_link("amount", "金额")}</th>
            </tr></thead>
            <tbody>
    """

    for r in records:
        filename = escape(str(r[1]))
        merchant_val = escape(str(r[3] or ""))
        date_val = escape(str(r[4] or ""))
        raw_text = escape(str(r[5] or ""))
        subtotal_val = r[7] if r[7] else 0.0
        tax_val = r[8] if r[8] else 0.0
        cat_code = escape(str(r[9] or "0"))
        amount_val = r[2] if r[2] else 0.0
        created_at_val = escape(str(r[10] or "未知"))
        
        display_cat = escape(cmap.get(cat_code, cat_code))
        cat_options = "".join([f"<option value='{escape(code)}' {'selected' if code==cat_code else ''}>{escape(name)} ({escape(code)})</option>" for code, name in cmap.items()])
        
        html += f"<tr class='history-row' data-detail='detail-{r[0]}' tabindex='0'>" \
                f"<td class='date-cell'>#{r[0]}</td>" \
                f"<td class='merchant-cell' title='{merchant_val}'>{merchant_val or '未知商户'}</td>" \
                f"<td class='date-cell'>{created_at_val}</td>" \
                f"<td class='date-cell'>{date_val or '未知'}</td>" \
                f"<td><span class='compact-badge' title='{display_cat}'>{display_cat}</span></td>" \
                f"<td class='amount-cell'>${amount_val:.2f}</td></tr>" \
                f"<tr class='detail-row' id='detail-{r[0]}'><td colspan='6'>" \
                f"<div class='history-detail'>" \
                f"<div class='detail-meta'>录入时间：{created_at_val} · 源文件：{filename}</div>" \
                f"<form action='/update' method='post'>" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='{escape(source_url)}'>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>商户名称</label><input type='text' name='merchant' value='{merchant_val}' required></div>" \
                f"<div class='form-group'><label>消费标签</label><select name='category'>{cat_options}</select></div>" \
                f"</div>" \
                f"<div class='form-group'><label>交易时间</label><input type='date' name='date' value='{date_val}'></div>" \
                f"<div class='form-row'>" \
                f"<div class='form-group'><label>税前 (Subtotal)</label><input type='number' step='0.01' name='subtotal' value='{subtotal_val}'></div>" \
                f"<div class='form-group'><label>税费 (Tax)</label><input type='number' step='0.01' name='tax' value='{tax_val}'></div>" \
                f"</div>" \
                f"<div class='form-group'><label>总金额 (Total)</label><input type='number' step='0.01' name='amount' value='{amount_val}' required></div>" \
                f"<button type='submit' class='btn-edit'>保存修改</button></form>" \
                f"<form action='/delete' method='post' onsubmit=\"return confirm('确定要删除这条历史记录吗？');\">" \
                f"<input type='hidden' name='id' value='{r[0]}'>" \
                f"<input type='hidden' name='source' value='{escape(source_url)}'>" \
                f"<button type='submit' class='btn-danger'>删除记录</button></form>" \
                f"<div class='ocr-details'><strong>OCR 原始信息</strong><br>{raw_text}</div>" \
                f"</div></td></tr>"

    if not records:
        html += "<tr><td colspan='6' style='text-align:center;color:var(--text-muted);padding:28px;'>暂无历史台账记录</td></tr>"

    return html + """
            </tbody>
        </table>
        </div>
        <script>
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

@app.get("/categories", response_class=HTMLResponse)
async def manage_categories():
    cmap = get_category_map()
    
    html = f"""
    <!doctype html>
    <html lang="zh-CN">
    <head>
        <title>标签管理 - SpendMoney</title>
        {COMMON_HEAD}
    </head>
    <body>
    <div class="container">
        <h2>SpendMoney 控制台</h2>
        <div class="nav-bar">
            <a href="/dashboard">📊 数据看板</a>
            <a href="/">🧾 上传与待办</a>
            <a href="/history">🗄️ 历史台账</a>
            <a href="/categories" class="active">🏷️ 标签管理</a>
        </div>
        
        <div class="card" style="background: var(--primary); color: #fff; border:none;">
            <h3 style="color: #fff; margin-top:0; border-bottom: 1px solid rgba(255,255,255,0.2);">新增字典标签</h3>
            <p style="font-size: 13px; color: #cbd5e1; margin-bottom: 16px;">代码 (Code) 必须使用纯数字 (如: 8, 12, 99)，作为底层唯一关联标识。</p>
            <form action="/category_add" method="post">
                <div class="form-row" style="align-items: flex-end;">
                    <div class="form-group" style="margin-bottom:0;"><label style="color:#e2e8f0;">唯一代码 (数字)</label><input type="number" name="code" required></div>
                    <div class="form-group" style="margin-bottom:0;"><label style="color:#e2e8f0;">全局显示名称</label><input type="text" name="name" required></div>
                    <button type="submit" style="width:auto; margin-bottom:0; background: var(--success);">新建标签</button>
                </div>
            </form>
        </div>

        <h3>现有标签字典</h3>
    """
    
    for code, name in cmap.items():
        html += f"""
        <div class='card' style="padding: 16px 24px;">
            <div class="form-row" style="align-items: flex-end; margin: 0;">
                <form action="/category_update" method="post" style="display:flex; flex:1; gap:12px; align-items:flex-end;">
                    <div class="form-group" style="margin-bottom:0; flex:1;">
                        <label>底层代码 (不可改)</label>
                        <input type="text" name="code" value="{escape(code)}" readonly>
                    </div>
                    <div class="form-group" style="margin-bottom:0; flex:2;">
                        <label>全局显示名称</label>
                        <input type="text" name="name" value="{escape(name)}" required>
                    </div>
                    <button type="submit" class="btn-edit" style="width:auto; margin-bottom:0;">同步改名</button>
                </form>
                <form action="/category_delete" method="post" onsubmit="return confirm('删除字典后，旧数据将只显示底层数字代码。确定删除吗？');" style="margin:0;">
                    <input type="hidden" name="code" value="{escape(code)}">
                    <button type="submit" class="btn-danger" style="width:auto; margin-bottom:0;">删除</button>
                </form>
            </div>
        </div>
        """
        
    return html + "</div></body></html>"

@app.post("/category_add", response_class=HTMLResponse)
async def category_add(code: str = Form(...), name: str = Form(...)):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO categories (code, name) VALUES (?, ?)", (code.strip(), name.strip()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass 
    conn.close()
    return f"""<script>window.location.href='/categories';</script>"""

@app.post("/category_update", response_class=HTMLResponse)
async def category_update(code: str = Form(...), name: str = Form(...)):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE categories SET name=? WHERE code=?", (name.strip(), code))
    conn.commit()
    conn.close()
    return f"""<script>window.location.href='/categories';</script>"""

@app.post("/category_delete", response_class=HTMLResponse)
async def category_delete(code: str = Form(...)):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM categories WHERE code=?", (code,))
    conn.commit()
    conn.close()
    return f"""<script>window.location.href='/categories';</script>"""

@app.post("/upload", response_class=HTMLResponse)
async def upload_receipt(file: UploadFile = File(...)):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    original_name = os.path.basename(file.filename or "receipt.jpg")
    safe_name = f"{uuid.uuid4().hex}_{original_name}"
    file_path = os.path.join(UPLOAD_DIR, safe_name)

    with open(file_path, "wb") as buffer:
        while content := await file.read(1024 * 1024):
            buffer.write(content)

    await asyncio.to_thread(compress_and_fix_image, file_path)

    preview_url = f"/uploads/{quote(safe_name)}"
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
    
    <form action="/confirm" method="post" onsubmit="showLoading()">
        <input type="hidden" name="filename" value="{escape(safe_name)}">
        <input type="hidden" id="crop_x" name="crop_x" value="0">
        <input type="hidden" id="crop_y" name="crop_y" value="0">
        <input type="hidden" id="crop_w" name="crop_w" value="0">
        <input type="hidden" id="crop_h" name="crop_h" value="0">
        <button type="submit" class="btn-primary">开始提取数据</button>
    </form>
    
    <form action="/cancel" method="post">
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
            <script>setTimeout(function(){{ window.location.href='/'; }}, 1000);</script>
        </body>
        </html>
    """

@app.post("/confirm", response_class=HTMLResponse)
async def confirm_receipt(
    filename: str = Form(...), 
    crop_x: int = Form(0), 
    crop_y: int = Form(0), 
    crop_w: int = Form(0), 
    crop_h: int = Form(0)
):
    file_path = os.path.join(UPLOAD_DIR, os.path.basename(filename))

    if not os.path.exists(file_path):
        return "File not found. <a href='/'>Return</a>"

    result = await asyncio.to_thread(process_receipt_file, file_path, os.path.basename(filename), crop_x, crop_y, crop_w, crop_h)
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
                <a href='/' class="btn-link btn-cancel">返回首页</a>
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
            <script>setTimeout(function(){{ window.location.href='/'; }}, 1000);</script>
        </body>
        </html>
    """

@app.post("/manual_add", response_class=HTMLResponse)
async def manual_add(
    merchant: str = Form(...),
    date: str = Form(""),
    subtotal: float = Form(0.0),
    tax: float = Form(0.0),
    amount: float = Form(...),
    category: str = Form("0")
):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    filename = f"manual_{uuid.uuid4().hex}"
    raw_text = "【手工录入】无原件"
    status = 'confirmed' 
    
    cursor.execute(
        "INSERT INTO records (filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
        (filename, amount, merchant, date, subtotal, tax, category, raw_text, status)
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
            <script>setTimeout(function(){{ window.location.href='/'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/update", response_class=HTMLResponse)
async def update(
    id: int = Form(...), 
    amount: float = Form(...), 
    merchant: str = Form(...), 
    date: str = Form(""),
    subtotal: float = Form(0.0),
    tax: float = Form(0.0),
    category: str = Form("0"),
    source: str = Form("/")
):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE records SET amount=?, merchant=?, date=?, subtotal=?, tax=?, category=?, status='confirmed' WHERE id=?", 
                   (amount, merchant, date, subtotal, tax, category, id))
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
            <script>setTimeout(function(){{ window.location.href='{escape(source)}'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/delete", response_class=HTMLResponse)
async def delete_record(id: int = Form(...), source: str = Form("/")):
    conn = sqlite3.connect('finance.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM records WHERE id=?", (id,))
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
            <script>setTimeout(function(){{ window.location.href='{escape(source)}'; }}, 800);</script>
        </body>
        </html>
    """

@app.post("/api/iphone-upload")
async def receive_iphone_receipt(request: Request):
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
            (filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
            """,
            (virtual_filename, total, merchant, date_str, subtotal, tax, category_code, raw_text_for_db, status)
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
