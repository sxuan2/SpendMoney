import calendar
import datetime
import sqlite3
from dataclasses import dataclass


@dataclass
class BudgetStatus:
    name: str
    amount: float
    budget: float
    percent: float
    remaining: float
    state: str


@dataclass
class RecentExpense:
    date: str
    merchant: str
    category: str
    amount: float
    is_amortized: bool


@dataclass
class DashboardStats:
    selected_month: str
    current_month_income: float
    current_month_expense: float
    current_month_net: float
    pending_count: int
    confirmed_count: int
    total_expense: float
    total_tax: float
    total_subtotal: float
    dates: list[str]
    income_amounts: list[float]
    expense_amounts: list[float]
    net_amounts: list[float]
    days_15: list[str]
    day_income_amounts: list[float]
    day_expense_amounts: list[float]
    day_net_amounts: list[float]
    dropdown_months: list[str]
    expense_category_data: list[tuple[str, float]]
    income_category_data: list[tuple[str, float]]
    expense_category_pie: list[dict[str, object]]
    income_category_pie: list[dict[str, object]]
    selected_month_income: float
    selected_month_expense: float
    selected_month_net: float
    selected_direct_expense: float
    selected_amortized_expense: float
    current_direct_expense: float
    current_amortized_expense: float
    avg_monthly_expense: float
    total_budget: float
    budget_used_percent: float
    budget_remaining: float
    projected_month_expense: float
    projected_budgeted_expense: float
    projected_budget_gap: float
    daily_budget_remaining: float
    month_days_elapsed: int
    month_days_remaining: int
    budgeted_expense: float
    unbudgeted_expense: float
    over_budget_count: int
    warning_budget_count: int
    budget_statuses: list[BudgetStatus]
    budget_suggestions: list[str]
    uncategorized_count: int
    amortized_item_count: int
    recent_expenses: list[RecentExpense]


def _normalize_record_type(value: str) -> str:
    value = (value or "expense").strip()
    return value if value in {"expense", "income"} else "expense"


def _add_months(month_text: str, offset: int) -> str:
    year, month_number = map(int, month_text.split("-"))
    absolute_month = year * 12 + month_number - 1 + offset
    return f"{absolute_month // 12:04d}-{absolute_month % 12 + 1:02d}"


def _allocation_months(record_type: str, amortization_months: int) -> int:
    if _normalize_record_type(record_type) == "income":
        return 1
    return max(1, int(amortization_months or 1))


def _recent_months(now: datetime.datetime, count: int) -> list[str]:
    months = []
    for i in range(count - 1, -1, -1):
        m = now.month - i
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        months.append(f"{y:04d}-{m:02d}")
    return months


def _budget_state(percent: float, budget: float) -> str:
    if budget <= 0:
        return "unset"
    if percent >= 100:
        return "over"
    if percent >= 80:
        return "warning"
    return "ok"


def _is_uncategorized_category(category_code: str, category_name: str | None, known_category_codes: set[str]) -> bool:
    code = str(category_code or "").strip()
    name = str(category_name or "").strip()
    if not code:
        return True
    if code not in known_category_codes:
        return True
    return "未分类" in name


