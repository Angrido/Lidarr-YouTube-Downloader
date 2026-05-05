with open("tests/test_routes.py", "r") as f:
    data = f.read()

# Replace the lambda mock
data = data.replace('''monkeypatch.setattr("app.load_config", lambda: {
            "acoustid_enabled": True,
            "acoustid_api_key": "test-key",
            "xml_metadata_enabled": False,
            "yt_force_ipv4": False,
            "yt_player_client": "",
        })''', '''monkeypatch.setattr("app.load_config", lambda: {
            "acoustid_enabled": True,
            "acoustid_api_key": "test-key",
            "xml_metadata_enabled": False,
            "yt_force_ipv4": False,
            "yt_player_client": "",
            "download_path": dl_path,
        })''')

with open("tests/test_routes.py", "w") as f:
    f.write(data)
