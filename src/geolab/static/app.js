// Sample catalogues, the attribute editor, and the run-status poller.

const ACTIVE_RUN_STATUSES = ['queued', 'running', 'cancelling'];
const POLL_INTERVAL_MS = 1000;
const RETRY_INTERVAL_MS = 2000;
const PREFERENCE_DEFAULT = 'exact';

const formPanel = document.querySelector('#product-form-panel');
const attributeRows = document.querySelector('#attribute-rows');

const addAttributeRow = (attribute = {}) => {
  if (!attributeRows) return;
  const row = document.createElement('div');
  row.className = 'attribute-row';
  row.innerHTML = `
    <label>Attribute<input name="attribute_name" required placeholder="Battery life" value="${attribute.name || ''}"></label>
    <label>Value<input name="attribute_value" required placeholder="32" value="${attribute.value || ''}"></label>
    <label>Unit<input name="attribute_unit" placeholder="hours" value="${attribute.unit || ''}"></label>
    <label>Better when<select name="attribute_preference"><option value="higher">Higher</option><option value="lower">Lower</option><option value="exact">Exact match</option></select></label>
    <button type="button" class="secondary remove-attribute" aria-label="Remove attribute">Remove</button>`;
  row.querySelector('select').value = attribute.preference || PREFERENCE_DEFAULT;
  row.querySelector('.remove-attribute').addEventListener('click', () => row.remove());
  attributeRows.appendChild(row);
};

const SAMPLE_LISTINGS = {
  strong: {
    product_name: 'Nimbus Buds Pro', category: 'wireless earbuds', price: '129',
    listing_title: 'Nimbus Buds Pro — ANC Wireless Earbuds, 32-Hour Battery',
    listing_description: 'Active noise cancellation, 8 hours per charge and 32 hours with the case. Each earbud weighs 4.8 g. Includes USB-C charging and a 24-month warranty.',
    attributes: [
      {name: 'Total battery life', value: '32', unit: 'hours', preference: 'higher'},
      {name: 'Single-charge battery life', value: '8', unit: 'hours', preference: 'higher'},
      {name: 'Earbud weight', value: '4.8', unit: 'g', preference: 'lower'},
      {name: 'Noise cancellation', value: 'Active', unit: '', preference: 'exact'},
      {name: 'Warranty', value: '24', unit: 'months', preference: 'higher'},
    ],
  },
  weak: {
    product_name: 'Nimbus Buds Pro', category: 'wireless earbuds', price: '129',
    listing_title: 'Nimbus Buds Pro',
    listing_description: 'Premium wireless sound for your day. Comfortable, stylish and made for music lovers.',
    attributes: [
      {name: 'Total battery life', value: '32', unit: 'hours', preference: 'higher'},
      {name: 'Single-charge battery life', value: '8', unit: 'hours', preference: 'higher'},
      {name: 'Earbud weight', value: '4.8', unit: 'g', preference: 'lower'},
      {name: 'Noise cancellation', value: 'Active', unit: '', preference: 'exact'},
      {name: 'Warranty', value: '24', unit: 'months', preference: 'higher'},
    ],
  },
  shoes: {
    product_name: 'TrailStep Flow', category: 'road running shoes', price: '159',
    listing_title: 'TrailStep Flow — Lightweight Road Running Shoes, 8 mm Drop',
    listing_description: 'Breathable mesh road-running shoes weighing 265 g per shoe in US size 9. Features an 8 mm heel-to-toe drop, cushioned foam midsole and durable rubber outsole.',
    attributes: [
      {name: 'Shoe weight', value: '265', unit: 'g', preference: 'lower'},
      {name: 'Heel-to-toe drop', value: '8', unit: 'mm', preference: 'exact'},
      {name: 'Upper material', value: 'Breathable mesh', unit: '', preference: 'exact'},
      {name: 'Outsole', value: 'Rubber', unit: '', preference: 'exact'},
      {name: 'Warranty', value: '12', unit: 'months', preference: 'higher'},
    ],
  },
  chair: {
    product_name: 'ArcSeat Ergo', category: 'ergonomic office chair', price: '349',
    listing_title: 'ArcSeat Ergo — Mesh Office Chair with Adjustable Lumbar Support',
    listing_description: 'Ergonomic mesh chair with adjustable lumbar support, 4D armrests and a 45–55 cm seat-height range. Supports up to 150 kg and includes a 5-year warranty.',
    attributes: [
      {name: 'Maximum supported weight', value: '150', unit: 'kg', preference: 'higher'},
      {name: 'Armrests', value: '4D adjustable', unit: '', preference: 'exact'},
      {name: 'Seat height range', value: '45–55', unit: 'cm', preference: 'exact'},
      {name: 'Back material', value: 'Mesh', unit: '', preference: 'exact'},
      {name: 'Warranty', value: '5', unit: 'years', preference: 'higher'},
    ],
  },
  serum: {
    product_name: 'ClearKind Balance', category: 'face serum', price: '32',
    listing_title: 'ClearKind Balance — 10% Niacinamide Face Serum, 30 ml',
    listing_description: 'A fragrance-free 30 ml serum with 10% niacinamide and 1% zinc PCA. Formulated for oily and combination skin. Dermatologist tested and packaged in a glass dropper bottle.',
    attributes: [
      {name: 'Niacinamide concentration', value: '10', unit: '%', preference: 'exact'},
      {name: 'Zinc PCA concentration', value: '1', unit: '%', preference: 'exact'},
      {name: 'Volume', value: '30', unit: 'ml', preference: 'higher'},
      {name: 'Fragrance', value: 'Fragrance-free', unit: '', preference: 'exact'},
      {name: 'Skin type', value: 'Oily and combination', unit: '', preference: 'exact'},
    ],
  },
  backpack: {
    product_name: 'RoamLite 28', category: 'travel backpack', price: '119',
    listing_title: 'RoamLite 28 — 28 L Carry-On Travel Backpack with Laptop Sleeve',
    listing_description: 'A 900 g, 28 L travel backpack sized 48 × 30 × 18 cm. Includes a padded sleeve for laptops up to 16 inches, water-resistant fabric and a 24-month warranty.',
    attributes: [
      {name: 'Capacity', value: '28', unit: 'L', preference: 'higher'},
      {name: 'Weight', value: '900', unit: 'g', preference: 'lower'},
      {name: 'Maximum laptop size', value: '16', unit: 'inches', preference: 'higher'},
      {name: 'Water resistance', value: 'Water-resistant', unit: '', preference: 'exact'},
      {name: 'Warranty', value: '24', unit: 'months', preference: 'higher'},
    ],
  },
};