def build_dashboard_stats(owner_user_id: str, month: str | None = None) -> DashboardStats:
    conn = sqlite3.connect("finance.db")
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS category_budgets
                      (owner_user_id TEXT NOT NULL,
                       category_code TEXT NOT NULL,
                       monthly_budget REAL NOT NULL DEFAULT 0,
                       PRIMARY KEY (owner_user_id, category_code))""")

    now = datetime.datetime.now()
    current_month = now.strftime("%Y-%m")
    selected_month = month if month else current_month

    cursor.execute("SELECT COUNT(*) FROM records WHERE owner_user_id=? AND status != 'confirmed'", (owner_user_id,))
    pending_count = cursor.fetchone()[0] or 0

    cursor.execute("""
        SELECT
            SUM(CASE WHEN COALESCE(record_type, 'expense') = 'income' THEN amount ELSE 0 END),
            SUM(CASE WHEN COALESCE(record_type, 'expense') = 'expense' THEN amount ELSE 0 END),
            SUM(CASE WHEN COALESCE(record_type, 'expense') = 'expense' THEN tax ELSE 0 END),
            SUM(CASE WHEN COALESCE(record_type, 'expense') = 'expense' THEN subtotal ELSE 0 END),
            COUNT(*)
        FROM records
        WHERE owner_user_id=? AND status = 'confirmed'
    """, (owner_user_id,))
    _total_income, total_expense, total_tax, total_subtotal, confirmed_count = cursor.fetchone()
    total_expense = total_expense or 0.0
    total_tax = total_tax or 0.0
    total_subtotal = total_subtotal or 0.0
    confirmed_count = confirmed_count or 0

    cursor.execute("""
        SELECT r.date, r.amount, COALESCE(r.amortization_months, 1), r.category,
               COALESCE(r.record_type, 'expense'), COALESCE(c.name, r.category), COALESCE(r.merchant, '')
        FROM records r
        LEFT JOIN categories c ON r.owner_user_id = c.owner_user_id AND r.category = c.code
        WHERE r.owner_user_id=? AND r.status = 'confirmed' AND r.date != ''
    """, (owner_user_id,))
    confirmed_records = cursor.fetchall()

    cursor.execute("SELECT code, name FROM categories WHERE owner_user_id=? AND category_type='expense'", (owner_user_id,))
    category_names_by_code = {str(code): name for code, name in cursor.fetchall()}

    cursor.execute("SELECT category_code, monthly_budget FROM category_budgets WHERE owner_user_id=?", (owner_user_id,))
    budgets_by_code = {str(code): float(amount or 0.0) for code, amount in cursor.fetchall()}
    conn.commit()
    conn.close()

    last_6_months = _recent_months(now, 6)
    monthly_income = {m: 0.0 for m in last_6_months}
    monthly_expense = {m: 0.0 for m in last_6_months}
    selected_expense_categories = {}
    selected_income_categories = {}
    selected_expense_by_code = {}
    current_month_income = 0.0
    current_month_expense = 0.0
    current_direct_expense = 0.0
    current_amortized_expense = 0.0
    selected_direct_expense = 0.0
    selected_amortized_expense = 0.0
    amortized_item_codes = set()

    for record_date, record_amount, amortization_months, category_code, record_type, cat_name, _merchant in confirmed_records:
        record_type = _normalize_record_type(record_type)
        category_code = str(category_code or "")
        category_names_by_code.setdefault(category_code, cat_name)
        start_month = str(record_date)[:7]
        months = _allocation_months(record_type, amortization_months)
        monthly_amount = (record_amount or 0.0) / months
        for offset in range(months):
            target_month = _add_months(start_month, offset)
            if target_month in monthly_income or target_month == selected_month or target_month == current_month:
                if record_type == "income":
                    if target_month in monthly_income:
                        monthly_income[target_month] += monthly_amount
                    if target_month == selected_month:
                        selected_income_categories[cat_name] = selected_income_categories.get(cat_name, 0.0) + monthly_amount
                    if target_month == current_month:
                        current_month_income += monthly_amount
                else:
                    if target_month in monthly_expense:
                        monthly_expense[target_month] += monthly_amount
                    if target_month == selected_month:
                        selected_expense_categories[cat_name] = selected_expense_categories.get(cat_name, 0.0) + monthly_amount
                        selected_expense_by_code[category_code] = selected_expense_by_code.get(category_code, 0.0) + monthly_amount
                        if months > 1:
                            selected_amortized_expense += monthly_amount
                            amortized_item_codes.add((str(record_date), category_code, float(record_amount or 0.0)))
                        else:
                            selected_direct_expense += monthly_amount
                    if target_month == current_month:
                        current_month_expense += monthly_amount
                        if months > 1:
                            current_amortized_expense += monthly_amount
                        else:
                            current_direct_expense += monthly_amount

    current_month_net = current_month_income - current_month_expense
    dates = list(last_6_months)
    income_amounts = [round(monthly_income[m], 2) for m in dates]
    expense_amounts = [round(monthly_expense[m], 2) for m in dates]
    net_amounts = [round(monthly_income[m] - monthly_expense[m], 2) for m in dates]
    avg_monthly_expense = sum(expense_amounts) / len(expense_amounts) if expense_amounts else 0.0

    days_15 = []
    for i in range(14, -1, -1):
        d = now - datetime.timedelta(days=i)
        days_15.append(d.strftime("%Y-%m-%d"))

    daily_income = {d: 0.0 for d in days_15}
    daily_expense = {d: 0.0 for d in days_15}
    for record_date, record_amount, _amortization_months, _category_code, record_type, _cat_name, _merchant in confirmed_records:
        date_key = str(record_date)[:10]
        if date_key not in daily_income:
            continue
        if _normalize_record_type(record_type) == "income":
            daily_income[date_key] += record_amount or 0.0
        else:
            daily_expense[date_key] += record_amount or 0.0

    day_income_amounts = [round(daily_income[d], 2) for d in days_15]
    day_expense_amounts = [round(daily_expense[d], 2) for d in days_15]
    day_net_amounts = [round(daily_income[d] - daily_expense[d], 2) for d in days_15]

    dropdown_months = []
    for i in range(6):
        m = now.month - i
        y = now.year
        if m <= 0:
            m += 12
            y -= 1
        dropdown_months.append(f"{y:04d}-{m:02d}")

    expense_category_data = sorted(selected_expense_categories.items(), key=lambda item: item[1], reverse=True)
    income_category_data = sorted(selected_income_categories.items(), key=lambda item: item[1], reverse=True)
    expense_category_pie = [{"name": r[0] if r[0] else "未知代码", "value": round(r[1], 2)} for r in expense_category_data]
    income_category_pie = [{"name": r[0] if r[0] else "未知代码", "value": round(r[1], 2)} for r in income_category_data]

    selected_month_income = sum(amount for _, amount in income_category_data)
    selected_month_expense = sum(amount for _, amount in expense_category_data)
    selected_month_net = selected_month_income - selected_month_expense
    total_budget = sum(budgets_by_code.values())
    selected_year, selected_month_number = map(int, selected_month.split("-"))
    month_days = calendar.monthrange(selected_year, selected_month_number)[1]
    if selected_month == current_month:
        month_days_elapsed = min(now.day, month_days)
    elif selected_month < current_month:
        month_days_elapsed = month_days
    else:
        month_days_elapsed = 0
    month_days_remaining = max(month_days - month_days_elapsed, 0)
    budgeted_expense = sum(amount for code, amount in selected_expense_by_code.items() if budgets_by_code.get(code, 0.0) > 0)
    unbudgeted_expense = selected_month_expense - budgeted_expense
    projected_month_expense = selected_month_expense
    projected_budgeted_expense = budgeted_expense
    if selected_month == current_month and month_days_elapsed > 0:
        projected_month_expense = selected_month_expense / month_days_elapsed * month_days
        projected_budgeted_expense = budgeted_expense / month_days_elapsed * month_days
    budget_remaining = total_budget - budgeted_expense
    projected_budget_gap = total_budget - projected_budgeted_expense
    daily_budget_remaining = budget_remaining / month_days_remaining if month_days_remaining > 0 else budget_remaining
    budget_used_percent = (budgeted_expense / total_budget * 100) if total_budget else 0.0

    budget_statuses = []
    for code, amount in sorted(selected_expense_by_code.items(), key=lambda item: item[1], reverse=True):
        budget = budgets_by_code.get(code, 0.0)
        percent = (amount / budget * 100) if budget else 0.0
        budget_statuses.append(BudgetStatus(
            name=category_names_by_code.get(code) or code or "未知代码",
            amount=round(amount, 2),
            budget=round(budget, 2),
            percent=round(percent, 1),
            remaining=round(budget - amount, 2),
            state=_budget_state(percent, budget),
        ))
    for code, budget in budgets_by_code.items():
        if code not in selected_expense_by_code and budget > 0:
            budget_statuses.append(BudgetStatus(
                name=category_names_by_code.get(code) or code,
                amount=0.0,
                budget=round(budget, 2),
                percent=0.0,
                remaining=round(budget, 2),
                state="ok",
            ))

    over_budget_count = sum(1 for item in budget_statuses if item.state == "over")
    warning_budget_count = sum(1 for item in budget_statuses if item.state == "warning")
    budget_suggestions = [name for name, _amount in expense_category_data[:3] if name]

    uncategorized_count = 0
    recent_expenses = []
    for record_date, record_amount, amortization_months, category_code, record_type, cat_name, merchant in confirmed_records:
        record_type = _normalize_record_type(record_type)
        if record_type != "expense":
            continue
        if str(record_date)[:7] == selected_month and _is_uncategorized_category(category_code, cat_name, set(category_names_by_code)):
            uncategorized_count += 1
        if str(record_date)[:7] == selected_month:
            recent_expenses.append(RecentExpense(
                date=str(record_date)[:10],
                merchant=merchant or "Unknown",
                category=cat_name or "未知代码",
                amount=round(record_amount or 0.0, 2),
                is_amortized=int(amortization_months or 1) > 1,
            ))
    recent_expenses.sort(key=lambda item: item.date, reverse=True)
    recent_expenses = recent_expenses[:10]

    return DashboardStats(
        selected_month=selected_month,
        current_month_income=current_month_income,
        current_month_expense=current_month_expense,
        current_month_net=current_month_net,
        pending_count=pending_count,
        confirmed_count=confirmed_count,
        total_expense=total_expense,
        total_tax=total_tax,
        total_subtotal=total_subtotal,
        dates=dates,
        income_amounts=income_amounts,
        expense_amounts=expense_amounts,
        net_amounts=net_amounts,
        days_15=days_15,
        day_income_amounts=day_income_amounts,
        day_expense_amounts=day_expense_amounts,
        day_net_amounts=day_net_amounts,
        dropdown_months=dropdown_months,
        expense_category_data=expense_category_data,
        income_category_data=income_category_data,
        expense_category_pie=expense_category_pie,
        income_category_pie=income_category_pie,
        selected_month_income=selected_month_income,
        selected_month_expense=selected_month_expense,
        selected_month_net=selected_month_net,
        selected_direct_expense=selected_direct_expense,
        selected_amortized_expense=selected_amortized_expense,
        current_direct_expense=current_direct_expense,
        current_amortized_expense=current_amortized_expense,
        avg_monthly_expense=avg_monthly_expense,
        total_budget=total_budget,
        budget_used_percent=round(budget_used_percent, 1),
        budget_remaining=round(budget_remaining, 2),
        projected_month_expense=round(projected_month_expense, 2),
        projected_budgeted_expense=round(projected_budgeted_expense, 2),
        projected_budget_gap=round(projected_budget_gap, 2),
        daily_budget_remaining=round(daily_budget_remaining, 2),
        month_days_elapsed=month_days_elapsed,
        month_days_remaining=month_days_remaining,
        budgeted_expense=round(budgeted_expense, 2),
        unbudgeted_expense=round(unbudgeted_expense, 2),
        over_budget_count=over_budget_count,
        warning_budget_count=warning_budget_count,
        budget_statuses=budget_statuses,
        budget_suggestions=budget_suggestions,
        uncategorized_count=uncategorized_count,
        amortized_item_count=len(amortized_item_codes),
        recent_expenses=recent_expenses,
    )
