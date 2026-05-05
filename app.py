from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "checkboxes.db"

app = Flask(__name__)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkbox_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                check1 INTEGER NOT NULL DEFAULT 0,
                check2 INTEGER NOT NULL DEFAULT 0,
                check3 INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO checkbox_state (id, check1, check2, check3)
            VALUES (1, 0, 0, 0)
            """
        )
        conn.commit()


def load_state() -> dict[str, bool]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT check1, check2, check3 FROM checkbox_state WHERE id = 1"
        ).fetchone()

    if row is None:
        return {"check1": False, "check2": False, "check3": False}

    return {
        "check1": bool(row["check1"]),
        "check2": bool(row["check2"]),
        "check3": bool(row["check3"]),
    }


def save_state(check1: bool, check2: bool, check3: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE checkbox_state
            SET check1 = ?, check2 = ?, check3 = ?
            WHERE id = 1
            """,
            (int(check1), int(check2), int(check3)),
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
        save_state(check1, check2, check3)
        return redirect(url_for("index"))

    state = load_state()
    return render_template("index.html", state=state)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
