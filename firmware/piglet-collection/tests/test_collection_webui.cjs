/* Execute the real embedded WebUI functions with a fake DOM and local fetch. */
'use strict';
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '..', 'source', 'Piglet', 'WebUI.cpp'), 'utf8');
test('complete embedded WebUI script parses after policy and timer changes', () => {
  const script = source.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(script);
  assert.doesNotThrow(() => new vm.Script(script[1]));
});
function section(start, end) {
  const first = source.indexOf(start);
  assert.notEqual(first, -1, 'Expected real source section: ' + start);
  const last = source.indexOf(end, first);
  assert.ok(last > first, 'Expected source terminator: ' + end);
  return source.slice(first, last);
}
const realCode = [
  section('const SECRET_SAVED_VALUE=', 'const WIFI24_RECOMMENDED='),
  section('async function loadStatus()', '\n/* ---- Files ---- */'),
  section('async function loadFiles()', '\n/* ---- Reset history ---- */'),
  section('async function doSave()', '\nasync function saveCfg()'),
  section('async function wigleUploadOne(name)', '\n/* ---- AP keep-alive modal ----'),
].join('\n');
const secretKeys = ['homePsk', 'wardriverPsk', 'wigleBasicToken', 'wdgwarsApiKey', 'collectionPsk', 'collectionToken'];
const baseConfig = () => ({
  board: 'xiao_c5', deviceName: 'Synthetic Piglet', scanProfile: 'kg',
  homeSsid: 'Synthetic WiFi', homePsk: 'synthetic-home-secret',
  wardriverSsid: 'Piglet-test-only', wardriverPsk: 'synthetic-ap-secret',
  wigleBasicToken: 'synthetic-wigle-secret', wdgwarsApiKey: 'synthetic-wars-secret',
  operatingMode: 'normal', collectionSsid: 'WDG-Collection-test',
  collectionPsk: 'synthetic-collection-secret', collectionHost: '10.72.0.1',
  collectionPort: '8765', collectionToken: 'synthetic-collection-token',
  wifi24Channels: '1,6,11', wifi5Channels: '36,40,44,48',
  wifi24DwellMs: '110', wifi5DwellMs: '100', bleEnabled: 'true',
  bleScanDurationMs: '1000', bleEveryNCycles: '5',
});

