import sqlite3

DB_NAME = 'finance.db'
DEFAULT_OWNER_USER_ID = '1'

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # 建立核心账单表
    cursor.execute('''CREATE TABLE IF NOT EXISTS records
                      (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                       filename TEXT UNIQUE, 
                       amount REAL, 
                       merchant TEXT, 
                       raw_text TEXT,
                       status TEXT DEFAULT 'processed')''')
    
    # 无损追加账单表字段
    try:
        cursor.execute("ALTER TABLE records ADD COLUMN date TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE records ADD COLUMN subtotal REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE records ADD COLUMN tax REAL DEFAULT 0.0")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE records ADD COLUMN category TEXT DEFAULT '0'")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE records ADD COLUMN created_at TEXT")
    except sqlite3.OperationalError:
        pass
    record_columns = {row[1] for row in cursor.execute("PRAGMA table_info(records)")}
    if "amortization_months" not in record_columns:
        cursor.execute("ALTER TABLE records ADD COLUMN amortization_months INTEGER DEFAULT 1")
    if "owner_user_id" not in record_columns:
        cursor.execute("ALTER TABLE records ADD COLUMN owner_user_id TEXT DEFAULT '1'")
    cursor.execute("UPDATE records SET amortization_months = 1 WHERE amortization_months IS NULL OR amortization_months < 1")
    cursor.execute("UPDATE records SET owner_user_id = ? WHERE owner_user_id IS NULL OR owner_user_id = ''", (DEFAULT_OWNER_USER_ID,))
        
    defaults = [
        ('1', '餐饮美食'), ('2', '服饰美容'),
        ('3', '交通汽车'), ('4', '居家生活'),
        ('5', '休闲娱乐'), ('6', '数码电器'),
        ('7', '医疗健康'), ('0', '未分类/其他')
    ]

    cursor.execute('''CREATE TABLE IF NOT EXISTS categories
                      (code TEXT PRIMARY KEY, name TEXT)''')
    category_columns = {row[1] for row in cursor.execute("PRAGMA table_info(categories)")}
    if "owner_user_id" not in category_columns:
        cursor.execute("ALTER TABLE categories RENAME TO categories_global_backup")
        cursor.execute('''CREATE TABLE categories
                          (owner_user_id TEXT NOT NULL DEFAULT '1',
                           code TEXT NOT NULL,
                           name TEXT,
                           PRIMARY KEY (owner_user_id, code))''')
        cursor.execute("INSERT INTO categories (owner_user_id, code, name) SELECT ?, code, name FROM categories_global_backup", (DEFAULT_OWNER_USER_ID,))
        cursor.execute("DROP TABLE categories_global_backup")

    cursor.execute("SELECT COUNT(*) FROM categories WHERE owner_user_id=?", (DEFAULT_OWNER_USER_ID,))
    if cursor.fetchone()[0] == 0:
        print("[*] 检测到默认用户标签字典为空，正在注入初始基础纯数字代码...")
        cursor.executemany("INSERT INTO categories (owner_user_id, code, name) VALUES (?, ?, ?)", [(DEFAULT_OWNER_USER_ID, code, name) for code, name in defaults])

    owner_ids = {row[0] for row in cursor.execute("SELECT DISTINCT owner_user_id FROM records WHERE owner_user_id IS NOT NULL AND owner_user_id != ''")}
    for owner_id in owner_ids:
        cursor.execute("SELECT COUNT(*) FROM categories WHERE owner_user_id=?", (owner_id,))
        if cursor.fetchone()[0] == 0:
            cursor.executemany("INSERT INTO categories (owner_user_id, code, name) VALUES (?, ?, ?)", [(owner_id, code, name) for code, name in defaults])
        cursor.execute("UPDATE records SET category = '0' WHERE owner_user_id=? AND category NOT IN (SELECT code FROM categories WHERE owner_user_id=?)", (owner_id, owner_id))

    cursor.execute("""CREATE TABLE IF NOT EXISTS api_keys
                      (api_key TEXT PRIMARY KEY,
                       owner_user_id TEXT NOT NULL,
                       label TEXT DEFAULT '',
                       is_active INTEGER NOT NULL DEFAULT 1,
                       created_at TEXT DEFAULT (datetime('now', 'localtime')))""")
        
    conn.commit()
    conn.close()

def record_exists(filename, owner_user_id=DEFAULT_OWNER_USER_ID):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM records WHERE filename=? AND owner_user_id=?", (filename, str(owner_user_id)))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def insert_record(filename, amount, merchant, date, subtotal, tax, category, raw_text, status='processed', owner_user_id=DEFAULT_OWNER_USER_ID):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO records (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))",
                   (str(owner_user_id), filename, amount, merchant, date, subtotal, tax, category, raw_text, status))
    conn.commit()
    conn.close()
