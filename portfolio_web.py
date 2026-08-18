import csv
import datetime
import hashlib
import io
import json
import sqlite3
import re
from html import escape
from typing import Optional
from urllib.parse import urlencode
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from portfolio import ALL_TRANSACTION_TYPES, SECURITY_TRANSACTION_TYPES, calculate_portfolio

router = APIRouter()
DB_NAME = "finance.db"
CURRENCIES = {"CAD", "USD", "CNY"}
LABELS = {"opening":"期初持仓","buy":"买入","sell":"卖出","transfer_in":"持仓转入","transfer_out":"持仓转出","deposit":"入金","withdrawal":"出金","dividend":"分红","interest":"利息","fee":"费用"}

def owner_id(request):
    return (request.headers.get("X-Authenticated-User-Id") or "1").strip()

def redirect(message, account_id=None):
    target=f"/spendmoney/portfolio/account/{account_id}" if account_id else "/spendmoney/portfolio"
    return RedirectResponse(target+"?"+urlencode({"message":message}),status_code=303)

STYLE = """<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{--bg:#f8fafc;--card:#fff;--text:#111827;--muted:#64748b;--line:#e5e7eb;--primary:#0f172a;--red:#dc2626}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.container{max-width:1050px;margin:auto;padding:24px 16px 60px}
.nav{display:flex;gap:8px;overflow:auto;padding:8px;background:#fff;border:1px solid var(--line);border-radius:14px;position:sticky;top:12px;z-index:2}.nav a{white-space:nowrap;text-decoration:none;color:var(--muted);padding:9px 13px;border-radius:9px;font-weight:600}.nav a.active{background:var(--primary);color:#fff}
h2{font-size:28px}h3{margin:28px 0 14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin-bottom:14px}.kpi{font-size:27px;font-weight:800}.muted{color:var(--muted);font-size:13px}
label{display:block;color:var(--muted);font-size:12px;font-weight:700;margin-bottom:6px}.field{margin-bottom:13px}.row{display:flex;gap:12px}.row .field{flex:1}input,select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:#fff;font-size:15px}button,.view{width:100%;display:block;text-align:center;text-decoration:none;border:0;border-radius:9px;padding:11px 15px;background:var(--primary);color:#fff;font-weight:700}.danger{width:auto;background:#fff;color:var(--red);border:1px solid #fca5a5;padding:6px 10px}.notice{background:#e0f2fe;color:#075985;padding:12px 15px;border-radius:10px;margin:14px 0}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);font-size:14px}th{color:var(--muted)}.history-table{min-width:900px;table-layout:fixed}.history-table th{white-space:nowrap}.history-table .date,.history-table .type,.history-table .symbol,.history-table .amount,.history-table .action{white-space:nowrap}.history-table .number,.history-table .amount{text-align:right;font-variant-numeric:tabular-nums}.type-pill{display:inline-block;padding:4px 8px;border-radius:999px;background:#f1f5f9;font-weight:700;font-size:12px}.note-text{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.45;overflow-wrap:anywhere}.history-table .action{text-align:right}.history-table .action .danger{min-width:46px;padding:6px 8px}.history-table tr:hover td{background:#f8fafc}@media(max-width:600px){.row{display:block}.card.scroll{padding:12px}}</style>"""

