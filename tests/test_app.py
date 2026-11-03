import copy
import http.client
import io
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import storage_guard as storage


def item(name, project=None, service=None, state="exited", replica="1", source=None, extra=None):
    labels = dict(extra or {})
    if project:
        labels["com.docker.compose.project"] = project
        labels["com.docker.compose.service"] = service or name
        labels["com.docker.compose.container-number"] = replica
    return {
        "Id": name + "-id", "Name": "/" + name, "Names": ["/" + name],
        "Labels": labels, "Config": {"Labels": labels},
        "State": {"Status": state}, "Image": "example/app:1", "Status": state,
        "HostConfig": {"RestartPolicy": {"Name": "no"}},
        "Mounts": [{"Type": "bind", "Source": source}] if source else [],
    }


class Docker:
    def __init__(self, *items):
        self.items = {value["Id"]: value for value in items}
        self.started, self.inspected, self.fail = [], [], set()

    def containers(self):
        return [dict(value, State=value["State"]["Status"]) for value in self.items.values()]

    def inspect(self, cid):
        self.inspected.append(cid)
        return copy.deepcopy(self.items[cid])

    def start(self, cid):
        if cid in self.fail:
            raise storage.DockerError("500: failed to start")
        self.started.append(cid)
        self.items[cid]["State"]["Status"] = "running"


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.store = app.Store(self.directory / "config")
        self.log = patch.object(storage, "log")
        self.log.start()

    def tearDown(self):
        self.log.stop()
        self.temporary.cleanup()

    def records(self, *items):
        return [app.identity(value) for value in items]

    def rule(self, scope, key, enabled, records):
        self.store.update_rule({"scope": scope, "key": key, "enabled": enabled, "revision": self.store.snapshot()["revision"]}, records)

    def settings(self, **values):
        self.store.update_settings({"revision": self.store.snapshot()["revision"], "settings": values})


class PolicyTests(Base):
    def test_discovery_groups_projects_and_standalone(self):
        docker = Docker(item("web", "site", "web"), item("db", "site", "db"), item("solo"))
        monitor = app.Monitor(self.store, docker)
        monitor.discover()
        records = monitor.snapshot()["containers"]
        self.assertEqual({r["stack"] for r in records}, {"site", "Sem stack"})
        self.assertEqual(len(records), 3)

    def test_container_choice_survives_recreated_id_and_renamed_compose_container(self):
        old, new = self.records(item("project-web-1", "project", "web"), item("new-web-name", "project", "web"))
        self.rule("container", old["key"], False, [old])
        reopened = app.Store(self.directory / "config")
        self.assertEqual(app.Store.enabled(new, reopened.snapshot()), (False, "container"))

    def test_replicas_have_separate_choices(self):
        one, two = self.records(item("web1", "project", "web", replica="1"), item("web2", "project", "web", replica="2"))
        self.rule("container", one["key"], False, [one, two])
        self.assertFalse(app.Store.enabled(one, self.store.snapshot())[0])
        self.assertTrue(app.Store.enabled(two, self.store.snapshot())[0])

    def test_individual_rule_overrides_stack_and_stack_toggle_resets_overrides(self):
        web, db = self.records(item("web", "project", "web"), item("db", "project", "db"))
        self.rule("stack", web["stack_key"], False, [web, db])
        self.rule("container", web["key"], True, [web, db])
        self.assertTrue(app.Store.enabled(web, self.store.snapshot())[0])
        self.assertFalse(app.Store.enabled(db, self.store.snapshot())[0])
        self.rule("stack", web["stack_key"], False, [web, db])
        self.assertFalse(app.Store.enabled(web, self.store.snapshot())[0])
        self.assertNotIn(web["key"], self.store.snapshot()["rules"])

    def test_stack_rule_applies_to_future_services_and_absent_overrides(self):
        old = self.records(item("old", "project", "worker"))[0]
        web, new = self.records(item("web", "project", "web"), item("new", "project", "worker"))
        self.rule("container", old["key"], True, [old])
        self.rule("stack", web["stack_key"], False, [web])
        self.assertEqual(app.Store.enabled(new, self.store.snapshot()), (False, "stack"))

    def test_default_only_applies_without_explicit_rules(self):
        web, db = self.records(item("web", "p", "web"), item("db"))
        self.rule("container", web["key"], True, [web, db])
        self.settings(default_enabled=False)
        self.assertTrue(app.Store.enabled(web, self.store.snapshot())[0])
        self.assertFalse(app.Store.enabled(db, self.store.snapshot())[0])

    def test_conflicting_revision_cannot_overwrite_choices(self):
        record = self.records(item("solo"))[0]
        revision = self.store.snapshot()["revision"]
        self.rule("container", record["key"], False, [record])
        with self.assertRaises(app.Conflict):
            self.store.update_rule({"scope": "container", "key": record["key"], "enabled": True, "revision": revision}, [record])
        self.assertFalse(app.Store.enabled(record, self.store.snapshot())[0])

    def test_invalid_or_protected_targets_cannot_be_enabled(self):
        records = self.records(item("auto-iniciar-containers"), item("task", extra={"com.docker.compose.oneoff": "True"}), item("swarm", extra={"com.docker.swarm.service.id": "123", "com.docker.stack.namespace": "s"}))
        for record in records:
            self.assertFalse(app.Store.enabled(record, self.store.snapshot())[0])
            with self.assertRaises(ValueError):
                self.rule("container", record["key"], True, records)

    def test_atomic_save_keeps_previous_file_if_replacement_fails(self):
        record = self.records(item("solo"))[0]
        before = self.store.path.read_bytes()
        with patch.object(app.os, "replace", side_effect=OSError("disk offline")):
            with self.assertRaises(OSError):
                self.rule("container", record["key"], False, [record])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertTrue(app.Store.enabled(record, self.store.snapshot())[0])
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])

    def test_invalid_settings_are_rejected_and_pause_is_persistent(self):
        for values in ({"interval": 0}, {"paused": "false"}, {"stable_seconds": True}, {"arbitrary": 1}):
            with self.assertRaises(ValueError):
                self.settings(**values)
        self.settings(paused=True, interval=90)
        restored = app.Store(self.directory / "config")
        self.assertTrue(restored.snapshot()["settings"]["paused"])
        self.assertEqual(restored.snapshot()["settings"]["interval"], 90)


