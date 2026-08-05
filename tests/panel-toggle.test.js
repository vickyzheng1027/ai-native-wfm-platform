const test = require('node:test');
const assert = require('node:assert/strict');
const { toggleCollapsiblePanel } = require('../public/panel-toggle.js');

test('Agent 面板可以折叠、保持状态并再次展开', () => {
  const body = { hidden: false };
  const attributes = {};
  const surface = { querySelector: selector => selector === '.collapsible-body' ? body : null };
  const toggle = {
    dataset: { taskId: 'task-1', togglePanel: 'steps' },
    closest: selector => selector === '.surface' ? surface : null,
    setAttribute: (name, value) => { attributes[name] = value; }
  };
  const state = {};

  assert.equal(toggleCollapsiblePanel(toggle, state), true);
  assert.equal(body.hidden, true);
  assert.equal(attributes['aria-expanded'], 'false');
  assert.equal(state['task-1:steps'], true);

  assert.equal(toggleCollapsiblePanel(toggle, state), false);
  assert.equal(body.hidden, false);
  assert.equal(attributes['aria-expanded'], 'true');
  assert.equal(state['task-1:steps'], false);
});
