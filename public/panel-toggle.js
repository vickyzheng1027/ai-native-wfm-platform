(function (root) {
  function toggleCollapsiblePanel(toggle, collapsedPanels) {
    const surface = toggle.closest('.surface');
    const body = surface && surface.querySelector('.collapsible-body');
    if (!body) return false;
    const key = `${toggle.dataset.taskId}:${toggle.dataset.togglePanel}`;
    const collapsed = !body.hidden;
    body.hidden = collapsed;
    collapsedPanels[key] = collapsed;
    toggle.setAttribute('aria-expanded', String(!collapsed));
    return collapsed;
  }

  root.toggleCollapsiblePanel = toggleCollapsiblePanel;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { toggleCollapsiblePanel };
  }
})(typeof window !== 'undefined' ? window : globalThis);
