(() => {
  const form = document.querySelector('[data-requirements-form]');
  if (!form) return;
  const start = form.elements.start_date;
  const end = form.elements.end_date;
  const days = form.elements.day_count;
  function updateDays() {
    const first = Date.parse(start.value + 'T00:00:00Z');
    const last = Date.parse(end.value + 'T00:00:00Z');
    const valid = Number.isFinite(first) && Number.isFinite(last) && last >= first;
    days.readOnly = valid;
    if (valid) days.value = Math.round((last - first) / 86400000) + 1;
  }
  start.addEventListener('change', updateDays);
  end.addEventListener('change', updateDays);
  updateDays();
})();
