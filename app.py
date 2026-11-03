"""Painel local de recuperacao de conteineres. Sem dependencias de terceiros."""
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import storage_guard as storage

VERSION = Path(__file__).with_name("VERSION").read_text().strip() if Path(__file__).with_name("VERSION").is_file() else "2.1.0"
SERVER_NAME = os.getenv("SERVER_NAME", "Servidor Docker").strip()[:80] or "Servidor Docker"
PORT = int(os.getenv("WEB_PORT", "8788"))
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
INDEX_HTML = None  # O empacotador injeta o HTML no Compose; no ZIP ele e um arquivo.


def now():
    return time.time()


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
    return {"name": name, "key": key, "stack": project or "Sem stack", "stack_key": stack_key, "service": service, "blocked": blocked, "swarm": swarm}


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
    def __init__(self, store, docker=None):
        self.store = store
        self.docker = docker or storage.Docker()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.records = []
        self.events = collections.deque(maxlen=200)
        self.ready = {}
        self.messages = {}
        self.diagnostics = {}
        self.started = now()
        self.last_inventory = None
        self.last_check = None
        self.next_check = now() + store.snapshot()["settings"]["initial_delay"]
        self.heartbeat = now()
        self.error = ""
        self.scanning = False

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
            record.update({"id": item["Id"], "state": item.get("State", "unknown"), "image": item.get("Image", ""), "status": item.get("Status", "")})
            records.append(record)
        self.store.seed_legacy(records)
        with self.lock:
            self.records = sorted(records, key=lambda record: (record["stack"].casefold(), record["name"].casefold()))
            self.last_inventory = now()
            self.error = ""
            present = {record["id"] for record in records}
            self.ready = {key: value for key, value in self.ready.items() if key in present}
            self.diagnostics = {key: value for key, value in self.diagnostics.items() if key in present}

    def current_records(self):
        with self.lock:
            return copy.deepcopy(self.records)

    def set_diagnostic(self, record, stage, message, attempt=False):
        with self.lock:
            diagnostic = self.diagnostics.setdefault(record["id"], {"attempts": 0, "last_attempt": None})
            diagnostic.update({"stage": stage, "message": message, "checked_at": now()})
            if attempt:
                diagnostic["attempts"] += 1
                diagnostic["last_attempt"] = now()

    def scan(self):
        with self.lock:
            self.scanning = True
            self.last_check = now()
        try:
            for record in self.current_records():
                self.heartbeat = now()
                if self.stop.is_set():
                    break
                config = self.store.snapshot()
                enabled, _ = Store.enabled(record, config)
                if config["settings"]["paused"] or not enabled or record["state"] not in ("exited", "created"):
                    self.ready.pop(record["id"], None)
                    continue
                try:
                    inspected = self.docker.inspect(record["id"])
                    if inspected.get("State", {}).get("Status") not in ("created", "exited"):
                        self.ready.pop(record["id"], None)
                        continue
                    if (inspected.get("HostConfig") or {}).get("AutoRemove"):
                        self.set_diagnostic(record, "protected", "Tarefa com remoção automática; recuperação ignorada.")
                        continue
                    mounts = storage.read_mounts()
                    signature = tuple(storage.probe_path(path, mounts, exact_mount=True) for path in storage.REQUIRED)
                    signature += tuple(storage.probe_path(path, mounts) for path in storage.data_sources(inspected, self.docker))
                    timer = time.monotonic()
                    previous = self.ready.get(record["id"])
                    if not previous or previous[0] != signature:
                        self.ready[record["id"]] = (signature, timer)
                    if timer - self.ready[record["id"]][1] < config["settings"]["stable_seconds"]:
                        self.set_diagnostic(record, "stabilizing", "Disco disponível. Aguardando estabilidade antes de iniciar.")
                        continue
                    # Revalida as escolhas imediatamente antes de solicitar a partida.
                    latest = self.store.snapshot()
                    if latest["settings"]["paused"] or not Store.enabled(record, latest)[0]:
                        self.ready.pop(record["id"], None)
                        continue
                    self.set_diagnostic(record, "starting", "Solicitando início ao Docker…", attempt=True)
                    self.docker.start(record["id"])
                    self.ready.pop(record["id"], None)
                    self.set_diagnostic(record, "requested", "Início solicitado. Aguardando confirmação do estado.")
                    self.event("Início solicitado para " + record["name"] + ".", name=record["name"], key=record["id"])
                    if self.stop.wait(config["settings"]["start_gap"]):
                        break
                except Exception as error:
                    self.ready.pop(record["id"], None)
                    message = str(error)
                    stage = "waiting_disk" if isinstance(error, OSError) else "error"
                    self.set_diagnostic(record, stage, message)
                    self.event(record["name"] + ": " + message, "warning" if stage == "waiting_disk" else "error", record["name"], record["id"])
        finally:
            with self.lock:
                self.scanning = False
                self.next_check = now() + self.store.snapshot()["settings"]["interval"]
                self.heartbeat = now()

    def snapshot(self):
        config = self.store.snapshot()
        with self.lock:
            records = copy.deepcopy(self.records)
            for record in records:
                record["enabled"], record["policy_source"] = Store.enabled(record, config)
                record["diagnostic"] = copy.deepcopy(self.diagnostics.get(record["id"], {}))
            return {
                "version": VERSION, "server_name": SERVER_NAME, "revision": config["revision"], "settings": config["settings"],
                "containers": records, "events": list(self.events), "error": self.error,
                "last_inventory": self.last_inventory, "last_check": self.last_check,
                "next_check": self.next_check, "scanning": self.scanning, "server_time": now(),
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
                self.next_check = now()
            try:
                if now() >= next_inventory:
                    self.discover()
                    next_inventory = now() + 10
                if now() >= self.next_check:
                    self.scan()
                    self.discover()
                    next_inventory = now() + 10
            except Exception as error:
                with self.lock:
                    self.error = str(error)
                    self.next_check = max(self.next_check, now() + 15)
                next_inventory = now() + 10
                self.event("Aguardando Docker: " + str(error), "error", key="daemon")
            self.wake.wait(1)


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
    server_version = "Sal0AutoStart/2.1"

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
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + (nonce or "unused") + "'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
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
                token, session = self.server.sessions.create()
            self.reply(200, {"authenticated": session["authenticated"], "setup_required": self.server.store.snapshot()["auth"] is None, "csrf": session["csrf"], "version": VERSION, "server_name": SERVER_NAME}, cookie=token)
        elif path == "/api/state":
            session = self.server.sessions.get(self.cookie_token())
            if not session or not session["authenticated"]:
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
                self.reply(403, {"error": "Sessão expirada. Atualize a página."})
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
            if not session["authenticated"]:
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


if __name__ == "__main__":
    main()
