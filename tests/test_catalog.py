import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml
import app
from zima_catalog import AppCatalog, AsyncAppCatalog
from test_app import Docker, item

PIXEL = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFUlEQVR4nGP0XPf+PwMDAwMTAxQAACviAuk7tqE1AAAAAElFTkSuQmCC'


def write_app(root, folder, name, services, main, project=None):
    directory = Path(root) / folder
    directory.mkdir(parents=True, exist_ok=True)
    document = {'name': project or folder, 'services': {key: {'container_name': value, 'environment': {'PASSWORD': 'never-export-this-secret'}} for key, value in services.items()},
                'x-casaos': {'main': main, 'title': {'en_US': 'English fallback', 'pt_BR': 'Português', 'custom': name}, 'icon': PIXEL}}
    (directory / 'docker-compose.yml').write_text(yaml.safe_dump(document, allow_unicode=True))


def seed_catalog(root):
    write_app(root, 'immich', 'Fotos da Casa', {'immich-server': 'immich-server', 'database': 'immich-postgres', 'redis': 'immich-redis'}, 'immich-server')
    write_app(root, 'jellyfin', 'Cinema Victor', {'jellyfin': 'jellyfin'}, 'jellyfin')
    return AppCatalog(root, ttl=0)


class CatalogTests(unittest.TestCase):
    def test_zima_names_icons_keep_rule_identities_and_saved_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = seed_catalog(root / 'apps')
            docker = Docker(item('immich-server', 'immich', 'immich-server'), item('immich-postgres', 'immich', 'database'), item('jellyfin', 'jellyfin', 'jellyfin'))
            store = app.Store(root / 'config')
            monitor = app.Monitor(store, docker, catalog)
            monitor.discover()
            initial = monitor.snapshot()
            before_keys = {r['name']: (r['key'], r['stack_key']) for r in initial['containers']}
            database = next(r for r in initial['containers'] if r['name'] == 'immich-postgres')
            store.update_rule({'scope': 'container', 'key': database['key'], 'enabled': False, 'revision': initial['revision']}, monitor.current_records())
            saved = store.snapshot()
            write_app(root / 'apps', 'immich', 'Biblioteca de Fotos', {'immich-server': 'immich-server', 'database': 'immich-postgres'}, 'immich-server')
            monitor.discover();current = monitor.snapshot()
            self.assertEqual(store.snapshot(), saved)
            self.assertEqual(before_keys, {r['name']: (r['key'], r['stack_key']) for r in current['containers']})
            group = next(g for g in current['apps'] if g['id'] == 'compose:immich')
            self.assertEqual(group['title'], 'Biblioteca de Fotos')
            self.assertEqual(group['icon'], PIXEL)
            self.assertEqual(group['main_container'], 'immich-server-id')
            self.assertEqual((group['scope'], group['key']), ('stack', 'compose:immich'))
            self.assertFalse(next(r for r in current['containers'] if r['name'] == 'immich-postgres')['enabled'])
            self.assertNotIn('never-export-this-secret', json.dumps(current))
            monitor.probes.close()

    def test_standalone_apps_are_separate_and_controls_do_not_change_neighbors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = app.Store(root / 'config')
            docker = Docker(item('one', extra={'name': 'Primeiro app', 'icon': 'https://example.org/one.png'}), item('two', extra={'name': 'Segundo app'}))
            monitor = app.Monitor(store, docker, AppCatalog(root / 'missing'))
            monitor.discover();state = monitor.snapshot()
            self.assertEqual(len(state['apps']), 2)
            self.assertTrue(all(g['scope'] == 'container' for g in state['apps']))
            selected = next(g for g in state['apps'] if g['title'] == 'Primeiro app')
            before = {r['name']: r['enabled'] for r in state['containers']}
            store.update_rule({'scope': selected['scope'], 'key': selected['key'], 'enabled': not before['one'], 'revision': state['revision']}, monitor.current_records())
            after = {r['name']: r['enabled'] for r in monitor.snapshot()['containers']}
            self.assertEqual(after['two'], before['two'])
            self.assertNotEqual(after['one'], before['one'])
            monitor.probes.close()

    def test_metadata_never_merges_distinct_recovery_stacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_app(root / 'apps', 'combined', 'Nome compartilhado', {'app': 'first', 'other': 'second'}, 'app')
            monitor = app.Monitor(app.Store(root / 'config'), Docker(item('first', 'project-a', 'app'), item('second', 'project-b', 'other')), AppCatalog(root / 'apps'))
            monitor.discover()
            self.assertEqual({g['key'] for g in monitor.snapshot()['apps']}, {'compose:project-a', 'compose:project-b'})
            monitor.probes.close()

    def test_invalid_metadata_and_icon_do_not_break_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory);bad = root / 'apps' / 'bad';bad.mkdir(parents=True)
            (bad / 'docker-compose.yml').write_text('!!python/object/apply:os.system [never-run]')
            monitor = app.Monitor(app.Store(root / 'config'), Docker(item('solo', extra={'icon': 'javascript:alert(1)'})), AppCatalog(root / 'apps'))
            monitor.discover();state = monitor.snapshot()
            self.assertEqual(state['apps'][0]['title'], 'solo')
            self.assertEqual(state['containers'][0]['icon'], '')
            self.assertEqual(state['error'], '')
            monitor.probes.close()

    def test_stalled_metadata_read_cannot_block_container_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = threading.Event();started = threading.Event()
            catalog = AsyncAppCatalog(Path(directory) / 'apps')
            def blocked_read():
                started.set();gate.wait(10);return [],False
            catalog.reader.snapshot = blocked_read
            monitor = app.Monitor(app.Store(Path(directory) / 'config'), Docker(item('server', 'stack', 'server')), catalog)
            worker = threading.Thread(target=monitor.discover)
            try:
                worker.start();worker.join(1)
                self.assertFalse(worker.is_alive(), 'Cosmetic metadata blocked recovery inventory')
                self.assertTrue(started.wait(1))
                self.assertEqual(len(monitor.snapshot()['containers']), 1)
            finally:
                gate.set();worker.join(2);monitor.probes.close()