@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request, message: str = ""):
    owner=owner_id(request); conn=sqlite3.connect(DB_NAME); cur=conn.cursor()
    account_rows=cur.execute("SELECT id,name,account_type,currency,initial_cash FROM investment_accounts WHERE owner_user_id=? AND is_active=1 ORDER BY name",(owner,)).fetchall()
    security_rows=cur.execute("SELECT id,symbol,exchange,name,asset_type,currency FROM securities WHERE owner_user_id=? AND is_active=1 ORDER BY symbol",(owner,)).fetchall();conn.close()
    accounts,holdings,errors=calculate_portfolio(owner)
    notices="".join(f"<div class='notice'>{escape(x)}</div>" for x in (([message] if message else [])+errors))
    holding_costs={a["id"]:sum(h.total_cost for h in holdings if h.account_id==a["id"]) for a in accounts}
    def build_account_card(account):
        cost=holding_costs[account["id"]]
        currency=escape(account["currency"])
        if cost>0:
            title="总资产（按持仓成本）"
            detail=f"未投资现金：{currency} {account['cash']:,.2f} · 持仓成本：{currency} {cost:,.2f}"
        else:
            title="账户余额（现金等价物）"
            detail="由入金、利息、出金和费用流水计算"
        return f"""<div class='card'><div class='muted'>{escape(account['name'])} · {escape(account['account_type'])}</div>
        <div class='muted' style='margin-top:12px'>{title}</div><div class='kpi'>{currency} {account['cash']+cost:,.2f}</div>
        <div class='muted' style='margin:8px 0 14px'>{detail}</div>
        <a class='view' href='/spendmoney/portfolio/account/{account["id"]}'>查看账户</a>
        <form action='/spendmoney/portfolio/account/clear-transactions' method='post' onsubmit="return confirm('确定清除此账户的所有交易吗？持仓和余额将归零，且无法恢复。')">
        <input type='hidden' name='account_id' value='{account["id"]}'><button class='danger' style='margin-top:10px' type='submit'>清除所有交易</button></form>
        <form action='/spendmoney/portfolio/account/delete' method='post' onsubmit="return confirm('确定删除这个账户吗？只有没有交易流水的账户才能删除。')">
        <input type='hidden' name='account_id' value='{account["id"]}'><button class='danger' style='margin-top:10px' type='submit'>删除账户</button></form></div>"""
    account_cards="".join(build_account_card(a) for a in accounts) or "<div class='card muted'>请先创建账户。</div>"
    account_options="".join(f"<option value='{r[0]}'>{escape(r[1])} ({escape(r[3])})</option>" for r in account_rows)
    security_options="".join(f"<option value='{r[0]}'>{escape(r[1])} · {escape(r[2] or 'N/A')} · {escape(r[5])}</option>" for r in security_rows)
    today=datetime.date.today().isoformat()
    return f"""<!doctype html><html lang="zh-CN"><head><title>资产组合</title>{STYLE}</head><body><div class="container">
    <h2>资产组合</h2><nav class="nav"><a href="/spendmoney/dashboard">📊 数据看板</a><a href="/spendmoney/">🧾 记账中心</a><a href="/spendmoney/history">🗄️ 历史台账</a><a href="/spendmoney/categories">🏷️ 标签管理</a><a class="active" href="/spendmoney/portfolio">💼 资产组合</a><a href="/nav/">🏠 返回主页</a></nav>{notices}
    <h3>账户</h3><div class="grid">{account_cards}</div>
    <h3>基础资料</h3><div class="grid"><div class="card"><h3 style="margin-top:0">新增账户</h3><form action="/spendmoney/portfolio/account/add" method="post">
    <div class="field"><label>账户名称</label><input name="name" required placeholder="Wealthsimple TFSA"></div><div class="row"><div class="field"><label>类型</label><select name="account_type"><option value="investment">普通投资</option><option value="cash">现金</option><option value="tfsa">TFSA</option><option value="rrsp">RRSP</option><option value="fhsa">FHSA</option><option value="crypto">Crypto</option></select></div><div class="field"><label>币种</label><select name="currency"><option>CAD</option><option>USD</option><option>CNY</option></select></div></div><div class="field"><label>期初现金</label><input type="number" step="0.01" name="initial_cash" value="0"></div><button>创建账户</button></form></div>
    <div class="card"><h3 style="margin-top:0">新增证券</h3><form action="/spendmoney/portfolio/security/add" method="post"><div class="row"><div class="field"><label>代码</label><input name="symbol" required placeholder="XEQT"></div><div class="field"><label>交易所</label><input name="exchange" placeholder="TSX"></div></div><div class="field"><label>名称</label><input name="name"></div><div class="row"><div class="field"><label>类型</label><select name="asset_type"><option value="stock">股票</option><option value="etf">ETF</option><option value="fund">基金</option><option value="bond">债券</option><option value="crypto">Crypto</option></select></div><div class="field"><label>币种</label><select name="currency"><option>CAD</option><option>USD</option><option>CNY</option></select></div></div><button>添加证券</button></form></div></div>
    <h3>录入交易</h3><div class="card"><form action="/spendmoney/portfolio/transaction/add" method="post"><div class="row"><div class="field"><label>账户</label><select name="account_id" required><option value="" disabled selected>选择账户</option>{account_options}</select></div><div class="field"><label>类型</label><select name="transaction_type" id="txType"><option value="opening">期初持仓</option><option value="buy">买入</option><option value="sell">卖出</option><option value="deposit">入金</option><option value="withdrawal">出金</option><option value="dividend">分红</option><option value="interest">利息</option><option value="fee">费用</option></select></div><div class="field"><label>日期</label><input type="date" name="trade_date" value="{today}" required></div></div>
    <div class="field security"><label>证券</label><select id="security" name="security_id"><option value="">选择证券</option>{security_options}</select></div><div class="row security"><div class="field"><label>股数</label><input type="number" min="0" step="any" name="quantity" value="0"></div><div class="field"><label>每股价格</label><input type="number" min="0" step="any" name="price" value="0"></div></div><div class="row"><div class="field"><label>总金额（现金交易必填）</label><input type="number" min="0" step="0.01" name="amount" value="0"><div class="muted">买卖填 0 时按股数 × 单价计算。</div></div><div class="field"><label>手续费</label><input type="number" min="0" step="0.01" name="fee" value="0"></div><div class="field"><label>币种</label><select name="currency"><option>CAD</option><option>USD</option><option>CNY</option></select></div></div><div class="field"><label>备注</label><input name="note"></div><button>保存交易</button></form></div>
    <h3>导入 Wealthsimple CSV</h3><div class="card"><form action="/spendmoney/portfolio/import/wealthsimple" method="post" enctype="multipart/form-data">
    <div class="field"><label>导入到哪个账户</label><select name="account_id" required><option value="" disabled selected>选择账户</option>{account_options}</select></div><div class="field"><label>CSV 文件</label><input type="file" name="file" accept=".csv,text/csv" required></div><div class="muted" style="margin-bottom:13px">支持入金、出金、利息、分红、费用和买卖记录；重复上传会自动跳过。</div><button>导入 CSV</button></form></div>
    <script>const t=document.getElementById('txType'),s=document.getElementById('security');function sync(){{const n=['opening','buy','sell'].includes(t.value);document.querySelectorAll('.security').forEach(x=>x.style.display=n?'':'none');s.required=n}}t.addEventListener('change',sync);sync()</script></body></html>"""

