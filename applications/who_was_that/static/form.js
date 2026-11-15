/* ==========================================================================
   Who Was That? — helpers for the server-rendered card and form pages.
   Adds/removes relative rows, searches addresses through the app's own
   same-origin endpoint, and confirms destructive forms. The pages work
   without this file; it only saves typing.
   ========================================================================== */

(function () {
  'use strict';

  const page = document.querySelector('.wwt-page');
  const BASE = (
    (page && page.dataset.wwtBase) || '/who-was-that'
  ).replace(/\/+$/, '');

  /* ---------- confirm before deleting ---------- */
  for (const form of document.querySelectorAll('form[data-confirm]')) {
    form.addEventListener('submit', function (event) {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  }

  /* ---------- relative rows ---------- */
  const list = document.getElementById('wwt-relatives');
  const template = document.getElementById('wwt-relative-template');

  function wireRelative(block) {
    const remove = block.querySelector('.wwt-rel-remove');
    if (remove) {
      remove.addEventListener('click', function () {
        block.remove();
      });
    }

    const picker = block.querySelector('.wwt-rel-pick');
    if (picker) {
      picker.addEventListener('change', function () {
        const option = picker.options[picker.selectedIndex];
        if (!option || !option.value) return;
        const name = block.querySelector('input[name="relative_name"]');
        const lat = block.querySelector('input[name="relative_lat"]');
        const lng = block.querySelector('input[name="relative_lng"]');
        const address = block.querySelector('input[name="relative_address"]');
        if (name) name.value = option.dataset.name || '';
        if (lat) lat.value = option.dataset.lat || '';
        if (lng) lng.value = option.dataset.lng || '';
        if (address) address.value = option.dataset.address || '';
      });
    }
  }

  if (list) {
    list.querySelectorAll('.wwt-rel-block').forEach(wireRelative);
  }

  const addButton = document.getElementById('wwt-add-relative');
  if (addButton && list && template) {
    addButton.addEventListener('click', function () {
      const fragment = template.content.cloneNode(true);
      const block = fragment.querySelector('.wwt-rel-block');
      list.appendChild(fragment);
      wireRelative(block);
    });
  }

  /* ---------- address search ---------- */
  const searchButton = document.getElementById('wwt-address-go');
  const searchInput = document.getElementById('wwt-address-query');
  const results = document.getElementById('wwt-address-results');

  async function search() {
    if (!searchButton || !searchInput || !results) return;
    const query = searchInput.value.trim();
    if (!query) return;

    results.innerHTML = '';
    const note = document.createElement('p');
    note.className = 'wwt-address-note';
    note.textContent = 'Searching…';
    results.appendChild(note);

    let payload = null;
    try {
      const response = await fetch(
        BASE + '/api/geocode?q=' + encodeURIComponent(query),
        { credentials: 'same-origin', headers: { Accept: 'application/json' } }
      );
      payload = await response.json();
    } catch (error) {
      payload = null;
    }

    results.innerHTML = '';
    if (!payload || payload.ok === false || !(payload.results || []).length) {
      const message = document.createElement('p');
      message.className = 'wwt-address-note';
      message.textContent = payload && payload.error
        ? payload.error + '. You can type the coordinates or drop the pin on the map.'
        : "Couldn't find that address. You can type the coordinates instead, or drop the pin on the map.";
      results.appendChild(message);
      return;
    }

    for (const result of payload.results) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'wwt-address-item';
      button.textContent = result.address || result.lat + ', ' + result.lng;
      button.addEventListener('click', function () {
        const address = document.getElementById('wwt-address');
        const lat = document.getElementById('wwt-lat');
        const lng = document.getElementById('wwt-lng');
        if (address) address.value = result.address || '';
        if (lat) lat.value = result.lat;
        if (lng) lng.value = result.lng;
        results.innerHTML = '';
        const note = document.createElement('p');
        note.className = 'wwt-address-note';
        note.textContent = 'Address filled in. Save the card to keep it.';
        results.appendChild(note);
      });
      results.appendChild(button);
    }
  }

  if (searchButton) {
    searchButton.addEventListener('click', search);
  }
  if (searchInput) {
    searchInput.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      search();
    });
  }
})();
