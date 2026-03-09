document.addEventListener('DOMContentLoaded', () => {

  // ── Income / Deduction card checkboxes ──────────────────────────────────
  // Listen on the checkbox `change` event (not click on the label) to avoid
  // the double-toggle that happens when a <label> wraps its <input>.
  document.querySelectorAll('.income-card input[type="checkbox"]').forEach(cb => {
    // Sync active class on first paint (page was reloaded with selections)
    cb.closest('.income-card').classList.toggle('income-card--active', cb.checked);

    cb.addEventListener('change', () => {
      cb.closest('.income-card').classList.toggle('income-card--active', cb.checked);
    });
  });

  // ── Radio pills ──────────────────────────────────────────────────────────
  document.querySelectorAll('.radio-group').forEach(group => {
    group.querySelectorAll('.radio-pill input[type="radio"]').forEach(input => {
      // Sync on load
      if (input.checked) input.closest('.radio-pill').classList.add('radio-pill--active');

      input.addEventListener('change', () => {
        group.querySelectorAll('.radio-pill').forEach(p => p.classList.remove('radio-pill--active'));
        if (input.checked) input.closest('.radio-pill').classList.add('radio-pill--active');
      });
    });
  });

  // ── Drag-over highlight ──────────────────────────────────────────────────
  document.querySelectorAll('.upload-zone').forEach(zone => {
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('upload-zone--over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('upload-zone--over'));
    zone.addEventListener('drop', () => zone.classList.remove('upload-zone--over'));
  });

});