@router.get("/portfolio/account/{account_id}",response_class=HTMLResponse)
async def account_detail(request:Request,account_id:int,message:str=""):
    owner=owner_id(request);conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    account_row=cur.execute("SELECT id,name,account_type,currency FROM investment_accounts WHERE id=? AND owner_user_id=? AND is_active=1",(account_id,owner)).fetchone()
    if not account_row:
        conn.close();return redirect("未找到该账户")
    rows=cur.execute("""SELECT t.id,t.trade_date,t.transaction_type,s.symbol,t.quantity,t.price,t.amount,t.fee,t.currency,t.note
      FROM investment_transactions t LEFT JOIN securities s ON s.id=t.security_id
      WHERE t.owner_user_id=? AND t.account_id=? ORDER BY t.trade_date DESC,t.id DESC""",(owner,account_id)).fetchall();conn.close()
    accounts,all_holdings,errors=calculate_portfolio(owner)
    account=next((a for a in accounts if a["id"]==account_id),None)
    holdings=[h for h in all_holdings if h.account_id==account_id]
    cost=sum(h.total_cost for h in holdings);cash=account["cash"] if account else 0
    holding_rows="".join(f"<tr><td><b>{escape(h.symbol)}</b><div class='muted'>{escape(h.security_name)}</div></td><td>{h.quantity:,.6f}</td><td>{h.currency} {h.average_cost:,.2f}</td><td>{h.currency} {h.total_cost:,.2f}</td></tr>" for h in holdings) or "<tr><td colspan='4' class='muted'>此账户没有证券持仓。</td></tr>"
    def transaction_row(row):
        is_security=row[2] in SECURITY_TRANSACTION_TYPES
        quantity=f"{float(row[4] or 0):,.6f}".rstrip("0").rstrip(".") if is_security else "—"
        amount=float(row[6] or (row[4] or 0)*(row[5] or 0))
        full_note=str(row[9] or "")
        short_note=full_note.split(": ",1)[1] if full_note.startswith("Wealthsimple ") and ": " in full_note else full_note
        return f"""<tr><td class="date">{escape(row[1])}</td><td class="type"><span class="type-pill">{LABELS.get(row[2],row[2])}</span></td>
        <td class="symbol">{escape(row[3] or '—')}</td><td class="number">{quantity}</td><td class="amount">{escape(row[8])} {amount:,.2f}</td>
        <td class="muted"><div class="note-text" title="{escape(full_note)}">{escape(short_note)}</div></td>
        <td class="action"><form action="/spendmoney/portfolio/transaction/delete" method="post" onsubmit="return confirm('删除后将重新计算此账户，确定吗？')">
        <input type="hidden" name="transaction_id" value="{row[0]}"><input type="hidden" name="return_account_id" value="{account_id}">
        <button class="danger">删除</button></form></td></tr>"""
    tx_rows="".join(transaction_row(row) for row in rows) or "<tr><td colspan='7' class='muted'>此账户暂无交易记录。</td></tr>"
    notices=([message] if message else [])+errors
    notice_html="".join(f"<div class='notice'>{escape(x)}</div>" for x in notices)
    if cost>0:
        summary=f"<div class='grid'><div class='card'><div class='muted'>总资产（按成本）</div><div class='kpi'>{escape(account_row[3])} {cash+cost:,.2f}</div></div><div class='card'><div class='muted'>未投资现金</div><div class='kpi'>{escape(account_row[3])} {cash:,.2f}</div></div><div class='card'><div class='muted'>持仓成本</div><div class='kpi'>{escape(account_row[3])} {cost:,.2f}</div></div></div>"
    else:
        summary=f"<div class='grid'><div class='card'><div class='muted'>账户余额（现金等价物）</div><div class='kpi'>{escape(account_row[3])} {cash:,.2f}</div></div></div>"
    return f"""<!doctype html><html lang="zh-CN"><head><title>{escape(account_row[1])} - 资产组合</title>{STYLE}</head><body><div class="container">
    <h2>{escape(account_row[1])}</h2><nav class="nav"><a href="/spendmoney/portfolio">← 返回账户</a><a href="/spendmoney/dashboard">📊 数据看板</a><a class="active" href="/spendmoney/portfolio/account/{account_id}">💼 {escape(account_row[1])}</a></nav>
    {notice_html}{summary}
    <h3>当前持仓</h3><div class="card scroll"><table><thead><tr><th>证券</th><th>股数</th><th>平均成本</th><th>持仓成本</th></tr></thead><tbody>{holding_rows}</tbody></table></div>
    <h3>历史交易</h3><div class="card scroll"><table class="history-table"><colgroup><col style="width:105px"><col style="width:72px"><col style="width:78px"><col style="width:105px"><col style="width:135px"><col><col style="width:60px"></colgroup><thead><tr><th>日期</th><th>类型</th><th>证券</th><th style="text-align:right">股数</th><th style="text-align:right">金额</th><th>说明</th><th></th></tr></thead><tbody>{tx_rows}</tbody></table></div>
    </div></body></html>"""