class MonitorTests(Base):
    def setUp(self):
        super().setUp()
        self.settings(stable_seconds=0, start_gap=0)
        self.host = self.directory / "host"
        self.host.mkdir()
        self.mountinfo = self.directory / "mountinfo"
        device = self.host.stat().st_dev
        self.device = str(os.major(device)) + ":" + str(os.minor(device))
        self.root_mount = f"1 0 {self.device} / / rw - ext4 /dev/root rw\n"
        self.mountinfo.write_text(self.root_mount)
        self.patches = [patch.object(storage, "HOST_ROOT", str(self.host)), patch.object(storage, "MOUNTINFO", str(self.mountinfo)), patch.object(storage, "REQUIRED", [])]
        for current in self.patches:
            current.start()

    def tearDown(self):
        for current in reversed(self.patches):
            current.stop()
        super().tearDown()

    def test_only_selected_items_start_and_pause_prevents_calls(self):
        docker = Docker(item("a"), item("b"), item("running", state="running"))
        monitor = app.Monitor(self.store, docker)
        monitor.discover()
        record = next(r for r in monitor.records if r["name"] == "b")
        self.rule("container", record["key"], False, monitor.records)
        self.settings(paused=True)
        monitor.scan()
        self.assertEqual(docker.started, [])
        self.settings(paused=False)
        monitor.scan()
        self.assertEqual(docker.started, ["a-id"])
        self.assertNotIn("b-id", docker.inspected)

    def test_missing_disk_becomes_available_then_selected_container_recovers(self):
        (self.host / "media/HD/app").mkdir(parents=True)
        docker = Docker(item("app", source="/media/HD/app"))
        monitor = app.Monitor(self.store, docker)
        monitor.discover()
        monitor.scan()
        self.assertEqual(docker.started, [])
        self.assertEqual(monitor.snapshot()["containers"][0]["diagnostic"]["stage"], "waiting_disk")
        self.mountinfo.write_text(self.root_mount + f"2 1 {self.device} / /media/HD rw - ext4 /dev/hd rw\n")
        monitor.scan()
        self.assertEqual(docker.started, ["app-id"])
        monitor.discover()
        result = monitor.snapshot()["containers"][0]
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["diagnostic"]["attempts"], 1)

    def test_stability_and_recheck_of_choice_before_start(self):
        docker = Docker(item("app"))
        monitor = app.Monitor(self.store, docker)
        monitor.discover()
        self.settings(stable_seconds=20)
        with patch.object(app.time, "monotonic", return_value=100):
            monitor.scan()
        self.assertEqual(docker.started, [])
        record = monitor.records[0]
        self.rule("container", record["key"], False, monitor.records)
        with patch.object(app.time, "monotonic", return_value=150):
            monitor.scan()
        self.assertEqual(docker.started, [])

    def test_one_failed_start_does_not_block_remaining_items(self):
        docker = Docker(item("a"), item("b"))
        docker.fail.add("a-id")
        monitor = app.Monitor(self.store, docker)
        monitor.discover()
        monitor.scan()
        self.assertEqual(docker.started, ["b-id"])
        self.assertEqual(monitor.snapshot()["containers"][0]["diagnostic"]["stage"], "error")
        docker.fail.clear()
        monitor.discover()
        monitor.scan()
        self.assertEqual(docker.started, ["b-id", "a-id"])

    def test_snapshot_does_not_expose_authentication_data(self):
        self.store.setup_password("a-private-password")
        monitor = app.Monitor(self.store, Docker(item("app")))
        monitor.discover()
        payload = json.dumps(monitor.snapshot())
        self.assertNotIn("digest", payload)
        self.assertNotIn("salt", payload)
        self.assertNotIn("a-private-password", self.store.path.read_text())


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        pass


