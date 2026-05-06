from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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

NUM_INTERSECTIONS = 20
CHOICE_KEYS = tuple(f"check{i}" for i in range(1, NUM_INTERSECTIONS + 1))
VALID_CHOICES = frozenset(CHOICE_KEYS)
APP_NAME = "交差点見守り"
JST = ZoneInfo("Asia/Tokyo")


def format_claim_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(JST).strftime("%H:%M")


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
    n = NUM_INTERSECTIONS
    with conn.cursor() as cur:
        cur.execute(
            "SELECT login_id, " + ", ".join(cols) + " FROM user_checkbox_state"
        )
        rows = cur.fetchall()
        for row in rows:
            login_id = row[0]
            flags = [bool(row[i + 1]) for i in range(n)]
            if sum(1 for f in flags if f) <= 1:
                continue
            chosen_idx = next(i for i, f in enumerate(flags) if f)
            sets = ", ".join(f"{cols[i]} = %s" for i in range(n))
            vals = tuple(i == chosen_idx for i in range(n)) + (login_id,)
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


def _ensure_claims_seeded(conn: psycopg.Connection) -> None:
    """初回のみ boolean 状態から claim 行を補完（時刻は現在時刻）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM intersection_claim LIMIT 1")
        if cur.fetchone() is not None:
            return
        cur.execute("SELECT login_id, " + ", ".join(CHOICE_KEYS) + " FROM user_checkbox_state")
        for row in cur.fetchall():
            lid = row[0]
            for i, col in enumerate(CHOICE_KEYS):
                if row[i + 1]:
                    cur.execute(
                        """
                        INSERT INTO intersection_claim (slot_index, login_id, claimed_at)
                        VALUES (%s, %s, NOW())
                        ON CONFLICT (slot_index) DO UPDATE
                        SET login_id = EXCLUDED.login_id,
                            claimed_at = EXCLUDED.claimed_at
                        """,
                        (i + 1, lid),
                    )


def init_db() -> None:
    col_defs = ",\n                    ".join(
        f"check{i} BOOLEAN NOT NULL DEFAULT FALSE" for i in range(1, NUM_INTERSECTIONS + 1)
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
            for i in range(1, NUM_INTERSECTIONS + 1):
                cur.execute(
                    f"""
                    ALTER TABLE user_checkbox_state
                    ADD COLUMN IF NOT EXISTS check{i} BOOLEAN NOT NULL DEFAULT FALSE
                    """
                )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS intersection_claim (
                    slot_index SMALLINT PRIMARY KEY
                        CHECK (slot_index >= 1 AND slot_index <= {NUM_INTERSECTIONS}),
                    login_id TEXT NOT NULL,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        _migrate_user_rows_for_unique_indexes(conn)
        _ensure_claims_seeded(conn)
        with conn.cursor() as cur:
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
    falses = ", ".join(["FALSE"] * NUM_INTERSECTIONS)
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

    return {CHOICE_KEYS[i]: bool(row[i]) for i in range(NUM_INTERSECTIONS)}


def choice_to_flags(choice: str) -> tuple[bool, ...]:
    """常に高々1つだけ True。choice が空または不正ならすべて False。"""
    if choice not in VALID_CHOICES:
        return (False,) * NUM_INTERSECTIONS
    idx = int(choice.replace("check", "")) - 1
    return tuple(i == idx for i in range(NUM_INTERSECTIONS))


def save_state(login_id: str, flags: tuple[bool, ...]) -> None:
    if len(flags) != NUM_INTERSECTIONS:
        raise ValueError(f"flags は{NUM_INTERSECTIONS}要素である必要があります")
    sets = ", ".join(f"{CHOICE_KEYS[i]} = %s" for i in range(NUM_INTERSECTIONS))
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


def sync_intersection_claim(login_id: str, choice: str) -> None:
    """boolean 更新後に、スロット取得時刻テーブルを同期する。"""
    slot: int | None = None
    if choice in VALID_CHOICES:
        slot = int(choice.replace("check", ""))
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM intersection_claim WHERE login_id = %s",
                (login_id,),
            )
            if slot is not None:
                cur.execute(
                    """
                    INSERT INTO intersection_claim (slot_index, login_id, claimed_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (slot_index) DO UPDATE
                    SET login_id = EXCLUDED.login_id,
                        claimed_at = EXCLUDED.claimed_at
                    """,
                    (slot, login_id),
                )
        conn.commit()


def selected_choice_from_state(state: dict[str, bool]) -> str:
    """表示用。複数 True のレガシーデータは先頭の項目のみ採用。"""
    for key in CHOICE_KEYS:
        if state.get(key):
            return key
    return ""


def choice_held_globally() -> dict[str, str]:
    """各交差点を保持している login_id（空きはキーなし）。"""
    cols = CHOICE_KEYS
    owners: dict[str, str] = {}
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT login_id, " + ", ".join(cols) + " FROM user_checkbox_state"
            )
            for row in cur.fetchall():
                lid = row[0]
                for i, col in enumerate(cols):
                    if row[i + 1]:
                        owners[col] = lid
    return owners


def claim_times_by_slot() -> dict[int, datetime]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT slot_index, claimed_at FROM intersection_claim")
            return {int(row[0]): row[1] for row in cur.fetchall()}


def occupants_with_times() -> dict[str, dict[str, str]]:
    """各占有中スロットの表示用 {login, time(HH:MM JST)}。"""
    base = choice_held_globally()
    times = claim_times_by_slot()
    out: dict[str, dict[str, str]] = {}
    for key, lid in base.items():
        idx = int(key.replace("check", ""))
        dt = times.get(idx)
        t = format_claim_time(dt)
        out[key] = {
            "login": lid,
            "time": t if t else "--:--",
        }
    return out


def choices_display() -> list[tuple[str, str]]:
    """テンプレート用 (value, 表示ラベル)"""
    return [(f"check{i}", f"交差点{i:02d}") for i in range(1, NUM_INTERSECTIONS + 1)]


def _no_slot_available(login_id: str, state: dict[str, bool], occupants: dict[str, str]) -> bool:
    stored = selected_choice_from_state(state)
    if stored:
        return False
    return all(k in occupants for k in CHOICE_KEYS)


def _success_payload(login_id: str) -> dict:
    occupants = choice_held_globally()
    state = load_state(login_id)
    return {
        "selected_choice": selected_choice_from_state(state),
        "occupants_display": occupants_with_times(),
        "login_id": login_id,
        "no_slot_available": _no_slot_available(login_id, state, occupants),
    }


def try_save_choice(login_id: str, choice: str) -> tuple[bool, str | None, dict]:
    """
    選択を保存。成功時は JSON 用 dict（occupants_display, selected_choice 等）を返す。
    """
    if choice not in VALID_CHOICES and choice != "":
        return False, "不正な選択です。", {}

    occupants_before = choice_held_globally()

    if choice == "":
        save_state(login_id, (False,) * NUM_INTERSECTIONS)
        sync_intersection_claim(login_id, "")
        return True, None, _success_payload(login_id)

    occ = occupants_before.get(choice)
    if occ is not None and occ != login_id:
        return (
            False,
            f"その交差点は {occ} さんが使用中です。別の交差点を選ぶか、しばらく待ってください。",
            {},
        )

    flags = choice_to_flags(choice)
    try:
        save_state(login_id, flags)
    except psycopg.errors.UniqueViolation:
        occupants = choice_held_globally()
        o2 = occupants.get(choice)
        if o2 is not None and o2 != login_id:
            return (
                False,
                f"その交差点は {o2} さんが使用中です。（同時に選ばれました）",
                {},
            )
        return (
            False,
            "別のユーザーが先に選んだため保存できませんでした。画面を更新してください。",
            {},
        )

    sync_intersection_claim(login_id, choice)
    return True, None, _success_payload(login_id)


def render_main_logged_in(login_id: str, error: str | None = None):
    ensure_user_row(login_id)
    state = load_state(login_id)
    stored = selected_choice_from_state(state)
    occupants = choice_held_globally()
    occupants_display = occupants_with_times()
    selected_choice = stored
    no_slot_available = _no_slot_available(login_id, state, occupants)
    return render_template(
        "index.html",
        app_name=APP_NAME,
        choices_display=choices_display(),
        state=state,
        selected_choice=selected_choice,
        login_id=login_id,
        error=error,
        occupants=occupants,
        occupants_display=occupants_display,
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
            occupants={},
            occupants_display={},
            no_slot_available=False,
        )

    return render_main_logged_in(login_id)


@app.post("/api/save-choice")
def api_save_choice():
    login_id = session.get("login_id")
    if not login_id:
        return jsonify({"ok": False, "error": "ログインが必要です。"}), 401

    ensure_user_row(login_id)
    data = request.get_json(silent=True) or {}
    choice = (data.get("choice") or "").strip()

    ok, err, payload = try_save_choice(login_id, choice)
    if not ok:
        return jsonify({"ok": False, "error": err or "保存に失敗しました。"}), 400

    return jsonify({"ok": True, **payload})


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
            occupants={},
            occupants_display={},
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
            occupants={},
            occupants_display={},
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
