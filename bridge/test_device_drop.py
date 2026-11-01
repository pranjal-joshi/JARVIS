"""Unreachable wireless ADB devices are disconnected after DROP_AFTER failed checks (no real adb)."""

import device_bridge as b


def fake_adb(listing, calls):
    def adb(*args, serial=None, binary=False, timeout=b.TIMEOUT):
        calls.append(args)
        if args[:2] == ("devices", "-l"):
            return listing
        if args[0] == "shell":
            raise b.ActionError("device offline")
        return ""
    return adb


def test_offline_wireless_dropped_on_second_check(monkeypatch):
    listing = ("List of devices attached\n"
               "192.168.0.9:41234 offline\n"
               "R58M1234 offline\n"
               "adb-XYZ._adb-tls-connect._tcp unauthorized\n")
    calls = []
    monkeypatch.setattr(b, "adb", fake_adb(listing, calls))
    monkeypatch.setattr(b, "_misses", {})
    b._devices()
    assert ("disconnect", "192.168.0.9:41234") not in calls  # one miss is not enough
    b._devices()
    assert [c for c in calls if c[0] == "disconnect"] == [("disconnect", "192.168.0.9:41234")]


def test_unanswering_connected_device_dropped(monkeypatch):
    calls = []
    monkeypatch.setattr(b, "adb", fake_adb("List of devices attached\n10.0.0.5:5555 device model:Pixel\n", calls))
    monkeypatch.setattr(b, "_misses", {})
    b._devices(); b._devices()
    assert ("disconnect", "10.0.0.5:5555") in calls
