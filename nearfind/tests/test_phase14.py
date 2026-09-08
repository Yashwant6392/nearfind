from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_script(name):
    return (ROOT / "static" / "js" / name).read_text(encoding="utf-8")


def test_polling_pauses_in_hidden_tabs_and_cleans_up():
    provider = read_script("provider-map.js")
    seeker = read_script("seeker-map.js")
    chat = read_script("chat.js")

    assert "if (fetchingQueries || document.hidden) return;" in provider
    assert "if (!document.hidden) fetchQueries();" in provider
    assert "clearInterval(pollTimer)" in provider
    assert "if (document.hidden || fetchingResponses && !options.force) return;" in seeker
    assert "if (!document.hidden) loadMessages();" in chat


def test_frontend_actions_use_safe_dom_and_shared_fetch_contract():
    scripts = [read_script(name) for name in ("main.js", "provider-map.js", "seeker-map.js", "chat.js")]
    combined = "\n".join(scripts)

    assert "innerHTML" not in combined
    assert "outerHTML" not in combined
    assert "new Function" not in combined
    assert "javascript:" not in combined
    assert "window.NearFindFetch" in combined
    assert "AbortController" in combined
    assert "SUPABASE_SERVICE_ROLE_KEY" not in combined
    assert "service_role" not in combined
