export function encodeCsv(rows) {
  return "\uFEFF" + rows.map(row => row.map(value => {
    let text = String(value ?? "");
    // Quoting alone does not stop spreadsheet formula execution.
    if (/^[\s\u0000-\u001f]*[=+\-@]/u.test(text) || /^[\t\r\n]/u.test(text)) text = "'" + text;
    return `"${text.replaceAll('"', '""')}"`;
  }).join(",")).join("\r\n") + "\r\n";
}

export function sortedRows(rows, { prop, order } = {}) {
  if (!prop || !order) return rows;
  const direction = order === "descending" ? -1 : 1;
  return [...rows].sort((a, b) => {
    const left = a[prop] ?? "", right = b[prop] ?? "";
    if (prop === "account_id") {
      const leftId = /^[1-9][0-9]*$/.test(String(left)) ? BigInt(left) : null;
      const rightId = /^[1-9][0-9]*$/.test(String(right)) ? BigInt(right) : null;
      if (leftId === null || rightId === null) {
        if (leftId !== rightId) return leftId === null ? 1 : -1;
        return 0;
      }
      return direction * (leftId < rightId ? -1 : leftId > rightId ? 1 : 0);
    }
    return direction * (typeof left === "number" && typeof right === "number"
      ? left - right
      : String(left).localeCompare(String(right), "zh-CN", { numeric: true }));
  });
}
