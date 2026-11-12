import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../pliqra/static/theme.js', import.meta.url), 'utf8');
function browser({saved = null, dark = false, blocked = false} = {}) {
  const controls = [{value: ''}, {value: ''}];
  const root = {dataset: {}, style: {}};
  const meta = {setAttribute: (_, value) => { meta.content = value; }};
  const events = {}, documentEvents = {};
  const media = {matches: dark, addEventListener: (_, handler) => { media.change = handler; }};
  const storage = {
    getItem() { if (blocked) throw new Error('Storage blocked'); return saved; },
    setItem(key, value) { if (blocked) throw new Error('Storage blocked'); saved = value; },
  };
  const context = vm.createContext({
    document: {
      documentElement: root,
      querySelector: () => meta,
      querySelectorAll: () => controls,
      addEventListener: (type, handler) => { documentEvents[type] = handler; },
    },
    window: {localStorage: storage, matchMedia: () => media, addEventListener: (type, handler) => { events[type] = handler; }},
  });
  vm.runInContext(source, context);
  return {root, meta, controls, events, documentEvents, media, storage, theme: context.window.PliqraTheme, saved: () => saved};
}

test('saved explicit preferences override the system before the console starts', () => {
  for (const saved of ['light', 'dark']) {
    const b = browser({saved, dark: saved === 'light'});
    assert.equal(b.root.dataset.theme, saved);
    assert.equal(b.root.style.colorScheme, saved);
    assert.equal(b.meta.content, saved === 'dark' ? '#16161a' : '#f7f7f8');
    assert.ok(b.controls.every(control => control.value === saved));
    assert.match(b.theme.control(), new RegExp(`value="${saved}" selected`));
  }
});

test('missing or invalid preferences follow system changes without saving an override', () => {
  for (const saved of [null, 'unexpected', 'system']) {
    const b = browser({saved});
    assert.equal(b.root.dataset.theme, 'light');
    b.media.matches = true;
    b.media.change();
    assert.equal(b.root.dataset.theme, 'dark');
    assert.ok(b.controls.every(control => control.value === 'system'));
    assert.equal(b.saved(), saved);
  }
});

test('changing a select synchronizes controls and persists across page loads', () => {
  const b = browser();
  b.documentEvents.change({target: {matches: selector => selector === '[data-theme-select]', value: 'dark'}});
  assert.ok(b.controls.every(control => control.value === 'dark'));
  assert.equal(browser({saved: b.saved()}).root.dataset.theme, 'dark');
  b.media.matches = false;
  b.media.change();
  assert.equal(b.root.dataset.theme, 'dark');
  b.theme.set('system');
  assert.equal(b.root.dataset.theme, 'light');
  assert.equal(b.saved(), 'system');
});

test('unavailable storage still allows a choice for this page', () => {
  const b = browser({blocked: true, dark: true});
  assert.equal(b.root.dataset.theme, 'dark');
  b.theme.set('light');
  assert.equal(b.root.dataset.theme, 'light');
});

test('cross-tab changes synchronize preferences; removal returns to the system', () => {
  const b = browser();
  b.events.storage({key: 'pliqra.appearance', newValue: 'dark', storageArea: b.storage});
  assert.equal(b.root.dataset.theme, 'dark');
  b.events.storage({key: 'pliqra.appearance', newValue: 'light', storageArea: {}});
  b.events.storage({key: 'unrelated', newValue: 'light', storageArea: b.storage});
  assert.equal(b.root.dataset.theme, 'dark');
  b.events.storage({key: 'pliqra.appearance', newValue: null, storageArea: b.storage});
  assert.equal(b.root.dataset.theme, 'light');
  b.theme.set('dark');
  b.events.storage({key: null, newValue: null, storageArea: b.storage});
  assert.equal(b.root.dataset.theme, 'light');
});

test('unrelated form changes do not change the theme', () => {
  const b = browser({saved: 'light'});
  b.documentEvents.change({target: {matches: () => false, value: 'dark'}});
  assert.equal(b.root.dataset.theme, 'light');
});

test('theme initialization precedes styles and deferred application scripts', () => {
  const html = readFileSync(new URL('../pliqra/static/index.html', import.meta.url), 'utf8');
  const boot = html.indexOf('<script src="/static/theme.js"></script>');
  assert.ok(boot > 0 && boot < html.indexOf('rel="stylesheet"'));
  assert.ok(boot < html.indexOf('src="/static/app.js"'));
});
