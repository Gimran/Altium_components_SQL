// A footprint dropdown can fall back to free text — the .PcbLib may have gained a pattern
// since the page loaded, and nothing should become unenterable because of that.
document.querySelectorAll('select[data-manual]').forEach((sel) => {
  sel.addEventListener('change', () => {
    if (sel.value !== sel.dataset.manual) return;
    const box = document.createElement('input');
    box.name = sel.name;
    box.setAttribute('maxlength', '255');
    box.placeholder = 'имя футпринта';
    sel.replaceWith(box);
    box.focus();
  });
});

// Column filters apply as soon as one is picked; page resets to the first.
document.querySelectorAll('#filterform select').forEach((sel) => {
  sel.addEventListener('change', () => sel.form.requestSubmit());
});

// Confirm destructive submits.
document.addEventListener('submit', (e) => {
  const msg = e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Ctrl+S saves the open record form.
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    const form = document.querySelector('form.record');
    if (form) { e.preventDefault(); form.requestSubmit(); }
  }
});

// "/" focuses the search box.
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
    const box = document.querySelector('.search input');
    if (box) { e.preventDefault(); box.focus(); box.select(); }
  }
});
