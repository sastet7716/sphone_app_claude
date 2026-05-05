from __future__ import annotations

import os
from flask import Flask, redirect, render_template, request, url_for
import psycopg


app = Flask(__name__)
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
                CREATE TABLE IF NOT EXISTS checkbox_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    check1 BOOLEAN NOT NULL DEFAULT FALSE,
                    check2 BOOLEAN NOT NULL DEFAULT FALSE,
                    check3 BOOLEAN NOT NULL DEFAULT FALSE,
                    check4 BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            # 既存環境(3項目版)からの移行用
            cur.execute(
                """
                ALTER TABLE checkbox_state
                ADD COLUMN IF NOT EXISTS check4 BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            cur.execute(
                """
                INSERT INTO checkbox_state (id, check1, check2, check3, check4)
                VALUES (1, FALSE, FALSE, FALSE, FALSE)
                ON CONFLICT (id) DO NOTHING
                """
            )
        conn.commit()


def load_state() -> dict[str, bool]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT check1, check2, check3, check4 FROM checkbox_state WHERE id = 1"
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


def save_state(check1: bool, check2: bool, check3: bool, check4: bool) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE checkbox_state
                SET check1 = %s, check2 = %s, check3 = %s, check4 = %s
                WHERE id = 1
                """,
                (check1, check2, check3, check4),
            )
        conn.commit()


# WSGIサーバー(gunicornなど)で起動されたときもDB初期化する
init_db()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        check1 = "check1" in request.form
        check2 = "check2" in request.form
        check3 = "check3" in request.form
        check4 = "check4" in request.form
        save_state(check1, check2, check3, check4)
        return redirect(url_for("index"))

    state = load_state()
    return render_template("index.html", state=state)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
