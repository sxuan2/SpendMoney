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
    if "record_type" not in record_columns:
        cursor.execute("ALTER TABLE records ADD COLUMN record_type TEXT DEFAULT 'expense'")
    cursor.execute("UPDATE records SET amortization_months = 1 WHERE amortization_months IS NULL OR amortization_months < 1")
    cursor.execute("UPDATE records SET owner_user_id = ? WHERE owner_user_id IS NULL OR owner_user_id = ''", (DEFAULT_OWNER_USER_ID,))
    cursor.execute("UPDATE records SET record_type = 'expense' WHERE record_type IS NULL OR record_type NOT IN ('expense', 'income')")
        
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

    cursor.execute('''CREATE TABLE IF NOT EXISTS categories
                      (code TEXT PRIMARY KEY, name TEXT)''')
    category_columns = {row[1] for row in cursor.execute("PRAGMA table_info(categories)")}
    if "owner_user_id" not in category_columns:
        cursor.execute("ALTER TABLE categories RENAME TO categories_global_backup")
        cursor.execute('''CREATE TABLE categories
                          (owner_user_id TEXT NOT NULL DEFAULT '1',
                           code TEXT NOT NULL,
                           name TEXT,
                           category_type TEXT NOT NULL DEFAULT 'expense',
                           PRIMARY KEY (owner_user_id, code))''')
        cursor.execute("INSERT INTO categories (owner_user_id, code, name, category_type) SELECT ?, code, name, 'expense' FROM categories_global_backup", (DEFAULT_OWNER_USER_ID,))
        cursor.execute("DROP TABLE categories_global_backup")
    elif "category_type" not in category_columns:
        cursor.execute("ALTER TABLE categories ADD COLUMN category_type TEXT NOT NULL DEFAULT 'expense'")

    def ensure_default_categories(owner_id):
        cursor.executemany(
            "INSERT OR IGNORE INTO categories (owner_user_id, code, name, category_type) VALUES (?, ?, ?, 'expense')",
            [(owner_id, code, name) for code, name in expense_defaults]
        )
        cursor.executemany(
            "INSERT OR IGNORE INTO categories (owner_user_id, code, name, category_type) VALUES (?, ?, ?, 'income')",
            [(owner_id, code, name) for code, name in income_defaults]
        )
        cursor.execute("UPDATE categories SET category_type='expense' WHERE owner_user_id=? AND (category_type IS NULL OR category_type NOT IN ('expense', 'income'))", (owner_id,))

    cursor.execute("SELECT COUNT(*) FROM categories WHERE owner_user_id=?", (DEFAULT_OWNER_USER_ID,))
    if cursor.fetchone()[0] == 0:
        print("[*] 检测到默认用户标签字典为空，正在注入初始标签代码...")
    ensure_default_categories(DEFAULT_OWNER_USER_ID)

    owner_ids = {row[0] for row in cursor.execute("SELECT DISTINCT owner_user_id FROM records WHERE owner_user_id IS NOT NULL AND owner_user_id != ''")}
    for owner_id in owner_ids:
        ensure_default_categories(owner_id)
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

def insert_record(filename, amount, merchant, date, subtotal, tax, category, raw_text, status='processed', owner_user_id=DEFAULT_OWNER_USER_ID, record_type='expense'):
    record_type = record_type if record_type in ('expense', 'income') else 'expense'
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO records (owner_user_id, filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at, record_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?)",
                   (str(owner_user_id), filename, amount, merchant, date, subtotal, tax, category, raw_text, status, record_type))
    conn.commit()
    conn.close()
