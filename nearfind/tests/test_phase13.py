from unittest.mock import patch

import app as nearfind


USER_ID = "11111111-1111-4111-8111-111111111111"


def login(client, role="seeker"):
    with client.session_transaction() as session:
        session["user_id"] = USER_ID
        session["role"] = role
        session["_csrf_token"] = "known-csrf-token"


def test_missing_page_returns_safe_html_404():
    client = nearfind.app.test_client()
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.content_type.startswith("text/html")
    assert "The requested page or resource was not found." in response.get_data(as_text=True)


def test_invalid_api_request_uses_structured_error():
    client = nearfind.app.test_client()
    login(client, role="provider")
    response = client.post(
        "/user/update-location",
        json={"lat": 91, "lng": 83.3},
        headers={"X-CSRFToken": "known-csrf-token"},
    )

    assert response.status_code == 400
    assert response.json["success"] is False
    assert set(response.json["error"]) == {"code", "message"}
    assert response.json["error"]["code"] == "bad_request"


def test_unauthorized_api_request_is_structured_and_does_not_redirect():
    client = nearfind.app.test_client()
    response = client.get("/notifications")

    assert response.status_code == 401
    assert response.json == {
        "success": False,
        "error": {
            "code": "authentication_required",
            "message": "Your session has expired. Please log in again.",
        },
    }
    assert not response.location


def test_unexpected_error_does_not_leak_internal_details():
    client = nearfind.app.test_client()
    login(client)
    with patch.object(nearfind, "table", side_effect=RuntimeError("secret database token")):
        response = client.get("/notifications")

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert response.json["error"]["code"] == "internal_error"
    assert "secret database token" not in body
    assert "Traceback" not in body


def test_frontend_contract_has_timeout_session_expiry_and_duplicate_guards():
    root = nearfind.__file__.replace("app.py", "")
    with open(f"{root}static/js/main.js", encoding="utf-8") as script_file:
        main_script = script_file.read()
    with open(f"{root}static/js/chat.js", encoding="utf-8") as script_file:
        chat_script = script_file.read()
    with open(f"{root}static/js/seeker-map.js", encoding="utf-8") as script_file:
        seeker_script = script_file.read()
    with open(f"{root}static/js/provider-map.js", encoding="utf-8") as script_file:
        provider_script = script_file.read()

    assert "AbortController" in main_script
    assert "Your session has expired. Please log in again." in main_script
    assert "let sending = false" in chat_script
    assert "finally" in chat_script
    assert "setInterval(() =>" in seeker_script
    assert "}, 3000)" in seeker_script
    assert "fetchingResponses = false" in seeker_script
    assert "if (document.hidden || fetchingResponses && !options.force) return;" in seeker_script
    assert "if (fetchingQueries || document.hidden) return;" in provider_script
    assert "clearInterval(pollTimer)" in provider_script
