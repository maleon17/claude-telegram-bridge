"""No function may assign a local named `t` while also calling `t(...)`.

Live regression 2026-09-25: `_chat_reader_loop` used `t` as a local variable
for the stream-json event's ``type`` field, which shadowed the module-level
`from strings import t` translate helper for the whole function body (Python
has no block scoping) -- every `t('some_key', ...)` call in that function
then raised `TypeError: 'str' object is not callable`, crashing the reader
thread on the very first error-result or generic-result tool card and making
every turn after it look like an unexplained "Ход прерван" interruption.
"""

import ast
import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = (
    "bridge.py", "handlers.py", "chat_process.py", "runtime.py",
    "state_store.py", "telegram_api.py",
)


def _shadowed_functions(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        assigns_t = any(
            (isinstance(n, ast.Name) and n.id == "t" and isinstance(n.ctx, ast.Store))
            or (isinstance(n, ast.arg) and n.arg == "t")
            for n in ast.walk(node)
        )
        calls_t = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "t"
            for n in ast.walk(node)
        )
        if assigns_t and calls_t:
            hits.append((node.name, node.lineno))
    return hits


def main():
    offenders = []
    for filename in FILES:
        path = os.path.join(ROOT, filename)
        if not os.path.exists(path):
            continue
        for name, lineno in _shadowed_functions(path):
            offenders.append(f"{filename}:{lineno} {name}()")
    assert not offenders, "functions shadowing the strings.t() helper: " + ", ".join(offenders)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: no function shadows the strings.t() translate helper with a local `t`.")
        raise SystemExit(0)
