// Progressive enhancement only. Reading and individual disclosures work without JavaScript.
document.querySelectorAll('.transcript-actions').forEach((actions) => {
  actions.hidden = false;
  actions.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-disclosures]');
    if (!button) return;
    const open = button.dataset.disclosures === 'open';
    document.querySelectorAll('.transcript-tool').forEach((tool) => { tool.open = open; });
  });
});
