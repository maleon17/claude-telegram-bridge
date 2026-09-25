"""The string catalog formats exactly, falls back to ru, and fails loudly.

Run directly with:
    python3 breaker_tests/test_strings.py
"""

import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strings import STRINGS, t  # noqa: E402


def main():
    assert t("handlers_handle_persona_reply_2", value0="таймаут") == \
        "Не удалось прочитать файл персоны: таймаут"
    assert t("handlers_handle_command_17", value0="model-x", value1="Список") == \
        "Модель «model-x» недоступна.\n\nСписок"

    try:
        t("missing_catalog_key")
    except KeyError:
        pass
    else:
        raise AssertionError("t() must raise KeyError for a key missing even from ru")

    assert t("handlers_handle_command_4", lang="xx") == STRINGS["ru"]["handlers_handle_command_4"]


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: string catalog formats correctly, falls back to ru, and fails loudly on a missing key.")
        raise SystemExit(0)
