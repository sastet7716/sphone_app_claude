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

# マルチテナント（最小: デプロイごとに1テナントを環境変数で指定）
TENANT_SLUG = os.environ.get("TENANT_SLUG", "default")
TENANT_NAME = os.environ.get("TENANT_NAME", "デフォルト組織")
TENANT_APP_TITLE = os.environ.get("TENANT_APP_TITLE", "資源予約")
DEFAULT_SLOT_COUNT = int(os.environ.get("DEFAULT_SLOT_COUNT", "20"))
DEFAULT_SLOT_LABEL_PREFIX = os.environ.get("DEFAULT_SLOT_LABEL_PREFIX", "スロット")

JST = ZoneInfo("Asia/Tokyo")


def format_claim_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(JST).strftime("%H:%M")


def get_connection() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL)


def init_db() -> None:
    """tenant / slot / slot_holder。旧 check 列モデルは削除（データ非移行）。"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS intersection_claim CASCADE")
            cur.execute("DROP TABLE IF EXISTS user_checkbox_state CASCADE")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenant (
                    id SERIAL PRIMARY KEY,
                    slug TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    app_title TEXT NOT NULL DEFAULT '資源予約'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS slot (
                    id SERIAL PRIMARY KEY,
                    tenant_id INTEGER NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
                    sort_order INTEGER NOT NULL,
                    label TEXT NOT NULL,
                    UNIQUE (tenant_id, sort_order)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS slot_holder (
                    slot_id INTEGER PRIMARY KEY REFERENCES slot(id) ON DELETE CASCADE,
                    tenant_id INTEGER NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
                    login_id TEXT NOT NULL,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS slot_holder_one_user_per_tenant
                ON slot_holder (tenant_id, login_id)
                """
            )
            cur.execute(
                """
                INSERT INTO tenant (slug, name, app_title)
                VALUES (%s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    app_title = EXCLUDED.app_title
                """,
                (TENANT_SLUG, TENANT_NAME, TENANT_APP_TITLE),
            )
            cur.execute("SELECT id FROM tenant WHERE slug = %s", (TENANT_SLUG,))
            tenant_row = cur.fetchone()
            assert tenant_row is not None
            tenant_id = tenant_row[0]

            cur.execute(
                "SELECT COUNT(*) FROM slot WHERE tenant_id = %s",
                (tenant_id,),
            )
            nslots = cur.fetchone()[0]
            if nslots == 0:
                for i in range(1, DEFAULT_SLOT_COUNT + 1):
                    label = f"{DEFAULT_SLOT_LABEL_PREFIX}{i:02d}"
                    cur.execute(
                        """
                        INSERT INTO slot (tenant_id, sort_order, label)
                        VALUES (%s, %s, %s)
                        """,
                        (tenant_id, i, label),
                    )
        conn.commit()


def get_tenant_id() -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tenant WHERE slug = %s", (TENANT_SLUG,))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"テナント slug={TENANT_SLUG!r} が見つかりません。")
            return int(row[0])


def list_slots(tenant_id: int) -> list[dict[str, int | str]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, label FROM slot
                WHERE tenant_id = %s
                ORDER BY sort_order
                """,
                (tenant_id,),
            )
            return [{"id": int(r[0]), "label": str(r[1])} for r in cur.fetchall()]


def slot_belongs_to_tenant(tenant_id: int, slot_id: int) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM slot WHERE id = %s AND tenant_id = %s",
                (slot_id, tenant_id),
            )
            return cur.fetchone() is not None


def holders_raw(tenant_id: int) -> dict[int, tuple[str, datetime]]:
    """slot_id -> (login_id, claimed_at)"""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.slot_id, h.login_id, h.claimed_at
                FROM slot_holder h
                WHERE h.tenant_id = %s
                """,
                (tenant_id,),
            )
            return {
                int(r[0]): (str(r[1]), r[2])
                for r in cur.fetchall()
            }


