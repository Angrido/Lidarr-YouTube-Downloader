with open("tests/test_routes.py", "r") as f:
    data = f.read()
data = data.replace('monkeypatch.setattr("app.DOWNLOAD_DIR", dl_path)', '')
data = data.replace('monkeypatch.setattr("app.DOWNLOAD_DIR", "")', '')
data = data.replace('"lidarr_api_key": "test_key",', '"download_path": dl_path,\n            "lidarr_api_key": "test_key",')
# The empty download_path one we need to be careful
with open("tests/test_routes.py", "w") as f:
    f.write(data)
