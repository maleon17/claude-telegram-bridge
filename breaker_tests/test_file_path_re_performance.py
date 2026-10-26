"""File path extraction stays fast on long paths without a known extension."""

import os
import sys
import tempfile
import time


TEST_ROOT = tempfile.mkdtemp(prefix="bridge_file_path_re_")
os.environ["TELEGRAM_BOT_TOKEN"] = "000000:FAKE-NOT-A-REAL-TOKEN-xxxxxxxxxxxxxxxxxxxxxxx"
os.environ["OWNER_ID"] = "1000000001"
os.environ["BRIDGE_STATE_FILE"] = os.path.join(TEST_ROOT, "state.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime import FILE_PATH_RE  # noqa: E402
from telegram_api import extract_existing_files  # noqa: E402


def main():
    long_path = "/segment" * 2500  # 20,000 characters, no supported extension
    started = time.perf_counter()
    assert extract_existing_files(long_path) == []
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"Path extraction took {elapsed:.3f}s"

    with tempfile.TemporaryDirectory(prefix="bridge_file_paths_") as directory:
        report = os.path.join(directory, "report.pdf")
        executable = os.path.join(directory, "real_file.exe")
        for path in (report, executable):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("test")
        assert extract_existing_files(f"Файл готов: {report}") == [report]
        assert extract_existing_files(os.path.join(directory, "nonexistent_xyz.pdf")) == []
        assert extract_existing_files(executable) == []
        assert FILE_PATH_RE.search("tmp/report.pdf") is None
        assert extract_existing_files("tmp/report.pdf") == []


if __name__ == "__main__":
    main()
    print("CLOSED: path extraction remains fast and accepts only existing absolute files with known extensions.")