def user_current_slot_id(tenant_id: int, login_id: str) -> int | None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT slot_id FROM slot_holder
                WHERE tenant_id = %s AND login_id = %s
                """,
                (tenant_id, login_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None


def occupants_display(tenant_id: int) -> dict[str, dict[str, str]]:
    """API/画面用: slot_id 文字列 -> {login, time}"""
    h = holders_raw(tenant_id)
    out: dict[str, dict[str, str]] = {}
    for sid, (lid, claimed_at) in h.items():
        t = format_claim_time(claimed_at)
        out[str(sid)] = {
            "login": lid,
            "time": t if t else "--:--",
        }
    return out


def holders_login_by_slot(tenant_id: int) -> dict[int, str]:
    return {sid: pair[0] for sid, pair in holders_raw(tenant_id).items()}


def _no_slot_available(
    tenant_id: int, login_id: str, slots: list[dict], holders: dict[int, str]
) -> bool:
    if user_current_slot_id(tenant_id, login_id) is not None:
        return False
    if not slots:
        return True
    return len(holders) >= len(slots)


def clear_holder(tenant_id: int, login_id: str) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM slot_holder WHERE tenant_id = %s AND login_id = %s",
                (tenant_id, login_id),
            )
        conn.commit()


def assign_holder(tenant_id: int, login_id: str, slot_id: int) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM slot_holder WHERE tenant_id = %s AND login_id = %s",
                (tenant_id, login_id),
            )
            cur.execute(
                """
                INSERT INTO slot_holder (slot_id, tenant_id, login_id, claimed_at)
                VALUES (%s, %s, %s, NOW())
                """,
                (slot_id, tenant_id, login_id),
            )
        conn.commit()


def try_save_choice(tenant_id: int, login_id: str, choice: str) -> tuple[bool, str | None, dict]:
    slots = list_slots(tenant_id)
    slot_ids = {s["id"] for s in slots}

    if choice != "":
        try:
            slot_id = int(choice)
        except ValueError:
            return False, "不正な選択です。", {}
        if slot_id not in slot_ids:
            return False, "不正な選択です。", {}

    if choice == "":
        clear_holder(tenant_id, login_id)
        holders = holders_login_by_slot(tenant_id)
        return True, None, _success_payload(tenant_id, login_id, slots, holders)

    slot_id = int(choice)
    holders = holders_login_by_slot(tenant_id)
    occupant = holders.get(slot_id)
    if occupant is not None and occupant != login_id:
        return (
            False,
            f"この枠は {occupant} さんが使用中です。別の枠を選ぶか、しばらく待ってください。",
            {},
        )

    try:
        assign_holder(tenant_id, login_id, slot_id)
    except psycopg.errors.UniqueViolation:
        holders = holders_login_by_slot(tenant_id)
        o2 = holders.get(slot_id)
        if o2 is not None and o2 != login_id:
            return (
                False,
                f"この枠は {o2} さんが使用中です。（同時に選ばれました）",
                {},
            )
        return (
            False,
            "別のユーザーが先に選んだため保存できませんでした。画面を更新してください。",
            {},
        )

    holders = holders_login_by_slot(tenant_id)
    return True, None, _success_payload(tenant_id, login_id, slots, holders)


def _success_payload(
    tenant_id: int,
    login_id: str,
    slots: list[dict],
    holders: dict[int, str],
) -> dict:
    cur_slot = user_current_slot_id(tenant_id, login_id)
    return {
        "selected_choice": str(cur_slot) if cur_slot is not None else "",
        "occupants_display": occupants_display(tenant_id),
        "login_id": login_id,
        "no_slot_available": _no_slot_available(tenant_id, login_id, slots, holders),
        "slot_count": len(slots),
    }


def render_main_logged_in(login_id: str, error: str | None = None):
    tenant_id = get_tenant_id()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT app_title FROM tenant WHERE id = %s",
                (tenant_id,),
            )
            row = cur.fetchone()
    app_title = str(row[0]) if row else TENANT_APP_TITLE

    slots = list_slots(tenant_id)
    holders = holders_login_by_slot(tenant_id)
    cur_slot = user_current_slot_id(tenant_id, login_id)
    selected_choice = str(cur_slot) if cur_slot is not None else ""

    return render_template(
        "index.html",
        app_name=app_title,
        slots=slots,
        selected_choice=selected_choice,
        login_id=login_id,
        error=error,
        occupants_display=occupants_display(tenant_id),
        no_slot_available=_no_slot_available(tenant_id, login_id, slots, holders),
        slot_count=len(slots),
    )


init_db()


@app.route("/", methods=["GET"])
def index():
    login_id = session.get("login_id")
    if not login_id:
        app_title = TENANT_APP_TITLE
        try:
            tid = get_tenant_id()
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT app_title FROM tenant WHERE id = %s",
                        (tid,),
                    )
                    r = cur.fetchone()
                    if r:
                        app_title = str(r[0])
        except Exception:
            pass
        return render_template(
            "index.html",
            app_name=app_title,
            slots=[],
            selected_choice="",
            login_id=None,
            error=None,
            occupants_display={},
            no_slot_available=False,
            slot_count=0,
        )

    return render_main_logged_in(login_id)


@app.post("/api/save-choice")
def api_save_choice():
    login_id = session.get("login_id")
    if not login_id:
        return jsonify({"ok": False, "error": "ログインが必要です。"}), 401

    tenant_id = get_tenant_id()
    data = request.get_json(silent=True) or {}
    choice = (data.get("choice") or "").strip()

    ok, err, payload = try_save_choice(tenant_id, login_id, choice)
    if not ok:
        return jsonify({"ok": False, "error": err or "保存に失敗しました。"}), 400

    return jsonify({"ok": True, **payload})


def read_users_from_csv() -> dict[str, str]:
    users: dict[str, str] = {}
    if not USERS_CSV_PATH.exists():
        return users
    with USERS_CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lid = (row.get("login_id") or "").strip()
            pw = (row.get("password") or "").strip()
            if lid and pw:
                users[lid] = pw
    return users


def is_valid_user(login_id: str, password: str) -> bool:
    return read_users_from_csv().get(login_id) == password


@app.route("/login", methods=["POST"])
def login():
    login_id = (request.form.get("login_id") or "").strip()
    password = (request.form.get("password") or "").strip()

    app_title = TENANT_APP_TITLE
    try:
        tid = get_tenant_id()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT app_title FROM tenant WHERE id = %s", (tid,))
                r = cur.fetchone()
                if r:
                    app_title = str(r[0])
    except Exception:
        pass

    if not login_id or not password:
        return render_template(
            "index.html",
            app_name=app_title,
            slots=[],
            selected_choice="",
            login_id=None,
            error="ログインIDとパスワードを入力してください。",
            occupants_display={},
            no_slot_available=False,
            slot_count=0,
        )

    if not is_valid_user(login_id, password):
        return render_template(
            "index.html",
            app_name=app_title,
            slots=[],
            selected_choice="",
            login_id=None,
            error="認証に失敗しました。IDまたはパスワードを確認してください。",
            occupants_display={},
            no_slot_available=False,
            slot_count=0,
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
