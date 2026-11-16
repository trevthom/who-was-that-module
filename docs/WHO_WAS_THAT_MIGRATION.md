# Migrating Who Was That? into Thompson

Who Was That? used to be a standalone, client-side application whose only
backend was the Nostr network: one encrypted notebook event per identity key,
stored on user-chosen relays. That backend is retired. The app now runs inside
the hub as a normal Flask module with tables in `data/thompson.db`.

- Module folder: `applications/who-was-that-module/` (this repository's root *is*
  that folder — `app.py` sits at the top, because the hub only registers a
  folder that directly contains `app.py`)
- Registry id / Blueprint name: `who_was_that_module` / `who_was_that`
- URL: `/who-was-that`
- Display name: **Who Was That?**

There is no login, no users, no roles, no sessions, no cookies, no CSRF tokens
and no per-user data. Everyone who can reach Thompson sees and edits the same
notebook, exactly like every other app in the hub.

## Feature inventory

Status legend: **preserved** (same behaviour, server data), **replaced** (same
UI, different backend), **removed** (meaningless without the relay backend).

| Old surface | Now | Status |
| --- | --- | --- |
| Login screen: generate key / paste key, "stay logged in", relay list | Gone; the hub has no login | removed |
| Full-screen map with pins, name labels, "N pins" clusters at one address | Same map, Leaflet vendored, tiles proxied same-origin | replaced |
| "You are here" dot and "Find me on the map at login" | Same, geolocation allowed for the hub origin | preserved |
| Dark/light theme, dark tile filter, theme remembered per identity | Same two themes, remembered in `who_was_that_settings` | replaced |
| Top bar: theme, People, Messages, Settings | Theme, People, Settings (Messages removed) | replaced |
| "Add person" and "My info" floating buttons | Same | preserved |
| Add person flow: choose location first, then details | Same | preserved |
| People panel with name search, A–Z ordering, empty state | Same panel, plus a server-rendered `/people` page | preserved |
| Card view: initials avatar, address + coordinates, birthday, pets, where you know them from, notes | Same | replaced |
| Relatives & children: type, name, own pin, "Show card", "Show on map", "Remove relationship" | Same; located relatives still become their own cards with a reciprocal inverse relationship | replaced |
| Editor: name, birthday, pets, source, notes, relatives, "Use someone from my people" | Same, minus the npub field | replaced |
| Address search (Nominatim), manual pin, reverse-geocode confirmation banner | Same UI, served by same-origin Flask endpoints | replaced |
| Delete card scrubs references on other cards | Same (matched by name + coordinates) | preserved |
| "Their npub" field, chat buttons, direct messages, unread bubbles, message notifications | Gone | removed |
| Share my card with an npub, shared-card merges, `sharedWith` bookkeeping | Gone | removed |
| Relay list, relay status dots, health checks, "Delete account" | Gone | removed |
| What is Nostr? / Issues / Donate footer popups, OsteN-style footer links | Gone (the required OpenStreetMap credit stays) | removed |
| Install prompt, app manifest, service worker, notification handlers | Hub owns the PWA shell; the app registers nothing | removed |
| Photos (`picture` field) | Removed by the old app already; not modelled | removed |
| Encounter/place entities | Never existed: a card's location *is* the place | n/a |
| Standalone notes entity | Never existed: notes are a field on a card | n/a |

Blocked or not rebuilt: **chat, card sharing and merges** only work between
Nostr identities, so there is no server-side equivalent. They were removed
rather than faked. Everything else the app did is now ordinary family data.

## Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/who-was-that` and `/who-was-that/` | map screen (panels + modals) |
| GET | `/who-was-that/people` | server-rendered people list, `?q=` search |
| GET | `/who-was-that/people/new` | server-rendered create form (`?self=1` for your card) |
| GET | `/who-was-that/people/<id>` | server-rendered card view |
| GET | `/who-was-that/people/<id>/edit` | server-rendered edit form |
| GET | `/who-was-that/self` | redirect to your card (or its create form) |
| POST | `/who-was-that/people` | create (plain form) |
| POST | `/who-was-that/people/<id>` | update (plain form) |
| POST | `/who-was-that/people/<id>/delete` | delete (plain form) |
| POST | `/who-was-that/people/<id>/relatives/<rid>/delete` | remove a relationship (plain form) |
| GET | `/who-was-that/api/state` | everything the map needs |
| POST | `/who-was-that/api/people` | create (JSON) |
| PUT/POST | `/who-was-that/api/people/<id>` | update (JSON) |
| DELETE | `/who-was-that/api/people/<id>` | delete (JSON) |
| POST | `/who-was-that/api/people/<id>/relatives/<rid>/remove` | remove a relationship (JSON) |
| POST | `/who-was-that/api/settings` | theme, find-me-on-login |
| GET | `/who-was-that/api/geocode?q=` | address search proxy |
| GET | `/who-was-that/api/reverse?lat=&lng=` | reverse geocode proxy |
| GET | `/who-was-that/tiles/<z>/<x>/<y>.png` | map tile proxy |
| GET | `/who-was-that/static/...` | vendored Leaflet, fonts, CSS, JS |

The map screen keeps the original single-page panels and modals (People, card,
editor, settings, address chooser). The people/card/form pages are the
deep-linkable, no-JavaScript equivalents. There is no history-based client
routing, so no catch-all fallback route is needed.

## Tables