@router.post("/portfolio/account/add")
async def add_account(request:Request,name:str=Form(...),account_type:str=Form("investment"),currency:str=Form("CAD"),initial_cash:float=Form(0)):
    owner,name,currency=owner_id(request),name.strip(),currency.strip().upper()
    if not name or currency not in CURRENCIES:return redirect("账户资料无效")
    conn=sqlite3.connect(DB_NAME);conn.execute("INSERT INTO investment_accounts(owner_user_id,name,account_type,currency,initial_cash) VALUES(?,?,?,?,?)",(owner,name,account_type.strip(),currency,initial_cash));conn.commit();conn.close();return redirect("账户已创建")

@router.post("/portfolio/account/clear-transactions")
async def clear_account_transactions(request:Request,account_id:int=Form(...)):
    owner=owner_id(request);conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        account=cur.execute("SELECT name FROM investment_accounts WHERE id=? AND owner_user_id=? AND is_active=1",(account_id,owner)).fetchone()
        if not account:
            conn.rollback();conn.close();return redirect("未找到该账户")
        cur.execute("DELETE FROM investment_transactions WHERE account_id=? AND owner_user_id=?",(account_id,owner))
        deleted=cur.rowcount
        conn.commit();conn.close();return redirect(f"账户「{account[0]}」已清除 {deleted} 条交易")
    except Exception:
        conn.rollback();conn.close();raise

@router.post("/portfolio/account/delete")
async def delete_account(request:Request,account_id:int=Form(...)):
    owner=owner_id(request);conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    account=cur.execute("SELECT name FROM investment_accounts WHERE id=? AND owner_user_id=?",(account_id,owner)).fetchone()
    if not account:
        conn.close();return redirect("未找到该账户")
    transaction_count=cur.execute("SELECT COUNT(*) FROM investment_transactions WHERE account_id=? AND owner_user_id=?",(account_id,owner)).fetchone()[0]
    if transaction_count:
        conn.close();return redirect(f"无法删除「{account[0]}」：账户还有 {transaction_count} 条交易，请先删除相关交易")
    cur.execute("DELETE FROM investment_accounts WHERE id=? AND owner_user_id=?",(account_id,owner))
    conn.commit();conn.close();return redirect(f"账户「{account[0]}」已删除")

