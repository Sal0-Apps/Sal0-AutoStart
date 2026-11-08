import os
import threading
import time
from unittest.mock import patch

import app
import storage_guard as storage
from test_app import Base, Docker, item


def wait_until(callback, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callback():
            return
        time.sleep(.02)
    raise AssertionError("A recuperação automática não ocorreu dentro do prazo de teste")


class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.host = self.directory / "host"
        self.host.mkdir()
        self.mountinfo = self.directory / "mountinfo"
        self.view = self.directory / "view-mountinfo"
        device = self.host.stat().st_dev
        self.device = f"{os.major(device)}:{os.minor(device)}"
        self.root_mount = f"1 0 {self.device} / / rw - ext4 /dev/root rw\n"
        self.mountinfo.write_text(self.root_mount)
        for name, value in (("HOST_ROOT", str(self.host)), ("MOUNTINFO", str(self.mountinfo)),
                            ("VIEW_MOUNTINFO", str(self.view)), ("REQUIRED", [])):
            current = patch.object(storage, name, value)
            current.start()
            self.addCleanup(current.stop)
        # Apenas os tempos de teste sao acelerados; nenhum clique/API acorda o ciclo.
        self.store.data["settings"].update(initial_delay=.1, interval=.1, stable_seconds=0, start_gap=0)
        self.monitors = []
        self.threads = []

    def tearDown(self):
        for monitor in self.monitors:
            monitor.stop.set()
            monitor.wake.set()
        for thread in self.threads:
            thread.join(timeout=3)
        for monitor in self.monitors:
            monitor.probes.close()
        super().tearDown()

    def monitor(self, docker, background=False):
        monitor = app.Monitor(self.store, docker)
        self.monitors.append(monitor)
        monitor.discover()
        if background:
            thread = threading.Thread(target=monitor.run, daemon=True)
            self.threads.append(thread)
            thread.start()
        return monitor

    def test_boot_starts_selected_without_http_or_login_and_recovers_another_stop(self):
        docker = Docker(item("selected"), item("excluded"))
        record = app.identity(docker.items["excluded-id"])
        self.rule("container", record["key"], False, [record])
        monitor = self.monitor(docker, background=True)
        wait_until(lambda: docker.started == ["selected-id"])
        docker.items["selected-id"]["State"]["Status"] = "exited"
        wait_until(lambda: docker.started == ["selected-id", "selected-id"])
        self.assertNotIn("excluded-id", docker.started)
        self.assertIsNone(self.store.snapshot()["auth"])
        self.assertGreater(monitor.last_check, 0)

    def test_disk_appearing_later_recovers_without_a_button(self):
        (self.host / "media/HD/app").mkdir(parents=True)
        docker = Docker(item("app", source="/media/HD/app"))
        monitor = self.monitor(docker, background=True)
        wait_until(lambda: monitor.snapshot()["containers"][0]["diagnostic"].get("stage") == "waiting_disk")
        self.assertEqual(docker.started, [])
        self.mountinfo.write_text(self.root_mount + f"2 1 {self.device} / /media/HD rw - ext4 /dev/hd rw\n")
        wait_until(lambda: docker.started == ["app-id"])

    def test_wall_clock_jump_does_not_delay_automatic_boot(self):
        wall = [1000000.0]
        with patch.object(app, "now", side_effect=lambda: wall[0]):
            docker = Docker(item("app"))
            monitor = self.monitor(docker)
            wall[0] -= 86400
            thread = threading.Thread(target=monitor.run, daemon=True)
            self.threads.append(thread)
            thread.start()
            wait_until(lambda: docker.started == ["app-id"])

    def test_hung_mount_probe_does_not_hold_up_a_diskless_container(self):
        fifo = self.directory / "blocked-mountinfo"
        os.mkfifo(fifo)
        storage.MOUNTINFO = str(fifo)
        (self.host / "apps/data").mkdir(parents=True)
        docker = Docker(item("a-blocked", source="/apps/data"), item("z-ready"))
        monitor = self.monitor(docker)
        monitor.probes.timeout = .2
        start = time.monotonic()
        monitor.scan()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(docker.started, ["z-ready-id"])
        diagnostic = next(r for r in monitor.snapshot()["containers"] if r["name"] == "a-blocked")["diagnostic"]
        self.assertIn("excedeu", diagnostic["message"])
        storage.MOUNTINFO = str(self.mountinfo)
        monitor.discover()
        monitor.scan()
        self.assertEqual(docker.started, ["z-ready-id", "a-blocked-id"])

    def test_monitor_mount_namespace_is_used_instead_of_pid_one_device_ids(self):
        (self.host / "media/HD/app").mkdir(parents=True)
        self.mountinfo.write_text("1 0 99:1 / / rw - ext4 /dev/other rw\n2 1 99:2 / /media/HD rw - ext4 /dev/other-hd rw\n")
        self.view.write_text(f"10 0 {self.device} / {self.host} rw - ext4 /dev/root rw\n"
                             f"11 10 {self.device} / {self.host}/media/HD rw - ext4 /dev/hd rw\n")
        docker = Docker(item("app", source="/media/HD/app"))
        monitor = self.monitor(docker)
        monitor.scan()
        self.assertEqual(docker.started, ["app-id"])

    def test_host_mount_not_yet_visible_does_not_start_on_an_empty_folder(self):
        (self.host / "media/HD/app").mkdir(parents=True)
        self.mountinfo.write_text(self.root_mount + f"2 1 {self.device} / /media/HD rw - ext4 /dev/hd rw\n")
        self.view.write_text(f"10 0 {self.device} / {self.host} rw - ext4 /dev/root rw\n")
        docker = Docker(item("app", source="/media/HD/app"))
        monitor = self.monitor(docker)
        monitor.scan()
        self.assertEqual(docker.started, [])
        self.assertIn("visivel", monitor.snapshot()["containers"][0]["diagnostic"]["message"])

    def test_binding_the_media_collection_with_mounted_children_is_allowed(self):
        (self.host / "media/HD").mkdir(parents=True)
        self.mountinfo.write_text(self.root_mount + f"2 1 {self.device} / /media/HD rw - ext4 /dev/hd rw\n")
        docker = Docker(item("jellyfin", source="/media"))
        monitor = self.monitor(docker)
        monitor.scan()
        self.assertEqual(docker.started, ["jellyfin-id"])

    def test_remounted_disk_uses_the_top_mount_instead_of_the_hidden_old_device(self):
        (self.host / "media/HD/app").mkdir(parents=True)
        self.mountinfo.write_text(self.root_mount + "2 1 99:1 / /media/HD rw - ext4 /dev/old rw\n" +
                                  f"3 2 {self.device} / /media/HD rw - ext4 /dev/new rw\n")
        docker = Docker(item("app", source="/media/HD/app"))
        monitor = self.monitor(docker)
        monitor.scan()
        self.assertEqual(docker.started, ["app-id"])

    def test_manual_all_starts_disabled_while_paused_but_skips_protected(self):
        docker = Docker(item("enabled"), item("disabled"), item("protected", extra={"autostart.ignore": "true"}))
        self.store.data["settings"].update(paused=True, default_enabled=False)
        monitor = self.monitor(docker)
        before = self.store.snapshot()
        monitor.request_start({"scope": "all"})
        monitor.scan()
        self.assertEqual(set(docker.started), {"enabled-id", "disabled-id"})
        self.assertEqual(monitor.snapshot()["manual"]["started"], 2)
        self.assertEqual(monitor.snapshot()["manual"]["pending"], 0)
        self.assertEqual(self.store.snapshot(), before)

    def test_manual_stack_waits_for_health_then_continues_without_another_click(self):
        server = item("a-server", "stack", "app", extra={"com.docker.compose.depends_on": "db:service_healthy:false"})
        database = item("z-database", "stack", "db")
        database["State"]["Health"] = {"Status": "starting"}
        docker = Docker(server, database)
        self.store.data["settings"].update(paused=True, default_enabled=False)
        monitor = self.monitor(docker, background=True)
        monitor.request_start({"scope": "stack", "key": "compose:stack"})
        wait_until(lambda: "z-database-id" in docker.started)
        self.assertNotIn("a-server-id", docker.started)
        docker.items["z-database-id"]["State"]["Health"]["Status"] = "healthy"
        wait_until(lambda: "a-server-id" in docker.started)
        self.assertEqual(monitor.snapshot()["manual"]["pending"], 0)

    def test_immediate_exit_is_not_reported_as_a_successful_start(self):
        docker = Docker(item("app"))
        monitor = self.monitor(docker)
        monitor.request_start({"scope": "all"})

        def fails(cid):
            docker.started.append(cid)
            docker.items[cid]["State"].update(Status="exited", ExitCode=2)

        with patch.object(docker, "start", side_effect=fails):
            monitor.scan()
        state = monitor.snapshot()
        self.assertEqual(state["manual"]["started"], 0)
        self.assertEqual(state["manual"]["pending"], 1)
        self.assertIn("saída 2", state["containers"][0]["diagnostic"]["message"])
        monitor.scan()
        self.assertEqual(monitor.snapshot()["manual"]["started"], 1)
