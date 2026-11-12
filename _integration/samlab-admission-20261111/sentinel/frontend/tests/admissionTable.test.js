import { test } from "node:test";
import assert from "node:assert/strict";
import { encodeCsv, sortedRows } from "../src/utils/admissionTable.js";

test("CSV keeps Unicode, quotes, commas and line breaks with UTF-8 BOM", () => {
  assert.equal(encodeCsv([["邮箱", 'a"b,c', "line1\nline2", null]]), '\uFEFF"邮箱","a""b,c","line1\nline2",""\r\n');
});

test("CSV neutralizes spreadsheet formulas including leading whitespace", () => {
  for (const value of ["=1+1", "+SUM(1)", "-1+1", "@SUM(1)", "  =1", "\t=1", "\r=1", "\ntext"]) {
    assert.ok(encodeCsv([[value]]).startsWith('\uFEFF"\''));
  }
  assert.equal(encodeCsv([["user@example.edu"]]), '\uFEFF"user@example.edu"\r\n');
});

test("sorting covers the whole filtered set without mutating it", () => {
  const rows = [{ username: "user10", created_at: 30 }, { username: "user2", created_at: 1 }, { username: "user1", created_at: 20 }];
  assert.deepEqual(sortedRows(rows, { prop: "username", order: "ascending" }).map(row => row.username), ["user1", "user2", "user10"]);
  assert.deepEqual(sortedRows(rows, { prop: "created_at", order: "descending" }).map(row => row.created_at), [30, 20, 1]);
  assert.equal(rows[0].username, "user10");
  assert.equal(sortedRows(rows), rows);
});