@router.post("/portfolio/security/add")
async def add_security(request:Request,symbol:str=Form(...),exchange:str=Form(""),name:str=Form(""),asset_type:str=Form("stock"),currency:str=Form("CAD")):
    owner,symbol,exchange,currency=owner_id(request),symbol.strip().upper(),exchange.strip().upper(),currency.strip().upper()
    if not symbol or currency not in CURRENCIES:return redirect("证券资料无效")
    conn=sqlite3.connect(DB_NAME)
    try:conn.execute("INSERT INTO securities(owner_user_id,symbol,exchange,name,asset_type,currency) VALUES(?,?,?,?,?,?)",(owner,symbol,exchange,name.strip(),asset_type.strip(),currency));conn.commit()
    except sqlite3.IntegrityError:conn.close();return redirect("同一交易所的证券代码已存在")
    conn.close();return redirect("证券已添加")

@router.post("/portfolio/transaction/add")
async def add_transaction(request:Request,account_id:int=Form(...),transaction_type:str=Form(...),trade_date:str=Form(...),security_id:Optional[int]=Form(None),quantity:float=Form(0),price:float=Form(0),amount:float=Form(0),fee:float=Form(0),currency:str=Form("CAD"),note:str=Form("")):
    owner,transaction_type,currency=owner_id(request),transaction_type.strip(),currency.strip().upper()
    try:datetime.date.fromisoformat(trade_date)
    except ValueError:return redirect("交易日期无效")
    if transaction_type not in ALL_TRANSACTION_TYPES or currency not in CURRENCIES or min(quantity,price,amount,fee)<0:return redirect("交易资料无效")
    conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    if not cur.execute("SELECT 1 FROM investment_accounts WHERE id=? AND owner_user_id=? AND is_active=1",(account_id,owner)).fetchone():conn.close();return redirect("账户不存在")
    if transaction_type in SECURITY_TRANSACTION_TYPES:
        valid=cur.execute("SELECT 1 FROM securities WHERE id=? AND owner_user_id=? AND is_active=1",(security_id,owner)).fetchone()
        if not valid or quantity<=0 or (price<=0 and amount<=0):conn.close();return redirect("证券交易必须填写证券、股数以及价格或总金额")
        if transaction_type=="sell":
            available=sum(h.quantity for h in calculate_portfolio(owner)[1] if h.account_id==account_id and h.security_id==security_id)
            if quantity>available+1e-9:conn.close();return redirect(f"卖出失败：当前最多可卖 {available:,.6f} 股")
    elif amount<=0 and not(transaction_type=="fee" and fee>0):conn.close();return redirect("现金交易必须填写正数金额")
    cur.execute("INSERT INTO investment_transactions(owner_user_id,account_id,security_id,transaction_type,trade_date,quantity,price,amount,fee,currency,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(owner,account_id,security_id if transaction_type in SECURITY_TRANSACTION_TYPES else None,transaction_type,trade_date,quantity,price,amount,fee,currency,note.strip()));conn.commit();conn.close();return redirect("交易已保存，持仓和现金已重新计算")
def csv_number(value):
    try:
        return float(str(value or "").replace(",","").strip() or 0)
    except ValueError:
        return 0.0

@router.post("/portfolio/import/wealthsimple")
async def import_wealthsimple(request:Request,account_id:int=Form(...),file:UploadFile=File(...)):
    owner=owner_id(request);content=await file.read()
    if len(content)>10*1024*1024:return redirect("CSV 文件超过 10 MB，未导入")
    try:text=content.decode("utf-8-sig")
    except UnicodeDecodeError:return redirect("CSV 必须使用 UTF-8 编码")
    reader=csv.DictReader(io.StringIO(text))
    required={"effective_at","account_id","account_type","activity_type","description","currency","net_cash_amount"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        return redirect("CSV 格式不正确：缺少 Wealthsimple 必需列")
    conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    account=cur.execute("SELECT name FROM investment_accounts WHERE id=? AND owner_user_id=? AND is_active=1",(account_id,owner)).fetchone()
    if not account:
        conn.close();return redirect("目标账户不存在")
    imported=duplicates=skipped=0
    for row in reader:
        effective=(row.get("effective_at") or "").strip()
        try:trade_date=datetime.date.fromisoformat(effective[:10]).isoformat()
        except ValueError:
            skipped+=1;continue
        activity=(row.get("activity_type") or "").strip().lower()
        subtype=(row.get("activity_sub_type") or "").strip().lower()
        description=(row.get("description") or "").strip()
        description_lower=description.lower()
        direction=(row.get("direction") or "").strip().lower()
        currency=(row.get("currency") or "").strip().upper()
        if currency not in CURRENCIES:
            skipped+=1;continue
        net_cash=csv_number(row.get("net_cash_amount"))
        quantity=abs(csv_number(row.get("quantity")))
        converted_price=abs(csv_number(row.get("unit_price")))
        fee=abs(csv_number(row.get("commission")))
        tx_type=None
        if "shares out of the account" in description_lower:
            tx_type="transfer_out"
        elif "shares into the account" in description_lower:
            tx_type="transfer_in"
        elif "trade" in activity or direction in {"buy","sell"}:
            tx_type=direction if direction in {"buy","sell"} else ("buy" if net_cash<0 else "sell")
        elif "dividend" in activity or "dividend" in description_lower:tx_type="dividend"
        elif "interest" in activity or "interest" in description_lower:tx_type="interest"
        elif "fee" in activity or "fee" in subtype:tx_type="fee"
        elif "deposit" in description_lower or direction in {"credit","in"}:tx_type="deposit"
        elif "withdraw" in description_lower or direction in {"debit","out"}:tx_type="withdrawal"
        elif "money" in activity:tx_type="deposit" if net_cash>0 else "withdrawal" if net_cash<0 else None
        if not tx_type:
            skipped+=1;continue
        security_id=None
        symbol=(row.get("symbol") or "").strip().upper()
        security_name=(row.get("name") or "").strip()
        amount=abs(net_cash)
        if tx_type in SECURITY_TRANSACTION_TYPES:
            if not symbol or quantity<=0 or (converted_price<=0 and amount<=0):
                skipped+=1;continue
            fx_match=re.search(r"FX Rate:\s*([0-9.]+)",description,re.IGNORECASE)
            fx_rate=csv_number(fx_match.group(1)) if fx_match else 0
            security_currency="USD" if fx_rate>0 else currency
            price=converted_price/fx_rate if fx_rate>0 else converted_price
            security=cur.execute("SELECT id FROM securities WHERE owner_user_id=? AND symbol=? AND currency=? ORDER BY id LIMIT 1",(owner,symbol,security_currency)).fetchone()
            if security:security_id=security[0]
            else:
                exchange="US" if security_currency=="USD" else ""
                cur.execute("INSERT INTO securities(owner_user_id,symbol,exchange,name,asset_type,currency) VALUES(?,?,?,?,'stock',?)",(owner,symbol,exchange,security_name,security_currency))
                security_id=cur.lastrowid
            if amount<=0:amount=quantity*converted_price
        elif amount<=0:
            skipped+=1;continue
        source_account=(row.get("account_id") or "").strip()
        canonical=json.dumps({key:(row.get(key) or "").strip() for key in reader.fieldnames},sort_keys=True,ensure_ascii=False)
        import_key=hashlib.sha256(f"wealthsimple|{account_id}|{canonical}".encode()).hexdigest()
        note=f"Wealthsimple {source_account}: {description}".strip()
        cur.execute("""INSERT OR IGNORE INTO investment_transactions
          (owner_user_id,account_id,security_id,transaction_type,trade_date,quantity,price,amount,fee,currency,note,import_key,import_source)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(owner,account_id,security_id,tx_type,trade_date,quantity if tx_type in SECURITY_TRANSACTION_TYPES else 0,price if tx_type in SECURITY_TRANSACTION_TYPES else 0,amount,fee,currency,note,import_key,"wealthsimple_csv"))
        if cur.rowcount:imported+=1
        else:duplicates+=1
    conn.commit();conn.close()
    return redirect(f"CSV 导入完成：新增 {imported} 条，重复 {duplicates} 条，跳过 {skipped} 条")


@router.post("/portfolio/transaction/delete")
async def delete_transaction(request:Request,transaction_id:int=Form(...),return_account_id:Optional[int]=Form(None)):
    owner=owner_id(request);conn=sqlite3.connect(DB_NAME);cur=conn.cursor()
    cur.execute("DELETE FROM investment_transactions WHERE id=? AND owner_user_id=?",(transaction_id,owner));deleted=cur.rowcount
    conn.commit();conn.close();return redirect("交易已删除" if deleted else "未找到交易",return_account_id)
