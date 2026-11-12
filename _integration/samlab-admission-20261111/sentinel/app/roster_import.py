"""Bounded CSV roster imports: preview, atomic commit, no account creation or passwords."""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
import uuid

from .auth_store import AuthError
from .samlab_admission import EMAIL

MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
HEADERS = {
    "邮箱": "email", "email": "email", "用户名": "username", "username": "username",
    "id": "account_id", "平台id": "account_id", "平台账号id": "account_id",
    "平台账号编号": "account_id", "account_id": "account_id",
}
ACCOUNT_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
MAX_ACCOUNT_ID = 9223372036854775807


def field_error(email, username, account_id):
    if not 3 <= len(email) <= 180 or not EMAIL.fullmatch(email) or ".." in email:
        return "邮箱格式无效或超过 180 个字符。"
    if (not 1 <= len(username) <= 100 or any(ord(ch) < 32 for ch in username)
            or username.startswith(("=", "+", "-", "@"))):
        return "用户名必填，须为 1 至 100 个字符，不能含控制符或公式前缀。"
    if not ACCOUNT_ID.fullmatch(account_id) or int(account_id) > MAX_ACCOUNT_ID:
        return "平台 ID 必填，须为 1 至 9223372036854775807 的整数，不能含前导零。"
    return ""


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
        if (None in columns or len(columns) != 3
                or set(columns) != {"email", "username", "account_id"}):
            raise AuthError("表头须包含“用户名、邮箱、ID”三列（支持 username、email、id，顺序不限）。"
                            "ID 为 Samlab 平台用户 ID。白名单永久有效，不接受有效期、密码或学号列。")
        rows = []
        for values in reader:
            if not values or not any(value.strip() for value in values):
                continue
            if len(rows) >= MAX_ROWS:
                raise AuthError("每次最多导入 2000 人。")
            row = {"line": reader.line_num, "email": "", "username": "", "account_id": "", "error": ""}
            if len(values) != len(columns):
                row["error"] = "列数与表头不一致。"
            else:
                data = dict(zip(columns, (value.strip() for value in values)))
                row["email"], row["username"], row["account_id"] = data["email"].lower(), data["username"], data["account_id"]
                row["error"] = field_error(row["email"], row["username"], row["account_id"])
            rows.append(row)
    except (csv.Error, StopIteration):
        raise AuthError("CSV 文件为空或格式不正确。") from None
    if not rows:
        raise AuthError("名单没有数据行。")
    return rows


def classify(c, rows):
    seen_emails, seen_ids, output = {}, {}, []
    for row in rows:
        item = dict(row)
        email, username, account_id = item["email"], item["username"], item.get("account_id", "")
        invalid = item["error"] or field_error(email, username, account_id)
        if item.get("expires_at") is not None:
            invalid = "旧预览包含有效期，请使用永久名单模板重新预览。"
        if invalid:
            item["state"], item["message"] = "error", invalid
        elif email in seen_emails:
            if seen_emails[email] == (account_id, username):
                item["state"], item["message"] = "skip", "文件内相同人员重复，跳过重复行。"
            else:
                item["state"], item["message"] = "error", "文件内同一邮箱对应的用户名或 ID 不一致。"
        elif account_id in seen_ids:
            item["state"], item["message"] = "error", "文件内同一平台 ID 对应多个邮箱。"
        else:
            existing = c.execute("SELECT * FROM samlab_members WHERE email=?", (email,)).fetchone()
            owner = c.execute("SELECT email FROM samlab_members WHERE account_id=?", (account_id,)).fetchone()
            if owner and owner["email"].lower() != email:
                item["state"], item["message"] = "error", "该平台 ID 已关联其他邮箱，不允许覆盖。"
            elif existing and existing["account_id"] not in (None, account_id):
                item["state"], item["message"] = "error", "该邮箱已关联其他平台 ID，不允许覆盖。"
            elif existing and (existing["deleted_at"] is not None or existing["status"] != "active"):
                item["state"], item["message"] = "skip", "人员已删除或停用，保留原状态，不恢复准入。"
            elif existing and existing["account_id"] == account_id:
                item["state"], item["message"] = "skip", "邮箱和平台 ID 已记录，保留原状态和账号绑定。"
            elif existing:
                if (existing["source"] == "roster" and not c.execute(
                        "SELECT 1 FROM samlab_requests WHERE member_id=?", (existing["id"],)).fetchone()):
                    item["state"], item["message"] = "update", "为原名单补录平台 ID，不新增人员，不确认远端账号状态。"
                else:
                    item["state"], item["message"] = "error", "该邮箱已有注册或账号关联流程，不能通过 CSV 修改绑定。"
            else:
                item["state"], item["message"] = "add", "新增永久名单并记录平台 ID，不代表已核实远端账号。"
        if not invalid:
            seen_emails.setdefault(email, (account_id, username))
            seen_ids.setdefault(account_id, email)
        output.append(item)
    return output


def summary(rows):
    return {state: sum(row["state"] == state for row in rows) for state in ("add", "update", "skip", "error")}


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
            if item["state"] == "update":
                existing = c.execute("SELECT id FROM samlab_members WHERE email=?", (item["email"],)).fetchone()
                c.execute("UPDATE samlab_members SET account_id=?,updated_at=? WHERE id=?",
                          (item["account_id"], now, existing["id"]))
                store._audit(c, actor, "whitelist.id_imported", existing["id"],
                             f"CSV 批次 {batch_id}；补录平台 ID {item['account_id']}；未修改准入或确认远端账号状态。")
                continue
            if item["state"] != "add":
                continue
            member_id = str(uuid.uuid4())
            c.execute("INSERT INTO samlab_members(id,person_no,username,email,status,created_at,updated_at,"
                      "source,display_username,account_id) VALUES (?,?,?,?,'active',?,?,'roster',?,?)",
                      (member_id, f"member:{member_id}", f"roster_{uuid.uuid4().hex}", item["email"],
                       now, now, item["username"], item["account_id"]))
            store._audit(c, actor, "whitelist.imported", member_id,
                         f"CSV 批次 {batch_id}；导入平台 ID {item['account_id']}；不代表邮箱验证或账号已开通。")
        result = {"batch_id": batch_id, "summary": summary(items), "rows": items}
        store._audit(c, actor, "roster.import.committed", batch_id, json.dumps(result["summary"]))
        c.execute("UPDATE samlab_import_batches SET result_json=? WHERE id=?", (json.dumps(result), batch_id))
        return result
