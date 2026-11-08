// Executa as funcoes reais da interface sem dependencias ou navegador.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const script = html.match(/<script nonce="__CSP_NONCE__">([\s\S]*?)<\/script>/)[1];
const declarations = ['esc', 'norm', 'clock', 'stateNames'].map(name => {
  const line = script.split('\n').find(line => line.startsWith('const ' + name + ' ='));
  assert.ok(line, 'Declaracao da interface ausente: ' + name);
  return line;
});
const functions = ['reason', 'toggle', 'row', 'matches', 'renderStats', 'renderMonitor', 'updateServerInfo'].map(name => {
  const match = script.match(new RegExp('function ' + name + '\\([^]*?\\n\\}'));
  assert.ok(match, 'Funcao da interface ausente: ' + name);
  return match[0];
});
const nodes = {};
const record = (name, state, health = '') => ({
  id: name + '-id', key: name, name, state, health, stack: 'immich', stack_key: 'compose:immich',
  service: name, image: 'example/app:1', status: state, enabled: true, policy_source: 'stack', blocked: '', diagnostic: {},
});
const server = record('immich-server', 'exited');
server.diagnostic = {
  stage: 'waiting_dependency', message: 'Aguardando dependencias: immich-postgres', oom_killed: true,
  paths: [{source: '/media/<img src=x>', ready: false, message: 'aguardando disco'}],
  dependencies: [{name: 'immich-postgres', state: 'running', health: 'starting', ready: false}],
};
const database = record('immich-postgres', 'running', 'starting');
const ml = record('immich-machine-learning', 'running', 'unhealthy');
const state = {settings: {paused: false}, containers: [server, database, ml]};
const context = {
  state, expandedRows: new Set([server.id]), busy: false,
  icons: {cube: '', play: '', shield: '', disk: ''},
  updateCountdown: () => {},
  el: id => nodes[id] || (nodes[id] = {}),
};
vm.createContext(context);
vm.runInContext(declarations.concat(functions).join('\n'), context);
assert.match(context.reason(ml), /teste de saúde falhou/);
assert.match(context.reason(database), /Aguardando o teste de saúde/);
assert.equal(context.reason(server), server.diagnostic.message);
assert.equal(context.matches(server, '', 'waiting'), true);
assert.equal(context.matches(ml, '', 'waiting'), false);
const details = context.row(server);
assert.match(details, /falta de memória/);
assert.match(details, /Aguardando dependência: immich-postgres/);
assert.match(details, /&lt;img src=x&gt;/);
assert.ok(!details.includes('<img src=x>'));
assert.match(context.row(ml), /Ativo · falha de saúde/);
context.renderStats();
assert.match(nodes.stats.innerHTML, /1 com falha de saúde/);
assert.match(nodes.stats.innerHTML, /Discos ou dependências/);
assert.match(html, /href="\/api\/diagnostics" download="sal0-autostart-diagnostico.json"/);
state.manual = {requested_at: 1, started: 0, pending: 1};
state.settings.paused = true;
context.renderMonitor();
assert.match(nodes['monitor-strip'].innerHTML, /Iniciar todos/);
assert.match(nodes['monitor-strip'].innerHTML, /Partida manual em andamento/);
const allButton = nodes['monitor-strip'].innerHTML.match(/<button[^>]*data-start="all"[^>]*>/)[0];
assert.ok(!allButton.includes('disabled'), 'A partida manual deve funcionar com o monitor pausado');
server.enabled = false;
server.manual_pending = true;
assert.match(context.reason(server), /^Partida manual/);
assert.match(context.row(server), /data-start="container"/);
context.updateServerInfo({auth_required: false});
assert.equal(nodes.logout.hidden, true);

async function sessionRetry() {
  const calls = [];
  context.fetch = async (target, options) => {
    calls.push({target, token: options.headers['X-CSRF-Token']});
    const result = calls.length === 1 ? {code: 'session_expired'} : target === '/api/session'
      ? {authenticated: true, csrf: 'renewed-token'} : {ok: true};
    return {ok: calls.length !== 1, status: calls.length === 1 ? 403 : 200, json: async () => result};
  };
  const request = script.match(/async function request\([^]*?\n\}/)[0];
  vm.runInContext("let csrf = 'old-token';\n" + request, context);
  assert.equal((await context.request('/api/start', {scope: 'all'})).ok, true);
  assert.deepEqual(calls.map(c => c.target), ['/api/start', '/api/session', '/api/start']);
  assert.equal(calls[2].token, 'renewed-token');
}
sessionRetry().then(() => console.log('OK: saude, partida manual, painel sem login e renovacao apos reiniciar.'))
  .catch(error => {console.error(error); process.exitCode = 1;});
