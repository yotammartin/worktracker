"""Run one read-only SQL query against the tracker database and print a table.

Usage: python3 tools/query.py "SELECT ..."
       python3 tools/query.py --settings        effective settings (defaults + overrides)

The database is opened read-only, and only plain reads are authorised: no ATTACH,
no PRAGMA writes and no extension loading. Unlike the sqlite3 shell, there are no
dot commands. Output is capped at MAX_ROWS rows.
"""
import json
import sqlite3
import sys

from common import DB_PATH, api

MAX_ROWS = 200
ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
READ_PRAGMAS = {"table_info", "table_list", "index_list", "index_info"}


def authorize(action, arg1, *_):
    if action in ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in READ_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def main(argv):
    if argv[1:] == ["--settings"]:
        print(json.dumps(api("/api/stats")["settings"], indent=2))
        return
    if len(argv) != 2:
        sys.exit(__doc__)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.set_authorizer(authorize)
    try:
        cur = conn.execute(argv[1])
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        sys.exit(f"Query failed: {e}")
    finally:
        conn.close()
    if cur.description is None:
        return
    head = [d[0] for d in cur.description]
    table = [head] + [[fmt(v) for v in r] for r in rows[:MAX_ROWS]]
    widths = [max(len(r[i]) for r in table) for i in range(len(head))]
    for n, r in enumerate(table):
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        if n == 0:
            print("  ".join("-" * w for w in widths))
    if len(rows) > MAX_ROWS:
        print(f"(first {MAX_ROWS} rows only; aggregate or add LIMIT)")


if __name__ == "__main__":
    main(sys.argv)
