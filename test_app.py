"""Pytest suite. Every test runs against its own temporary SQLite database."""
import re

import pytest

import app as courier


@pytest.fixture()
def client(tmp_path):
    application = courier.create_app({
        "TESTING": True,
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test-secret",
        "STAFF_USERNAME": "staff",
        "STAFF_PASSWORD": "pw-for-tests",
    })
    return application.test_client()


def csrf(client, path="/book"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="_csrf" value="([0-9a-f]+)"', html).group(1)


def booking(**over):
    data = {"sender_name": "Anita Raj", "sender_phone": "9876543210",
            "receiver_name": "Karthik S", "receiver_phone": "9123456780",
            "origin": "coimbatore", "destination": "chennai", "weight_kg": "2",
            "service": "Express", "contents": "Books"}
    data.update(over)
    return data


def book_parcel(client, **over):
    data = booking(**over)
    data["_csrf"] = csrf(client)
    resp = client.post("/book", data=data)
    assert resp.status_code == 302, resp.get_data(as_text=True)
    return resp.headers["Location"].rsplit("/", 1)[1]


def login(client):
    return client.post("/staff/login", data={
        "username": "staff", "password": "pw-for-tests", "_csrf": csrf(client, "/staff/login")})


# ---------- pure functions ----------
def test_price_calculation():
    assert courier.calculate_price("Standard", 1) == 60
    assert courier.calculate_price("Express", 2) == 150
    assert courier.calculate_price("Overnight", 0.5) == 177.5


def test_estimate_delivery_uses_service_days():
    from datetime import datetime
    base = datetime(2026, 1, 1)
    assert courier.estimate_delivery("Overnight", base) == "2026-01-02"
    assert courier.estimate_delivery("Standard", base) == "2026-01-06"


def test_mask_phone():
    assert courier.mask_phone("9876543210") == "XXXXXX3210"


# ---------- public pages ----------
def test_home_and_health(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").get_json() == {"status": "ok"}


def test_booking_creates_shipment_with_first_event(client):
    tid = book_parcel(client)
    assert re.fullmatch(r"PT\d{10}", tid)
    body = client.get(f"/track?id={tid}").get_data(as_text=True)
    assert "Booked" in body and "Coimbatore" in body and "Chennai" in body


def test_booking_rejects_bad_input(client):
    for bad in ({"sender_phone": "12345"}, {"weight_kg": "99"}, {"weight_kg": "abc"},
                {"destination": "Coimbatore"}, {"service": "Teleport"}, {"receiver_name": ""}):
        data = booking(**bad)
        data["_csrf"] = csrf(client)
        assert client.post("/book", data=data).status_code == 400, bad


def test_post_without_csrf_token_is_rejected(client):
    assert client.post("/book", data=booking()).status_code == 400


def test_track_unknown_and_malformed_numbers(client):
    assert "No parcel found" in client.get("/track?id=PT0000000000").get_data(as_text=True)
    assert "start with PT" in client.get("/track?id=abc").get_data(as_text=True)


def test_tracking_is_case_insensitive_and_hides_phone(client):
    tid = book_parcel(client)
    body = client.get(f"/track?id={tid.lower()}").get_data(as_text=True)
    assert "XXXXXX6780" in body and "9123456780" not in body


def test_api_track(client):
    tid = book_parcel(client)
    data = client.get(f"/api/track/{tid}").get_json()
    assert data["status"] == "Booked" and data["events"][0]["status"] == "Booked"
    assert client.get("/api/track/PT1111111111").status_code == 404


# ---------- staff console ----------
def test_console_requires_login(client):
    resp = client.get("/staff")
    assert resp.status_code == 302 and "/staff/login" in resp.headers["Location"]


def test_login_wrong_password(client):
    resp = client.post("/staff/login", data={"username": "staff", "password": "nope",
                                             "_csrf": csrf(client, "/staff/login")})
    assert resp.status_code == 401


def test_login_blocks_open_redirect(client):
    resp = client.post("/staff/login?next=//evil.example", data={
        "username": "staff", "password": "pw-for-tests", "_csrf": csrf(client, "/staff/login")})
    assert resp.headers["Location"].endswith("/staff")


def test_staff_can_update_status_and_timeline_grows(client):
    tid = book_parcel(client)
    login(client)
    resp = client.post(f"/staff/shipment/{tid}/update", data={
        "status": "In Transit", "location": "salem hub", "note": "Left the hub",
        "_csrf": csrf(client, f"/staff/shipment/{tid}")})
    assert resp.status_code == 302
    data = client.get(f"/api/track/{tid}").get_json()
    assert data["status"] == "In Transit" and len(data["events"]) == 2
    assert data["events"][0]["location"] == "Salem Hub"


def test_delivered_parcel_is_locked(client):
    tid = book_parcel(client)
    login(client)
    token = csrf(client, f"/staff/shipment/{tid}")
    client.post(f"/staff/shipment/{tid}/update",
                data={"status": "Delivered", "location": "Chennai", "_csrf": token})
    client.post(f"/staff/shipment/{tid}/update",
                data={"status": "In Transit", "location": "Chennai", "_csrf": token})
    data = client.get(f"/api/track/{tid}").get_json()
    assert data["status"] == "Delivered" and len(data["events"]) == 2


def test_exception_keeps_previous_progress_step(client):
    tid = book_parcel(client)
    login(client)
    token = csrf(client, f"/staff/shipment/{tid}")
    for status in ("In Transit", "Exception"):
        client.post(f"/staff/shipment/{tid}/update",
                    data={"status": status, "location": "Erode", "_csrf": token})
    body = client.get(f"/track?id={tid}").get_data(as_text=True)
    assert "There is a delay" in body
    conn_events = client.get(f"/api/track/{tid}").get_json()["events"]
    assert courier.progress_step("Exception", conn_events) == 2


def test_console_search_and_filter(client):
    tid = book_parcel(client)
    login(client)
    assert tid in client.get("/staff?q=Anita").get_data(as_text=True)
    assert tid not in client.get("/staff?status=Delivered").get_data(as_text=True)
    assert tid in client.get("/staff?status=Booked").get_data(as_text=True)
