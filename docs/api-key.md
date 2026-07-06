# SpendMoney iPhone API Key 生成说明

用于给某个用户生成独立的 iPhone 快捷指令 API Key。生成后，这个用户通过 `/spendmoney/api/iphone-upload` POST 的账单会写入自己的账本。

## 1. 找到用户 ID

在主 Django 容器里查看用户 ID：

```bash
docker exec portal_web python manage.py shell -c "from django.contrib.auth.models import User; print(list(User.objects.filter(is_active=True).values_list('id','username')))"
```

记下目标用户的 `id`。

## 2. 生成 API Key

替换下面命令里的 `OWNER_USER_ID` 和 `label`。

```bash
cd /home/ubuntu/spendmoney && python3 - <<'PY'
import secrets, sqlite3

owner_user_id = "OWNER_USER_ID"
label = "iPhone Shortcut"

api_key = secrets.token_urlsafe(32)

conn = sqlite3.connect("finance.db")
cur = conn.cursor()
cur.execute(
    "INSERT INTO api_keys (api_key, owner_user_id, label, is_active) VALUES (?, ?, ?, 1)",
    (api_key, owner_user_id, label),
)
conn.commit()
conn.close()

print(api_key)
PY
```

例如某个用户 ID 是 `3`，就把：

```python
owner_user_id = "OWNER_USER_ID"
```

改成：

```python
owner_user_id = "3"
```

## 3. 快捷指令配置

POST 地址：

```text
/spendmoney/api/iphone-upload
```

Header：

```text
X-API-Key: 这个用户自己的 key
```

请求体仍按现有快捷指令格式发送，例如：

```json
{
  "raw_text": "{\"merchant\":\"Store\",\"date\":\"2026-07-06\",\"subtotal\":10.0,\"tax\":0.8,\"total\":10.8}"
}
```

## 4. 查看已有 API Keys

不要打印完整 key，只看归属和标签：

```bash
cd /home/ubuntu/spendmoney && python3 - <<'PY'
import sqlite3
conn = sqlite3.connect("finance.db")
cur = conn.cursor()
for row in cur.execute("SELECT owner_user_id, label, is_active, length(api_key), created_at FROM api_keys ORDER BY owner_user_id, label"):
    print(row)
conn.close()
PY
```

## 5. 停用某个 key

如果要停用，把对应 key 的 `is_active` 改成 `0`：

```bash
cd /home/ubuntu/spendmoney && python3 - <<'PY'
import sqlite3

api_key = "PASTE_KEY_HERE"

conn = sqlite3.connect("finance.db")
cur = conn.cursor()
cur.execute("UPDATE api_keys SET is_active=0 WHERE api_key=?", (api_key,))
conn.commit()
conn.close()
PY
```
