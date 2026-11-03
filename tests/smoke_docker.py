"""Testa a imagem com Docker real em um runner descartavel do CI."""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request


def docker(*arguments, check=True):
    result = subprocess.run(["docker", *arguments], text=True, capture_output=True)
    if check and result.returncode:
        raise RuntimeError("Docker " + arguments[0] + ": " + result.stderr.strip())
    return result.stdout.strip()


def wait_for(callback, message, timeout=60):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = callback()
            if result:
                return result
        except (OSError, urllib.error.URLError, RuntimeError, ValueError) as error:
            last = type(error).__name__
        time.sleep(1)
    raise RuntimeError(message + (" (" + last + ")" if last else ""))


def main():
    image = sys.argv[1]
    prefix = "sal0-ci-" + secrets.token_hex(4)
    selected, excluded, monitor, database, redis, server = (prefix + suffix for suffix in (
        "-selected", "-excluded", "-monitor", "-z-database", "-y-redis", "-a-immich"))
    volume = prefix + "-config"
    containers = [monitor, selected, excluded, server, redis, database]
    password = secrets.token_urlsafe(24)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf = ""
    origin = ""

    def request(path, body=None):
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": origin}
        with opener.open(urllib.request.Request(origin + path, data=payload, headers=headers), timeout=5) as response:
            return json.load(response)

    def fixture(name, service, project=None, dependencies=None, healthcheck=None):
        arguments = ["create", "--name", name,
               "--label", "com.docker.compose.project=" + (project or prefix),
               "--label", "com.docker.compose.service=" + service,
               "--label", "com.docker.compose.container-number=1"]
        if dependencies:
            arguments.extend(["--label", "com.docker.compose.depends_on=" + dependencies])
        if healthcheck:
            arguments.extend(["--health-cmd", healthcheck, "--health-interval", "1s",
                              "--health-timeout", "2s", "--health-retries", "1"])
        arguments.extend(["--entrypoint", "python", image, "-c", "import time; time.sleep(300)"])
        docker(*arguments)

    def status(name):
        return docker("inspect", "--format", "{{.State.Status}}", name)

    def panel_origin():
        # O Docker pode escolher outra porta ao reiniciar um binding dinamico.
        port = docker("port", monitor, "8788/tcp").splitlines()[0].rsplit(":", 1)[1]
        return "http://127.0.0.1:" + port

    def login():
        nonlocal csrf
        csrf = request("/api/session")["csrf"]
        csrf = request("/api/login", {"password": password})["csrf"]

    try:
        fixture(selected, "selected")
        fixture(excluded, "excluded")
        docker("volume", "create", volume)
        docker("run", "-d", "--name", monitor, "--label", "autostart.ignore=true",
               "--read-only", "--init", "--cap-drop=ALL", "--cap-add=DAC_READ_SEARCH",
               "--security-opt=no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m",
               "-e", "INITIAL_DELAY_SECONDS=3600", "-e", "DISK_STABLE_SECONDS=0",
               "-e", "ONLY_CONTAINERS=" + selected + "," + excluded,
               "-e", "START_GAP_SECONDS=0", "-e", "CHECK_INTERVAL_SECONDS=15",
               "-p", "127.0.0.1::8788",
               "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", "/:/host:ro,rslave",
               "-v", "/proc/1/mountinfo:/host-mountinfo:ro",
               "-v", volume + ":/config", image)
        origin = panel_origin()
        wait_for(lambda: request("/health")["ok"], "O painel não iniciou.")
        session = request("/api/session")
        assert session["setup_required"]
        csrf = session["csrf"]
        csrf = request("/api/setup", {"password": password})["csrf"]
        state = wait_for(lambda: (snapshot if any(r["name"] == selected for r in snapshot["containers"]) else None) if (snapshot := request("/api/state")) else None,
                         "O inventário não encontrou os contêineres de teste.")
        target = next(r for r in state["containers"] if r["name"] == selected)
        assert target["stack"] == prefix
        state = request("/api/rules", {"scope": "stack", "key": target["stack_key"], "enabled": False, "revision": state["revision"]})
        state = request("/api/rules", {"scope": "container", "key": target["key"], "enabled": True, "revision": state["revision"]})
        request("/api/check", {})
        wait_for(lambda: status(selected) == "running", "O contêiner selecionado não iniciou.")
        assert status(excluded) == "created", "O monitor iniciou um contêiner desmarcado."
        print("OK: descoberta real da stack e início apenas do contêiner selecionado.")

        docker("restart", monitor)
        origin = panel_origin()
        wait_for(lambda: request("/health")["ok"], "O painel não voltou após reiniciar.")
        login()
        state = wait_for(lambda: (snapshot if any(r["name"] == selected for r in snapshot["containers"]) else None) if (snapshot := request("/api/state")) else None,
                         "O inventário não voltou após reiniciar.")
        assert next(r for r in state["containers"] if r["name"] == selected)["enabled"]
        assert not next(r for r in state["containers"] if r["name"] == excluded)["enabled"]
        docker("stop", "--time", "1", selected)
        request("/api/check", {})
        wait_for(lambda: status(selected) == "running", "A escolha salva não foi aplicada após reiniciar.")
        assert status(excluded) == "created"
        print("OK: senha e escolhas persistiram e a recuperação continuou após reiniciar.")

        # Reproduz a estrutura do Immich com banco e Redis: os testes de saude
        # sao reais, mas usam a propria imagem, sem baixar ou alterar um banco.
        project = prefix + "-immich"
        fixture(server, "immich-server", project,
                dependencies="database:service_healthy:false,redis:service_healthy:false")
        fixture(database, "database", project,
                healthcheck='python -c "from pathlib import Path; raise SystemExit(not Path(\'/tmp/ready\').is_file())"')
        fixture(redis, "redis", project, healthcheck='python -c "pass"')
        request("/api/check", {})
        state = wait_for(lambda: (snapshot if any(r["name"] == server for r in snapshot["containers"]) else None)
                         if (snapshot := request("/api/state")) else None,
                         "A stack com dependências não apareceu no inventário.")
        target = next(r for r in state["containers"] if r["name"] == server)
        request("/api/rules", {"scope": "stack", "key": target["stack_key"], "enabled": True, "revision": state["revision"]})
        wait_for(lambda: status(database) == "running" and status(redis) == "running",
                 "As dependências selecionadas não iniciaram.")
        diagnostic = wait_for(lambda: (record["diagnostic"] if record["diagnostic"].get("stage") == "waiting_dependency" else None)
                              if (record := next(r for r in request("/api/state")["containers"] if r["name"] == server)) else None,
                              "O servidor não aguardou a saúde das dependências.")
        assert status(server) == "created", "O servidor iniciou antes de o banco ficar saudável."
        assert any(d.get("name") == database and not d["ready"] for d in diagnostic["dependencies"])
        docker("exec", database, "python", "-c", "from pathlib import Path; Path('/tmp/ready').touch()")
        wait_for(lambda: docker("inspect", "--format", "{{.State.Health.Status}}", database) == "healthy",
                 "O healthcheck do banco de teste não ficou saudável.")
        wait_for(lambda: docker("inspect", "--format", "{{.State.Health.Status}}", redis) == "healthy",
                 "O healthcheck do Redis de teste não ficou saudável.")
        request("/api/check", {})
        wait_for(lambda: status(server) == "running", "O servidor não iniciou após as dependências ficarem saudáveis.")
        report = request("/api/diagnostics")
        assert any(r["name"] == server for r in report["containers"])
        assert "auth" not in report
        print("OK: servidor aguardou banco e Redis saudáveis; diagnóstico autenticado disponível.")
    except Exception:
        print(docker("logs", "--tail", "100", monitor, check=False))
        raise
    finally:
        for name in containers:
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
