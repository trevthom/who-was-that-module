/* ==========================================================================
   Who Was That? — the map screen.

   Ported from the original app.js + ui.js. Everything that depended on the
   old relay backend is gone: no identity, no relays, no chat, no shared
   cards, no notifications.
   People, relatives and settings now come from same-origin Flask endpoints
   under this app's own prefix and are stored in the hub's SQLite database.
   ========================================================================== */

(function () {
  'use strict';

  const app = document.getElementById('wwt-app');
  if (!app) return;

  const BASE = (app.dataset.base || '/who-was-that').replace(/\/+$/, '');
  const RELATIVE_TYPES = [
    'Parent', 'Child', 'Sibling', 'Spouse',
    'Grandparent', 'Grandchild', 'Cousin', 'Friend', 'Colleague', 'Other',
  ];
  const MAP_DEFAULT = { lat: 30, lng: 0, zoom: 2 };
  const MAP_LOCATED_ZOOM = 16;

  const state = {
    self: null,
    people: [],
    settings: { theme: 'dark', find_me_on_login: false, find_me_asked: false },
  };

  let map = null;
  let markersLayer = null;
  let userDot = null;
  let draftPin = null;
  let pickHandler = null;
  let tilesWarned = false;

  let editorDraft = null;
  let editorIsSelf = false;
  let editorId = null;

  let panelOpen = false;
  let mapBusy = false;
  let pollBusy = false;
  let lastRemoteFingerprint = "";

  /* ---------- tiny helpers ---------- */

  const $ = (id) => document.getElementById(id);

  function node(html) {
    const template = document.createElement('template');
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value).replace(
      /[&<>"']/g,
      (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
      }[character])
    );
  }

  function initials(name) {
    const cleaned = (name || '').trim();
    if (!cleaned) return '?';
    return cleaned.split(/\s+/).slice(0, 2).map((word) => word[0].toUpperCase()).join('');
  }

  function deepCopy(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function firstName(value) {
    return ((value || '').trim().split(/\s+/)[0] || '').toLowerCase();
  }

  function findPerson(id) {
    if (id === 'self') return state.self;
    return state.people.find((person) => String(person.id) === String(id)) || null;
  }

  function findRelativeCard(relative) {
    if (!relative) return null;
    if (relative.linked_person_id) {
      const linked = findPerson(relative.linked_person_id);
      if (linked) return linked;
    }
    if (relative.lat === null || !relative.name) return null;
    return (
      state.people.find(
        (person) =>
          person.location &&
          person.name &&
          person.name.toLowerCase() === relative.name.toLowerCase() &&
          Math.abs(person.location.lat - relative.lat) < 0.000001 &&
          Math.abs(person.location.lng - relative.lng) < 0.000001
      ) || null
    );
  }

  /* ---------- network ---------- */

  async function api(path, options) {
    const settings = options || {};
    const init = {
      method: settings.method || 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    };
    if (settings.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(settings.body);
    }

    let response;
    try {
      response = await fetch(BASE + path, init);
    } catch (error) {
      throw new Error('Could not reach Thompson. Check the connection and try again.');
    }

    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }

    if (!response.ok || !data || data.ok === false) {
      throw new Error((data && data.error) || 'Something went wrong.');
    }
    return data;
  }

  function remoteFingerprint(next) {
    return JSON.stringify({
      self: next.self || null,
      people: Array.isArray(next.people) ? next.people : [],
    });
  }

  function applyState(next) {
    if (!next) return;
    state.self = next.self || null;
    state.people = Array.isArray(next.people) ? next.people : [];
    state.settings = next.settings || state.settings;
    lastRemoteFingerprint = remoteFingerprint(next);
    applyTheme(state.settings.theme);
  }

  /** Adopt people another device changed, without touching display settings. */
  function applyRemoteState(next) {
    state.self = next.self || null;
    state.people = Array.isArray(next.people) ? next.people : [];
    lastRemoteFingerprint = remoteFingerprint(next);
    renderPins();
  }

  /**
   * Only pull in remote people while nobody is in the middle of something:
   * no open panel or editor, no pin being placed, no map drag, no typing.
   * A refresh then cannot move what the person is looking at or typing.
   */
  function canApplyRemote() {
    if (document.hidden) return false;
    if (panelOpen || document.querySelector('.wwt-modal')) return false;
    if (pickHandler || draftPin || mapBusy) return false;
    const loading = $('app-loading');
    if (loading && !loading.classList.contains('hidden')) return false;
    const active = document.activeElement;
    if (
      active &&
      (active.matches('input, textarea, select') || active.isContentEditable)
    ) {
      return false;
    }
    return true;
  }

  async function pollState() {
    if (document.hidden || pollBusy) return;
    pollBusy = true;
    try {
      const data = await api('/api/state');
      if (!data || !data.state) return;
      if (!canApplyRemote()) return;
      if (remoteFingerprint(data.state) === lastRemoteFingerprint) return;
      applyRemoteState(data.state);
    } catch (error) {
      // Keep the last snapshot; the next tick tries again.
    } finally {
      pollBusy = false;
    }
  }

  /* ---------- theme ---------- */

  const SUN_ICON =
    '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.6 4.6l1.8 1.8M17.6 17.6l1.8 1.8M19.4 4.6l-1.8 1.8M6.4 17.6l-1.8 1.8"/></svg>';
  const MOON_ICON =
    '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4 7 7 0 1 0 20 14.5z"/></svg>';

  function applyTheme(theme) {
    const next = theme === 'light' ? 'light' : 'dark';
    app.dataset.theme = next;
    app.classList.toggle('wwt-map-dark', next === 'dark');
    updateThemeButton(next);
  }

  function updateThemeButton(theme) {
    const button = $('btn-theme');
    if (!button) return;
    const dark = theme === 'dark';
    button.innerHTML = dark ? SUN_ICON : MOON_ICON;
    button.title = dark ? 'Light Mode' : 'Dark Mode';
    button.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode');
  }

  async function setTheme(theme) {
    state.settings.theme = theme;
    applyTheme(theme);
    try {
      const data = await api('/api/settings', { method: 'POST', body: { theme } });
      state.settings = data.settings;
    } catch (error) {
      toast(error.message, true);
    }
  }

  /* ---------- map ---------- */

  function initMap() {
    const container = $('map');
    if (!container) return;
    if (typeof window.L === 'undefined') {
      container.innerHTML =
        '<div class="wwt-empty">The map could not load. You can still add and edit people from the people list.</div>';
      return;
    }

    map = window.L.map(container, { zoomControl: false, attributionControl: false })
      .setView([MAP_DEFAULT.lat, MAP_DEFAULT.lng], MAP_DEFAULT.zoom);
    map.on('dragstart zoomstart', () => { mapBusy = true; });
    map.on('dragend zoomend', () => { mapBusy = false; });
    window.L.control.zoom({ position: 'bottomleft' }).addTo(map);

    const tiles = window.L.tileLayer(BASE + '/tiles/{z}/{x}/{y}.png', { maxZoom: 19 });
    tiles.on('tileerror', () => {
      if (tilesWarned) return;
      tilesWarned = true;
      toast('Map tiles are unavailable — you can still add and edit people.', true);
    });
    tiles.addTo(map);

    markersLayer = window.L.layerGroup().addTo(map);
    map.on('click', (event) => {
      if (!pickHandler) return;
      const handler = pickHandler;
      pickHandler = null;
      container.classList.remove('picking');
      handler({
        lat: Number(event.latlng.lat.toFixed(6)),
        lng: Number(event.latlng.lng.toFixed(6)),
      });
    });

    setTimeout(() => map && map.invalidateSize(), 200);
  }

  function invalidateMap() {
    if (map) setTimeout(() => map.invalidateSize(), 50);
  }

  function panTo(lat, lng, zoom) {
    if (map) map.setView([lat, lng], zoom || MAP_LOCATED_ZOOM);
  }

  function showUserLocation(location) {
    if (!map) return;
    const icon = window.L.divIcon({
      className: '',
      html: '<div class="wwt-userdot"><span class="wwt-userdot-core"></span></div>',
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    });
    if (userDot) userDot.setLatLng([location.lat, location.lng]);
    else {
      userDot = window.L.marker([location.lat, location.lng], {
        icon,
        interactive: false,
        zIndexOffset: -100,
      }).addTo(map);
    }
  }

  function showDraftPin(location) {
    if (!map) return;
    const icon = window.L.divIcon({
      className: '',
      html: '<div class="wwt-pin wwt-pin-draft"><span class="wwt-pin-dot"></span></div>',
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    });
    if (draftPin) draftPin.setLatLng([location.lat, location.lng]);
    else {
      draftPin = window.L.marker([location.lat, location.lng], {
        icon,
        interactive: false,
        zIndexOffset: 500,
      }).addTo(map);
    }
    map.setView([location.lat, location.lng], Math.max(map.getZoom(), MAP_LOCATED_ZOOM), {
      animate: true,
    });
  }

  function clearDraftPin() {
    if (draftPin && map) map.removeLayer(draftPin);
    draftPin = null;
  }

  function enablePickMode(callback) {
    if (!map) {
      toast('The map is unavailable — type the coordinates instead.', true);
      callback(null);
      return;
    }
    pickHandler = callback;
    $('map').classList.add('picking');
  }

  function cancelPickMode() {
    pickHandler = null;
    const container = $('map');
    if (container) container.classList.remove('picking');
  }

  function renderPins() {
    if (!markersLayer) return;
    markersLayer.clearLayers();

    if (state.self && state.self.location) {
      addPersonMarker(state.self, 'self', () => openCardView('self'));
    }

    const groups = new Map();
    for (const person of state.people) {
      if (!person.location) continue;
      const key =
        person.address && person.address.trim()
          ? 'a:' + person.address.trim().toLowerCase()
          : 'c:' + person.location.lat + ',' + person.location.lng;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(person);
    }

    for (const group of groups.values()) {
      if (group.length > 1) {
        addClusterMarker(group[0].location, group.length, () => openClusterList(group));
      } else {
        const person = group[0];
        addPersonMarker(person, 'person', () => openCardView(person.id));
      }
    }
  }

  function addPersonMarker(person, kind, onClick) {
    const label = person.name || (kind === 'self' ? 'You' : 'Unnamed');
    const icon = window.L.divIcon({
      className: '',
      html:
        '<div class="wwt-pin wwt-pin-' + kind + '" title="' + escapeHtml(label) + '">' +
        '<span class="wwt-pin-dot"></span>' +
        '<span class="wwt-pin-label">' + escapeHtml(label) + '</span></div>',
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    });
    window.L.marker([person.location.lat, person.location.lng], { icon })
      .on('click', onClick)
      .addTo(markersLayer);
  }

  function addClusterMarker(location, count, onClick) {
    const icon = window.L.divIcon({
      className: '',
      html:
        '<div class="wwt-pin wwt-cluster" title="' + count + ' pins">' +
        '<span class="wwt-cluster-dot">' + count + '</span>' +
        '<span class="wwt-pin-label">' + count + ' pins</span></div>',
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    });
    window.L.marker([location.lat, location.lng], { icon })
      .on('click', onClick)
      .addTo(markersLayer);
  }

  function locateOrWarn() {
    if (!window.isSecureContext) {
      toast('Location needs a secure (https://) address — use the Tailscale address.', true);
      return Promise.resolve();
    }
    if (!navigator.geolocation) {
      toast('Location is not supported on this device.', true);
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      navigator.geolocation.getCurrentPosition(
        (position) => {
          const location = {
            lat: Number(position.coords.latitude.toFixed(6)),
            lng: Number(position.coords.longitude.toFixed(6)),
          };
          if (map) {
            map.invalidateSize();
            showUserLocation(location);
            map.setView([location.lat, location.lng], MAP_LOCATED_ZOOM, { animate: true });
          }
          resolve(location);
        },
        (error) => {
          const code = error && error.code;
          let message;
          if (code === 1) {
            message = 'Location permission was blocked — allow it in your browser’s site settings, then switch it on in Settings';
          } else if (code === 3) {
            message = 'Location timed out — try again, or pin places by hand';
          } else {
            message = "Couldn't get your location just now — you can still pin places by hand";
          }
          toast(message, true);
          resolve(null);
        },
        { enableHighAccuracy: false, timeout: 15000, maximumAge: 60000 }
      );
    });
  }

  /* ---------- toasts ---------- */

  function toast(message, isError) {
    const root = $('toast-root');
    if (!root) return;
    const item = node('<div class="wwt-toast' + (isError ? ' err' : '') + '">' + escapeHtml(message) + '</div>');
    root.appendChild(item);
    setTimeout(() => {
      item.style.opacity = '0';
      item.style.transition = 'opacity .3s';
    }, 2600);
    setTimeout(() => item.remove(), 3000);
  }

  /* ---------- panel + modal plumbing ---------- */

  function openPanel(title, bodyNode, footNode) {
    const panel = $('panel');
    panel.innerHTML = '';
    const head = node(
      '<div class="wwt-panel-head"><h2>' + escapeHtml(title) + '</h2>' +
      '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>'
    );
    head.querySelector('.wwt-x-btn').onclick = closePanel;
    panel.appendChild(head);

    const body = node('<div class="wwt-panel-body"></div>');
    body.appendChild(bodyNode);
    panel.appendChild(body);

    if (footNode) {
      const foot = node('<div class="wwt-panel-foot"></div>');
      foot.appendChild(footNode);
      panel.appendChild(foot);
    }

    panel.classList.remove('hidden');
    $('overlay').classList.remove('hidden');
    $('overlay').onclick = closePanel;
    panelOpen = true;
    invalidateMap();
  }

  function closePanel() {
    const panel = $('panel');
    if (panel) panel.classList.add('hidden');
    const modalRoot = $('modal-root');
    if (!modalRoot || !modalRoot.hasChildNodes()) $('overlay').classList.add('hidden');
    panelOpen = false;
    invalidateMap();
  }

  function openModal(content, onDismiss) {
    const root = $('modal-root');
    root.innerHTML = '';
    const scrim = node('<div class="wwt-modal-scrim"></div>');
    scrim.appendChild(content);
    scrim.addEventListener('click', (event) => {
      if (event.target !== scrim) return;
      if (onDismiss) onDismiss();
      else closeModal();
    });
    root.appendChild(scrim);
  }

  function closeModal() {
    const root = $('modal-root');
    if (root) root.innerHTML = '';
    const panel = $('panel');
    if (!panel || panel.classList.contains('hidden')) {
      const overlay = $('overlay');
      if (overlay) overlay.classList.add('hidden');
    }
  }

  function showLoading(text) {
    const element = $('app-loading');
    if (!element) return;
    const label = $('app-loading-text');
    if (label) label.textContent = text || 'Loading…';
    element.classList.remove('hidden');
  }

  function hideLoading() {
    const element = $('app-loading');
    if (element) element.classList.add('hidden');
  }

  function confirmDialog(options) {
    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>' + escapeHtml(options.title) + '</h2></div>' +
      '<div class="wwt-modal-body"><p style="line-height:1.55;margin:0">' + options.message + '</p></div>' +
      '<div class="wwt-modal-foot">' +
      '<button class="wwt-btn wwt-btn-ghost" type="button" data-x>Cancel</button>' +
      '<button class="wwt-btn ' + (options.danger ? 'wwt-btn-danger-solid' : 'wwt-btn-primary') +
      '" type="button" data-yes>' + escapeHtml(options.confirmLabel || 'Confirm') + '</button>' +
      '</div></div>'
    );
    modal.querySelector('[data-x]').onclick = closeModal;
    modal.querySelector('[data-yes]').onclick = function () {
      closeModal();
      if (options.onConfirm) options.onConfirm();
    };
    openModal(modal);
  }

  /* ---------- people panel ---------- */

  function openPeoplePanel() {
    const body = node('<div></div>');
    body.appendChild(node('<input class="wwt-input wwt-people-search" type="search" placeholder="Search people…" aria-label="Search people">'));
    const list = node('<div></div>');
    body.appendChild(list);

    const draw = (query) => {
      const needle = (query || '').toLowerCase();
      list.innerHTML = '';
      const people = state.people
        .filter((person) => (person.name || '').toLowerCase().includes(needle))
        .sort((a, b) => firstName(a.name).localeCompare(firstName(b.name), undefined, { sensitivity: 'base' }));

      if (!people.length) {
        list.appendChild(
          node(
            '<div class="wwt-empty">' +
            '<svg viewBox="0 0 24 24" width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="9" cy="8" r="3.4"/><path d="M3 19a6 6 0 0 1 12 0"/></svg>' +
            '<p>No people yet. Tap <b>Add person</b> to pin someone.</p></div>'
          )
        );
        return;
      }

      for (const person of people) {
        const row = node(
          '<div class="wwt-person-row">' +
          '<div class="wwt-person-avatar">' + escapeHtml(initials(person.name)) + '</div>' +
          '<div class="wwt-person-meta">' +
          '<div class="name">' + escapeHtml(person.name || 'Unnamed') + '</div>' +
          '<div class="sub">' +
          escapeHtml(person.known_from || (person.location ? 'Pinned on map' : 'No location')) +
          '</div></div></div>'
        );
        row.onclick = () => {
          closePanel();
          openCardView(person.id);
        };
        list.appendChild(row);
      }
    };

    body.querySelector('.wwt-people-search').addEventListener('input', (event) => draw(event.target.value));
    draw('');

    const foot = node('<button class="wwt-btn wwt-btn-pin block" type="button">Add a person</button>');
    foot.onclick = () => {
      closePanel();
      openNewPersonPinFirst();
    };
    openPanel('People', body, foot);
  }

  function openClusterList(people) {
    const body = node('<div></div>');
    for (const person of people) {
      const row = node(
        '<div class="wwt-person-row">' +
        '<div class="wwt-person-avatar">' + escapeHtml(initials(person.name)) + '</div>' +
        '<div class="wwt-person-meta">' +
        '<div class="name">' + escapeHtml(person.name || 'Unnamed') + '</div>' +
        '<div class="sub">' + escapeHtml(person.known_from || 'Pinned here') + '</div>' +
        '</div></div>'
      );
      row.onclick = () => {
        closePanel();
        openCardView(person.id);
      };
      body.appendChild(row);
    }
    openPanel(people.length + ' people at this address', body);
  }

  /* ---------- card view ---------- */

  function openCardView(idOrSelf) {
    const isSelf = idOrSelf === 'self';
    const card = isSelf ? state.self : findPerson(idOrSelf);
    if (!card) {
      toast('Card not found', true);
      return;
    }

    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>' + (isSelf ? 'My information' : '') + '</h2>' +
      '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>' +
      '<div class="wwt-modal-body"></div>' +
      '<div class="wwt-modal-foot"></div></div>'
    );
    const body = modal.querySelector('.wwt-modal-body');
    const foot = modal.querySelector('.wwt-modal-foot');

    body.appendChild(
      node(
        '<div class="wwt-card-top">' +
        '<div class="wwt-card-photo">' + escapeHtml(initials(card.name)) + '</div>' +
        '<div><h2>' + escapeHtml(card.name || 'Unnamed') + '</h2></div></div>'
      )
    );

    if (card.location) {
      const coordinates = card.location.lat + ', ' + card.location.lng;
      body.appendChild(
        node(
          card.address
            ? '<div class="wwt-card-loc"><div class="wwt-card-loc-line">📍 ' +
              escapeHtml(card.address) +
              ' <span class="wwt-card-loc-coord">(' + coordinates + ')</span></div></div>'
            : '<div class="wwt-card-loc"><div class="wwt-card-loc-line">📍 ' + coordinates + '</div></div>'
        )
      );
    }

    const detail = (label, value) => {
      if (!value) return;
      body.appendChild(
        node(
          '<div class="wwt-detail"><div class="k">' + escapeHtml(label) + '</div>' +
          '<div class="v">' + escapeHtml(value) + '</div></div>'
        )
      );
    };
    detail('Birthday', card.birthday);
    detail('Pets', card.pets);
    detail('Where you know them from', card.known_from);
    detail('Notes', card.notes);

    if ((card.relatives || []).length) {
      const ownerId = isSelf ? 'self' : card.id;
      const wrap = node('<div class="wwt-detail"><div class="k">Relatives &amp; children</div></div>');
      card.relatives.forEach((relative) => {
        const hasLocation = relative.lat !== null && relative.lng !== null;
        const relativeCard = findRelativeCard(relative);
        const line = node(
          '<div class="wwt-rel-line">' +
          '<div class="wwt-rel-head"><span class="wwt-rel-type">' + escapeHtml(relative.type) + '</span>' +
          '<span class="v">' + escapeHtml(relative.name || '—') + '</span></div>' +
          '<div class="wwt-rel-actions">' +
          (relativeCard ? '<button class="wwt-btn wwt-btn-ghost xsmall rel-card" type="button">Show card</button>' : '') +
          (hasLocation ? '<button class="wwt-btn wwt-btn-ghost xsmall rel-map" type="button">Show on map</button>' : '') +
          '<button class="wwt-btn wwt-btn-ghost xsmall rel-remove" type="button">Remove relationship</button>' +
          '</div></div>'
        );

        if (relativeCard) {
          line.querySelector('.rel-card').onclick = () => {
            closeModal();
            openCardView(relativeCard.is_self ? 'self' : relativeCard.id);
          };
        }
        if (hasLocation) {
          line.querySelector('.rel-map').onclick = () => {
            closeModal();
            panTo(relative.lat, relative.lng);
          };
        }
        line.querySelector('.rel-remove').onclick = () => {
          confirmDialog({
            title: 'Remove relationship?',
            message:
              'This removes <b>' + escapeHtml(relative.name || 'this relative') +
              '</b> as a relationship on this card. Their own pin and card (if any) stay.',
            confirmLabel: 'Remove',
            danger: true,
            onConfirm: () => removeRelative(ownerId, relative.id),
          });
        };
        wrap.appendChild(line);
      });
      body.appendChild(wrap);
    }

    if (card.location) {
      const button = node('<button class="wwt-btn wwt-btn-ghost" type="button">Show on map</button>');
      button.onclick = () => {
        closeModal();
        panTo(card.location.lat, card.location.lng);
      };
      foot.appendChild(button);
    }

    const edit = node('<button class="wwt-btn wwt-btn-primary" type="button">Edit</button>');
    edit.onclick = () => {
      closeModal();
      openEditor(isSelf ? 'self' : card.id);
    };
    foot.appendChild(edit);

    if (!isSelf) {
      const remove = node('<button class="wwt-btn wwt-btn-danger" type="button">Delete</button>');
      remove.onclick = () => {
        confirmDialog({
          title: 'Delete this card?',
          message:
            'This removes <b>' + escapeHtml(card.name || 'this person') +
            '</b> from the notebook. This can\'t be undone.',
          confirmLabel: 'Delete',
          danger: true,
          onConfirm: () => deletePerson(card.id),
        });
      };
      foot.appendChild(remove);
    }

    modal.querySelector('.wwt-x-btn').onclick = closeModal;
    openModal(modal);
  }

  /* ---------- editor ---------- */

  function blankCard() {
    return {
      name: '',
      birthday: '',
      pets: '',
      known_from: '',
      notes: '',
      address: '',
      lat: null,
      lng: null,
      relatives: [],
    };
  }

  function emptyRelative() {
    return { type: 'Parent', name: '', lat: null, lng: null, address: '' };
  }

  function openEditor(idOrSelf) {
    editorIsSelf = idOrSelf === 'self';
    if (idOrSelf === 'self') {
      editorDraft = state.self ? deepCopy(state.self) : blankCard();
      editorId = state.self ? state.self.id : 'new';
    } else if (idOrSelf === 'new') {
      editorDraft = blankCard();
      editorId = 'new';
    } else {
      const person = findPerson(idOrSelf);
      editorDraft = person ? deepCopy(person) : blankCard();
      editorId = person ? person.id : 'new';
    }
    if (!Array.isArray(editorDraft.relatives)) editorDraft.relatives = [];
    renderEditor();
  }

  function locationStatusHtml(card) {
    if (!card.location) return 'No location set';
    if (card.address) {
      return '📍 ' + escapeHtml(card.address) +
        ' <span class="mono xsmall">(' + card.location.lat + ', ' + card.location.lng + ')</span>';
    }
    return '📍 <span class="mono xsmall">' + card.location.lat + ', ' + card.location.lng + '</span>';
  }

  function relativeLocationHtml(relative) {
    if (relative.lat === null || relative.lng === null) return 'No location';
    if (relative.address) {
      return '📍 ' + escapeHtml(relative.address) +
        ' <span class="mono xsmall">(' + relative.lat + ', ' + relative.lng + ')</span>';
    }
    return '📍 <span class="mono xsmall">' + relative.lat + ', ' + relative.lng + '</span>';
  }

  function renderEditor() {
    const card = editorDraft;
    const title = editorIsSelf ? 'My information' : editorId && editorId !== 'new' ? 'Edit person' : 'Add person';

    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>' + escapeHtml(title) + '</h2>' +
      '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>' +
      '<div class="wwt-modal-body">' +
      '<div class="wwt-name-row">' +
      '<div class="wwt-name-field"><label class="wwt-field-label">Name</label>' +
      '<input id="ed-name" class="wwt-input" value="' + escapeHtml(card.name) + '" placeholder="Their name"></div>' +
      '<div class="wwt-bday-field"><label class="wwt-field-label">Birthday</label>' +
      '<input id="ed-birthday" class="wwt-input" type="date" value="' + escapeHtml(card.birthday) + '"></div>' +
      '</div>' +
      '<label class="wwt-field-label">Pets</label>' +
      '<input id="ed-pets" class="wwt-input" value="' + escapeHtml(card.pets) + '" placeholder="e.g. dog, Rex">' +
      (editorIsSelf
        ? ''
        : '<label class="wwt-field-label">Where you know them from</label>' +
          '<input id="ed-knownfrom" class="wwt-input" value="' + escapeHtml(card.known_from) +
          '" placeholder="e.g. conference, gym, neighbour">') +
      '<label class="wwt-field-label">General notes</label>' +
      '<textarea id="ed-notes" class="wwt-input" placeholder="Anything you want to remember">' +
      escapeHtml(card.notes) + '</textarea>' +
      '<label class="wwt-field-label">Relatives &amp; children</label>' +
      '<div id="ed-relatives"></div>' +
      '<button id="ed-add-rel" class="wwt-btn wwt-btn-ghost small" type="button">+ Add relative or child</button>' +
      '<hr class="wwt-ed-divider">' +
      '<label class="wwt-field-label">Location</label>' +
      '<button id="ed-pin" class="wwt-btn wwt-btn-pin block" type="button">' +
      (card.location ? 'Move pin / change address' : 'Set address or pin') + '</button>' +
      '<div id="ed-pin-status" class="wwt-pin-status ' + (card.location ? 'set' : '') + '">' +
      locationStatusHtml(card) + '</div>' +
      '</div>' +
      '<div class="wwt-modal-foot">' +
      '<button class="wwt-btn wwt-btn-ghost" type="button" data-x>Cancel</button>' +
      '<button class="wwt-btn wwt-btn-primary" id="ed-save" type="button">Save</button>' +
      '</div></div>'
    );

    const relWrap = modal.querySelector('#ed-relatives');
    renderRelatives(relWrap, modal);

    modal.querySelector('#ed-add-rel').onclick = () => {
      editorDraft = captureEditorForm(modal);
      editorDraft.relatives.push(emptyRelative());
      renderRelatives(relWrap, modal);
    };

    modal.querySelector('#ed-pin').onclick = () => {
      editorDraft = captureEditorForm(modal);
      closeModal();
      chooseLocation({
        title: editorIsSelf ? 'Set your location' : 'Set ' + (editorDraft.name || 'this person') + "'s location",
        onResult: (location) => {
          if (location) {
            editorDraft.location = { lat: location.lat, lng: location.lng };
            editorDraft.lat = location.lat;
            editorDraft.lng = location.lng;
            editorDraft.address = location.address || '';
          }
          renderEditor();
        },
      });
    };

    modal.querySelector('#ed-save').onclick = () => {
      const values = captureEditorForm(modal);
      if (!(values.name || '').trim()) {
        toast('Give them a name first', true);
        return;
      }
      saveCard(values);
    };

    modal.querySelector('.wwt-x-btn').onclick = closeModal;
    modal.querySelectorAll('[data-x]').forEach((button) => (button.onclick = closeModal));
    openModal(modal);
  }

  function renderRelatives(wrap, modal) {
    wrap.innerHTML = '';
    editorDraft.relatives.forEach((relative, index) => {
      const hasLocation = relative.lat !== null && relative.lng !== null;
      const block = node(
        '<div class="wwt-rel-block" data-index="' + index + '">' +
        '<button class="wwt-rel-remove" type="button" aria-label="Remove">×</button>' +
        '<label class="wwt-field-label" style="margin-top:0">Relationship</label>' +
        '<select class="wwt-input rel-type">' +
        RELATIVE_TYPES.map((type) => '<option' + (type === relative.type ? ' selected' : '') + '>' + type + '</option>').join('') +
        '</select>' +
        '<button class="wwt-btn wwt-btn-ghost small rel-frompeople" type="button">Use someone from my people</button>' +
        '<label class="wwt-field-label">Name</label>' +
        '<input class="wwt-input rel-name" value="' + escapeHtml(relative.name) + '" placeholder="Relative\'s name">' +
        '<button class="wwt-btn wwt-btn-ghost small rel-pin" type="button">' +
        (hasLocation ? 'Move pin / change address' : 'Set address or pin') + '</button>' +
        '<div class="wwt-pin-status ' + (hasLocation ? 'set' : '') + '">' +
        relativeLocationHtml(relative) + '</div></div>'
      );

      block.querySelector('.rel-frompeople').onclick = () => {
        editorDraft = captureEditorForm(modal);
        pickFromPeople((person) => {
          if (person) {
            const target = editorDraft.relatives[index];
            if (person.name) target.name = person.name;
            if (person.location) {
              target.lat = person.location.lat;
              target.lng = person.location.lng;
              target.address = person.address || '';
            }
          }
          renderEditor();
        });
      };

      block.querySelector('.wwt-rel-remove').onclick = () => {
        editorDraft = captureEditorForm(modal);
        editorDraft.relatives.splice(index, 1);
        renderRelatives(wrap, modal);
      };

      block.querySelector('.rel-pin').onclick = () => {
        editorDraft = captureEditorForm(modal);
        const name = editorDraft.relatives[index].name || 'this relative';
        closeModal();
        chooseLocation({
          title: "Set " + name + "'s location",
          onResult: (location) => {
            if (location) {
              editorDraft.relatives[index].lat = location.lat;
              editorDraft.relatives[index].lng = location.lng;
              editorDraft.relatives[index].address = location.address || '';
            }
            renderEditor();
          },
        });
      };

      wrap.appendChild(block);
    });
  }

  function pickFromPeople(onPick) {
    const candidates = state.people.filter((person) => String(person.id) !== String(editorId));
    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>Choose from your people</h2>' +
      '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>' +
      '<div class="wwt-modal-body"></div></div>'
    );
    const body = modal.querySelector('.wwt-modal-body');

    if (!candidates.length) {
      body.appendChild(
        node('<div class="wwt-empty"><p>No other people to choose from yet. Add someone to your map first.</p></div>')
      );
    } else {
      candidates
        .slice()
        .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
        .forEach((person) => {
          const row = node(
            '<div class="wwt-person-row">' +
            '<div class="wwt-person-avatar">' + escapeHtml(initials(person.name)) + '</div>' +
            '<div class="wwt-person-meta"><div class="name">' + escapeHtml(person.name || 'Unnamed') + '</div>' +
            '<div class="sub">' +
            escapeHtml(person.address || (person.location ? 'Pinned on map' : 'No details')) +
            '</div></div></div>'
          );
          row.onclick = () => {
            closeModal();
            onPick(person);
          };
          body.appendChild(row);
        });
    }

    const dismiss = () => {
      closeModal();
      onPick(null);
    };
    modal.querySelector('.wwt-x-btn').onclick = dismiss;
    openModal(modal, dismiss);
  }

  function captureEditorForm(modal) {
    const draft = deepCopy(editorDraft);
    const value = (selector) => {
      const element = modal.querySelector(selector);
      return element ? element.value.trim() : '';
    };

    draft.name = value('#ed-name');
    draft.birthday = value('#ed-birthday');
    draft.pets = value('#ed-pets');
    const knownFrom = modal.querySelector('#ed-knownfrom');
    if (knownFrom) draft.known_from = knownFrom.value.trim();
    const notes = modal.querySelector('#ed-notes');
    draft.notes = notes ? notes.value : draft.notes;

    modal.querySelectorAll('.wwt-rel-block').forEach((block) => {
      const index = Number(block.dataset.index);
      if (!draft.relatives[index]) return;
      draft.relatives[index].type = block.querySelector('.rel-type').value;
      draft.relatives[index].name = block.querySelector('.rel-name').value.trim();
    });
    return draft;
  }

  async function saveCard(values) {
    const payload = {
      name: values.name,
      birthday: values.birthday,
      pets: values.pets,
      known_from: values.known_from || (editorIsSelf && state.self ? state.self.known_from : ''),
      notes: values.notes,
      address: values.address || '',
      lat: values.lat === undefined ? null : values.lat,
      lng: values.lng === undefined ? null : values.lng,
      relatives: (values.relatives || []).map((relative) => ({
        type: relative.type,
        name: relative.name,
        lat: relative.lat,
        lng: relative.lng,
        address: relative.address || '',
      })),
      is_self: editorIsSelf,
    };

    try {
      const data =
        editorId && editorId !== 'new'
          ? await api('/api/people/' + editorId, { method: 'PUT', body: payload })
          : await api('/api/people', { method: 'POST', body: payload });

      applyState(data.state);
      renderPins();
      closeModal();
      toast(editorIsSelf ? 'Your info saved' : 'Card saved');
    } catch (error) {
      toast(error.message || 'Could not save that card.', true);
    }
  }

  async function deletePerson(personId) {
    try {
      const data = await api('/api/people/' + personId, { method: 'DELETE' });
      applyState(data.state);
      renderPins();
      closeModal();
      toast('Card deleted');
    } catch (error) {
      toast(error.message || 'Could not delete that card.', true);
    }
  }

  async function removeRelative(ownerId, relativeId) {
    if (!relativeId) {
      toast('Save the card first, then remove the relationship.', true);
      return;
    }
    try {
      const data = await api(
        '/api/people/' + ownerId + '/relatives/' + relativeId + '/remove',
        { method: 'POST' }
      );
      applyState(data.state);
      renderPins();
      closeModal();
      toast('Relationship removed');
    } catch (error) {
      toast(error.message || 'Could not remove that relationship.', true);
    }
  }

  /* ---------- pin picking / location chooser ---------- */

  function startPick(message, onPicked, onCancel) {
    const banner = $('pick-banner');
    $('pick-banner-text').textContent = message;
    $('pick-confirm').classList.add('hidden');
    banner.classList.remove('hidden');
    $('overlay').classList.add('hidden');

    const cancel = () => {
      banner.classList.add('hidden');
      cancelPickMode();
      if (onCancel) onCancel();
      else renderEditor();
    };
    $('pick-cancel').onclick = cancel;

    enablePickMode((location) => {
      if (!location) {
        cancel();
        return;
      }
      banner.classList.add('hidden');
      onPicked(location);
    });
  }

  function startConfirm(message, onConfirm, onCancel) {
    const banner = $('pick-banner');
    $('pick-banner-text').textContent = message;
    banner.classList.remove('hidden');
    $('overlay').classList.add('hidden');
    const confirmButton = $('pick-confirm');
    confirmButton.classList.remove('hidden');
    confirmButton.onclick = () => {
      banner.classList.add('hidden');
      confirmButton.classList.add('hidden');
      onConfirm();
    };
    $('pick-cancel').onclick = () => {
      banner.classList.add('hidden');
      confirmButton.classList.add('hidden');
      if (onCancel) onCancel();
    };
  }

  function noMatchHtml() {
    return (
      '<div class="muted small wwt-no-match">Couldn\'t find that address. If it\'s correct, ' +
      'it may not be on <a href="https://www.openstreetmap.org/" target="_blank" ' +
      'rel="noopener noreferrer">OpenStreetMap</a> yet — consider adding it there. ' +
      'Or place a pin manually below.</div>'
    );
  }

  function chooseLocation(options) {
    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>' + escapeHtml(options.title || 'Set location') + '</h2>' +
      '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>' +
      '<div class="wwt-modal-body">' +
      '<label class="wwt-field-label">Search an address</label>' +
      '<div class="wwt-addr-row">' +
      '<input id="addr-q" class="wwt-input" placeholder="e.g. 350 Fifth Ave, New York" autocomplete="off" ' +
      'autocorrect="off" autocapitalize="off" spellcheck="false">' +
      '<button id="addr-search" class="wwt-btn wwt-btn-ghost small" type="button">Search</button></div>' +
      '<div id="addr-results" class="wwt-addr-results"></div>' +
      '<div class="wwt-addr-or"><span>or</span></div>' +
      '<button id="addr-manual" class="wwt-btn wwt-btn-pin block" type="button">Place a pin manually</button>' +
      '</div></div>'
    );
    const results = modal.querySelector('#addr-results');

    const doSearch = async () => {
      const query = modal.querySelector('#addr-q').value;
      if (!query.trim()) return;
      results.innerHTML = '<div class="muted small">Searching…</div>';
      try {
        const data = await api('/api/geocode?q=' + encodeURIComponent(query));
        const found = data.results || [];
        if (!found.length) {
          results.innerHTML = noMatchHtml();
          return;
        }
        results.innerHTML = '';
        found.forEach((result) => {
          const item = node('<button class="wwt-addr-item" type="button"></button>');
          item.textContent = result.address;
          item.onclick = () => confirmAddress(result);
          results.appendChild(item);
        });
      } catch (error) {
        results.innerHTML =
          '<div class="muted small">' + escapeHtml(error.message || 'Search failed') +
          '. You can place a pin manually.</div>';
      }
    };

    modal.querySelector('#addr-search').onclick = doSearch;
    modal.querySelector('#addr-q').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        doSearch();
      }
    });

    modal.querySelector('#addr-manual').onclick = () => {
      closeModal();
      startPick(
        'Tap the map to drop a pin',
        (location) => afterManualPin(location),
        () => options.onResult(null)
      );
    };

    const dismiss = () => {
      closeModal();
      options.onResult(null);
    };
    modal.querySelector('.wwt-x-btn').onclick = dismiss;
    openModal(modal, dismiss);

    function confirmAddress(result) {
      closeModal();
      showDraftPin({ lat: result.lat, lng: result.lng });
      startConfirm(
        'Place pin at: ' + result.address,
        () => {
          clearDraftPin();
          options.onResult({ lat: result.lat, lng: result.lng, address: result.address });
        },
        () => {
          clearDraftPin();
          chooseLocation(options);
        }
      );
    }

    async function afterManualPin(location) {
      const coordinates = { lat: location.lat, lng: location.lng };
      let address = '';
      try {
        const data = await api('/api/reverse?lat=' + coordinates.lat + '&lng=' + coordinates.lng);
        address = data.address || '';
      } catch (error) {
        address = '';
      }

      if (!address) {
        options.onResult({ lat: coordinates.lat, lng: coordinates.lng, address: '' });
        return;
      }

      const confirmation = node(
        '<div class="wwt-modal" role="dialog" aria-modal="true">' +
        '<div class="wwt-modal-head"><h2>Is this the address?</h2>' +
        '<button class="wwt-x-btn" type="button" aria-label="Close">×</button></div>' +
        '<div class="wwt-modal-body">' +
        '<p class="muted small">Your pin looks like it\'s at:</p>' +
        '<div class="wwt-addr-found">📍 ' + escapeHtml(address) + '</div>' +
        '<div class="wwt-addr-choice">' +
        '<button class="wwt-btn wwt-btn-primary block" id="pin-use-addr" type="button">Use this address</button>' +
        '<button class="wwt-btn wwt-btn-ghost block" id="pin-diff-addr" type="button">Enter a different address</button>' +
        '<button class="wwt-btn wwt-btn-ghost block" id="pin-coords" type="button">Use coordinates only</button>' +
        '</div></div></div>'
      );
      confirmation.querySelector('#pin-use-addr').onclick = () => {
        closeModal();
        options.onResult({ lat: coordinates.lat, lng: coordinates.lng, address });
      };
      confirmation.querySelector('#pin-diff-addr').onclick = () => {
        closeModal();
        chooseLocation(options);
      };
      confirmation.querySelector('#pin-coords').onclick = () => {
        closeModal();
        options.onResult({ lat: coordinates.lat, lng: coordinates.lng, address: '' });
      };
      confirmation.querySelector('.wwt-x-btn').onclick = () => {
        closeModal();
        options.onResult({ lat: coordinates.lat, lng: coordinates.lng, address: '' });
      };
      openModal(confirmation);
    }
  }

  function openNewPersonPinFirst() {
    closeModal();
    closePanel();
    chooseLocation({
      title: 'Where do you know this person?',
      onResult: (location) => {
        if (!location) return;
        editorDraft = blankCard();
        editorDraft.location = { lat: location.lat, lng: location.lng };
        editorDraft.lat = location.lat;
        editorDraft.lng = location.lng;
        editorDraft.address = location.address || '';
        editorIsSelf = false;
        editorId = 'new';
        renderEditor();
      },
    });
  }

  /* ---------- settings ---------- */

  function openSettingsPanel() {
    const settings = state.settings;
    const body = node(
      '<div>' +
      '<div class="wwt-set-section"><h3>Location</h3>' +
      '<div class="wwt-set-row"><span>Find me on the map at login</span>' +
      '<button class="wwt-toggle ' + (settings.find_me_on_login ? 'on' : '') +
      '" id="set-loc" type="button" aria-pressed="' + (settings.find_me_on_login ? 'true' : 'false') +
      '" aria-label="Find me on the map at login"></button></div>' +
      '<p class="muted small" style="line-height:1.5;margin:6px 0 0">' +
      'Your browser asks before sharing your location. It is used to centre the map; nothing else is kept.</p>' +
      '</div>' +
      '<div class="wwt-set-section"><h3>Appearance</h3>' +
      '<div class="wwt-set-row"><span>Theme</span>' +
      '<button class="wwt-btn wwt-btn-ghost small" id="set-theme" type="button">' +
      (settings.theme === 'dark' ? 'Light mode' : 'Dark mode') + '</button></div>' +
      '</div></div>'
    );

    body.querySelector('#set-loc').onclick = () => toggleFindMe(!state.settings.find_me_on_login);
    body.querySelector('#set-theme').onclick = () => {
      setTheme(state.settings.theme === 'dark' ? 'light' : 'dark').then(openSettingsPanel);
    };

    openPanel('Settings', body);
  }

  async function toggleFindMe(value) {
    try {
      const data = await api('/api/settings', {
        method: 'POST',
        body: { find_me_on_login: value, find_me_asked: true },
      });
      state.settings = data.settings;
      if (value) await locateOrWarn();
      if (panelOpen) openSettingsPanel();
    } catch (error) {
      toast(error.message || 'Could not save that setting.', true);
    }
  }

  function askLocationPrompt() {
    const modal = node(
      '<div class="wwt-modal" role="dialog" aria-modal="true">' +
      '<div class="wwt-modal-head"><h2>Share your location?</h2></div>' +
      '<div class="wwt-modal-body"><p style="line-height:1.55;margin:0">' +
      'Let Who Was That? center the map on you so you can start pinning right away. ' +
      'Your location is only used to centre the map and to place pins you confirm.</p></div>' +
      '<div class="wwt-modal-foot">' +
      '<button class="wwt-btn wwt-btn-ghost" id="loc-no" type="button">Not now</button>' +
      '<button class="wwt-btn wwt-btn-primary" id="loc-yes" type="button">Use my location</button>' +
      '</div></div>'
    );
    const choose = async (value) => {
      closeModal();
      try {
        const data = await api('/api/settings', {
          method: 'POST',
          body: { find_me_on_login: value, find_me_asked: true },
        });
        state.settings = data.settings;
      } catch (error) {
        /* the prompt is not worth an error toast */
      }
      if (value) await locateOrWarn();
    };
    modal.querySelector('#loc-yes').onclick = () => choose(true);
    modal.querySelector('#loc-no').onclick = () => choose(false);
    openModal(modal, () => choose(false));
  }

  /* ---------- deep links ---------- */

  function handleDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const lat = Number.parseFloat(params.get('lat'));
    const lng = Number.parseFloat(params.get('lng'));
    if (Number.isFinite(lat) && Number.isFinite(lng)) {
      panTo(lat, lng);
    }

    const personParam = params.get('person');
    if (personParam) {
      const person = findPerson(personParam);
      if (person) {
        if (person.location) panTo(person.location.lat, person.location.lng);
        openCardView(person.is_self ? 'self' : person.id);
        return;
      }
    }

    if (params.get('panel') === 'people') {
      openPeoplePanel();
    } else if (params.get('add') === 'person') {
      openNewPersonPinFirst();
    }
  }

  /* ---------- chrome ---------- */

  function wireChrome() {
    $('btn-theme').addEventListener('click', () => {
      setTheme(state.settings.theme === 'dark' ? 'light' : 'dark');
    });
    $('btn-people').addEventListener('click', openPeoplePanel);
    $('btn-settings').addEventListener('click', openSettingsPanel);
    $('btn-add-person').addEventListener('click', openNewPersonPinFirst);
    $('btn-self-card').addEventListener('click', () => {
      if (state.self) openCardView('self');
      else openEditor('self');
    });
  }

  /* ---------- boot ---------- */

  async function boot() {
    applyTheme(app.dataset.theme || 'dark');
    initMap();
    wireChrome();
    showLoading('Loading your notebook…');

    try {
      const data = await api('/api/state');
      applyState(data.state);
    } catch (error) {
      toast(error.message || 'Could not load your people.', true);
    }

    hideLoading();
    renderPins();
    handleDeepLink();

    if (!state.settings.find_me_asked) askLocationPrompt();
    else if (state.settings.find_me_on_login) locateOrWarn();
  }

  boot().then(() => {
    window.setInterval(pollState, 2000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) pollState();
    });
    window.addEventListener('focus', pollState);
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) pollState();
    });
  });
})();
