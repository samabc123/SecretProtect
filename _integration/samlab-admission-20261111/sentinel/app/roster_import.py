"""Bounded CSV roster imports: preview, atomic commit, no account creation or passwords."""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
import unicodedata
import uuid

from .auth_store import AuthError
from .samlab_admission import EMAIL

MAX_BYTES = 1024 * 1024
MAX_ROWS = 2000
HEADERS = {
    "邮箱": "email", "email": "email", "用户名": "username", "username": "username",
    "平台账号编号": "account_id",
}
ID_HEADERS = {
    "id", "uid", "userid", "accountid", "platformid", "platformuserid", "platformaccountid", "samlabid",
    "用户id", "人员id", "成员id", "账号id", "账户id", "平台id", "平台用户id", "平台账号id",
    "平台账户id", "实验室平台id", "samlab用户id",
}
ACCOUNT_ID = re.compile(r"[1-9][0-9]{0,18}\Z")
MAX_ACCOUNT_ID = 9223372036854775807


def resolve_header(value):
    # Match known user-account identifiers, not unrelated fields such as group_id or request_id.
    normalized = re.sub(r"[\s_-]+", "", unicodedata.normalize("NFKC", value).casefold())
    return "account_id" if normalized in ID_HEADERS else HEADERS.get(normalized)


def member_field_errors(email, username, account_id):
    errors = {}
    if not 3 <= len(email) <= 180 or not EMAIL.fullmatch(email) or ".." in email:
        errors["email"] = "邮箱格式无效或超过 180 个字符。"
    if (not 1 <= len(username) <= 100 or any(ord(ch) < 32 for ch in username)
            or username.startswith(("=", "+", "-", "@"))):
        errors["username"] = "用户名必填，须为 1 至 100 个字符，不能含控制符或公式前缀。"
    if not ACCOUNT_ID.fullmatch(account_id) or int(account_id) > MAX_ACCOUNT_ID:
        errors["account_id"] = "平台 ID 必填，须为 1 至 9223372036854775807 的整数，不能含前导零。"
    return errors


def field_error(email, username, account_id):
    return next(iter(member_field_errors(email, username, account_id).values()), "")


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
        columns = [resolve_header(value) for value in header]
        if columns.count("account_id") > 1:
            raise AuthError("表头须包含“用户名、邮箱、ID”三列：检测到多个 ID 列，请仅保留一个平台用户 ID 列。")
        if (None in columns or len(columns) != 3
                or set(columns) != {"email", "username", "account_id"}):
            raise AuthError("表头须包含“用户名、邮箱、ID”三列，顺序不限。支持 username、email，"
                            "以及 id、用户id、user_id、平台ID 等写法，忽略大小写、空格、下划线和连字符。"
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
            elif existing and existing["account_id"] is None and (
                    existing["source"] != "roster" or c.execute(
                        "SELECT 1 FROM samlab_requests WHERE member_id=?", (existing["id"],)).fetchone()):
                item["state"], item["message"] = "error", "该邮箱已有注册或账号关联流程，不能通过 CSV 修改绑定。"
            elif existing and (existing["deleted_at"] is not None or existing["status"] != "active"):
                item["state"], item["message"] = "restore", "恢复原人员的永久白名单，保留账号绑定和历史审计，远端权限以平台同步为准。"
                item["member_id"], item["policy_version"] = existing["id"], existing["policy_version"]
            elif existing and existing["account_id"] == account_id:
                item["state"], item["message"] = "skip", "邮箱和平台 ID 已记录，保留原状态和账号绑定。"
            elif existing:
                item["state"], item["message"] = "update", "为原名单补录平台 ID，不新增人员，不确认远端账号状态。"
            else:
                item["state"], item["message"] = "add", "新增永久名单并记录平台 ID，不代表已核实远端账号。"
        if not invalid:
            seen_emails.setdefault(email, (account_id, username))
            seen_ids.setdefault(account_id, email)
        output.append(item)
    return output


def summary(rows):
    return {state: sum(row["state"] == state for row in rows) for state in ("add", "update", "restore", "skip", "error")}


def preview(store, raw, actor):
    return preview_rows(store, parse_csv(raw), actor)


def preview_rows(store, rows, actor):
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
                  (batch_id, actor, json.dumps(items, ensure_ascii=False), time.time() + 900, time.time()))
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
        previewed = json.loads(batch["rows_json"])
        items = classify(c, previewed)
        if any(row["state"] == "error" for row in items):
            raise AuthError("名单包含无效行，未写入任何人员，请修正文件后重新预览。", 409)
        # A preview made before deletion must not silently become permission to restore access.
        for before, current in zip(previewed, items):
            if current["state"] == "restore" and (
                    before.get("state") != "restore" or before.get("member_id") != current["member_id"]
                    or before.get("policy_version") != current["policy_version"]):
                raise AuthError("人员准入状态在预览后发生变化，请重新预览并确认恢复白名单。", 409)
        manual = all(row.get("entry_source") == "manual" for row in items)
        context = f"手动添加批次 {batch_id}" if manual else f"CSV 批次 {batch_id}"
        now = time.time()
        for item in items:
            if item["state"] == "restore":
                c.execute("UPDATE samlab_members SET status='active',deleted_at=NULL,expires_at=NULL,"
                          "account_id=?,policy_version=policy_version+1,sync_state='pending_create',updated_at=? "
                          "WHERE id=?", (item["account_id"], now, item["member_id"]))
                store._audit(c, actor, "whitelist.restored", item["member_id"],
                             f"{context}；重新添加恢复永久准入；平台 ID {item['account_id']}；"
                             "保留原人员、账号绑定和删除审计；未确认远端权限已恢复。")
                continue
            if item["state"] == "update":
                existing = c.execute("SELECT id FROM samlab_members WHERE email=?", (item["email"],)).fetchone()
                c.execute("UPDATE samlab_members SET account_id=?,updated_at=? WHERE id=?",
                          (item["account_id"], now, existing["id"]))
                store._audit(c, actor, "whitelist.id_imported", existing["id"],
                             f"{context}；补录平台 ID {item['account_id']}；未修改准入或确认远端账号状态。")
                continue
            if item["state"] != "add":
                continue
            member_id = str(uuid.uuid4())
            source = "manual" if manual else "roster"
            c.execute("INSERT INTO samlab_members(id,person_no,username,email,status,created_at,updated_at,"
                      "source,display_username,account_id) VALUES (?,?,?,?,'active',?,?,?,?,?)",
                      (member_id, f"member:{member_id}", f"roster_{uuid.uuid4().hex}", item["email"],
                       now, now, source, item["username"], item["account_id"]))
            store._audit(c, actor, "whitelist.manual_added" if manual else "whitelist.imported", member_id,
                         f"{context}；记录平台 ID {item['account_id']}；不代表邮箱验证或账号已开通。")
        result = {"batch_id": batch_id, "summary": summary(items), "rows": items}
        store._audit(c, actor, "member.manual.committed" if manual else "roster.import.committed",
                     batch_id, json.dumps(result["summary"]))
        c.execute("UPDATE samlab_import_batches SET result_json=? WHERE id=?", (json.dumps(result), batch_id))
        return result
