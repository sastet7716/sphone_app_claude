from __future__ import annotations

import csv
import os
from pathlib import Path

from flask import Flask, redirect, render_template, request, session, url_for
import psycopg
import psycopg.errors


BASE_DIR = Path(__file__).resolve().parent
USERS_CSV_PATH = BASE_DIR / "users.csv"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL が未設定です。無料PostgreSQLの接続URLを環境変数に設定してください。"
    )

# 交差点01〜12（内部キーは check1 … check12）
CHOICE_KEYS = tuple(f"check{i}" for i in range(1, 13))
VALID_CHOICES = frozenset(CHOICE_KEYS)
APP_NAME = "交差点見守り"


def get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def _migrate_user_rows_for_unique_indexes(conn: psycopg.Connection) -> None:
    """
    既存DBに「同じ項目を複数ユーザーがTRUE」などあり、
    部分UNIQUE INDEX 作成が失敗する場合の解消用。

    1) 1ユーザー1項目まで（先に付いた方を残す）
    2) 各項目は1ユーザーまで（login_id 昇順で先頭1名を残し他をFALSE）
    """
    cols = CHOICE_KEYS
    with conn.cursor() as cur:
        cur.execute(
            "SELECT login_id, "
            + ", ".join(cols)
            + " FROM user_checkbox_state"
        )
        rows = cur.fetchall()
        for row in rows:
            login_id = row[0]
            flags = [bool(row[i + 1]) for i in range(12)]
            if sum(1 for f in flags if f) <= 1:
                continue
            chosen_idx = next(i for i, f in enumerate(flags) if f)
            sets = ", ".join(f"{cols[i]} = %s" for i in range(12))
            vals = tuple(i == chosen_idx for i in range(12)) + (login_id,)
            cur.execute(
                f"""
                UPDATE user_checkbox_state
                SET {sets}
                WHERE login_id = %s
                """,
                vals,
            )

    with conn.cursor() as cur:
        for col in cols:
            cur.execute(
                f"""
                SELECT login_id FROM user_checkbox_state
                WHERE {col} IS TRUE
                ORDER BY login_id
                """
            )
            holders = [r[0] for r in cur.fetchall()]
            if len(holders) <= 1:
                continue
            for lid in holders[1:]:
                cur.execute(
                    f"""
                    UPDATE user_checkbox_state SET {col} = FALSE WHERE login_id = %s
                    """,
                    (lid,),
                )


def init_db() -> None:
    col_defs = ",\n                    ".join(
        f"check{i} BOOLEAN NOT NULL DEFAULT FALSE" for i in range(1, 13)
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS user_checkbox_state (
                    login_id TEXT PRIMARY KEY,
                    {col_defs}
                )
                """
            )
            # 旧4列のみのDBを12列へ拡張
            for i in range(5, 13):
                cur.execute(
                    f"""
                    ALTER TABLE user_checkbox_state
                    ADD COLUMN IF NOT EXISTS check{i} BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
        _migrate_user_rows_for_unique_indexes(conn)
        with conn.cursor() as cur:
            # 各交差点は常に最大1ユーザーまで（同時更新の競合もDBで防止）
            for col in CHOICE_KEYS:
                cur.execute(
                    f"""
                    CREATE UNIQUE INDEX IF NOT EXISTS user_checkbox_unique_{col}
                    ON user_checkbox_state ((1))
                    WHERE {col} IS TRUE
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
    cols = ", ".join(["login_id"] + list(CHOICE_KEYS))
    falses = ", ".join(["FALSE"] * 12)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO user_checkbox_state ({cols})
                VALUES (%s, {falses})
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
                SELECT """
                + ", ".join(CHOICE_KEYS)
                + """
                FROM user_checkbox_state
                WHERE login_id = %s
                """,
                (login_id,),
            )
            row = cur.fetchone()

    if row is None:
        return {k: False for k in CHOICE_KEYS}

    return {CHOICE_KEYS[i]: bool(row[i]) for i in range(12)}


def choice_to_flags(choice: str) -> tuple[bool, ...]:
    """常に高々1つだけ True。choice が空または不正ならすべて False。"""
    if choice not in VALID_CHOICES:
        return (False,) * 12
    idx = int(choice.replace("check", "")) - 1
    return tuple(i == idx for i in range(12))


def save_state(login_id: str, flags: tuple[bool, ...]) -> None:
    if len(flags) != 12:
        raise ValueError("flags は12要素である必要があります")
    sets = ", ".join(f"{CHOICE_KEYS[i]} = %s" for i in range(12))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE user_checkbox_state
                SET {sets}
                WHERE login_id = %s
                """,
                (*flags, login_id),
            )
        conn.commit()


def selected_choice_from_state(state: dict[str, bool]) -> str:
    """表示用。複数 True のレガシーデータは先頭の項目のみ採用。"""
    for key in CHOICE_KEYS:
        if state.get(key):
            return key
    return ""


def choice_held_by_others(exclude_login_id: str) -> dict[str, str]:
    """各交差点を、自分以外のどの login_id が保持しているか。"""
    cols = CHOICE_KEYS
    owners: dict[str, str] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT login_id, """
                + ", ".join(cols)
                + """
                FROM user_checkbox_state
                WHERE login_id != %s
                """,
                (exclude_login_id,),
            )
            for row in cur.fetchall():
                lid = row[0]
                for i, col in enumerate(cols):
                    if row[i + 1]:
                        owners[col] = lid
    return owners


