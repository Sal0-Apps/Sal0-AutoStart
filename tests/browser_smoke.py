"""Exercita os botoes reais em navegador, usando apenas conteineres simulados."""
import os
import shutil
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

from test_app import Docker as FixtureDocker, item
from test_catalog import seed_catalog
import app
import storage_guard as storage


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "host/media/HD/app").mkdir(parents=True)
        device = root.stat().st_dev
        (root / "mountinfo").write_text(f"1 0 {os.major(device)}:{os.minor(device)} / / rw - ext4 /dev/root rw\n")
        storage.HOST_ROOT, storage.MOUNTINFO, storage.VIEW_MOUNTINFO = str(root / "host"), str(root / "mountinfo"), str(root / "view")
        storage.REQUIRED = []
        store = app.Store(root / "config")
        store.data["settings"].update(paused=True, default_enabled=False, initial_delay=0, stable_seconds=0, start_gap=0, interval=15)
        store.setup_password("previous-installation-password")
        database = item("immich-postgres", "immich", "database", state="created")
        redis = item("immich-redis", "immich", "redis", state="created")
        for target in (database, redis):
            target["State"]["Health"] = {"Status": "healthy"}
        server = item("immich-server", "immich", "immich-server", state="created", extra={
            "com.docker.compose.depends_on": "database:service_healthy:false,redis:service_healthy:false"})
        disk = item("jellyfin", "jellyfin", "jellyfin", source="/media/HD/app")
        protected = item("protected-job", extra={"autostart.ignore": "true"})
        catalog = seed_catalog(root / 'host/var/lib/casaos/apps')
        monitor = app.Monitor(store, FixtureDocker(server, database, redis, disk, protected), catalog)
        panel = app.PanelServer(("127.0.0.1", 0), store, monitor)
        worker = threading.Thread(target=monitor.run, daemon=True)
        web = threading.Thread(target=panel.serve_forever, daemon=True)
        worker.start()
        web.start()
        output = Path("test-artifacts")
        output.mkdir(exist_ok=True)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, executable_path=shutil.which('google-chrome'))
                page = browser.new_page(viewport={"width": 1280, "height": 1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{panel.server_address[1]}")
                all_button = page.get_by_role("button", name="Iniciar todos", exact=True)
                all_button.wait_for()
                assert not page.locator("#auth").is_visible()
                assert not page.locator("#logout").is_visible()
                stack = page.locator('.stack[data-app-id="compose:immich"]')
                page.get_by_role('button', name='Expandir Fotos da Casa', exact=True).wait_for()
                assert stack.locator('.container-row').count() == 0
                page.get_by_role('button', name='Expandir Fotos da Casa', exact=True).click()
                expect(stack.locator('.container-row')).to_have_count(3)
                expect(stack.locator('.stack-icon.image-loaded')).to_be_visible()
                page.locator('#search').fill('immich-postgres')
                expect(page.locator('#stack-list .stack')).to_have_count(1)
                expect(stack.locator('.container-row')).to_have_count(3)
                page.locator('#search').fill('')
                def rule(action):
                    with page.expect_response(lambda response: response.url.endswith('/api/rules') and response.request.method == 'POST') as result:
                        action()
                    response = result.value
                    assert response.ok, response.text()
                    return response.json()
                rule(lambda: page.get_by_role('checkbox', name='Recuperação de immich-redis', exact=True).check())
                stack_switch = page.get_by_role('checkbox', name='Recuperação da stack Fotos da Casa', exact=True)
                expect(stack_switch).to_be_enabled()
                assert stack_switch.evaluate('element=>element.indeterminate')
                selected = rule(lambda: stack_switch.check())
                assert all(r['enabled'] for r in selected['containers'] if r['stack_key'] == 'compose:immich')
                rule(lambda: stack_switch.uncheck())
                page.screenshot(path=str(output / "desktop.png"), full_page=True)
                for width in (390,360):
                    page.set_viewport_size({"width": width, "height": 844})
                    # A rotating decoration must stay inside its cell at every angle.
                    orbit=page.locator('.sal0-app-art .sal0-power-orbit')
                    for phase in (0,2250,6750):
                        orbit.evaluate('(el,phase)=>{for(const animation of el.getAnimations()){animation.pause();animation.currentTime=phase;}}',phase)
                        assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth'), 'Rolagem horizontal no celular durante a órbita'
                    orbit.evaluate('el=>el.getAnimations().forEach(animation=>animation.play())')
                page.screenshot(path=str(output / "mobile.png"), full_page=True)
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), "Rolagem horizontal no celular"
                all_button.click()
                page.get_by_text("Última partida manual: 3 iniciado(s) · 1 pendente(s)", exact=False).wait_for(timeout=15000)
                assert monitor.snapshot()["manual"]["started"] == 3
                assert monitor.snapshot()["settings"]["paused"]
                assert not any(record["enabled"] for record in monitor.snapshot()["containers"])
                assert monitor.docker.items[protected["Id"]]["State"]["Status"] == "exited"
                disk_row = page.locator(".container-row").filter(has_text="jellyfin")
                disk_row.get_by_role("button", name="Detalhes", exact=True).click()
                page.get_by_text("Aguardando: /media/HD/app", exact=False).wait_for()
                page.screenshot(path=str(output / "mobile-started.png"), full_page=True)
                page.reload()
                all_button.wait_for()
                page.get_by_role('button', name='Recolher Fotos da Casa', exact=True).wait_for()
                assert not page.locator("#auth").is_visible()
                page.get_by_role('button', name='Escolher tema visual', exact=True).click()
                page.locator('[data-sal0-mode-choice=light]').click()
                page.locator('[data-sal0-close]').click()
                page.screenshot(path=str(output / 'mobile-theme.png'), full_page=True)
                assert not errors, errors
                browser.close()
            print("OK: nomes/ícones do ZimaOS, stacks recolhidas, busca de serviços, seleção parcial, expansão persistente, desktop/390/360px e Iniciar todos sem login.")
        finally:
            monitor.stop.set()
            monitor.wake.set()
            panel.shutdown()
            panel.server_close()
            worker.join(timeout=5)
            web.join(timeout=5)
            monitor.probes.close()


if __name__ == "__main__":
    main()