class HTTPTests(Base):
    def setUp(self):
        super().setUp()
        self.monitor = app.Monitor(self.store, Docker(item("app", "my-stack", "app")))
        self.monitor.discover()
        self.sessions = app.Sessions()
        self.server = type("FakeServer", (), {"store": self.store, "monitor": self.monitor, "sessions": self.sessions})()
        self.cookie = ""
        self.csrf = ""

    def http(self, method, path, body=None, origin="http://server:8788", csrf=True):
        data = json.dumps(body).encode() if body is not None else b""
        headers = {"Host": "server:8788", "Cookie": "autostart_session=" + self.cookie, "Connection": "close"}
        if method == "POST":
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(data)), "Origin": origin})
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        raw = (method + " " + path + " HTTP/1.0\r\n" + "".join(key + ": " + value + "\r\n" for key, value in headers.items()) + "\r\n").encode() + data
        connection = MemorySocket(raw)
        app.Handler(connection, ("127.0.0.1", 12345), self.server)
        response = http.client.HTTPResponse(type("Reply", (), {"makefile": lambda *_: io.BytesIO(connection.output)})())
        response.begin()
        content = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0].partition("=")[2]
        if response.getheader("Content-Type", "").startswith("application/json"):
            content = json.loads(content)
            if isinstance(content, dict) and "csrf" in content:
                self.csrf = content["csrf"]
        return response.status, content, response

    def setup_login(self):
        self.http("GET", "/api/session")
        status, _, _ = self.http("POST", "/api/setup", {"password": "test-password"})
        self.assertEqual(status, 200)

    def test_password_setup_authenticated_state_and_logout(self):
        self.assertEqual(self.http("GET", "/api/state")[0], 401)
        self.setup_login()
        status, body, _ = self.http("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["containers"][0]["name"], "app")
        self.assertEqual(self.http("POST", "/api/logout", {})[0], 200)
        self.assertEqual(self.http("GET", "/api/state")[0], 401)
        self.http("GET", "/api/session")
        self.assertEqual(self.http("POST", "/api/login", {"password": "wrong-password"})[0], 401)
        self.assertEqual(self.http("POST", "/api/login", {"password": "test-password"})[0], 200)

    def test_setup_requires_same_origin_and_csrf(self):
        self.http("GET", "/api/session")
        self.assertEqual(self.http("POST", "/api/setup", {"password": "test-password"}, csrf=False)[0], 403)
        self.assertEqual(self.http("POST", "/api/setup", {"password": "test-password"}, origin="http://elsewhere.test")[0], 403)
        self.assertIsNone(self.store.snapshot()["auth"])

    def test_rule_api_and_conflict_and_settings(self):
        self.setup_login()
        record = self.monitor.current_records()[0]
        body = {"scope": "container", "key": record["key"], "enabled": False, "revision": self.store.snapshot()["revision"]}
        status, result, _ = self.http("POST", "/api/rules", body)
        self.assertEqual(status, 200)
        self.assertFalse(result["containers"][0]["enabled"])
        self.assertTrue(self.monitor.wake.is_set())
        self.assertEqual(self.http("POST", "/api/rules", body)[0], 409)
        self.assertEqual(self.http("POST", "/api/settings", {"revision": result["revision"], "settings": {"paused": True}})[0], 200)
        self.assertEqual(self.http("POST", "/api/check", {})[0], 200)

    def test_login_throttling(self):
        self.setup_login()
        self.http("POST", "/api/logout", {})
        self.http("GET", "/api/session")
        for _ in range(5):
            self.assertEqual(self.http("POST", "/api/login", {"password": "wrong-password"})[0], 401)
        self.assertEqual(self.http("POST", "/api/login", {"password": "test-password"})[0], 429)

    def test_html_is_self_contained_and_nonce_matches_header(self):
        status, body, response = self.http("GET", "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__CSP_NONCE__", body)
        self.assertIn("nonce-", response.getheader("Content-Security-Policy"))
        self.assertNotIn(b'<script src="http', body)
        self.assertNotIn(b'<link href="http', body)

    def test_brand_icon_and_configurable_server_name(self):
        status, body, response = self.http("GET", "/icon.svg")
        self.assertEqual(status, 200)
        self.assertIn("image/svg+xml", response.getheader("Content-Type"))
        self.assertIn(b"<svg", body)
        _, session, _ = self.http("GET", "/api/session")
        self.assertEqual(session["server_name"], app.SERVER_NAME)


if __name__ == "__main__":
    unittest.main(verbosity=2)
