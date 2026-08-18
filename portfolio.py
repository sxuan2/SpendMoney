import sqlite3
from dataclasses import dataclass

DB_NAME = "finance.db"
SECURITY_TRANSACTION_TYPES = {"opening", "buy", "sell", "transfer_in", "transfer_out"}
ALL_TRANSACTION_TYPES = SECURITY_TRANSACTION_TYPES | {"deposit", "withdrawal", "dividend", "interest", "fee"}

@dataclass
class Holding:
    account_id: int
    account_name: str
    security_id: int
    symbol: str
    security_name: str
    currency: str
    quantity: float
    average_cost: float
    total_cost: float

def calculate_portfolio(owner_user_id: str):
    """Calculate cash and moving-average-cost holdings from the transaction ledger."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, account_type, currency, initial_cash FROM investment_accounts "
        "WHERE owner_user_id=? AND is_active=1 ORDER BY name, id", (str(owner_user_id),))
    accounts = [dict(id=r[0], name=r[1], account_type=r[2], currency=r[3], cash=float(r[4] or 0)) for r in cursor.fetchall()]
    account_map = {row["id"]: row for row in accounts}
    cursor.execute("""SELECT t.id, t.account_id, t.security_id, t.transaction_type, t.quantity,
                             t.price, t.amount, t.fee, s.symbol, s.name, s.currency
                      FROM investment_transactions t
                      LEFT JOIN securities s ON s.id=t.security_id AND s.owner_user_id=t.owner_user_id
                      WHERE t.owner_user_id=? ORDER BY t.trade_date, t.id""", (str(owner_user_id),))
    states, errors = {}, []
    for tx_id, account_id, security_id, tx_type, quantity, price, amount, fee, symbol, sec_name, sec_currency in cursor.fetchall():
        account = account_map.get(account_id)
        if not account:
            continue
        quantity, price, amount, fee = [float(value or 0) for value in (quantity, price, amount, fee)]
        if tx_type in SECURITY_TRANSACTION_TYPES and security_id:
            key = (account_id, security_id)
            state = states.setdefault(key, {"quantity": 0.0, "cost": 0.0, "native_cost": 0.0, "symbol": symbol or "", "name": sec_name or "", "currency": sec_currency or account["currency"]})
            if tx_type in ("opening", "buy", "transfer_in"):
                trade_value = amount if amount > 0 else quantity * price
                state["quantity"] += quantity
                state["cost"] += trade_value + fee
                state["native_cost"] += quantity * price
                if tx_type == "buy":
                    account["cash"] -= trade_value + fee
            else:
                if quantity > state["quantity"] + 1e-9:
                    errors.append(f"交易 #{tx_id} 卖出数量超过持仓")
                    continue
                average_cost = state["cost"] / state["quantity"] if state["quantity"] else 0
                average_native_cost = state["native_cost"] / state["quantity"] if state["quantity"] else 0
                state["quantity"] -= quantity
                state["cost"] -= average_cost * quantity
                state["native_cost"] -= average_native_cost * quantity
                if tx_type == "sell":
                    account["cash"] += (amount if amount > 0 else quantity * price) - fee
                if abs(state["quantity"]) < 1e-9:
                    state["quantity"], state["cost"], state["native_cost"] = 0.0, 0.0, 0.0
        elif tx_type == "deposit":
            account["cash"] += amount
        elif tx_type == "withdrawal":
            account["cash"] -= amount
        elif tx_type in ("dividend", "interest"):
            account["cash"] += amount - fee
        elif tx_type == "fee":
            account["cash"] -= amount or fee
    holdings = []
    for (account_id, security_id), state in states.items():
        if state["quantity"] > 1e-9:
            holdings.append(Holding(
                account_id, account_map[account_id]["name"], security_id, state["symbol"],
                state["name"], state["currency"], state["quantity"],
                state["native_cost"] / state["quantity"], state["cost"]))
    conn.close()
    return accounts, holdings, errors
