import datetime
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from portfolio import calculate_portfolio

router = APIRouter()
DB_NAME = "finance.db"
SUPPORTED_CURRENCIES = {"CAD", "USD", "CNY"}


def api_owner(request: Request) -> str:
    api_key = (request.headers.get("X-API-Key") or "").strip()
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute(
        "SELECT owner_user_id FROM api_keys WHERE api_key=? AND is_active=1", (api_key,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")
    return str(row[0])


def normalized_timestamp(value) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError("quoted_at is required")
    parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def positive_number(value, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number")
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return number


@router.get("/api/portfolio/securities")
async def api_portfolio_securities(request: Request, include_inactive: bool = False):
    owner = api_owner(request)
    _, holdings, _ = calculate_portfolio(owner)
    held_ids = sorted({holding.security_id for holding in holdings})
    if not held_ids and not include_inactive:
        return {"base_currency": "CAD", "securities": []}
    conn = sqlite3.connect(DB_NAME)
    params = [owner]
    where = ["s.owner_user_id=?"]
    if not include_inactive:
        placeholders = ",".join("?" for _ in held_ids)
        where.append(f"s.id IN ({placeholders})")
        params.extend(held_ids)
    rows = conn.execute(
        f"""SELECT s.id,s.symbol,s.exchange,s.name,s.asset_type,s.currency,
                   p.price,p.currency,p.quoted_at,p.source
            FROM securities s
            LEFT JOIN security_prices p ON p.id=(
                SELECT p2.id FROM security_prices p2
                WHERE p2.owner_user_id=s.owner_user_id AND p2.security_id=s.id
                ORDER BY p2.quoted_at DESC,p2.id DESC LIMIT 1
            )
            WHERE {' AND '.join(where)}
            ORDER BY s.symbol,s.currency,s.exchange""",
        params,
    ).fetchall()
    conn.close()
    return {
        "base_currency": "CAD",
        "securities": [
            {
                "security_id": row[0], "symbol": row[1], "exchange": row[2],
                "name": row[3], "asset_type": row[4], "currency": row[5],
                "latest_price": row[6], "latest_price_currency": row[7],
                "latest_quoted_at": row[8], "latest_source": row[9],
            }
            for row in rows
        ],
    }


@router.post("/api/portfolio/prices")
async def api_portfolio_prices(request: Request):
    owner = api_owner(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    source = str(payload.get("source") or "").strip()[:100]
    prices = payload.get("prices")
    if not source or not isinstance(prices, list) or not prices or len(prices) > 500:
        raise HTTPException(status_code=400, detail="source and 1-500 prices are required")
    conn = sqlite3.connect(DB_NAME)
    inserted = duplicates = 0
    rejected = []
    for index, item in enumerate(prices):
        try:
            if not isinstance(item, dict):
                raise ValueError("price item must be an object")
            security_id = int(item.get("security_id"))
            row = conn.execute(
                "SELECT currency FROM securities WHERE id=? AND owner_user_id=? AND is_active=1",
                (security_id, owner),
            ).fetchone()
            if not row:
                raise ValueError("security not found for this API key")
            currency = str(item.get("currency") or "").strip().upper()
            if currency != row[0]:
                raise ValueError(f"currency must be {row[0]}")
            price = positive_number(item.get("price"), "price")
            quoted_at = normalized_timestamp(item.get("quoted_at"))
            cursor = conn.execute(
                """INSERT OR IGNORE INTO security_prices
                   (owner_user_id,security_id,price,currency,quoted_at,source)
                   VALUES(?,?,?,?,?,?)""",
                (owner, security_id, price, currency, quoted_at, source),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
        except (TypeError, ValueError) as exc:
            rejected.append({"index": index, "error": str(exc)})
    conn.commit()
    conn.close()
    return {"inserted": inserted, "duplicates": duplicates, "rejected": rejected}


@router.post("/api/portfolio/exchange-rates")
async def api_portfolio_exchange_rates(request: Request):
    owner = api_owner(request)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    source = str(payload.get("source") or "").strip()[:100]
    rates = payload.get("rates")
    if not source or not isinstance(rates, list) or not rates or len(rates) > 100:
        raise HTTPException(status_code=400, detail="source and 1-100 rates are required")
    conn = sqlite3.connect(DB_NAME)
    inserted = duplicates = 0
    rejected = []
    for index, item in enumerate(rates):
        try:
            if not isinstance(item, dict):
                raise ValueError("rate item must be an object")
            base = str(item.get("base_currency") or "").strip().upper()
            quote = str(item.get("quote_currency") or "").strip().upper()
            if base not in SUPPORTED_CURRENCIES or quote not in SUPPORTED_CURRENCIES or base == quote:
                raise ValueError("invalid currency pair")
            rate = positive_number(item.get("rate"), "rate")
            quoted_at = normalized_timestamp(item.get("quoted_at"))
            cursor = conn.execute(
                """INSERT OR IGNORE INTO exchange_rates
                   (owner_user_id,base_currency,quote_currency,rate,quoted_at,source)
                   VALUES(?,?,?,?,?,?)""",
                (owner, base, quote, rate, quoted_at, source),
            )
            if cursor.rowcount:
                inserted += 1
            else:
                duplicates += 1
        except (TypeError, ValueError) as exc:
            rejected.append({"index": index, "error": str(exc)})
    conn.commit()
    conn.close()
    return {"inserted": inserted, "duplicates": duplicates, "rejected": rejected}
