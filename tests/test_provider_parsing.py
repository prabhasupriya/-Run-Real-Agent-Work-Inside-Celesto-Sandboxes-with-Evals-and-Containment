"""Parsing of real Celesto CLI envelopes (shapes copied from observed output)."""
from agent.providers import CelestoCliProvider, _err_text, _parse_envelope

BANNER = "Celesto may send limited local usage counts (installation ID...). Opt out: celesto config telemetry off\n"
ERR = '{"ok": false, "command": "sandbox.create", "exit_code": 1, "data": null, ' \
      '"error": {"code": "missing_dependency", "message": "No such file: ssh-keygen"}}'
DEL_NOT_FOUND = '{"ok": false, "command": "sandbox.delete", "exit_code": 1, "data": {"failed": ' \
                '[{"vm_id": "sbx-x", "error": "Computer \'sbx-x\' was not found"}]}, "error": null}'


def test_banner_before_json_is_ignored():
    p = _parse_envelope(BANNER + ERR, 1)
    assert p["ok"] is False and p["command"] == "sandbox.create"
    assert _err_text(p) == "missing_dependency: No such file: ssh-keygen"


def test_non_json_falls_back():
    p = _parse_envelope("plain text", 0)
    assert p["ok"] is True and p["data"]["stdout"] == "plain text"


def test_delete_not_found_is_success(monkeypatch):
    prov = CelestoCliProvider()
    monkeypatch.setattr(prov, "_call", lambda *a, **k: _parse_envelope(DEL_NOT_FOUND, 1))
    prov.delete("sbx-x")  # must not raise


def test_scope_flag_follows_provider(monkeypatch):
    monkeypatch.setenv("CELESTO_PROVIDER", "cloud")
    assert CelestoCliProvider().scope == "--cloud"
    monkeypatch.setenv("CELESTO_PROVIDER", "local")
    assert CelestoCliProvider().scope == "--local"


def test_exec_parses_result_and_errors(monkeypatch):
    prov = CelestoCliProvider()
    ok = '{"ok": true, "data": {"stdout": "hi\\n", "stderr": "", "exit_code": 0}}'
    monkeypatch.setattr(prov, "_call", lambda *a, **k: _parse_envelope(ok, 0))
    r = prov.exec("sbx-x", "echo hi")
    assert r.ok and r.stdout == "hi\n"
    monkeypatch.setattr(prov, "_call", lambda *a, **k: _parse_envelope(ERR, 1))
    import pytest
    from agent.providers import SandboxError
    with pytest.raises(SandboxError):
        prov.exec("sbx-x", "echo hi")


def test_cloud_create_has_no_name_flag_and_maps_ids(monkeypatch):
    monkeypatch.setenv("CELESTO_PROVIDER", "cloud")
    prov = CelestoCliProvider()
    calls = []

    def fake(args, timeout=180):
        calls.append(args)
        if args[0] == "list":
            return {"ok": True, "data": {"computers": [{"id": "vm-123"}]}}
        if args[0] == "create":
            return {"ok": True, "data": {"id": "vm-123"}}
        if args[0] == "exec":
            return {"ok": True, "data": {"stdout": "hi\n", "exit_code": 0}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(prov, "_call", fake)
    prov.create("sbx-abcdef123")
    assert not any("--name" in a for a in calls if a[0] == "create")
    assert prov.exec("sbx-abcdef123", "echo hi").ok
    assert [a for a in calls if a[0] == "exec"][0][1] == "vm-123"      # real id used, not our name
    assert prov.list_names() == ["sbx-abcdef123"]                       # mapped back to our name
    prov.delete("sbx-abcdef123")
    assert [a for a in calls if a[0] == "delete"][0][1] == "vm-123"


def test_create_id_found_by_listing_diff(monkeypatch):
    monkeypatch.setenv("CELESTO_PROVIDER", "cloud")
    prov = CelestoCliProvider()
    state = {"n": 0}

    def fake(args, timeout=180):
        if args[0] == "list":
            state["n"] += 1
            return {"ok": True, "data": [] if state["n"] == 1 else [{"vm_id": "vm-9"}]}
        return {"ok": True, "data": {"message": "created"}}   # no id in create output

    monkeypatch.setattr(prov, "_call", fake)
    prov.create("sbx-diff00001")
    assert prov._real("sbx-diff00001") == "vm-9"


def test_usage_error_falls_back_to_bare_args(monkeypatch):
    monkeypatch.setenv("CELESTO_PROVIDER", "cloud")
    prov = CelestoCliProvider()
    seen = []

    def fake(args, timeout=180):
        seen.append(args)
        if "--timeout" in args:
            return {"ok": False, "error": {"code": "usage_error", "message": "--timeout unavailable"}}
        return {"ok": True, "data": {"stdout": "x", "exit_code": 0}}

    monkeypatch.setattr(prov, "_call", fake)
    assert prov.exec("vm-1", "echo x").ok and len(seen) == 2