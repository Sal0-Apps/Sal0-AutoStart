"""Inicia conteineres parados depois que seus caminhos de armazenamento ficam disponiveis."""
import collections
import http.client
import json
import os
import posixpath
import re
import signal
import socket
import stat
import threading
import time
from urllib.parse import quote


def names(value):
    return {part.strip().lstrip("/") for part in value.split(",") if part.strip()}


STOP = threading.Event()
INTERVAL = max(15, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))
INITIAL_DELAY = max(0, int(os.getenv("INITIAL_DELAY_SECONDS", "45")))
STABLE_SECONDS = max(0, int(os.getenv("DISK_STABLE_SECONDS", "20")))
START_GAP = max(0, int(os.getenv("START_GAP_SECONDS", "3")))
HOST_ROOT = os.getenv("HOST_ROOT", "/host").rstrip("/")
MOUNTINFO = os.getenv("HOST_MOUNTINFO", "/host-mountinfo")
IGNORE = names(os.getenv("IGNORE_CONTAINERS", "")) | {"auto-iniciar-containers"}
ONLY = names(os.getenv("ONLY_CONTAINERS", ""))
REQUIRED = json.loads(os.getenv("REQUIRED_MOUNTS", "[]"))
PROTECTED = (
    "/var/lib/casaos_data/.media", "/DATA/.media", "/run/media", "/media", "/mnt", "/DATA"
)


class DockerError(Exception):
    pass


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, timeout=120):
        super().__init__("localhost", timeout=timeout)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class Docker:
    def __init__(self):
        self.prefix = ""

    def request(self, method, path, raw=False):
        connection = UnixHTTP()
        try:
            connection.request(method, ("" if raw else self.prefix) + path)
            response = connection.getresponse()
            body = response.read()
            if response.status not in (200, 201, 204, 304):
                try:
                    message = json.loads(body).get("message", "erro Docker")
                except (ValueError, AttributeError):
                    message = body.decode(errors="replace")
                raise DockerError(str(response.status) + ": " + str(message)[:600])
            return json.loads(body) if body else None
        finally:
            connection.close()

    def containers(self):
        # Usa a versao anunciada pelo proprio servidor; sem cliente/pip adicional.
        self.prefix = "/v" + self.request("GET", "/version", raw=True)["ApiVersion"]
        return self.request("GET", "/containers/json?all=1")

    def inspect(self, container_id):
        return self.request("GET", "/containers/" + quote(container_id, safe="") + "/json")

    def volume(self, volume_name):
        return self.request("GET", "/volumes/" + quote(volume_name, safe=""))

    def start(self, container_id):
        self.request("POST", "/containers/" + quote(container_id, safe="") + "/start")


def within(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def resolve_host(path):
    # Resolve links absolutos no HOST, inclusive /DATA/AppData -> /media/HD/AppData.
    # Path.resolve('/host/...') seguiria links absolutos para a raiz do conteiner.
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("caminho do host invalido")
    pending = collections.deque(path.split("/"))
    resolved = []
    hops = 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = "/" + "/".join(resolved + [part])
        try:
            mode = os.lstat(HOST_ROOT + candidate).st_mode
        except FileNotFoundError:
            mode = 0
        if stat.S_ISLNK(mode):
            hops += 1
            if hops > 40:
                raise OSError("muitos links simbolicos: " + path)
            target = os.readlink(HOST_ROOT + candidate)
            if target.startswith("/"):
                resolved.clear()
            pending.extendleft(reversed(target.split("/")))
        else:
            resolved.append(part)
    return "/" + "/".join(resolved)


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def read_mounts():
    mounts = []
    with open(MOUNTINFO, encoding="utf-8") as source:
        for line in source:
            left, right = line.rstrip("\n").split(" - ", 1)
            fields, tail = left.split(), right.split()
            mounts.append({
                "id": fields[0], "device": fields[2],
                "root": unescape_mount(fields[3]), "target": unescape_mount(fields[4]),
                "fs": tail[0], "source": unescape_mount(tail[1]),
            })
    if not any(mount["target"] == "/" for mount in mounts):
        raise OSError("tabela de montagens do host indisponivel")
    return mounts


def probe_path(source, mounts, exact_mount=False):
    path = resolve_host(source)
    candidates = [mount for mount in mounts if within(path, mount["target"])]
    owner = max(candidates, key=lambda mount: len(mount["target"]))
    if exact_mount and owner["target"] != path:
        raise OSError("aguardando ponto de montagem: " + source)
    for prefix in PROTECTED:
        if within(path, prefix):
            suffix = path[len(prefix):].strip("/")
            boundary = prefix if prefix == "/DATA" or not suffix else prefix + "/" + suffix.split("/")[0]
            if not within(owner["target"], boundary):
                raise OSError("aguardando disco montado: " + source)
            break
    status = os.stat(HOST_ROOT + path)
    device = str(os.major(status.st_dev)) + ":" + str(os.minor(status.st_dev))
    if device != owner["device"]:
        raise OSError("aguardando montagem visivel no monitor: " + source)
    return (path, owner["id"], owner["device"], owner["root"], owner["target"], owner["source"])


def data_sources(container, docker):
    sources = set()
    for mount in container.get("Mounts", []):
        source = mount.get("Source", "")
        if mount.get("Type") == "volume" and mount.get("Name"):
            volume = docker.volume(mount["Name"])
            options = volume.get("Options") or {}
            # Volumes locais que apontam para pastas de outro disco.
            if volume.get("Driver") == "local" and "bind" in options.get("o", "").split(","):
                source = options.get("device", source)
            elif volume.get("Driver") != "local" or options:
                # NFS/CIFS e plugins: a montagem e validada pelo proprio Docker.
                continue
        if source.startswith("/"):
            sources.add(source)
    host_config = container.get("HostConfig") or {}
    for mount in host_config.get("Mounts") or []:
        if mount.get("Type") == "bind" and mount.get("Source", "").startswith("/"):
            sources.add(mount["Source"])
    for bind in host_config.get("Binds") or []:
        source = bind.split(":", 1)[0]
        if source.startswith("/"):
            sources.add(source)
    return sorted(sources)


def touch():
    with open("/tmp/autostart-heartbeat", "w", encoding="ascii") as heartbeat:
        heartbeat.write(str(time.time()))


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()) + " | " + message, flush=True)


