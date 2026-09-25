"""The string catalog formats exactly, falls back to ru, and fails loudly.

Run directly with:
    python3 breaker_tests/test_strings.py
"""

import os
from collections import Counter
from string import Formatter
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from strings import COMMAND_DESCRIPTIONS, STRINGS, t  # noqa: E402


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
    source = STRINGS["ru"]
    formatter = Formatter()
    assert set(COMMAND_DESCRIPTIONS) == set(STRINGS)
    for language, translated in STRINGS.items():
        assert set(translated) == set(source), language
        assert set(COMMAND_DESCRIPTIONS[language]) == set(COMMAND_DESCRIPTIONS["ru"])
        assert all(1 <= len(description) <= 256 for description in COMMAND_DESCRIPTIONS[language].values())
        for key, original in source.items():
            fields = Counter(field for _, field, _, _ in formatter.parse(original) if field)
            translated_fields = Counter(field for _, field, _, _ in formatter.parse(translated[key]) if field)
            assert fields == translated_fields, (language, key)
            assert original.count("```") == translated[key].count("```"), (language, key)
            assert original.count("`") == translated[key].count("`"), (language, key)
            assert original.count("<details>") == translated[key].count("<details>"), (language, key)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"STILL BROKEN: {exc}")
        raise SystemExit(1)
    else:
        print("CLOSED: string catalog formats correctly, falls back to ru, and fails loudly on a missing key.")
        raise SystemExit(0)
