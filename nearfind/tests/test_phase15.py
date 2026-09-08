from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(name):
    return (ROOT / "static" / "js" / name).read_text(encoding="utf-8")


def test_live_location_pauses_hidden_tab_updates():
    chat = read_script("chat.js")
    assert "if (document.hidden || !sharing) return;" in chat
    assert "clearWatch" in chat
    assert "pagehide" in chat


def test_response_details_dialog_has_programmatic_label():
    template = (ROOT / "templates" / "seeker" / "responses.html").read_text(encoding="utf-8")
    assert 'aria-labelledby="responseDetailsTitle"' in template
    assert 'id="responseDetailsTitle"' in template


def test_phase15_frontend_preserves_shared_fetch_and_duplicate_guards():
    scripts = [read_script(name) for name in ("main.js", "chat.js", "provider-map.js", "seeker-map.js")]
    combined = "\n".join(scripts)
    assert "window.NearFindFetch" in combined
    assert "AbortController" in combined
    assert "let sending = false" in combined
    assert "selectingResponses.has(responseId)" in combined
    assert "resolving" in combined