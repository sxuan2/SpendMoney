import sqlite3

DB_NAME = 'finance.db'

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
    cursor.execute("UPDATE records SET amortization_months = 1 WHERE amortization_months IS NULL OR amortization_months < 1")
        
    # 【修复核心】：使用 IF NOT EXISTS，如果 categories 表已经存在，绝对不砸表，完 good 保留你的自定义标签
    cursor.execute('''CREATE TABLE IF NOT EXISTS categories 
                      (code TEXT PRIMARY KEY, name TEXT)''')
    
    # 【修复核心】：只有当 categories 表里一条数据都没有（也就是你第一次全新部署系统）时，才注入默认基础标签
    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        print("[*] 检测到标签字典为空，正在注入初始基础纯数字代码...")
        defaults = [
            ('1', '餐饮美食'), ('2', '服饰美容'), 
            ('3', '交通汽车'), ('4', '居家生活'), 
            ('5', '休闲娱乐'), ('6', '数码电器'), 
            ('7', '医疗健康'), ('0', '未分类/其他')
        ]
        cursor.executemany("INSERT INTO categories (code, name) VALUES (?, ?)", defaults)
    
    # 历史遗留脏数据清洗对齐（依旧保留，防止有异常垃圾代码导致前端对齐失败）
    cursor.execute("UPDATE records SET category = '0' WHERE category NOT IN (SELECT code FROM categories)")
        
    conn.commit()
    conn.close()

def record_exists(filename):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM records WHERE filename=?", (filename,))
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def insert_record(filename, amount, merchant, date, subtotal, tax, category, raw_text, status='processed'):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO records (filename, amount, merchant, date, subtotal, tax, category, raw_text, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))", 
                   (filename, amount, merchant, date, subtotal, tax, category, raw_text, status))
    conn.commit()
    conn.close()
