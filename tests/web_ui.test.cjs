// Run with: node --test tests/web_ui.test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../jevgame/web/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

test('entire game script parses', () => {
  new vm.Script(script);
});

test('mission sliders update readouts and shared Jev/manual request parameters', () => {
  const elements = {};
  for (const id of ['distanceSlider', 'fuelSlider', 'distanceReadout', 'fuelReadout',
                    'attitudeThresholdSlider', 'throttleThresholdSlider',
                    'attitudeThresholdReadout', 'throttleThresholdReadout', 'seedInput']) {
    elements[id] = { value: '', addEventListener(event, callback) { this[event] = callback; } };
  }
  elements.distanceSlider.value = '60';
  elements.fuelSlider.value = '100';
  const context = vm.createContext({ document: { getElementById: id => elements[id] },
                                    window: { addEventListener() {} }, URLSearchParams });
  vm.runInContext('let attitudeThreshold = .55, throttleThreshold = .55;', context);
  vm.runInContext(script.slice(script.indexOf('// Mission controls: read visible values'),
                              script.indexOf('function enterReplayMode()')), context);
  assert.equal(vm.runInContext('missionParams().toString()', context), 'distance=60&fuel=100');
  // Restoring a form or setting its values need not dispatch an input event.
  elements.distanceSlider.value = '480';
  elements.fuelSlider.value = '25';
  elements.seedInput.value = ' 42 ';
  assert.equal(vm.runInContext('missionParams().toString()', context), 'distance=480&fuel=25&seed=42');
  assert.equal(elements.distanceReadout.textContent, '480 m');
  assert.equal(elements.fuelReadout.textContent, '25 units');
  assert.equal(vm.runInContext('missionParams().toString()', context), 'distance=480&fuel=25&seed=42');
  assert.equal((script.match(/const params = missionParams\(\);/g) || []).length, 2);
  assert.match(html, /id="distanceSlider" min="20" max="480"/);
  elements.distanceSlider.value = '240';
  elements.distanceSlider.input();
  elements.fuelSlider.value = '40';
  elements.fuelSlider.change();
  assert.equal(elements.distanceReadout.textContent, '240 m');
  assert.equal(elements.fuelReadout.textContent, '40 units');
  assert.equal(vm.runInContext('missionParams().toString()', context), 'distance=240&fuel=40&seed=42');
});

test('command log renders both independent control decisions including confidence and fallbacks', () => {
  const log = {};
  const context = vm.createContext({ document: { getElementById: () => log }, commandLogEntries: [{
    tick: 7, attitude: 'hold_attitude', throttle: 'thrust_75',
    attitudeOutcome: 'discarded_low_confidence', throttleOutcome: 'applied',
    attitudeConfidence: .4, throttleConfidence: .98,
    groundSpeed: 2, descentSpeed: 1, distanceToTarget: 60,
  }] });
  vm.runInContext(script.slice(script.indexOf('function actionLogClass('), script.indexOf('function pct(')), context);
  vm.runInContext('renderCommandLog()', context);
  assert.equal((log.innerHTML.match(/class="clr-control link-/g) || []).length, 2);
  for (const text of ['Attitude', 'Throttle', 'hold_attitude', 'thrust_75', '40%', '98%', 'outcome-discarded']) {
    assert.ok(log.innerHTML.includes(text), text);
  }
  assert.ok(!log.innerHTML.includes('undefined'));
});

test('actual Jev and manual launch functions send visible values without slider events', async () => {
  const elements = new Map();
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { value: '', textContent: '', addEventListener() {} });
    return elements.get(id);
  } };
  document.getElementById('distanceSlider').value = '480';
  document.getElementById('fuelSlider').value = '25';
  const urls = [];
  const context = vm.createContext({ document, URLSearchParams,
    window: { addEventListener() {} },
    EventSource: class { constructor(url) { urls.push(url); } close() {} },
    fetch: async url => { urls.push(url); return { json: async () => ({
      episode_id: 'human-test', frame: { x_offset_from_pad_center: 480, fuel: 25, tick: 0 },
    }) }; },
    setInterval: () => 1, clearInterval() {}, stopManual() {}, resetJevLink() {},
    setReplayControlsEnabled() {}, drawTimeline() {}, beginTransition() {},
    logHumanCommand() {}, manualTick() {},
  });
  vm.runInContext(`let eventSource = null, timelineRedrawTimer = null;
    let mode, meta, frames, actions, prevDisplay, targetDisplay, currentAction;
    let manualEpisodeId, manualTimer;
    const MANUAL_DT_MS = 60, attitudeThreshold = .55, throttleThreshold = .55;`, context);
  for (const name of ['readMissionControls', 'missionParams', 'showFlightStart', 'flyNewEpisode', 'playManually']) {
    const pattern = new RegExp('(?:async )?function ' + name + '\\([^]*?\\n\\}');
    vm.runInContext(script.match(pattern)[0], context);
  }
  vm.runInContext('flyNewEpisode()', context);
  await vm.runInContext('playManually()', context);
  assert.equal(urls.length, 2);
  assert.match(urls[0], /^\/api\/episode\/stream\?/);
  assert.match(urls[1], /^\/api\/manual\/start\?/);
  for (const url of urls) {
    const params = new URL(url, 'http://localhost').searchParams;
    assert.equal(params.get('distance'), '480');
    assert.equal(params.get('fuel'), '25');
  }
  assert.equal(document.getElementById('flightStart').textContent,
               'Flight started: 480 m horizontally from pad · 25 fuel units');
});

test('camera keeps high-altitude ship and ground in view during long approaches', () => {
  const context = vm.createContext({ W: 900, H: 620 });
  vm.runInContext(script.match(/const WORLD_Y_MIN[\s\S]*?function worldToScreen[^]*?\n\}/)[0], context);
  vm.runInContext('cameraYMax = Math.max(WORLD_Y_MAX, 170 + 12)', context);
  const ship = vm.runInContext('worldToScreen(480, 170, 480)', context);
  const ground = vm.runInContext('worldToScreen(480, 0, 480)', context);
  assert.equal(ship[0], 450);
  assert.ok(ship[1] > 0 && ship[1] < ground[1]);
  assert.ok(ground[1] < 620);
});
