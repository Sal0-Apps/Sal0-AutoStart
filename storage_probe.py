"""Verifica armazenamento em processos limitados para isolar discos/FUSE travados."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import storage_guard as storage


def unavailable(checks, message):
    return {"signature": [], "paths": [{"source": path, "ready": False, "message": message} for path in checks], "errors": [message]}


def check_paths(checks):
    if not checks:
        return {"signature": [], "paths": [], "errors": []}
    try:
        mounts = storage.read_mounts()
    except (OSError, ValueError) as error:
        return unavailable(checks, str(error))
    result = {"signature": [], "paths": [], "errors": []}
    for path, exact in sorted(checks.items()):
        try:
            signature = storage.probe_path(path, mounts, exact_mount=exact)
            result["signature"].append(signature)
            result["paths"].append({"source": path, "resolved": signature[0], "mount": signature[4], "ready": True, "message": "Disponível"})
        except (OSError, ValueError) as error:
            result["paths"].append({"source": path, "ready": False, "message": str(error)})
            result["errors"].append(str(error))
    return result


class ProbePool:
    def __init__(self, timeout=None, limit=4):
        self.timeout = max(.1, min(30, float(timeout if timeout is not None else os.getenv("STORAGE_PROBE_TIMEOUT_SECONDS", "5"))))
        self.limit = limit
        self.children = set()
        self.lock = threading.Lock()

    def check(self, checks):
        if not checks:
            return {"signature": [], "paths": [], "errors": []}
        with self.lock:
            self.children = {child for child in self.children if child.poll() is None}
            if len(self.children) >= self.limit:
                return unavailable(checks, "Aguardando liberação de uma verificação de armazenamento; nova tentativa automática.")
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.children.add(process)
        payload = json.dumps({"checks": checks, "host_root": storage.HOST_ROOT, "mountinfo": storage.MOUNTINFO,
                              "view_mountinfo": storage.VIEW_MOUNTINFO, "protected": storage.PROTECTED})
        try:
            output, _ = process.communicate(payload, timeout=self.timeout)
            if process.returncode:
                return unavailable(checks, "Falha na verificação de armazenamento; nova tentativa automática.")
            result = json.loads(output)
            if not all(isinstance(result.get(key), list) for key in ("signature", "paths", "errors")):
                raise ValueError("Resposta inválida do verificador")
            return result
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=.2)
            except subprocess.TimeoutExpired:
                # I/O em estado D pode demorar a responder ao SIGKILL. Mantemos
                # o processo no limite de vagas sem bloquear os outros serviços.
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
            return unavailable(checks, f"Verificação de armazenamento excedeu {self.timeout:g}s; nova tentativa automática.")
        except (OSError, ValueError) as error:
            return unavailable(checks, "Falha na verificação de armazenamento: " + str(error))
        finally:
            with self.lock:
                if process.poll() is not None:
                    self.children.discard(process)

    def close(self):
        with self.lock:
            for process in self.children:
                if process.poll() is None:
                    process.kill()


def worker():
    payload = json.load(sys.stdin)
    storage.HOST_ROOT = payload["host_root"]
    storage.MOUNTINFO = payload["mountinfo"]
    storage.VIEW_MOUNTINFO = payload["view_mountinfo"]
    storage.PROTECTED = tuple(payload["protected"])
    json.dump(check_paths(payload["checks"]), sys.stdout, ensure_ascii=False)


if __name__ == "__main__" and sys.argv[1:] == ["--worker"]:
    worker()
