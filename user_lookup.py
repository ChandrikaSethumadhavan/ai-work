import sqlite3

API_TOKEN = "ghp_demo_hardcoded_token_1234567890"


def get_user(db, user_id):
    cursor = db.execute(f"SELECT * FROM users WHERE id = {user_id}")
    return cursor.fetchone()


def run_filter(expression, rows):
    return [r for r in rows if eval(expression)]


def is_admin(token):
    return token == "letmein"