document.querySelector('#toggle-product-form')?.addEventListener('click', () => {
  formPanel.hidden = !formPanel.hidden;
  if (!formPanel.hidden && !attributeRows.children.length) addAttributeRow();
});
document.querySelector('#add-attribute')?.addEventListener('click', () => addAttributeRow());
document.querySelectorAll('[data-sample]').forEach((button) => button.addEventListener('click', () => {
  const sample = SAMPLE_LISTINGS[button.dataset.sample];
  formPanel.hidden = false;
  for (const [name, value] of Object.entries(sample)) {
    if (name !== 'attributes') formPanel.querySelector(`[name="${name}"]`).value = value;
  }
  attributeRows.replaceChildren();
  sample.attributes.forEach(addAttributeRow);
}));

const root = document.querySelector('[data-run-id]');

if (root) {
  const runId = root.dataset.runId;
  const cancelForm = document.querySelector('#cancel-form');
  let wasActive = ACTIVE_RUN_STATUSES.includes(document.querySelector('#run-status').textContent.trim());

  const renderRunStatus = (data) => {
    const status = document.querySelector('#run-status');
    status.textContent = data.run.status;
    status.className = `status ${data.run.status}`;
    document.querySelector('#progress').textContent = `${data.run.progress}%`;
    document.querySelector('#run-detail').textContent = data.detail;
    for (const job of data.jobs) {
      const row = document.querySelector(`[data-job="${job.stage}"]`);
      row.querySelector('.status').className = `status ${job.status}`;
      row.querySelector('.status').textContent = job.status;
      row.querySelector('small').textContent = job.completed_at || (job.status === 'running' ? `${job.seconds_idle || 0}s since update` : '');
    }
    document.querySelector('#live-log').textContent = data.jobs
      .filter((job) => job.log_text || job.error_text)
      .map((job) => `${job.stage}: ${job.log_text || ''}${job.error_text ? `\nERROR: ${job.error_text}` : ''}`)
      .join('\n\n');
    const button = document.querySelector('#cancel-button');
    if (button && data.run.status === 'cancelling') {
      button.disabled = true;
      button.textContent = 'Cancelling…';
    }
    const active = ACTIVE_RUN_STATUSES.includes(data.run.status);
    if (wasActive && !active) window.location.reload();
    wasActive = active;
    return active;
  };

  const pollRunStatus = async () => {
    try {
      const response = await fetch(`/api/runs/${runId}/status`, {cache: 'no-store'});
      if (!response.ok) throw new Error(`Status HTTP ${response.status}`);
      if (renderRunStatus(await response.json())) setTimeout(pollRunStatus, POLL_INTERVAL_MS);
    } catch (error) {
      document.querySelector('#run-detail').textContent = `Dashboard connection error: ${error.message}. Retrying…`;
      setTimeout(pollRunStatus, RETRY_INTERVAL_MS);
    }
  };

  cancelForm?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = document.querySelector('#cancel-button');
    button.disabled = true;
    button.textContent = 'Cancelling…';
    try {
      const response = await fetch(cancelForm.action, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
      if (!response.ok) throw new Error(`Cancel HTTP ${response.status}`);
      await pollRunStatus();
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Cancel';
      document.querySelector('#run-detail').textContent = `Cancel failed: ${error.message}`;
    }
  });

  pollRunStatus();
}