class Guard:
    def __init__(self, docker):
        self.docker = docker
        self.ready = {}
        self.messages = {}

    def report(self, key, message):
        previous = self.messages.get(key)
        if previous is None or previous[0] != message or time.monotonic() - previous[1] >= 1800:
            log(message)
            self.messages[key] = (message, time.monotonic())

    def eligible(self, container):
        name = container.get("Name", "").lstrip("/")
        labels = (container.get("Config") or {}).get("Labels") or {}
        return (
            container.get("State", {}).get("Status") in ("created", "exited")
            and name not in IGNORE and (not ONLY or name in ONLY)
            and str(labels.get("autostart.ignore", "false")).lower() not in ("true", "1", "yes")
            and str(labels.get("com.docker.compose.oneoff", "false")).lower() != "true"
            and not (container.get("HostConfig") or {}).get("AutoRemove", False)
            and not labels.get("com.docker.swarm.service.id")
        )

    def scan(self):
        containers = self.docker.containers()
        mounts = read_mounts()
        current_ids = {item["Id"] for item in containers}
        for old_id in set(self.ready) - current_ids:
            self.ready.pop(old_id, None)
        for item in sorted(containers, key=lambda candidate: (candidate.get("Names") or [candidate["Id"]])[0]):
            if STOP.is_set():
                break
            touch()
            container_id = item["Id"]
            if item.get("State") not in ("created", "exited"):
                self.ready.pop(container_id, None)
                continue
            name = (item.get("Names") or [container_id[:12]])[0].lstrip("/")
            try:
                container = self.docker.inspect(container_id)
                if not self.eligible(container):
                    self.ready.pop(container_id, None)
                    continue
                # Rele a tabela por candidato: evita usar uma montagem que acabou de sumir.
                mounts = read_mounts()
                signature = tuple(probe_path(path, mounts, exact_mount=True) for path in REQUIRED)
                signature += tuple(probe_path(path, mounts) for path in data_sources(container, self.docker))
                now = time.monotonic()
                previous = self.ready.get(container_id)
                if previous is None or previous[0] != signature:
                    self.ready[container_id] = (signature, now)
                if now - self.ready[container_id][1] < STABLE_SECONDS:
                    self.report(container_id, name + ": caminhos disponiveis; aguardando estabilidade do disco.")
                    continue
                self.docker.start(container_id)
                self.ready.pop(container_id, None)
                self.report(container_id, name + ": inicio solicitado ao Docker; verificarei no proximo ciclo.")
                touch()
                if STOP.wait(START_GAP):
                    break
            except Exception as error:
                self.ready.pop(container_id, None)
                self.report(container_id, name + ": " + str(error) + "; nova tentativa no proximo ciclo.")


def main():
    if not isinstance(REQUIRED, list) or not all(isinstance(path, str) and path.startswith("/") for path in REQUIRED):
        raise ValueError("REQUIRED_MOUNTS deve ser uma lista JSON de caminhos absolutos")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    touch()
    log("Monitor iniciado. Verificacao a cada " + str(INTERVAL) + "s; espera inicial " + str(INITIAL_DELAY) + "s.")
    if STOP.wait(INITIAL_DELAY):
        return
    guard = Guard(Docker())
    while not STOP.is_set():
        try:
            guard.scan()
        except Exception as error:
            guard.report("daemon", "Aguardando Docker/montagens: " + str(error))
        touch()
        STOP.wait(INTERVAL)


if __name__ == "__main__":
    main()
