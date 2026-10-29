"""Manage to-dos and close or reopen the day through the running tracker.

Usage: python3 tools/todo.py list
       python3 tools/todo.py add [today|tomorrow|YYYY-MM-DD] TEXT   one item per line (default: tomorrow)
       python3 tools/todo.py done ID          tick off
       python3 tools/todo.py drop ID          close without doing it (stays visible, struck through)
       python3 tools/todo.py undo ID          reopen a done or dropped item
       python3 tools/todo.py edit ID TEXT
       python3 tools/todo.py move ID today|tomorrow|YYYY-MM-DD
       python3 tools/todo.py remove ID        delete for good (prefer drop)
       python3 tools/todo.py close            close today: later activity counts as after hours
       python3 tools/todo.py reopen

Goes through the dashboard API, so changes are validated and logged like dashboard edits.
"""
import sys

from common import api


def show(lists):
    for title, key in (("Today", "today"), ("Tomorrow and later", "tomorrow")):
        print(f"{title}:")
        for t in lists[key] or []:
            mark = "-" if t.get("dropped") else "x" if t["done_at"] else " "
            print(f"  [{mark}] #{t['id']} {t['text']}  (for {t['for_day']})")
        if not lists[key]:
            print("  (none)")


def change(action, **body):
    show(api("/api/todo", {"action": action, **body})["todos"])


def main(argv):
    args = argv[1:]
    if args == ["list"]:
        show(api("/api/stats")["todos"])
    elif args and args[0] == "add" and len(args) in (2, 3):
        change("add", text=args[-1], split=True, **{"for": args[1] if len(args) == 3 else "tomorrow"})
    elif args and args[0] in ("done", "drop", "undo", "remove") and len(args) == 2:
        change("delete" if args[0] == "remove" else args[0], id=int(args[1]))
    elif args and args[0] == "edit" and len(args) == 3:
        change("edit", id=int(args[1]), text=args[2])
    elif args and args[0] == "move" and len(args) == 3:
        change("move", id=int(args[1]), **{"for": args[2]})
    elif args in (["close"], ["reopen"]):
        res = api("/api/close", {"reopen": args[0] == "reopen"})
        print(f"Day {res['day']} {'reopened' if args[0] == 'reopen' else 'closed'}.")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
