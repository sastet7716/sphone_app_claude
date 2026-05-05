from __future__ import annotations

import csv
import os
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for
import psycopg


BASE_DIR = Path(__file__).resolve().parent
USERS_CSV_PATH = BASE_DIR / "users.csv"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL が未設定です。無料PostgreSQLの接続URLを環境変数に設定してください。"
    )


def get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def init_db() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_checkbox_state (
                    login_id TEXT PRIMARY KEY,
                    check1 BOOLEAN NOT NULL DEFAULT FALSE,
                    check2 BOOLEAN NOT NULL DEFAULT FALSE,
                    check3 BOOLEAN NOT NULL DEFAULT FALSE,
                    check4 BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
        conn.commit()


def read_users_from_csv() -> dict[str, str]:
    users: dict[str, str] = {}

    if not USERS_CSV_PATH.exists():
        return users

    with USERS_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            login_id = (row.get("login_id") or "").strip()
            password = (row.get("password") or "").strip()
            if login_id and password:
                users[login_id] = password

    return users


def is_valid_user(login_id: str, password: str) -> bool:
    users = read_users_from_csv()
    return users.get(login_id) == password


def ensure_user_row(login_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_checkbox_state (login_id, check1, check2, check3, check4)
                VALUES (%s, FALSE, FALSE, FALSE, FALSE)
                ON CONFLICT (login_id) DO NOTHING
                """,
                (login_id,),
            )
        conn.commit()


def load_state(login_id: str) -> dict[str, bool]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT check1, check2, check3, check4
                FROM user_checkbox_state
                WHERE login_id = %s
                """,
                (login_id,),
            )
            row = cur.fetchone()

    if row is None:
        return {"check1": False, "check2": False, "check3": False, "check4": False}

    return {
        "check1": bool(row[0]),
        "check2": bool(row[1]),
        "check3": bool(row[2]),
        "check4": bool(row[3]),
    }


VALID_CHOICES = frozenset({"check1", "check2", "check3", "check4"})


def choice_to_flags(choice: str) -> tuple[bool, bool, bool, bool]:
    """常に高々1つだけ True。choice が空または不正ならすべて False。"""
    if choice not in VALID_CHOICES:
        return False, False, False, False
    return (
        choice == "check1",
        choice == "check2",
        choice == "check3",
        choice == "check4",
    )


def save_state(
    login_id: str, check1: bool, check2: bool, check3: bool, check4: bool
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE user_checkbox_state
                SET check1 = %s, check2 = %s, check3 = %s, check4 = %s
                WHERE login_id = %s
                """,
                (check1, check2, check3, check4, login_id),
            )
        conn.commit()


def selected_choice_from_state(state: dict[str, bool]) -> str:
    """表示用。複数 True のレガシーデータは先頭の項目のみ採用。"""
    for key in ("check1", "check2", "check3", "check4"):
        if state.get(key):
            return key
    return ""


# WSGIサーバー(gunicornなど)で起動されたときもDB初期化する
init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    login_id = session.get("login_id")
    if not login_id:
        return render_template("index.html", state=None, login_id=None, error=None)

    ensure_user_row(login_id)

    if request.method == "POST":
        choice = (request.form.get("choice") or "").strip()
        check1, check2, check3, check4 = choice_to_flags(choice)
        save_state(login_id, check1, check2, check3, check4)
        return redirect(url_for("index"))

    state = load_state(login_id)
    selected_choice = selected_choice_from_state(state)
    return render_template(
        "index.html",
        state=state,
        selected_choice=selected_choice,
        login_id=login_id,
        error=None,
    )


@app.route("/login", methods=["POST"])
def login():
    login_id = (request.form.get("login_id") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not login_id or not password:
        return render_template(
            "index.html",
            state=None,
            login_id=None,
            error="ログインIDとパスワードを入力してください。",
        )

    if not is_valid_user(login_id, password):
        return render_template(
            "index.html",
            state=None,
            login_id=None,
            error="認証に失敗しました。IDまたはパスワードを確認してください。",
        )

    session["login_id"] = login_id
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("login_id", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
