"""Painel local de recuperacao de conteineres."""
import collections
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from graphlib import CycleError, TopologicalSorter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import storage_guard as storage
from storage_probe import ProbePool
from zima_catalog import AsyncAppCatalog, presentation

VERSION = Path(__file__).with_name("VERSION").read_text().strip() if Path(__file__).with_name("VERSION").is_file() else "2.5.1"
SERVER_NAME = os.getenv("SERVER_NAME", "Servidor Docker").strip()[:80] or "Servidor Docker"
PORT = int(os.getenv("WEB_PORT", "8788"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").strip().lower() in ("true", "1", "yes")
INDEX_HTML = None  # O empacotador injeta o HTML no Compose; no ZIP ele e um arquivo.


def now():
    return time.time()


def compose_dependencies(labels):
    dependencies = []
    for value in labels.get("com.docker.compose.depends_on", "").split(","):
        parts = value.strip().split(":")
        if parts[0]:
            # A terceira parte e restart, NAO required. false nao torna o banco opcional.
            dependencies.append({"service": parts[0], "condition": parts[1] if len(parts) > 1 and parts[1] else "running_or_healthy"})
    return dependencies


def health_status(item):
    state = item.get("State")
    if isinstance(state, dict):
        return (state.get("Health") or {}).get("Status", "")
    status = item.get("Status", "")
    return "unhealthy" if "(unhealthy)" in status else "starting" if "(health: starting)" in status else "healthy" if "(healthy)" in status else ""


def dependency_order(records):
    services = collections.defaultdict(list)
    for record in records:
        if record["service"] and not record["oneoff"]:
            services[(record["stack_key"], record["service"])].append(record["id"])
    graph = {record["id"]: {cid for dependency in record["dependencies"]
             for cid in services[(record["stack_key"], dependency["service"])]} for record in records}
    sorter = TopologicalSorter(graph)
    try:
        sorter.prepare()
    except CycleError:
        # Itens fora do ciclo continuam funcionando; os demais explicam a espera.
        pass
    ids = []
    while sorter.is_active():
        group = sorter.get_ready()
        ids.extend(group)
        sorter.done(*group)
    by_id = {record["id"]: record for record in records}
    seen = set(ids)
    return [by_id[cid] for cid in ids] + [record for record in records if record["id"] not in seen]


def identity(item):
    labels = item.get("Labels") or (item.get("Config") or {}).get("Labels") or {}
    name = item.get("Name") or (item.get("Names") or [item.get("Id", "")[:12]])[0]
    name = name.lstrip("/")
    swarm = bool(labels.get("com.docker.swarm.service.id"))
    project = labels.get("com.docker.stack.namespace") if swarm else labels.get("com.docker.compose.project")
    service = labels.get("com.docker.compose.service", "")
    stack_key = ("swarm:" if swarm else "compose:") + project if project else "standalone"
    replica = labels.get("com.docker.compose.container-number", "1")
    key = json.dumps(["compose", project, service, replica], ensure_ascii=False, separators=(",", ":")) if project and service and not swarm else json.dumps(["name", name], ensure_ascii=False, separators=(",", ":"))
    blocked = ""
    if swarm:
        blocked = "Gerenciado pelo Docker Swarm"
    elif name == "auto-iniciar-containers" or str(labels.get("autostart.ignore", "false")).lower() in ("true", "1", "yes"):
        blocked = "Monitor ou contêiner excluído por configuração"
    elif str(labels.get("com.docker.compose.oneoff", "false")).lower() == "true":
        blocked = "Tarefa temporária do Compose"
    return {"name": name, "key": key, "stack": project or "Sem stack", "stack_key": stack_key, "service": service,
            "blocked": blocked, "swarm": swarm, "oneoff": str(labels.get("com.docker.compose.oneoff", "false")).lower() == "true",
            "dependencies": compose_dependencies(labels) if project and not swarm else []}


class Conflict(Exception):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "config.json"
        self.lock = threading.RLock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get("schema") != 1 or not isinstance(self.data.get("rules"), dict):
                raise ValueError("Configuração inválida em /config/config.json; restaure o arquivo antes de iniciar.")
        else:
            self.data = {
                "schema": 1, "revision": 0, "auth": None, "rules": {}, "stack_rules": {},
                "settings": {
                    "paused": False, "default_enabled": not bool(storage.ONLY),
                    "interval": storage.INTERVAL, "initial_delay": storage.INITIAL_DELAY,
                    "stable_seconds": storage.STABLE_SECONDS, "start_gap": storage.START_GAP,
                },
                "legacy_seeded": not bool(storage.ONLY or storage.IGNORE - {"auto-iniciar-containers"}),
            }
            self._write(self.data)

    def _write(self, value):
        temporary = self.directory / (".config-" + secrets.token_hex(6) + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def _commit(self, value):
        value["revision"] = self.data["revision"] + 1
        self._write(value)
        self.data = value

    def seed_legacy(self, records):
        with self.lock:
            if self.data.get("legacy_seeded") or not records:
                return
            value = copy.deepcopy(self.data)
            for record in records:
                if record["name"] in storage.IGNORE:
                    value["rules"][record["key"]] = False
                elif record["name"] in storage.ONLY:
                    value["rules"][record["key"]] = True
            value["legacy_seeded"] = True
            self._commit(value)

    @staticmethod
    def enabled(record, config):
        if record.get("blocked"):
            return False, "protected"
        if record["key"] in config["rules"]:
            return config["rules"][record["key"]], "container"
        if record["stack_key"] in config["stack_rules"]:
            return config["stack_rules"][record["stack_key"]], "stack"
        return config["settings"]["default_enabled"], "default"

    def update_rule(self, body, records):
        if type(body.get("enabled")) is not bool or body.get("scope") not in ("container", "stack"):
            raise ValueError("Escolha inválida.")
        key, scope = body.get("key"), body["scope"]
        with self.lock:
            if body.get("revision") != self.data["revision"]:
                raise Conflict("As escolhas mudaram em outra aba. Atualize e tente novamente.")
            matching = [record for record in records if record["key" if scope == "container" else "stack_key"] == key]
            if not matching or not any(not record["blocked"] for record in matching):
                raise ValueError("Contêiner ou stack indisponível para recuperação.")
            value = copy.deepcopy(self.data)
            value["rules" if scope == "container" else "stack_rules"][key] = body["enabled"]
            if scope == "stack":
                # O controle da stack aplica a escolha a TODOS e aos futuros servicos.
                project = key.removeprefix("compose:")
                for rule_key in list(value["rules"]):
                    parts = json.loads(rule_key)
                    if (parts[0] == "compose" and parts[1] == project) or (key == "standalone" and parts[0] == "name") or any(record["key"] == rule_key for record in matching):
                        value["rules"].pop(rule_key, None)
            self._commit(value)

    def update_settings(self, body):
        values = body.get("settings")
        if not isinstance(values, dict) or not values:
            raise ValueError("Configuração vazia.")
        limits = {"interval": (15, 3600), "initial_delay": (0, 3600), "stable_seconds": (0, 600), "start_gap": (0, 60)}
        for key, value in values.items():
            if key in ("paused", "default_enabled"):
                if type(value) is not bool:
                    raise ValueError("Valor inválido: " + key)
            elif key in limits:
                if type(value) is not int or not limits[key][0] <= value <= limits[key][1]:
                    raise ValueError("Tempo fora do intervalo permitido: " + key)
            else:
                raise ValueError("Configuração desconhecida.")
        with self.lock:
            if body.get("revision") != self.data["revision"]:
                raise Conflict("A configuração mudou em outra aba. Atualize e tente novamente.")
            value = copy.deepcopy(self.data)
            value["settings"].update(values)
            self._commit(value)

    def setup_password(self, password):
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            raise ValueError("Use uma senha entre 8 e 256 caracteres.")
        with self.lock:
            if self.data["auth"] is not None:
                raise Conflict("A senha já foi configurada. Entre no painel.")
            value = copy.deepcopy(self.data)
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000).hex()
            value["auth"] = {"salt": salt, "digest": digest, "iterations": 310000}
            self._commit(value)

    def verify_password(self, password):
        auth = self.snapshot()["auth"]
        if auth is None or not isinstance(password, str) or len(password) > 256:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(auth["salt"]), auth["iterations"]).hex()
        return hmac.compare_digest(actual, auth["digest"])


class Monitor:
    def __init__(self, store, docker=None, catalog=None):
        self.store = store
        self.docker = docker or storage.Docker()
        metadata_path = os.getenv("SAL0_ZIMA_APPS")
        self.catalog = catalog or AsyncAppCatalog(metadata_path or storage.HOST_ROOT + "/var/lib/casaos/apps",
                          directory_resolver=None if metadata_path else lambda: storage.HOST_ROOT + storage.resolve_host("/var/lib/casaos/apps"))
        self.apps = []
        self.catalog_status = {"available": False, "apps": 0}
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.records = []
        self.events = collections.deque(maxlen=200)
        self.ready = {}
        self.messages = {}
        self.diagnostics = {}
        self.probes = ProbePool()
        self.manual_targets = {}
        self.manual_report = {"total": 0, "started": 0, "skipped": 0, "failed": 0, "requested_at": None}
        self.started = now()
        self.last_inventory = None
        self.last_check = None
        self.deadline = time.monotonic() + store.snapshot()["settings"]["initial_delay"]
        self.heartbeat = now()
        self.error = ""
        self.scanning = False

    @property
    def next_check(self):
        return now() + max(0, self.deadline - time.monotonic())

    def request_start(self, body):
        scope, key = body.get("scope", "all"), body.get("key")
        if scope not in ("all", "selected", "stack", "container"):
            raise ValueError("Escolha de partida inválida.")
        config = self.store.snapshot()
        with self.lock:
            candidates = [record for record in self.records if not record["blocked"] and record["state"] in ("created", "exited")]
            if scope in ("stack", "container"):
                field = "stack_key" if scope == "stack" else "key"
                candidates = [record for record in candidates if record[field] == key]
            elif scope == "selected":
                candidates = [record for record in candidates if Store.enabled(record, config)[0]]
            for record in candidates:
                self.manual_targets[record["id"]] = time.monotonic() + 300
            if candidates:
                self.manual_report = {"total": len(self.manual_targets), "started": 0, "skipped": 0, "failed": 0, "requested_at": now()}
        self.event("Partida manual solicitada para " + str(len(candidates)) + " contêiner(es) parado(s).", key="manual")
        self.wake.set()

    def finish_manual(self, cid, result):
        with self.lock:
            if cid in self.manual_targets:
                self.manual_targets.pop(cid)
                self.manual_report[result] += 1

    def is_manual(self, cid):
        with self.lock:
            return cid in self.manual_targets

    def event(self, message, level="info", name="", key="global"):
        with self.lock:
            previous = self.messages.get(key)
            if previous and previous[0] == message and now() - previous[1] < 1800:
                return
            self.messages[key] = (message, now())
            self.events.appendleft({"time": now(), "message": message, "level": level, "name": name})
        storage.log(message)

    def discover(self):
        raw = self.docker.containers()
        records = []
        for item in raw:
            record = identity(item)
            record.update({"id": item["Id"], "state": item.get("State", "unknown"), "image": item.get("Image", ""),
                           "status": item.get("Status", ""), "health": health_status(item)})
            records.append(record)
        apps, catalog_status = presentation(raw, records, self.catalog)
        self.store.seed_legacy(records)
        with self.lock:
            self.records = sorted(records, key=lambda record: (record["stack"].casefold(), record["name"].casefold()))
            self.apps, self.catalog_status = apps, catalog_status
            self.last_inventory = now()
            self.error = ""
            present = {record["id"] for record in records}
            self.ready = {key: value for key, value in self.ready.items() if key in present}
            self.diagnostics = {key: value for key, value in self.diagnostics.items() if key in present}
            for cid in set(self.manual_targets) - present:
                self.finish_manual(cid, "skipped")

    def current_records(self):
        with self.lock:
            return copy.deepcopy(self.records)

    def set_diagnostic(self, record, stage=None, message=None, attempt=False, **details):
        with self.lock:
            diagnostic = self.diagnostics.setdefault(record["id"], {"attempts": 0, "last_attempt": None})
            diagnostic.update(details, checked_at=now())
            if stage is not None:
                diagnostic["stage"] = stage
            if message is not None:
                diagnostic["message"] = message
            if attempt:
                diagnostic["attempts"] += 1
                diagnostic["last_attempt"] = now()

    def storage_status(self, inspected):
        checks = {path: False for path in storage.data_sources(inspected, self.docker)}
        checks.update({path: True for path in storage.REQUIRED})
        result = self.probes.check(checks)
        return tuple(tuple(part) for part in result["signature"]), result["paths"], result["errors"]

    def dependency_status(self, record, records, inspect):
        reports, errors = [], []
        for dependency in record["dependencies"]:
            service, condition = dependency["service"], dependency["condition"]
            matches = [candidate for candidate in records if candidate["stack_key"] == record["stack_key"]
                       and candidate["service"] == service and not candidate["oneoff"]]
            if not matches:
                reports.append({"service": service, "condition": condition, "ready": False, "state": "missing", "health": ""})
                errors.append(service + " (não encontrado nesta stack)")
            for candidate in matches:
                details = inspect(candidate["id"])
                state = details.get("State") or {}
                status, health = state.get("Status", "unknown"), health_status(details)
                running = status == "running" and not state.get("Paused") and not state.get("Restarting")
                if condition == "service_healthy":
                    ready = running and health == "healthy"
                elif condition == "service_completed_successfully":
                    ready = status == "exited" and state.get("ExitCode") == 0
                elif condition == "service_started":
                    ready = running
                elif condition == "running_or_healthy":
                    ready = running and health in ("", "healthy")
                else:
                    ready = False
                enabled = Store.enabled(candidate, self.store.snapshot())[0]
                reports.append({"service": service, "name": candidate["name"], "condition": condition,
                                "state": status, "health": health, "ready": ready, "recovery_enabled": enabled})
                if not ready:
                    explanation = {"created": "ainda não iniciou", "exited": "parado", "restarting": "reiniciando",
                                   "paused": "pausado", "dead": "requer intervenção"}.get(status, status)
                    if running:
                        explanation = {"starting": "teste de saúde em andamento", "unhealthy": "teste de saúde falhou"}.get(health, "sem teste de saúde" if condition == "service_healthy" else "condição ainda não atendida")
                    if condition not in ("service_healthy", "service_completed_successfully", "service_started", "running_or_healthy"):
                        explanation = "condição desconhecida: " + condition
                    if not running and self.is_manual(candidate["id"]):
                        explanation += "; partida manual pendente"
                    elif not running and not enabled:
                        explanation += "; recuperação desativada"
                    errors.append(candidate["name"] + " (" + explanation + ")")
        return reports, errors

    def scan(self):
        with self.lock:
            self.scanning = True
            self.last_check = now()
        try:
            records = self.current_records()
            inspected_cache = {}

            def inspect(cid):
                if cid not in inspected_cache:
                    inspected_cache[cid] = self.docker.inspect(cid)
                return inspected_cache[cid]

            completed_services = {(record["stack_key"], dependency["service"]) for record in records
                                  for dependency in record["dependencies"] if dependency["condition"] == "service_completed_successfully"}
            for record in dependency_order(records):
                self.heartbeat = now()
                if self.stop.is_set():
                    break
                config = self.store.snapshot()
                enabled, _ = Store.enabled(record, config)
                with self.lock:
                    expires = self.manual_targets.get(record["id"])
                if expires is not None and expires <= time.monotonic():
                    self.finish_manual(record["id"], "failed")
                    self.set_diagnostic(record, "error", "Partida manual não concluída em 5 minutos. Veja o motivo nos detalhes e tente novamente.")
                manual = self.is_manual(record["id"])
                if record["state"] not in ("exited", "created"):
                    self.ready.pop(record["id"], None)
                    self.finish_manual(record["id"], "started" if record["state"] == "running" else "skipped")
                    continue
                if record["blocked"] or not manual and (config["settings"]["paused"] or not enabled):
                    self.ready.pop(record["id"], None)
                    continue
                try:
                    inspected = inspect(record["id"])
                    runtime = inspected.get("State") or {}
                    self.set_diagnostic(record, docker_error=runtime.get("Error", ""),
                                        exit_code=runtime.get("ExitCode"), oom_killed=bool(runtime.get("OOMKilled")))
                    if inspected.get("State", {}).get("Status") not in ("created", "exited"):
                        self.ready.pop(record["id"], None)
                        self.finish_manual(record["id"], "started" if runtime.get("Status") == "running" else "skipped")
                        continue
                    if (inspected.get("HostConfig") or {}).get("AutoRemove"):
                        self.set_diagnostic(record, "protected", "Tarefa com remoção automática; recuperação ignorada.")
                        self.finish_manual(record["id"], "skipped")
                        continue
                    if (record["stack_key"], record["service"]) in completed_services and runtime.get("Status") == "exited" and runtime.get("ExitCode") == 0:
                        self.ready.pop(record["id"], None)
                        self.set_diagnostic(record, "completed", "Tarefa já concluída com sucesso; aguardada pela stack sem executar novamente.")
                        self.finish_manual(record["id"], "skipped")
                        continue
                    self.set_diagnostic(record, "checking_storage", "Verificando armazenamento…", paths=[], dependencies=[])
                    signature, paths, errors = self.storage_status(inspected)
                    self.set_diagnostic(record, paths=paths)
                    if errors:
                        self.ready.pop(record["id"], None)
                        message = "Armazenamento indisponível: " + "; ".join(errors)
                        self.set_diagnostic(record, "waiting_disk", message)
                        self.event(record["name"] + ": " + message, "warning", record["name"], record["id"])
                        continue
                    timer = time.monotonic()
                    previous = self.ready.get(record["id"])
                    if not previous or previous[0] != signature:
                        self.ready[record["id"]] = (signature, timer)
                    dependencies, errors = self.dependency_status(record, records, inspect)
                    self.set_diagnostic(record, dependencies=dependencies)
                    if errors:
                        message = "Aguardando dependências: " + "; ".join(errors)
                        self.set_diagnostic(record, "waiting_dependency", message)
                        self.event(record["name"] + ": " + message, "warning", record["name"], record["id"])
                        continue
                    if signature and not manual and timer - self.ready[record["id"]][1] < config["settings"]["stable_seconds"]:
                        self.set_diagnostic(record, "stabilizing", "Disco disponível. Aguardando estabilidade antes de iniciar.")
                        continue
                    # Revalida as escolhas imediatamente antes de solicitar a partida.
                    latest = self.store.snapshot()
                    if not self.is_manual(record["id"]) and (latest["settings"]["paused"] or not Store.enabled(record, latest)[0]):
                        self.ready.pop(record["id"], None)
                        continue
                    self.set_diagnostic(record, "starting", "Solicitando início ao Docker…", attempt=True)
                    self.docker.start(record["id"])
                    inspected_cache.pop(record["id"], None)
                    confirmed = inspect(record["id"])
                    status = (confirmed.get("State") or {}).get("Status", "unknown")
                    with self.lock:
                        for current in self.records:
                            if current["id"] == record["id"]:
                                current.update(state=status, health=health_status(confirmed), status=status)
                    if status == "running":
                        self.ready.pop(record["id"], None)
                        self.finish_manual(record["id"], "started")
                        self.set_diagnostic(record, "running", "Início confirmado pelo Docker.")
                        self.event("Início confirmado para " + record["name"] + ".", name=record["name"], key=record["id"])
                    else:
                        state = confirmed.get("State") or {}
                        message = state.get("Error") or "Após a partida o contêiner ficou em " + status + " (saída " + str(state.get("ExitCode", "?")) + "). Nova tentativa automática."
                        self.set_diagnostic(record, "error", message)
                        self.event(record["name"] + ": " + message, "error", record["name"], record["id"])
                    if self.stop.wait(config["settings"]["start_gap"]):
                        break
                except Exception as error:
                    message = str(error)
                    self.set_diagnostic(record, "error", message)
                    self.event(record["name"] + ": " + message, "error", record["name"], record["id"])
        finally:
            with self.lock:
                self.scanning = False
                settings = self.store.snapshot()["settings"]
                delays = [settings["interval"]]
                if self.manual_targets:
                    delays.append(2)
                if any(diagnostic.get("stage") == "waiting_dependency" for diagnostic in self.diagnostics.values()):
                    delays.append(3)
                for signature, since in self.ready.values():
                    remaining = settings["stable_seconds"] - (time.monotonic() - since)
                    if signature and remaining > 0:
                        delays.append(remaining)
                self.deadline = time.monotonic() + max(.2, min(delays))
                self.heartbeat = now()

    def snapshot(self):
        config = self.store.snapshot()
        with self.lock:
            records = copy.deepcopy(self.records)
            for record in records:
                record["enabled"], record["policy_source"] = Store.enabled(record, config)
                record["diagnostic"] = copy.deepcopy(self.diagnostics.get(record["id"], {}))
                record["manual_pending"] = record["id"] in self.manual_targets
            return {
                "version": VERSION, "server_name": SERVER_NAME, "revision": config["revision"], "settings": config["settings"],
                "containers": records, "apps": copy.deepcopy(self.apps), "catalog": dict(self.catalog_status), "events": list(self.events), "error": self.error,
                "last_inventory": self.last_inventory, "last_check": self.last_check,
                "next_check": self.next_check, "scanning": self.scanning, "server_time": now(),
                "manual": dict(self.manual_report, pending=len(self.manual_targets)),
                "uptime": now() - self.started,
            }

    def run(self):
        self.event("Monitor iniciado. Preferências carregadas.")
        next_inventory = 0
        while not self.stop.is_set():
            self.heartbeat = now()
            requested = self.wake.is_set()
            self.wake.clear()
            if requested:
                next_inventory = 0
                self.deadline = time.monotonic()
            try:
                if time.monotonic() >= next_inventory:
                    self.discover()
                    next_inventory = time.monotonic() + 5
                if time.monotonic() >= self.deadline:
                    self.scan()
                    self.discover()
                    next_inventory = time.monotonic() + 5
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    self.deadline = time.monotonic() + 3
                next_inventory = time.monotonic() + 3
                self.event("Aguardando Docker: " + str(error), "error", key="daemon")
            self.wake.wait(.5)


class Sessions:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.failures = collections.defaultdict(list)

    def create(self, authenticated=False):
        with self.lock:
            self.sessions = {token: session for token, session in self.sessions.items() if session["expires"] > now()}
            token = secrets.token_urlsafe(32)
            session = {"authenticated": authenticated, "csrf": secrets.token_urlsafe(24), "expires": now() + (86400 if authenticated else 1800)}
            self.sessions[token] = session
            return token, copy.deepcopy(session)

    def get(self, token):
        with self.lock:
            session = self.sessions.get(token)
            return copy.deepcopy(session) if session and session["expires"] > now() else None

    def limited(self, ip):
        with self.lock:
            self.failures[ip] = [stamp for stamp in self.failures[ip] if stamp > now() - 60]
            return len(self.failures[ip]) >= 5


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, store, monitor):
        self.store, self.monitor, self.sessions = store, monitor, Sessions()
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Sal0AutoStart/" + VERSION

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def cookie_token(self):
        for cookie in self.headers.get("Cookie", "").split(";"):
            key, _, value = cookie.strip().partition("=")
            if key == "autostart_session":
                return value
        return ""

    def reply(self, status, body, content_type="application/json; charset=utf-8", cookie=None, nonce=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + (nonce or "unused") + "'; style-src 'unsafe-inline'; img-src 'self' data: https: http:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if cookie is not None:
            self.send_header("Set-Cookie", "autostart_session=" + cookie + "; Path=/; HttpOnly; SameSite=Strict; Max-Age=" + ("86400" if cookie else "0"))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/":
            nonce = secrets.token_urlsafe(20)
            page = INDEX_HTML if INDEX_HTML is not None else Path(__file__).with_name("index.html").read_text()
            self.reply(200, page.replace("__CSP_NONCE__", nonce), "text/html; charset=utf-8", nonce=nonce)
        elif path == "/icon.svg":
            icon = Path(__file__).with_name("assets").joinpath("icon.svg").read_text()
            self.reply(200, icon, "image/svg+xml; charset=utf-8")
        elif path == "/health":
            budget = max(300, self.server.store.snapshot()["settings"]["interval"] + 180)
            healthy = now() - self.server.monitor.heartbeat < budget
            self.reply(200 if healthy else 503, {"ok": healthy})
        elif path == "/api/session":
            session = self.server.sessions.get(self.cookie_token())
            token = None
            if session is None:
                token, session = self.server.sessions.create(authenticated=not AUTH_REQUIRED)
            self.reply(200, {"authenticated": not AUTH_REQUIRED or session["authenticated"], "auth_required": AUTH_REQUIRED,
                             "setup_required": AUTH_REQUIRED and self.server.store.snapshot()["auth"] is None,
                             "csrf": session["csrf"], "version": VERSION, "server_name": SERVER_NAME}, cookie=token)
        elif path in ("/api/state", "/api/diagnostics"):
            session = self.server.sessions.get(self.cookie_token())
            if AUTH_REQUIRED and (not session or not session["authenticated"]):
                self.reply(401, {"error": "Entre no painel para continuar."})
            else:
                self.reply(200, self.server.monitor.snapshot())
        else:
            self.reply(404, {"error": "Página não encontrada."})

    def do_POST(self):
        try:
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            parsed = urlsplit(origin) if origin else None
            if self.headers.get("Sec-Fetch-Site") == "cross-site" or (parsed and (parsed.netloc != host or parsed.scheme not in ("http", "https"))):
                self.reply(403, {"error": "Origem não permitida."})
                return
            session = self.server.sessions.get(self.cookie_token())
            if not session or not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), session["csrf"]):
                self.reply(403, {"error": "Sessão expirada. Atualize a página.", "code": "session_expired"})
                return
            if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                self.reply(415, {"error": "Envie os dados em JSON."})
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.reply(413, {"error": "Dados fora do limite permitido."})
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Dados inválidos.")
            path = urlsplit(self.path).path
            if path in ("/api/setup", "/api/login"):
                if not AUTH_REQUIRED:
                    self.reply(400, {"error": "O painel está configurado para abrir sem senha."})
                    return
                ip = self.client_address[0]
                if self.server.sessions.limited(ip):
                    self.reply(429, {"error": "Aguarde um minuto antes de tentar novamente."})
                    return
                if path == "/api/setup":
                    self.server.store.setup_password(body.get("password"))
                elif not self.server.store.verify_password(body.get("password")):
                    with self.server.sessions.lock:
                        self.server.sessions.failures[ip].append(now())
                    self.reply(401, {"error": "Senha incorreta."})
                    return
                with self.server.sessions.lock:
                    self.server.sessions.sessions.pop(self.cookie_token(), None)
                    self.server.sessions.failures.pop(ip, None)
                token, session = self.server.sessions.create(authenticated=True)
                self.reply(200, {"ok": True, "csrf": session["csrf"]}, cookie=token)
                return
            if AUTH_REQUIRED and not session["authenticated"]:
                self.reply(401, {"error": "Entre no painel para continuar."})
                return
            if path == "/api/rules":
                self.server.store.update_rule(body, self.server.monitor.current_records())
                self.server.monitor.event("Preferência de recuperação salva.", key="policy")
                self.server.monitor.wake.set()
            elif path == "/api/settings":
                self.server.store.update_settings(body)
                self.server.monitor.event("Configuração do monitor salva.", key="settings")
                self.server.monitor.wake.set()
            elif path == "/api/check":
                self.server.monitor.wake.set()
                self.server.monitor.event("Verificação solicitada pelo painel.", key="check")
            elif path == "/api/start":
                self.server.monitor.request_start(body)
            elif path == "/api/logout":
                with self.server.sessions.lock:
                    self.server.sessions.sessions.pop(self.cookie_token(), None)
                self.reply(200, {"ok": True}, cookie="")
                return
            else:
                self.reply(404, {"error": "Ação não encontrada."})
                return
            self.reply(200, self.server.monitor.snapshot())
        except Conflict as error:
            self.reply(409, {"error": str(error)})
        except (ValueError, TypeError) as error:
            self.reply(400, {"error": str(error)})
        except OSError:
            self.reply(500, {"error": "Não foi possível salvar. Verifique o armazenamento de /config."})
        except Exception:
            self.reply(500, {"error": "Falha interna. Consulte os logs do monitor."})


def main():
    store = Store(CONFIG_DIR)
    monitor = Monitor(store)
    server = PanelServer(("0.0.0.0", PORT), store, monitor)
    worker = threading.Thread(target=monitor.run, name="monitor", daemon=True)
    worker.start()

    def shutdown(*_):
        monitor.stop.set()
        monitor.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, shutdown)
    storage.log("Painel Sal0 AutoStart disponível na porta " + str(PORT) + ".")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        shutdown()
        server.server_close()
        worker.join(timeout=5)
        monitor.probes.close()


if __name__ == "__main__":
    main()
