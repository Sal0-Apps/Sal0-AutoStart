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
const functions = ['reason', 'toggle', 'row', 'matches', 'renderStats'].map(name => {
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
console.log('OK: saude, dependencias, filtros, diagnostico e escape de dados na interface.');
