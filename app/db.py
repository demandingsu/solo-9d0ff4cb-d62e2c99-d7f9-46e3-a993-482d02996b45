"""SQLite 持久层：登记读数、复测区间与诊断结果，按班次查询。

写路径均为“先算后写、单事务提交”：诊断先在内存中算完，再把
登记/复测与诊断结果放进同一事务落库；任一步失败整体回滚，
旧的读数与诊断原样保留。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join("data", "app.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS registrations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shift      TEXT NOT NULL,
    ports_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    segment_key     TEXT NOT NULL,
    src             TEXT NOT NULL,
    dst             TEXT NOT NULL,
    capacity        INTEGER NOT NULL,
    meter_lo        INTEGER NOT NULL,
    meter_hi        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remeasurements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    shift       TEXT NOT NULL,
    segment_key TEXT NOT NULL,
    rlo         INTEGER NOT NULL,
    rhi         INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnoses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shift           TEXT NOT NULL,
    registration_id INTEGER NOT NULL REFERENCES registrations(id),
    inputs_json     TEXT NOT NULL,
    result_json     TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reg_shift  ON registrations(shift, id);
CREATE INDEX IF NOT EXISTS idx_rem_shift  ON remeasurements(shift, id);
CREATE INDEX IF NOT EXISTS idx_diag_shift ON diagnoses(shift, id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _conn() as conn:
        conn.executescript(_SCHEMA)


# ---------------- 查询 ----------------

def latest_registration(shift: str) -> dict | None:
    with _conn() as conn:
        reg = conn.execute(
            "SELECT * FROM registrations WHERE shift=? ORDER BY id DESC LIMIT 1",
            (shift,)).fetchone()
        if reg is None:
            return None
        segs = conn.execute(
            "SELECT segment_key, src, dst, capacity, meter_lo, meter_hi "
            "FROM readings WHERE registration_id=? ORDER BY id",
            (reg["id"],)).fetchall()
    return {"id": reg["id"], "shift": reg["shift"],
            "ports": json.loads(reg["ports_json"]),
            "created_at": reg["created_at"],
            "segments": [dict(s) for s in segs]}


def remeasurements_for_shift(shift: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT segment_key, rlo, rhi, created_at FROM remeasurements "
            "WHERE shift=? ORDER BY id", (shift,)).fetchall()
    return [dict(r) for r in rows]


def diagnoses_for_shift(shift: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, shift, registration_id, inputs_json, result_json, "
            "created_at FROM diagnoses WHERE shift=? ORDER BY id DESC",
            (shift,)).fetchall()
    return [{"id": r["id"], "shift": r["shift"],
             "registration_id": r["registration_id"],
             "inputs": json.loads(r["inputs_json"]),
             "result": json.loads(r["result_json"]),
             "created_at": r["created_at"]} for r in rows]


def latest_diagnosis(shift: str) -> dict | None:
    rows = diagnoses_for_shift(shift)
    return rows[0] if rows else None


def list_shifts() -> list[str]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT shift FROM registrations "
            "GROUP BY shift ORDER BY MAX(id) DESC").fetchall()
    return [r["shift"] for r in rows]


# ---------------- 写入（单事务，失败整体回滚） ----------------

def save_registration_with_diagnosis(shift: str, ports: list[str],
                                     segments: list[dict], inputs: dict,
                                     result: dict) -> int:
    """登记一班读数并连同诊断结果落库，返回诊断 id。"""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO registrations(shift, ports_json, created_at) "
            "VALUES (?,?,?)", (shift, json.dumps(ports), _now()))
        reg_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO readings(registration_id, segment_key, src, dst, "
            "capacity, meter_lo, meter_hi) VALUES (?,?,?,?,?,?,?)",
            [(reg_id, s["key"], s["from_node"], s["to_node"], s["capacity"],
              s["meter_lo"], s["meter_hi"]) for s in segments])
        cur = conn.execute(
            "INSERT INTO diagnoses(shift, registration_id, inputs_json, "
            "result_json, created_at) VALUES (?,?,?,?,?)",
            (shift, reg_id, json.dumps(inputs, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), _now()))
        return cur.lastrowid


def save_remeasures_with_diagnosis(shift: str, remeasures: list[dict],
                                   registration_id: int, inputs: dict,
                                   result: dict) -> int:
    """补登复测区间并与新诊断同事务生效，返回诊断 id。"""
    with _conn() as conn:
        conn.executemany(
            "INSERT INTO remeasurements(shift, segment_key, rlo, rhi, "
            "created_at) VALUES (?,?,?,?,?)",
            [(shift, r["segment_key"], r["rlo"], r["rhi"], _now())
             for r in remeasures])
        cur = conn.execute(
            "INSERT INTO diagnoses(shift, registration_id, inputs_json, "
            "result_json, created_at) VALUES (?,?,?,?,?)",
            (shift, registration_id, json.dumps(inputs, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), _now()))
        return cur.lastrowid
