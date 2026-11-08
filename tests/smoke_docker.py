"""Recuperacao automatica e partida manual com Docker real em runner descartavel."""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def docker(*arguments, check=True):
    result = subprocess.run(["docker", *arguments], text=True, capture_output=True, timeout=45)
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
        time.sleep(.5)
    raise RuntimeError(message + (" (" + last + ")" if last else ""))


def main():
    image = sys.argv[1]
    prefix = "sal0-ci-" + secrets.token_hex(4)
    selected, excluded, monitor, database, redis, server, protected, blocked = (
        prefix + suffix for suffix in ("-selected", "-excluded", "-monitor", "-z-database", "-y-redis", "-a-immich", "-protected", "-a-wait-disk"))
    volume = prefix + "-config"
    containers = [monitor, selected, excluded, server, redis, database, protected, blocked]
    bind_directory = tempfile.TemporaryDirectory(prefix=prefix + "-bind-")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    csrf, origin = "", ""

    def request(path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        headers = {} if body is None else {"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": origin}
        with opener.open(urllib.request.Request(origin + path, data=payload, headers=headers), timeout=5) as response:
            return json.load(response)

    def fixture(name, service, project=None, dependencies=None, healthcheck=None, ignored=False, bind=None):
        arguments = ["create", "--name", name, "--label", "com.docker.compose.project=" + (project or prefix),
                     "--label", "com.docker.compose.service=" + service, "--label", "com.docker.compose.container-number=1"]
        if ignored:
            arguments.extend(["--label", "autostart.ignore=true"])
        if dependencies:
            arguments.extend(["--label", "com.docker.compose.depends_on=" + dependencies])
        if healthcheck:
            arguments.extend(["--health-cmd", healthcheck, "--health-interval", "1s", "--health-timeout", "2s", "--health-retries", "1"])
        if bind:
            arguments.extend(["-v", bind + ":/data:ro"])
        arguments.extend(["--entrypoint", "python", image, "-c", "import time; time.sleep(300)"])
        docker(*arguments)

    def status(name):
        return docker("inspect", "--format", "{{.State.Status}}", name)

    def panel_origin():
        port = docker("port", monitor, "8788/tcp").splitlines()[0].rsplit(":", 1)[1]
        return "http://127.0.0.1:" + port

    try:
        fixture(selected, "selected", bind=bind_directory.name)
        fixture(excluded, "excluded")
        fixture(protected, "protected", ignored=True)
        fixture(blocked, "disk-app", project=prefix + "-disk", bind="/media/" + prefix + "-missing-disk/app")
        docker("volume", "create", volume)
        environment = ["-e", "INITIAL_DELAY_SECONDS=0", "-e", "DISK_STABLE_SECONDS=0",
                       "-e", "ONLY_CONTAINERS=" + selected + "," + blocked, "-e", "IGNORE_CONTAINERS=" + excluded,
                       "-e", "START_GAP_SECONDS=0", "-e", "CHECK_INTERVAL_SECONDS=15", "-e", "AUTH_REQUIRED=false"]
        # Simula uma instalacao anterior com senha no mesmo volume.
        docker("run", "--rm", *environment, "-v", volume + ":/config", "--entrypoint", "python", image, "-c",
               "from app import Store; Store('/config').setup_password('integration-old-password')")
        docker("run", "-d", "--name", monitor, "--label", "autostart.ignore=true", "--read-only", "--init",
               "--cap-drop=ALL", "--cap-add=DAC_READ_SEARCH", "--security-opt=no-new-privileges:true",
               "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m", *environment,
               "-p", "127.0.0.1::8788", "-v", "/var/run/docker.sock:/var/run/docker.sock",
               "-v", "/:/host:ro,rslave", "-v", "/proc/1/mountinfo:/host-mountinfo:ro", "-v", volume + ":/config", image)

        # Nenhuma chamada HTTP, login, /check ou mudanca de preferencia ate
        # comprovar a partida exclusiva do ciclo em segundo plano.
        wait_for(lambda: status(selected) == "running", "O ciclo automático não iniciou o selecionado sem cliques.")
        assert status(excluded) == "created"
        assert status(protected) == "created"
        assert status(blocked) == "created"
        print("OK: boot automático sem HTTP ou login; disco ausente não bloqueou os outros contêineres.", flush=True)

        origin = panel_origin()
        wait_for(lambda: request("/health")["ok"], "O painel não iniciou.")
        session = request("/api/session")
        assert session["authenticated"] and not session["setup_required"] and not session["auth_required"]
        csrf = session["csrf"]
        state = request("/api/state")
        target = next(r for r in state["containers"] if r["name"] == selected)
        assert target["stack"] == prefix
        wait_for(lambda: next(r for r in request("/api/state")["containers"] if r["name"] == blocked)["diagnostic"].get("stage") == "waiting_disk",
                 "O disco ausente não recebeu diagnóstico de espera.")
        state = request("/api/settings", {"revision": state["revision"], "settings": {"paused": True}})
        # Somente a stack criada pelo teste recebe a partida manual.
        request("/api/start", {"scope": "stack", "key": target["stack_key"]})
        wait_for(lambda: status(excluded) == "running", "A partida manual não iniciou o desmarcado enquanto pausado.")
        state = request("/api/state")
        assert state["settings"]["paused"]
        assert not next(r for r in state["containers"] if r["name"] == excluded)["enabled"]
        assert status(protected) == "created"
        print("OK: painel abriu sem senha existente; partida manual funcionou pausada e preservou as escolhas.", flush=True)

        request("/api/settings", {"revision": state["revision"], "settings": {"paused": False}})
        docker("stop", "--time", "5", monitor)
        docker("stop", "--time", "1", selected)
        docker("stop", "--time", "1", excluded)
        docker("start", monitor)
        wait_for(lambda: status(selected) == "running", "A recuperação não voltou automaticamente após reiniciar o monitor.")
        assert status(excluded) == "exited"
        docker("stop", "--time", "1", selected)
        wait_for(lambda: status(selected) == "running", "O ciclo periódico não recuperou uma nova parada sem cliques.")
        assert status(excluded) == "exited"
        origin = panel_origin()
        session = request("/api/session")
        assert session["authenticated"] and not session["setup_required"]
        csrf = session["csrf"]
        print("OK: reinício e nova parada foram recuperados sem /check; painel continuou sem login.", flush=True)

        state = request("/api/state")
        request("/api/settings", {"revision": state["revision"], "settings": {"paused": True}})
        project = prefix + "-immich"
        fixture(server, "immich-server", project, dependencies="database:service_healthy:false,redis:service_healthy:false")
        fixture(database, "database", project,
                healthcheck='python -c "from pathlib import Path; raise SystemExit(not Path(\'/tmp/ready\').is_file())"')
        fixture(redis, "redis", project, healthcheck='python -c "pass"')
        state = wait_for(lambda: (snapshot if any(r["name"] == server for r in snapshot["containers"]) else None)
                         if (snapshot := request("/api/state")) else None, "A stack de teste não apareceu.")
        target = next(r for r in state["containers"] if r["name"] == server)
        request("/api/start", {"scope": "stack", "key": target["stack_key"]})
        wait_for(lambda: status(database) == "running" and status(redis) == "running", "As dependências não iniciaram.")
        wait_for(lambda: next(r for r in request("/api/state")["containers"] if r["name"] == server)["diagnostic"].get("stage") == "waiting_dependency",
                 "O servidor não aguardou o banco.")
        assert status(server) == "created"
        docker("exec", database, "python", "-c", "from pathlib import Path; Path('/tmp/ready').touch()")
        wait_for(lambda: status(server) == "running", "A partida não continuou sozinha após o banco ficar saudável.")
        report = request("/api/diagnostics")
        assert report["settings"]["paused"]
        assert any(r["name"] == server and not r["enabled"] for r in report["containers"])
        assert "integration-old-password" not in json.dumps(report)
        print("OK: partida manual da stack aguardou banco e Redis e continuou automaticamente sem outro clique.", flush=True)
    except Exception:
        print(docker("logs", "--tail", "120", monitor, check=False))
        raise
    finally:
        for name in containers:
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)
        bind_directory.cleanup()


if __name__ == "__main__":
    main()