All in `data/thompson.db`, created by `init_app()` on every start:

| Table | Holds |
| --- | --- |
| `who_was_that_people` | one row per card; `is_self = 1` is your own card (unique) |
| `who_was_that_relatives` | relatives/children rows, optionally linked to another card |
| `who_was_that_settings` | the single shared row: `theme`, `find_me_on_login`, `find_me_asked` |

Indexes: name (case-insensitive) for list/search, `(lat, lng)` for map queries,
`is_self`, relatives by `person_id` and `linked_person_id`, plus unique partial
indexes on `legacy_id` for the importer and on `is_self = 1`.

Old notebook field → column:

| Old dataset | New |
| --- | --- |
| `self` (one card) | `who_was_that_people.is_self = 1` |
| `people[]` | `who_was_that_people` rows |
| card `name/birthday/pets/knownFrom/notes/address` | same-named columns |
| card `location {lat,lng}` | `lat`, `lng` |
| card `relatives[] {type,name,location,address}` | `who_was_that_relatives` |
| card `id` | `legacy_id` (import only) |
| `settings.theme` | `who_was_that_settings.theme` |
| `settings.shareLocationOnLogin` | `find_me_on_login` (+ `find_me_asked`) |
| `settings.relays`, `handledShares`, `sharedWith`, `npub`, `picture` | dropped |

## Data migration

The old app kept nothing on the device, so there is no local database to copy.
The only export path is the old app itself, while logged in (the user holds
their own key):

```js
// browser console of the old WhoIsThat app, while logged in
const { getState } = await import('./js/state.js');
copy(JSON.stringify(getState().dataset));
```

That produces a read-only JSON export of the public domain only: your card,
the people, their relatives, the theme. It contains no private material, no
relay credentials and no chat.

Then, from the hub root:

```bash
# 1. See what would happen without writing anything
.venv/bin/python -m applications.who_was_that.migrate \
  --file ~/notebook.json --dry-run

# 2. Import (the hub database is backed up to data/backups/ first)
.venv/bin/python -m applications.who_was_that.migrate \
  --file ~/notebook.json

# 3. Re-running is safe: already-imported cards are skipped
.venv/bin/python -m applications.who_was_that.migrate \
  --file ~/notebook.json
```

The importer refuses a raw relay event (encrypted notebook) and never reads or
stores identity keys, relay settings, shared-card bookkeeping or photos.
Located relatives are linked to their own cards, exactly as saving a card does,
so counts match the export. Verify with:

```bash
sqlite3 data/thompson.db \
  "SELECT COUNT(*) FROM who_was_that_people; SELECT COUNT(*) FROM who_was_that_relatives;"
```

Keep the old relay data until the port is accepted.

## Maps and the Content Security Policy

The hub still sends its original policy, unchanged:

```
img-src 'self' data:; connect-src 'self'; script-src 'self'; font-src 'self'
```

So the app keeps the map inside that boundary instead of widening it:

- **Leaflet 1.9.4** is vendored in `static/leaflet/` (JS, CSS, marker images).
- **Map tiles** are fetched by `GET /who-was-that/tiles/<z>/<x>/<y>.png`, which
  proxies OpenStreetMap tiles with an identifying user agent, a small in-memory
  cache (512 tiles / 7 days) and a valid 1×1 transparent PNG when the network is
  unavailable. Required attribution stays in the app footer.
- **Address search and reverse geocoding** go through
  `/who-was-that/api/geocode` and `/api/reverse`, which call Nominatim at its
  required one-request-per-second rate with a 24-hour cache.
- **Fonts** (Inter, Space Grotesk, Space Mono — latin subsets) are self-hosted in
  `static/fonts/`, so the page issues no third-party font request.

The one central change is in `hub/__init__.py`: `Permissions-Policy` now sends
`geolocation=(self)` so the preserved "Find me on the map at login" toggle can
work. It still denies camera and microphone, and geolocation is same-origin
only.

Without tiles the app still lists, creates, edits and deletes people and notes;
only the basemap goes blank, and the app says so with a toast.

Tiles and geocoding can be switched off entirely for offline installs:

| Variable | Default | Meaning |
| --- | --- | --- |
| `WHO_WAS_THAT_TILES` | `on` | `off` returns blank tiles and makes no tile requests |
| `WHO_WAS_THAT_TILE_URL` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` | tile source |
| `WHO_WAS_THAT_GEOCODE_URL` | `https://nominatim.openstreetmap.org/search` | address search |
| `WHO_WAS_THAT_REVERSE_URL` | `https://nominatim.openstreetmap.org/reverse` | reverse geocoding |
| `WHO_WAS_THAT_USER_AGENT` | `ThompsonFamilyHub/1.0 …` | identifying user agent |
| `WHO_WAS_THAT_TILE_TIMEOUT` | `6` | seconds per tile |
| `WHO_WAS_THAT_GEOCODE_TIMEOUT` | `8` | seconds per geocode request |

## Known limitations

- Chat, card sharing and shared-card merges are gone: they were Nostr-identity
  features with no server-side equivalent.
- The old app's "delete account" is not needed and not rebuilt; delete a card
  from its card view instead.
- Server-rendered people/card/form pages use the hub's typography and tokens,
  while the map screen keeps the app's own dark/light palette. Both come from
  the same data.
- Address search needs outbound access to Nominatim from the hub host. When it
  is unavailable the app falls back to typing coordinates or dropping a pin.
