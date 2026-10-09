// Execute the shipped inline script, with only browser I/O replaced. No npm dependencies.
// Real payloads are supplied by the Python archive integration fixture via stdin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.attributes = {}; this.style = {};
    this.hidden = false; this.className = ''; this.listeners = {};
    this.classList = {contains: name => this.className.split(' ').includes(name)};
  }
  set textContent(value) { this.children = []; this.text = String(value); }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('Renderer must use text nodes, not HTML interpolation'); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.text = ''; this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  get firstElementChild() { return this.children[0]; }
}
const elements = new Map([...input.html.matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
elements.get('probability-bar').append(new Element('span'));
const document = {
  getElementById: id => elements.get(id) || null,
  createElement: tag => new Element(tag),
  createElementNS: (namespace, tag) => new Element(tag),
};
const timers = new Map();
const intervals = [];
let timerId = 0;
let calls = [];
const response = payload => ({ok: true, json: async () => structuredClone(payload)});
let fetchImpl = async url => {
  if (url.startsWith('/api/analytics/forecasts')) return response(input.forecasts);
  if (url.startsWith('/api/analytics')) return response(input.analytics);
  return response({status: 'pending', ok: false});
};
const context = vm.createContext({document, console, Date, AbortController,
  fetch: (url, options) => { calls.push({url, options}); return fetchImpl(url, options); },
  setTimeout: (fn, delay) => { timers.set(++timerId, {fn, delay}); return timerId; },
  clearTimeout: id => timers.delete(id),
  setInterval: (fn, delay) => intervals.push({fn, delay}),
});
const run = source => vm.runInContext(source, context);
const text = id => elements.get(id).textContent;
const descendants = element => element.children.flatMap(child => [child, ...descendants(child)]);
const tags = (id, tag) => descendants(elements.get(id)).filter(node => node.tagName === tag);
const flush = () => new Promise(resolve => setImmediate(resolve));
let checks = 0;
async function check(name, fn) { await fn(); checks++; console.log(`PASS ${name}`); }

(async () => {
  assert.ok(elements.has('historical-analytics'), 'Missing analytics UI');
  run(input.html.match(/<script>([\s\S]*?)<\/script>/)[1]);
  await flush();
  context.fixture = input.analytics;
  context.rowsFixture = input.forecasts;

  await check('actual payload summary, absent scored count, coverage and timestamp units', () => {
    assert.match(text('analytics-summary'), /35\s*\/\s*100/);
    assert.match(text('analytics-summary'), /60\.0%/);
    assert.match(text('analytics-summary'), /40\.0%.*75\.0%/);
    assert.match(text('analytics-summary'), /Scored.*—/);
    assert.match(text('analytics-summary'), /0\.200/);
    assert.match(text('analytics-summary'), /0\.250/);
    assert.match(text('analytics-coverage'), /30/);
    assert.match(text('analytics-coverage'), /8.*s/);
    assert.match(text('analytics-updated'), /Report updated/);
    assert.match(text('analytics-updated'), /age/i);
    assert.match(text('analytics-trend-time'), /2027/); // epoch seconds, not milliseconds (1970)
    assert.match(text('analytics-trend-time'), /Live archive/);
    assert.equal(tags('analytics-trend', 'svg').length, 2);
    for (const svg of tags('analytics-trend', 'svg')) {
      assert.equal(svg.getAttribute('role'), 'img');
      assert.ok(svg.getAttribute('aria-labelledby'));
    }
    assert.match(text('analytics-trend-values'), /52\.0%/);
    assert.match(text('analytics-trend-values'), /0\.328/);
    assert.equal(tags('analytics-forecasts', 'tr').length, 25);
  });
  await check('stale available report is diagnostic-only with retained report values', () => {
    run("renderAnalytics({...fixture, report_freshness:'stale', freshness:{status:'stale'}, validation_ready:false})");
    assert.match(text('analytics-status'), /stale/i);
    assert.equal(elements.get('analytics-warning').hidden, false);
    assert.match(text('analytics-warning'), /diagnostic.only/i);
    assert.match(text('analytics-summary'), /60\.0%/);
    assert.doesNotMatch(text('analytics-status'), /ready|validated/i);
    run("renderAnalytics({...fixture, report_freshness:'fresh', freshness:{status:'stale'}})");
    assert.match(text('analytics-status'), /stale/i);
  });
  await check('pending, unavailable and null clear previous report successes', () => {
    for (const state of ['pending', 'unavailable', null]) {
      context.state = state;
      run('renderAnalytics(state ? {status:state, reason:"Fixture reason"} : null)');
      assert.doesNotMatch(text('analytics-summary'), /60\.0%|0\.200/);
      assert.equal(tags('analytics-trend', 'circle').length, 0);
      assert.match(text('analytics-status'), state === 'pending' ? /pending|awaiting/i : /unavailable/i);
      assert.equal(elements.get('analytics-warning').hidden, true);
    }
    run("renderAnalytics({...fixture, summary:{eligible_count:0, minimum_count:100}, trend:[]})");
    assert.match(text('analytics-status'), /empty|no.*forecast/i);
    assert.match(text('analytics-summary'), /0\s*\/\s*100/);
    assert.doesNotMatch(text('analytics-summary'), /0\.0%|0\.000/);
  });
  await check('metric gaps stay disconnected and invalid numbers never enter SVG', () => {
    run(`renderAnalytics({...fixture, trend:[
      {issued_at:1800000000, count:25, accuracy:0, brier_score:0},
      {issued_at:1800000900, count:9},
      {issued_at:1800001800, count:25, accuracy:1, brier_score:1},
      {issued_at:1800002700, count:25, accuracy:.5, brier_score:.25},
      {issued_at:1800003600, count:25, accuracy:Infinity, brier_score:NaN},
      {issued_at:null, count:25, accuracy:.5, brier_score:.2}
    ]})`);
    assert.equal(tags('analytics-trend', 'circle').length, 6);
    // A single two-point segment per metric; never a line across the missing second point.
    const lines = tags('analytics-trend', 'polyline');
    assert.equal(lines.length, 2);
    for (const line of lines) assert.equal(line.getAttribute('points').trim().split(/\s+/).length, 2);
    for (const node of descendants(elements.get('analytics-trend'))) {
      assert.doesNotMatch(Object.values(node.attributes).join(' '), /NaN|Infinity/);
    }
    assert.match(text('analytics-trend-values'), /0\.0%/);
    assert.match(text('analytics-trend-values'), /100\.0%/);
  });
  await check('pending report keeps only the newly supplied live archive trend', () => {
    run('renderAnalytics(fixture)');
    run("renderAnalytics({status:'pending', trend:[{issued_at:1800000000, count:10, accuracy:.7, brier_score:.1}]})");
    assert.match(text('analytics-status'), /pending/i);
    assert.doesNotMatch(text('analytics-summary'), /60\.0%|0\.200/);
    assert.match(text('analytics-trend-values'), /70\.0%/);
    assert.doesNotMatch(text('analytics-trend-values'), /52\.0%/);
    assert.equal(tags('analytics-trend', 'circle').length, 2);
    assert.equal(tags('analytics-trend', 'polyline').length, 0);
  });
  await check('hostile archive text is literal; table bounds and nulls survive', () => {
    context.hostile = '<img src=x onerror="globalThis.pwned=true">';
    run('renderAnalytics({status:"pending", reason:hostile})');
    assert.ok(text('analytics-message').includes(context.hostile));
    run(`renderAnalyticsForecasts({status:'available', rows:Array.from({length:30}, () => ({
      market_ticker:hostile, issued_at:null, probability_up:null, result:null,
      timing_status:'unknown', yes_mid:null, settlement_available_at:null, settlement_delay_seconds:null
    }))})`);
    assert.equal(tags('analytics-forecasts', 'tr').length, 25);
    assert.ok(text('analytics-forecasts').includes(context.hostile));
    assert.match(text('analytics-forecasts'), /—/);
    assert.doesNotMatch(text('analytics-forecasts'), /0\.0%|1970|TRADE NOW/);
    assert.equal(tags('analytics-forecasts', 'img').length, 0);
    assert.equal(context.pwned, undefined);
    for (const payload of [{status:'pending'}, {status:'unavailable'}, null, {status:'available', rows:[]}]) {
      context.tablePayload = payload;
      run('renderAnalyticsForecasts(tablePayload)');
      assert.equal(tags('analytics-forecasts', 'tr').length, 1);
      assert.doesNotMatch(text('analytics-forecasts'), /onerror/);
      assert.match(text('analytics-forecasts'), payload?.status === 'pending' ? /awaiting/i
        : payload?.status === 'available' ? /no.*forecast/i : /unavailable/i);
    }
  });
  await check('startup and manual refresh poll both analytics routes without overlap', async () => {
    assert.ok(calls.some(call => call.url.startsWith('/api/analytics/forecasts?limit=25')));
    assert.ok(calls.some(call => /^\/api\/analytics\?/.test(call.url)));
    calls = [];
    const pending = [];
    fetchImpl = (url, options) => new Promise((resolve, reject) => {
      if (url.startsWith('/api/analytics')) {
        pending.push({url, resolve});
        options.signal.addEventListener('abort', () => reject(new Error('aborted')));
      } else resolve(response({ok:false}));
    });
    elements.get('refresh').listeners.click();
    run('refreshAnalytics(); refreshAnalytics()');
    assert.equal(calls.filter(call => call.url.startsWith('/api/analytics')).length, 2);
    for (const entry of pending) entry.resolve(response(entry.url.includes('/forecasts') ? input.forecasts : input.analytics));
    await flush();
    assert.match(text('analytics-summary'), /60\.0%/);
    assert.equal(timers.size, 0);
    assert.ok(intervals.some(interval => interval.delay === 30000));
    calls = [];
    for (const interval of intervals.filter(item => item.delay === 30000)) interval.fn();
    assert.equal(calls.filter(call => call.url.startsWith('/api/analytics')).length, 2);
    for (const entry of pending) entry.resolve(response(entry.url.includes('/forecasts') ? input.forecasts : input.analytics));
    await flush();
  });
  await check('network, HTTP, JSON, and API failures clear analytics only; refresh recovers', async () => {
    for (const failure of [() => Promise.reject(new Error('offline')),
                          async () => ({ok:false}),
                          async () => ({ok:true, json:async () => { throw new Error('invalid JSON'); }}),
                          async () => response({status:'unavailable'})]) {
      run('renderAnalytics(fixture); renderAnalyticsForecasts(rowsFixture)');
      elements.get('status-text').textContent = 'Live sentinel';
      elements.get('trade-signal').textContent = 'WAIT sentinel';
      fetchImpl = failure;
      await run('refreshAnalytics()');
      assert.match(text('analytics-status'), /unavailable/i);
      assert.doesNotMatch(text('analytics-summary'), /60\.0%/);
      assert.equal(tags('analytics-trend', 'circle').length, 0);
      assert.match(text('analytics-forecasts'), /unavailable/i);
      assert.equal(text('status-text'), 'Live sentinel');
      assert.equal(text('trade-signal'), 'WAIT sentinel');
    }
    fetchImpl = async url => response(url.includes('/forecasts') ? input.forecasts : input.analytics);
    await run('refreshAnalytics()');
    assert.match(text('analytics-summary'), /60\.0%/);
  });
  await check('hanging requests time out, abort and release the next refresh', async () => {
    fetchImpl = (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('aborted')));
    });
    const request = run('refreshAnalytics()');
    assert.ok(timers.size > 0);
    for (const timer of [...timers.values()]) {
      assert.ok(timer.delay > 0 && timer.delay <= 15000);
      timer.fn();
    }
    await request;
    assert.match(text('analytics-status'), /unavailable/i);
    assert.match(text('analytics-forecasts'), /unavailable/i);
    assert.equal(timers.size, 0);
    fetchImpl = async url => response(url.includes('/forecasts') ? input.forecasts : input.analytics);
    await run('refreshAnalytics()');
    assert.match(text('analytics-summary'), /60\.0%/);
  });
  await check('live failures and expiry remain independent from analytics state', async () => {
    run(`render({generated_at:new Date().toISOString(), prediction:{trade_signal:'TRADE',
      recommendation:{valid_until:Date.now()/1000 + 60}}})`);
    fetchImpl = async () => { throw new Error('analytics offline'); };
    await run('refreshAnalytics()');
    assert.equal(text('trade-signal'), 'TRADE NOW');
    assert.match(text('status-text'), /^Live/);
    run('recommendationDeadline = Date.now() - 1; updateClock()');
    assert.equal(text('trade-signal'), 'WAIT / EVIDENCE EXPIRED');
    run('renderAnalytics(fixture); renderAnalyticsForecasts(rowsFixture)');
    await run('refresh()');
    assert.equal(text('trade-signal'), 'WAIT / DATA UNAVAILABLE');
    assert.match(text('analytics-summary'), /60\.0%/);
    assert.equal(tags('analytics-forecasts', 'tr').length, 25);
    assert.match(text('analytics-status'), /available/i);
  });
  console.log(`${checks} renderer and polling checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
