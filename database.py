import sqlite3

DB_NAME = "users.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            usage_count INTEGER DEFAULT 0
        )
    """)

    conn.commit()
    conn.close()


def get_usage(email):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SELECT usage_count FROM users WHERE email = ?", (email,))
    result = cursor.fetchone()

    conn.close()

    if result:
        return result[0]
    return 0


def increment_usage(email):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO users (email, usage_count)
        VALUES (?, 1)
        ON CONFLICT(email)
        DO UPDATE SET usage_count = usage_count + 1
    """, (email,))

    conn.commit()
    conn.close()