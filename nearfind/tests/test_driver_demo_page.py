from unittest.mock import patch

import app as nearfind


def test_driver_demo_page_is_public_and_does_not_access_database():
    nearfind.app.config.update(TESTING=True)
    client = nearfind.app.test_client()

    with patch.object(nearfind, "table", side_effect=AssertionError("demo page must not query tables")), patch.object(
        nearfind, "rpc", side_effect=AssertionError("demo page must not call RPCs")
    ):
        response = client.get("/driver-demo")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Driver backend is not installed in NearFind V1" in page
    assert "Do not submit" in page
    assert "Preview seeker request form" not in page


def test_seeker_can_open_read_only_form_from_driver_demo():
    nearfind.app.config.update(TESTING=True)
    client = nearfind.app.test_client()
    with client.session_transaction() as session:
        session.update(user_id="11111111-1111-4111-8111-111111111111", role="seeker")

    response = client.get("/driver-demo")

    assert response.status_code == 200
    assert b'href="/seeker/driver-request"' in response.data
    assert b"read-only UI preview" in response.data
