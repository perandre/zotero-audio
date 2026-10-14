import pytest


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path_factory, monkeypatch):
    """Never let tests read the developer's live runtime (settings, speech.json, catalog)."""
    monkeypatch.setenv("ZOTERO_AUDIO_RUNTIME", str(tmp_path_factory.mktemp("runtime")))
    for name in ("ZOTERO_AUDIO_SPEECH_PROVIDER", "ZOTERO_AUDIO_REMOTE_SPEECH_HOST", "ZOTERO_AUDIO_REMOTE_SPEECH_COMMAND"):
        monkeypatch.delenv(name, raising=False)
