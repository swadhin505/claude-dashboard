/* Transport and UI state only. All counts, prices and charts are rendered by Python. */
const scanButton = document.querySelector('#scan-button');
const scanStatus = document.querySelector('#scan-status');
if (scanButton && scanStatus) {
  scanButton.addEventListener('click', async () => {
    const idleLabel = scanButton.textContent;
    scanButton.disabled = true;
    scanButton.setAttribute('aria-busy', 'true');
    scanButton.textContent = 'Scanning local sources…';
    scanStatus.hidden = false;
    scanStatus.textContent = 'Reading local sources and pricing new revisions. Existing reports remain available in other tabs.';
    try {
      const response = await fetch('/api/scan', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'X-Scan-Token': document.querySelector('meta[name="scan-token"]').content }
      });
      const result = await response.json();
      if (response.ok) {
        window.location.reload();
      } else {
        scanStatus.textContent = result.message || result.detail || 'Refresh failed. Check Data health and retry.';
      }
    } catch {
      scanStatus.textContent = 'Connection lost. The scan may still be running. Reload this page to check before retrying.';
    } finally {
      scanButton.disabled = false;
      scanButton.removeAttribute('aria-busy');
      scanButton.textContent = idleLabel;
    }
  });
}