function browser(config = baseConfig()) {
  const elements = new Map();
  const posts = [];
  const calls = [];
  const errors = [];
  const behavior = { saveOk: true, saveStatus: 422, saveError: 'synthetic save rejected', profileOk: true,
    filesPayload: { ok: true, files: [] }, externalUploadsAllowed: config.operatingMode !== 'uconsole' };
  function get(id) {
    if (!elements.has(id)) elements.set(id, {
      id, value: '', dataset: {}, style: {}, textContent: '', innerHTML: '',
      classList: { toggle() {}, add() {}, remove() {} },
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    $: get,
    console: { error(...args) { errors.push(args); } },
    setPill() {}, setText() {}, updateBleStatusPill() {}, updateWifiDedupeStatus() {},
    updateCollectionStatus() {}, wigleStatusText: () => 'Synthetic status',
    formatBytes: value => String(value) + ' bytes', setWigleMsg() {},
    applyLoadedScanProfileConfig(profile, channels24, channels5, dwell24, dwell5) {
      for (const [key, value] of Object.entries({ scanProfile: profile, wifi24Channels: channels24,
        wifi5Channels: channels5, wifi24DwellMs: dwell24, wifi5DwellMs: dwell5 })) get(key).value = value;
    },
    updateUploadedRetentionControls() {},
    prepareChannelProfileSave: () => behavior.profileOk,
    prepareDwellSave() {}, validateBleTiming() {},
    fetch: async (url, options) => {
      calls.push(url);
      if (url === '/files.json') return { ok: true, json: async () => ({
        ...behavior.filesPayload, externalLogUploadsAllowed: behavior.externalUploadsAllowed }) };
      if (url.startsWith('/wigle/upload?name=')) {
        assert.equal(options.method, 'POST');
        return { ok: true, json: async () => ({ ok: true, message: 'Synthetic manual upload' }) };
      }
      if (url === '/status.json') {
        const masked = { ...config };
        for (const key of secretKeys) masked[key] = config[key] ? '(set)' : '';
        return { ok: true, json: async () => ({ allowScan: true, sdOk: true,
          externalLogUploadsAllowed: behavior.externalUploadsAllowed, config: masked }) };
      }
      assert.equal(url, '/saveConfig', 'No unrelated endpoint may be called');
      assert.equal(options.method, 'POST');
      assert.equal(options.headers['Content-Type'], 'text/plain');
      posts.push(options.body);
      if (behavior.saveOk) {
        for (const line of options.body.split('\n')) {
          if (!line || line.startsWith('#')) continue;
          const equals = line.indexOf('=');
          assert.ok(equals > 0);
          config[line.slice(0, equals)] = line.slice(equals + 1);
        }
      }
      return { ok: behavior.saveOk, status: behavior.saveStatus, text: async () => behavior.saveError };
    },
  });
  vm.runInContext(realCode, context, { filename: 'Piglet-WebUI-real-sections.js' });
  return { context, config, get, posts, calls, errors, behavior };
}

function posted(body, key) {
  const prefix = key + '=';
  return body.split('\n').find(line => line.startsWith(prefix));
}

test('status masks every saved secret, including Collection credentials', async () => {
  const page = browser();
  await page.context.loadStatus();
  for (const key of secretKeys) {
    assert.equal(page.get(key).value, '************', key);
    assert.equal(page.get(key).dataset.secretState, 'saved', key);
    assert.notEqual(page.get(key).value, page.config[key]);
  }
  assert.equal(page.get('operatingMode').value, 'normal');
  assert.deepEqual(page.errors, []);
});

test('focus without typing never clears a saved Collection token or PSK', async () => {
  const page = browser();
  await page.context.loadStatus();
  const before = { ...page.config };
  for (const key of ['collectionToken', 'collectionPsk']) {
    page.context.prepareSecretEdit(page.get(key));
    assert.equal(page.get(key).value, '');
  }
  await page.context.doSave();
  for (const key of secretKeys) {
    assert.equal(posted(page.posts[0], key), undefined, key);
    assert.equal(page.config[key], before[key], key);
    assert.equal(page.get(key).value, '************', key);
  }
});

test('explicit Clear sends collectionToken= and reloads an empty saved state', async () => {
  const page = browser();
  await page.context.loadStatus();
  page.context.clearSavedSecret('collectionToken');
  assert.equal(page.get('collectionToken').dataset.secretClear, '1');
  await page.context.doSave();
  assert.equal(posted(page.posts[0], 'collectionToken'), 'collectionToken=');
  assert.equal(page.config.collectionToken, '');
  assert.equal(page.get('collectionToken').value, '');
  assert.equal(page.get('collectionToken').dataset.secretState, undefined);
  assert.equal(page.get('collectionToken').dataset.secretClear, undefined);
  assert.equal(page.config.homePsk, 'synthetic-home-secret');
  assert.equal(page.config.collectionPsk, 'synthetic-collection-secret');
  assert.deepEqual(page.calls, ['/status.json', '/saveConfig', '/status.json']);
});

test('editing the Collection token sends only its new real value, never the mask', async () => {
  const page = browser();
  await page.context.loadStatus();
  page.context.prepareSecretEdit(page.get('collectionToken'));
  page.get('collectionToken').value = 'synthetic-replacement-token';
  await page.context.doSave();
  assert.equal(posted(page.posts[0], 'collectionToken'), 'collectionToken=synthetic-replacement-token');
  assert.ok(!page.posts[0].includes('************'));
  assert.equal(page.config.collectionToken, 'synthetic-replacement-token');
  assert.equal(page.get('collectionToken').value, '************');
});

test('Normal and UConsole mode persist across a fresh WebUI page load', async () => {
  const config = baseConfig();
  for (const mode of ['uconsole', 'normal']) {
    const page = browser(config);
    await page.context.loadStatus();
    page.get('operatingMode').value = mode;
    await page.context.doSave();
    assert.equal(posted(page.posts[0], 'operatingMode'), 'operatingMode=' + mode);
    const reloaded = browser(config);
    await reloaded.context.loadStatus();
    assert.equal(reloaded.get('operatingMode').value, mode);
    assert.equal(config.scanProfile, 'kg');
    assert.equal(config.wifi24Channels, '1,6,11');
    assert.equal(config.wifi5Channels, '36,40,44,48');
  }
});

test('existing WiGLE key clear and untouched home/AP passwords keep their original behavior', async () => {
  const page = browser();
  await page.context.loadStatus();
  page.context.prepareSecretEdit(page.get('homePsk'));
  page.context.prepareSecretEdit(page.get('wardriverPsk'));
  page.context.clearSavedSecret('wigleBasicToken');
  await page.context.doSave();
  assert.equal(posted(page.posts[0], 'wigleBasicToken'), 'wigleBasicToken=');
  assert.equal(page.config.wigleBasicToken, '');
  assert.equal(page.config.homePsk, 'synthetic-home-secret');
  assert.equal(page.config.wardriverPsk, 'synthetic-ap-secret');
  assert.equal(page.config.wdgwarsApiKey, 'synthetic-wars-secret');
  assert.equal(posted(page.posts[0], 'homePsk'), undefined);
  assert.equal(posted(page.posts[0], 'wardriverPsk'), undefined);
});

test('HTTP save failure is surfaced and reload preserves the actual saved mode/token', async () => {
  const page = browser();
  await page.context.loadStatus();
  page.behavior.saveOk = false;
  page.get('operatingMode').value = 'uconsole';
  page.context.clearSavedSecret('collectionToken');
  await assert.rejects(page.context.doSave(), /synthetic save rejected/);
  assert.equal(page.config.operatingMode, 'normal');
  assert.equal(page.config.collectionToken, 'synthetic-collection-token');
  assert.equal(page.get('operatingMode').value, 'normal');
  assert.equal(page.get('collectionToken').value, '************');
  assert.equal(page.get('collectionToken').dataset.secretClear, undefined);
});

test('existing channel validation still blocks Save before any request', async () => {
  const page = browser();
  await page.context.loadStatus();
  page.behavior.profileOk = false;
  await assert.rejects(page.context.doSave(), /Choose at least one channel profile/);
  assert.equal(page.posts.length, 0);
});

test('Normal file list keeps legacy uploaded and pending presentation/actions', async () => {
  const page = browser();
  page.behavior.filesPayload.files = [{ name: '/uploaded/old.csv', size: 100, uploaded: true },
    { name: '/logs/pending.csv', size: 200, uploaded: false }];
  await page.context.loadFiles();
  const html = page.get('files').innerHTML;
  assert.ok(html.includes('class="file-badge uploaded">uploaded</span>'));
  assert.ok(html.includes('class="file-badge log">Not Uploaded</span>'));
  assert.ok(!html.includes('UConsole received'));
  assert.ok(!html.includes("wigleUploadOne('/uploaded/old.csv')"));
  assert.ok(html.includes("wigleUploadOne('/logs/pending.csv')"));
  assert.ok(html.includes("delFile('/uploaded/old.csv')"));
  assert.ok(!html.includes('Delete (protected)'));
  assert.equal(page.calls.filter(url => url.startsWith('/wigle/upload')).length, 0);
});

test('UConsole receipt badge never offers a direct upload, including already collected CSV', async () => {
  const page = browser();
  page.config.operatingMode = 'uconsole';
  page.behavior.externalUploadsAllowed = false;
  page.behavior.filesPayload.files = [{ name: '/uploaded/collected.csv', size: 100,
    uploaded: true, collectionReceived: true }];
  await page.context.loadFiles();
  const html = page.get('files').innerHTML;
  assert.ok(html.includes('UConsole received'));
  assert.ok(!html.includes('wigleUploadOne('));
  assert.ok(html.includes('Collection only'));
  assert.equal(page.calls.filter(url => url.startsWith('/wigle/upload')).length, 0);
});

test('unconfirmed UConsole files never receive a false Collection badge', async () => {
  for (const uploaded of [false, true]) {
    const page = browser();
    page.config.operatingMode = 'uconsole';
    page.behavior.externalUploadsAllowed = false;
    page.behavior.filesPayload.files = [{ name: uploaded ? '/uploaded/legacy.csv' : '/logs/pending.csv',
      size: 100, uploaded, collectionReceived: false }];
    await page.context.loadFiles();
    const html = page.get('files').innerHTML;
    assert.ok(!html.includes('UConsole received'));
    assert.equal(html.includes('Not Uploaded'), !uploaded);
    assert.equal(html.includes('wigleUploadOne('), false);
    assert.equal(page.calls.filter(url => url.startsWith('/wigle/upload')).length, 0);
  }
});

test('protected pending CSV explains disabled Delete and Collection-only delivery', async () => {
  const page = browser();
  page.behavior.externalUploadsAllowed = false;
  page.behavior.filesPayload.files = [{ name: '/logs/kept.csv', size: 100,
    uploaded: false, collectionReceived: false, collectionProtected: true }];
  await page.context.loadFiles();
  const html = page.get('files').innerHTML;
  assert.ok(html.includes('disabled title="Waiting for Collection acknowledgement and archive handover"'));
  assert.ok(html.includes('Delete (protected)'));
  assert.ok(!html.includes("delFile('/logs/kept.csv')"));
  assert.ok(!html.includes('wigleUploadOne('));
  assert.ok(html.includes('Collection only'));
  assert.equal(page.calls.filter(url => url.startsWith('/wigle/upload')).length, 0);
});

test('active boot mode controls both upload buttons and policy message independently of saved next mode', async () => {
  const page = browser();
  page.config.operatingMode = 'normal'; // saved selection; active mode is still UConsole
  page.behavior.externalUploadsAllowed = false;
  await page.context.loadStatus();
  assert.equal(page.get('wigleUploadAllBtn').disabled, true);
  assert.equal(page.get('wdgwarsUploadAllBtn').disabled, true);
  assert.equal(page.get('externalUploadPolicy').style.display, '');
  page.config.operatingMode = 'uconsole'; // saved next boot; active Normal still allows uploads
  page.behavior.externalUploadsAllowed = true;
  await page.context.loadStatus();
  assert.equal(page.get('wigleUploadAllBtn').disabled, false);
  assert.equal(page.get('wdgwarsUploadAllBtn').disabled, false);
  assert.equal(page.get('externalUploadPolicy').style.display, 'none');
  assert.deepEqual(page.errors, []);
});