def first_free_choice(held_by_others: dict[str, str]) -> str | None:
    """他ユーザーに取られていない最初の交差点。空きがなければ None。"""
    for k in CHOICE_KEYS:
        if k not in held_by_others:
            return k
    return None


def choices_display() -> list[tuple[str, str]]:
    """テンプレート用 (value, 表示ラベル)"""
    return [(f"check{i}", f"交差点{i:02d}") for i in range(1, 13)]


def render_main_logged_in(login_id: str, error: str | None = None):
    ensure_user_row(login_id)
    state = load_state(login_id)
    stored = selected_choice_from_state(state)
    choice_owners = choice_held_by_others(login_id)
    ff = first_free_choice(choice_owners)
    # DBに未保存のときは画面も未選択（先頭の空きを自動では選ばない）
    selected_choice = stored
    no_slot_available = not stored and ff is None
    return render_template(
        "index.html",
        app_name=APP_NAME,
        choices_display=choices_display(),
        state=state,
        selected_choice=selected_choice,
        login_id=login_id,
        error=error,
        choice_owners=choice_owners,
        no_slot_available=no_slot_available,
    )


# WSGIサーバー(gunicornなど)で起動されたときもDB初期化する
init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    login_id = session.get("login_id")
    if not login_id:
        return render_template(
            "index.html",
            app_name=APP_NAME,
            choices_display=choices_display(),
            state=None,
            login_id=None,
            error=None,
            choice_owners={},
            no_slot_available=False,
        )

    if request.method == "POST":
        choice = (request.form.get("choice") or "").strip()
        if choice not in VALID_CHOICES and choice != "":
            return render_main_logged_in(
                login_id,
                error="不正な選択です。",
            )

        # 未選択のまま保存 = DB上もすべて OFF（ラジオをクリックで解除した状態）
        if choice == "":
            save_state(login_id, (False,) * 12)
            return redirect(url_for("index"))

        held = choice_held_by_others(login_id)
        if choice in held:
            who = held[choice]
            return render_main_logged_in(
                login_id,
                error=f"その交差点は {who} さんが使用中です。別の交差点を選ぶか、しばらく待ってください。",
            )
        flags = choice_to_flags(choice)
        try:
            save_state(login_id, flags)
        except psycopg.errors.UniqueViolation:
            held_after = choice_held_by_others(login_id)
            if choice in held_after:
                err = f"その交差点は {held_after[choice]} さんが使用中です。（同時に選ばれました）"
            else:
                err = "別のユーザーが先に選んだため保存できませんでした。画面を更新してください。"
            return render_main_logged_in(login_id, error=err)
        return redirect(url_for("index"))

    return render_main_logged_in(login_id)


@app.route("/login", methods=["POST"])
def login():
    login_id = (request.form.get("login_id") or "").strip()
    password = (request.form.get("password") or "").strip()

    if not login_id or not password:
        return render_template(
            "index.html",
            app_name=APP_NAME,
            choices_display=choices_display(),
            state=None,
            login_id=None,
            error="ログインIDとパスワードを入力してください。",
            choice_owners={},
            no_slot_available=False,
        )

    if not is_valid_user(login_id, password):
        return render_template(
            "index.html",
            app_name=APP_NAME,
            choices_display=choices_display(),
            state=None,
            login_id=None,
            error="認証に失敗しました。IDまたはパスワードを確認してください。",
            choice_owners={},
            no_slot_available=False,
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
