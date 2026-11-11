"""Bounded CSV roster imports: preview, atomic commit, no account creation or passwords."""

from __future__ import annotations

import csv
import io
import json
import secrets
import time
import uuid

from .auth_store import AuthError
from .samlab_admission import EMAIL

MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
HEADERS = {"邮箱": "email", "email": "email", "用户名": "username", "username": "username"}


def initialize(c):
    c.execute("""CREATE TABLE IF NOT EXISTS samlab_import_batches (
        id TEXT PRIMARY KEY, actor TEXT NOT NULL, rows_json TEXT NOT NULL,
        expires_at REAL NOT NULL, result_json TEXT, created_at REAL NOT NULL)""")


def parse_csv(raw: bytes):
    if len(raw) > MAX_BYTES:
        raise AuthError("名单文件不能超过 1 MiB。", 413)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            raise AuthError("请上传 UTF-8 或 GB18030 编码的 CSV 文件。") from None
    if "\x00" in text:
        raise AuthError("请上传 CSV 文本，不支持 Excel 工作簿或二进制文件。")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader)
        columns = [HEADERS.get(value.strip().lower()) for value in header]
        if not columns or None in columns or len(set(columns)) != len(columns) or "email" not in columns:
            raise AuthError("表头必须包含“邮箱”；仅支持“邮箱、用户名”。白名单永久有效，不接受有效期、密码或学号列。")
        rows = []
        for values in reader:
            if not values or not any(value.strip() for value in values):
                continue
            if len(rows) >= MAX_ROWS:
                raise AuthError("每次最多导入 2000 人。")
            row = {"line": reader.line_num, "email": "", "username": "", "error": ""}
            if len(values) != len(columns):
                row["error"] = "列数与表头不一致。"
            else:
                data = dict(zip(columns, (value.strip() for value in values)))
                row["email"], row["username"] = data["email"].lower(), data.get("username", "")
                if not 3 <= len(row["email"]) <= 180 or not EMAIL.fullmatch(row["email"]) or ".." in row["email"]:
                    row["error"] = "邮箱格式无效或超过 180 个字符。"
                elif len(row["username"]) > 100 or any(ord(ch) < 32 for ch in row["username"]) or row["username"].startswith(("=", "+", "-", "@")):
                    row["error"] = "用户名须在 100 个字符以内，不能含控制符或公式前缀。"
            rows.append(row)
    except (csv.Error, StopIteration):
        raise AuthError("CSV 文件为空或格式不正确。") from None
    if not rows:
        raise AuthError("名单没有数据行。")
    return rows


def classify(c, rows):
    seen, output = set(), []
    for row in rows:
        item = dict(row)
        email = item["email"]
        if item["error"]:
            item["state"], item["message"] = "error", item["error"]
        elif item.get("expires_at") is not None:
            item["state"], item["message"] = "error", "旧预览包含有效期，请使用永久名单模板重新预览。"
        elif email in seen:
            item["state"], item["message"] = "skip", "文件内邮箱重复。"
        elif c.execute("SELECT 1 FROM samlab_members WHERE email=?", (email,)).fetchone():
            item["state"], item["message"] = "skip", "邮箱已有白名单记录，保留原状态和账号绑定。"
        else:
            item["state"], item["message"] = "add", "新增名单；已有申请仍需管理员确认。"
        seen.add(email)
        output.append(item)
    return output


def summary(rows):
    return {state: sum(row["state"] == state for row in rows) for state in ("add", "skip", "error")}


def preview(store, raw, actor):
    rows = parse_csv(raw)
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        initialize(c)
        c.execute("DELETE FROM samlab_import_batches WHERE expires_at<?", (time.time(),))
        pending = c.execute("SELECT count(*) FROM samlab_import_batches WHERE actor=?", (actor,)).fetchone()[0]
        if pending >= 20:
            raise AuthError("待确认批次过多，请稍后重试。", 429)
        batch_id = secrets.token_urlsafe(24)
        items = classify(c, rows)
        c.execute("INSERT INTO samlab_import_batches VALUES (?,?,?,?,NULL,?)",
                  (batch_id, actor, json.dumps(rows, ensure_ascii=False), time.time() + 900, time.time()))
        return {"batch_id": batch_id, "rows": items, "summary": summary(items), "expires_in": 900}


def commit(store, batch_id, actor):
    with store.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        initialize(c)
        batch = c.execute("SELECT * FROM samlab_import_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["actor"] != actor:
            raise AuthError("导入批次不存在。", 404)
        if batch["expires_at"] <= time.time():
            raise AuthError("预览已过期，请重新选择文件。", 409)
        if batch["result_json"]:
            return json.loads(batch["result_json"])
        items = classify(c, json.loads(batch["rows_json"]))
        if any(row["state"] == "error" for row in items):
            raise AuthError("名单包含无效行，未写入任何人员，请修正文件后重新预览。", 409)
        now = time.time()
        for item in items:
            if item["state"] != "add":
                continue
            member_id = str(uuid.uuid4())
            c.execute("INSERT INTO samlab_members(id,person_no,username,email,status,created_at,updated_at,"
                      "source,display_username) VALUES (?,?,?,?,'active',?,?,'roster',?)",
                      (member_id, f"member:{member_id}", f"roster_{uuid.uuid4().hex}", item["email"],
                       now, now, item["username"] or None))
            store._audit(c, actor, "whitelist.imported", member_id, f"CSV 批次 {batch_id}；不代表邮箱验证或账号已开通。")
        result = {"batch_id": batch_id, "summary": summary(items), "rows": items}
        store._audit(c, actor, "roster.import.committed", batch_id, json.dumps(result["summary"]))
        c.execute("UPDATE samlab_import_batches SET result_json=? WHERE id=?", (json.dumps(result), batch_id))
        return result
