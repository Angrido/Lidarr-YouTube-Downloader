with open("tests/test_routes.py", "r") as f:
    data = f.read()

# Since we don't have the exact old text, we will just insert load_config replacements.
# However, we can patch `app.load_config` for tests where we need `download_path` to be correctly mocked.

import re

def insert_config(test_name, extra_lines):
    global data
    pattern = r'(def ' + test_name + r'\([^)]*\):\n)'
    data = re.sub(pattern, r'\1' + extra_lines, data)

insert_config('test_successful_download', '        monkeypatch.setattr("app.load_config", lambda: {"download_path": str(tmp_path / "downloads"), "lidarr_path": "", "acoustid_enabled": True, "acoustid_api_key": "test-key"})\n')
insert_config('test_no_download_path_returns_400', '        monkeypatch.setattr("app.load_config", lambda: {"download_path": "", "lidarr_path": ""})\n')
insert_config('test_ytdlp_exception_sets_failed_status', '        monkeypatch.setattr("app.load_config", lambda: {"download_path": str(tmp_path / "downloads"), "lidarr_path": ""})\n')
insert_config('test_file_not_created_sets_failed_status', '        monkeypatch.setattr("app.load_config", lambda: {"download_path": str(tmp_path / "downloads"), "lidarr_path": ""})\n')

with open("tests/test_routes.py", "w") as f:
    f.write(data)
