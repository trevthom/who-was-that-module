"""Who Was That? domain logic: people, relatives, settings and map state.

Everything is family-shared. There is no owner column, no user table and no
per-user filtering: the hub is open and the tailnet is the access boundary.

The old app kept one encrypted notebook object per identity. The same domain
now lives in normal SQLite tables:

``who_was_that_people``     a card per person (plus one ``is_self`` card)
``who_was_that_relatives``  the relatives/children rows attached to a card
``who_was_that_settings``   the two shared display preferences
"""

from __future__ import annotations

from datetime import date
from typing import Any

from shared.database import get_db
from shared.utilities import clean_text, utc_now

RELATIVE_TYPES = (
    "Parent",
    "Child",
    "Sibling",
    "Spouse",
    "Grandparent",
    "Grandchild",
    "Cousin",
    "Friend",
    "Colleague",
    "Other",
)

# How a relationship reads from the other side. Symmetric ones map to
# themselves; Friend/Colleague/Other have no clear inverse, so a linked card
# gains no back-reference for them (same rule as the original app).
INVERSE_RELATION = {
    "Child": "Parent",
    "Parent": "Child",
    "Sibling": "Sibling",
    "Grandchild": "Grandparent",
    "Grandparent": "Grandchild",
    "Cousin": "Cousin",
    "Spouse": "Spouse",
}

MAX_RELATIVES = 100
THEMES = ("dark", "light")

SCHEMA = """
CREATE TABLE IF NOT EXISTS who_was_that_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id TEXT,
    is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
    name TEXT NOT NULL,
    birthday TEXT NOT NULL DEFAULT '',
    pets TEXT NOT NULL DEFAULT '',
    known_from TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    lat REAL,
    lng REAL,
    address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS who_was_that_people_legacy
    ON who_was_that_people (legacy_id) WHERE legacy_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS who_was_that_people_one_self
    ON who_was_that_people (is_self) WHERE is_self = 1;

CREATE INDEX IF NOT EXISTS who_was_that_people_name
    ON who_was_that_people (name COLLATE NOCASE);

CREATE INDEX IF NOT EXISTS who_was_that_people_map
    ON who_was_that_people (lat, lng);

CREATE INDEX IF NOT EXISTS who_was_that_people_self
    ON who_was_that_people (is_self, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS who_was_that_relatives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL
        REFERENCES who_was_that_people (id) ON DELETE CASCADE,
    legacy_id TEXT,
    relation_type TEXT NOT NULL DEFAULT 'Other',
    name TEXT NOT NULL DEFAULT '',
    lat REAL,
    lng REAL,
    address TEXT NOT NULL DEFAULT '',
    linked_person_id INTEGER
        REFERENCES who_was_that_people (id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS who_was_that_relatives_legacy
    ON who_was_that_relatives (legacy_id) WHERE legacy_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS who_was_that_relatives_person
    ON who_was_that_relatives (person_id, position);

CREATE INDEX IF NOT EXISTS who_was_that_relatives_link
    ON who_was_that_relatives (linked_person_id);

CREATE INDEX IF NOT EXISTS who_was_that_relatives_name
    ON who_was_that_relatives (name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS who_was_that_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    theme TEXT NOT NULL DEFAULT 'dark' CHECK (theme IN ('dark', 'light')),
    find_me_on_login INTEGER NOT NULL DEFAULT 0
        CHECK (find_me_on_login IN (0, 1)),
    find_me_asked INTEGER NOT NULL DEFAULT 0 CHECK (find_me_asked IN (0, 1)),
    updated_at TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    """Create the app's tables. Safe to run on every startup."""

    db = get_db()
    db.executescript(SCHEMA)
    db.execute(
        """
        INSERT OR IGNORE INTO who_was_that_settings
            (id, theme, find_me_on_login, find_me_asked, updated_at)
        VALUES (1, 'dark', 0, 0, ?)
        """,
        (utc_now(),),
    )
    db.commit()


# --- settings ---------------------------------------------------------------


def get_settings() -> dict:
    row = get_db().execute(
        "SELECT theme, find_me_on_login, find_me_asked FROM who_was_that_settings WHERE id = 1"
    ).fetchone()
    if not row:
        return {"theme": "dark", "find_me_on_login": False, "find_me_asked": False}
    return {
        "theme": row["theme"],
        "find_me_on_login": bool(row["find_me_on_login"]),
        "find_me_asked": bool(row["find_me_asked"]),
    }


def update_settings(
    *,
    theme: str | None = None,
    find_me_on_login: bool | None = None,
    find_me_asked: bool | None = None,
) -> dict:
    current = get_settings()
    db = get_db()
    db.execute(
        """
        UPDATE who_was_that_settings
        SET theme = ?, find_me_on_login = ?, find_me_asked = ?, updated_at = ?
        WHERE id = 1
        """,
        (
            theme if theme in THEMES else current["theme"],
            1 if (find_me_on_login if find_me_on_login is not None else current["find_me_on_login"]) else 0,
            1 if (find_me_asked if find_me_asked is not None else current["find_me_asked"]) else 0,
            utc_now(),
        ),
    )
    db.commit()
    return get_settings()


# --- reading ----------------------------------------------------------------


def _relative_dict(row) -> dict:
    return {
        "id": row["id"],
        "type": row["relation_type"],
        "name": row["name"],
        "lat": row["lat"],
        "lng": row["lng"],
        "address": row["address"],
        "linked_person_id": row["linked_person_id"],
        "location": (
            {"lat": row["lat"], "lng": row["lng"]}
            if row["lat"] is not None and row["lng"] is not None
            else None
        ),
    }


def _person_dict(row, relatives: list[dict]) -> dict:
    return {
        "id": row["id"],
        "is_self": bool(row["is_self"]),
        "name": row["name"],
        "birthday": row["birthday"],
        "pets": row["pets"],
        "known_from": row["known_from"],
        "notes": row["notes"],
        "lat": row["lat"],
        "lng": row["lng"],
        "address": row["address"],
        "location": (
            {"lat": row["lat"], "lng": row["lng"]}
            if row["lat"] is not None and row["lng"] is not None
            else None
        ),
        "relatives": relatives,
    }


def _relatives_by_person(person_ids: list[int]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {person_id: [] for person_id in person_ids}
    if not person_ids:
        return grouped

    placeholders = ",".join("?" for _ in person_ids)
    rows = get_db().execute(
        f"""
        SELECT * FROM who_was_that_relatives
        WHERE person_id IN ({placeholders})
        ORDER BY position ASC, id ASC
        """,
        tuple(person_ids),
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["person_id"], []).append(_relative_dict(row))
    return grouped


def _serialize_rows(rows) -> list[dict]:
    grouped = _relatives_by_person([row["id"] for row in rows])
    return [_person_dict(row, grouped.get(row["id"], [])) for row in rows]


def list_people(query: str = "", *, include_self: bool = False) -> list[dict]:
    """Everyone pinned on the map, sorted the way the old list sorted them."""

    db = get_db()
    sql = "SELECT * FROM who_was_that_people WHERE is_self = 0"
    params: list[Any] = []
    cleaned = (query or "").strip()[:120]
    if cleaned:
        escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND name LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    sql += " ORDER BY name COLLATE NOCASE ASC, id ASC"

    rows = db.execute(sql, tuple(params)).fetchall()
    people = _serialize_rows(rows)
    if include_self:
        self_row = get_self()
        if self_row:
            people.insert(0, self_row)
    return people


def get_person(person_id: int) -> dict | None:
    row = get_db().execute(
        "SELECT * FROM who_was_that_people WHERE id = ?", (person_id,)
    ).fetchone()
    if not row:
        return None
    return _serialize_rows([row])[0]


def get_self() -> dict | None:
    row = get_db().execute(
        "SELECT * FROM who_was_that_people WHERE is_self = 1"
    ).fetchone()
    if not row:
        return None
    return _serialize_rows([row])[0]


def state_payload() -> dict:
    """Everything the map screen needs in one same-origin JSON response."""

    db = get_db()
    all_rows = db.execute(
        "SELECT * FROM who_was_that_people ORDER BY is_self DESC, name COLLATE NOCASE ASC, id ASC"
    ).fetchall()
    grouped = _relatives_by_person([row["id"] for row in all_rows])
    people: list[dict] = []
    self_card: dict | None = None
    for row in all_rows:
        person = _person_dict(row, grouped.get(row["id"], []))
        if person["is_self"]:
            self_card = person
        else:
            people.append(person)

    return {
        "self": self_card,
        "people": people,
        "settings": get_settings(),
        "map": {"default": {"lat": 30, "lng": 0, "zoom": 2}, "located_zoom": 16},
    }


# --- validation -------------------------------------------------------------


def _coerce_coordinate_pair(lat, lng) -> tuple[float | None, float | None, str | None]:
    lat_text = clean_text(lat, 32)
    lng_text = clean_text(lng, 32)
    if not lat_text and not lng_text:
        return None, None, None
    if not lat_text or not lng_text:
        return None, None, "A location needs both a latitude and a longitude."
    try:
        lat_value = float(lat_text)
        lng_value = float(lng_text)
    except (TypeError, ValueError):
        return None, None, "That location is not a pair of numbers."
    if not -90 <= lat_value <= 90 or not -180 <= lng_value <= 180:
        return None, None, "That location is outside the map."
    return round(lat_value, 6), round(lng_value, 6), None


def validate_relatives(raw) -> tuple[list[dict] | None, str | None]:
    if raw in (None, ""):
        return [], None
    if not isinstance(raw, (list, tuple)):
        return None, "Relatives must be a list."

    cleaned: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, "Each relative needs a name."
        name = clean_text(item.get("name"), 120)
        relation_type = clean_text(item.get("type") or item.get("relation_type"), 40)
        address = clean_text(item.get("address"), 300)
        lat, lng, error = _coerce_coordinate_pair(item.get("lat"), item.get("lng"))
        if error:
            return None, error
        if not name and lat is None:
            continue
        if relation_type not in RELATIVE_TYPES:
            relation_type = "Other"
        cleaned.append(
            {
                "type": relation_type,
                "name": name,
                "lat": lat,
                "lng": lng,
                "address": address,
                "position": index,
            }
        )

    if len(cleaned) > MAX_RELATIVES:
        return None, "That is more relatives than one card can hold."
    return cleaned, None


def validate_person(data) -> tuple[dict | None, list[dict] | None, str | None]:
    """Validate a person payload from JSON or a form.

    Returns ``(values, relatives, error)``. ``values`` is still returned when
    there is an error so a form can keep what the person typed.
    """

    if not isinstance(data, dict):
        return None, None, "Send the person as an object."

    values = {
        "name": clean_text(data.get("name"), 120),
        "birthday": clean_text(data.get("birthday"), 10),
        "pets": clean_text(data.get("pets"), 200),
        "known_from": clean_text(data.get("known_from"), 200),
        "notes": clean_text(data.get("notes"), 5000),
        "address": clean_text(data.get("address"), 300),
    }
    lat, lng, error = _coerce_coordinate_pair(data.get("lat"), data.get("lng"))
    values["lat"] = lat
    values["lng"] = lng

    relatives = None
    if not error:
        relatives, error = validate_relatives(data.get("relatives"))

    if error:
        return values, relatives, error

    if not values["name"]:
        return values, relatives, "Give them a name first."

    if values["birthday"]:
        try:
            date.fromisoformat(values["birthday"])
        except ValueError:
            return values, relatives, "Use a real birthday (YYYY-MM-DD)."

    return values, relatives, None


def person_data_from_form(form) -> dict:
    """Read the plain HTML form into the same shape ``validate_person`` wants."""

    relatives: list[dict] = []
    types = form.getlist("relative_type")
    names = form.getlist("relative_name")
    lats = form.getlist("relative_lat")
    lngs = form.getlist("relative_lng")
    addresses = form.getlist("relative_address")
    for index, relation_type in enumerate(types):
        relatives.append(
            {
                "type": relation_type,
                "name": names[index] if index < len(names) else "",
                "lat": lats[index] if index < len(lats) else "",
                "lng": lngs[index] if index < len(lngs) else "",
                "address": addresses[index] if index < len(addresses) else "",
            }
        )

    return {
        "name": form.get("name"),
        "birthday": form.get("birthday"),
        "pets": form.get("pets"),
        "known_from": form.get("known_from"),
        "notes": form.get("notes"),
        "address": form.get("address"),
        "lat": form.get("lat"),
        "lng": form.get("lng"),
        "relatives": relatives,
        "is_self": form.get("is_self") == "1",
    }


# --- writing ----------------------------------------------------------------


def _replace_relatives(person_id: int, relatives: list[dict]) -> None:
    db = get_db()
    db.execute("DELETE FROM who_was_that_relatives WHERE person_id = ?", (person_id,))
    now = utc_now()
    for index, relative in enumerate(relatives):
        db.execute(
            """
            INSERT INTO who_was_that_relatives
                (person_id, relation_type, name, lat, lng, address, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                person_id,
                relative.get("type") or "Other",
                relative.get("name") or "",
                relative.get("lat"),
                relative.get("lng"),
                relative.get("address") or "",
                relative.get("position", index),
                now,
            ),
        )


def create_person(values: dict, relatives: list[dict], *, is_self: bool = False) -> int:
    db = get_db()
    if is_self:
        existing = db.execute(
            "SELECT id FROM who_was_that_people WHERE is_self = 1"
        ).fetchone()
        if existing:
            update_person(existing["id"], values, relatives)
            return int(existing["id"])

    now = utc_now()
    cursor = db.execute(
        """
        INSERT INTO who_was_that_people
            (is_self, name, birthday, pets, known_from, notes, lat, lng, address,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            1 if is_self else 0,
            values["name"],
            values["birthday"],
            values["pets"],
            values["known_from"],
            values["notes"],
            values["lat"],
            values["lng"],
            values["address"],
            now,
            now,
        ),
    )
    person_id = int(cursor.lastrowid)
    _replace_relatives(person_id, relatives)
    db.commit()
    materialize_relatives(person_id)
    return person_id


def update_person(person_id: int, values: dict, relatives: list[dict]) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT id FROM who_was_that_people WHERE id = ?", (person_id,)
    ).fetchone()
    if not row:
        return False

    db.execute(
        """
        UPDATE who_was_that_people
        SET name = ?, birthday = ?, pets = ?, known_from = ?, notes = ?,
            lat = ?, lng = ?, address = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            values["name"],
            values["birthday"],
            values["pets"],
            values["known_from"],
            values["notes"],
            values["lat"],
            values["lng"],
            values["address"],
            utc_now(),
            person_id,
        ),
    )
    _replace_relatives(person_id, relatives)
    db.commit()
    materialize_relatives(person_id)
    return True


def delete_person(person_id: int) -> bool:
    """Delete a card and scrub every reference to that person.

    The old app removed the person's entry from other cards' relatives lists
    (matching by identity key, or by name at the same spot). There are no
    per-person keys any more, so matching is by name and coordinates.
    """

    db = get_db()
    row = db.execute(
        "SELECT * FROM who_was_that_people WHERE id = ?", (person_id,)
    ).fetchone()
    if not row or row["is_self"]:
        return False

    lat = row["lat"] if row["lat"] is not None else 0.0
    lng = row["lng"] if row["lng"] is not None else 0.0
    has_location = row["lat"] is not None and row["lng"] is not None

    db.execute(
        """
        DELETE FROM who_was_that_relatives
        WHERE person_id <> ?
          AND (
              linked_person_id = ?
              OR (
                  ? = 1
                  AND linked_person_id IS NULL
                  AND lower(name) = lower(?)
                  AND abs(lat - ?) < 0.000001
                  AND abs(lng - ?) < 0.000001
              )
          )
        """,
        (
            person_id,
            person_id,
            1 if has_location else 0,
            row["name"],
            lat,
            lng,
        ),
    )
    db.execute("DELETE FROM who_was_that_people WHERE id = ?", (person_id,))
    db.commit()
    return True


def remove_relative(person_id: int, relative_id: int) -> bool:
    """Remove a relationship, and the matching back-reference."""

    db = get_db()
    relate = db.execute(
        "SELECT * FROM who_was_that_relatives WHERE id = ? AND person_id = ?",
        (relative_id, person_id),
    ).fetchone()
    if not relate:
        return False
    owner = db.execute(
        "SELECT id, name FROM who_was_that_people WHERE id = ?", (person_id,)
    ).fetchone()

    db.execute("DELETE FROM who_was_that_relatives WHERE id = ?", (relative_id,))

    target_id = relate["linked_person_id"]
    if target_id is None:
        matched = _find_same_person(
            relate["name"], relate["lat"], relate["lng"], exclude_id=person_id
        )
        target_id = matched["id"] if matched else None

    if target_id and owner:
        db.execute(
            """
            DELETE FROM who_was_that_relatives
            WHERE person_id = ?
              AND (linked_person_id = ? OR lower(name) = lower(?))
            """,
            (target_id, person_id, owner["name"]),
        )

    db.commit()
    return True


# --- relatives becoming their own pins -------------------------------------


def _near(first: float | None, second: float | None) -> bool:
    if first is None or second is None:
        return False
    return abs(first - second) < 0.000001


def _find_same_person(
    name: str, lat: float | None, lng: float | None, *, exclude_id: int | None = None
):
    if not name or lat is None or lng is None:
        return None
    sql = """
        SELECT * FROM who_was_that_people
        WHERE lower(name) = lower(?)
          AND abs(lat - ?) < 0.000001
          AND abs(lng - ?) < 0.000001
    """
    params: list[Any] = [name, lat, lng]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return get_db().execute(sql + " ORDER BY id LIMIT 1", tuple(params)).fetchone()


def materialize_relatives(person_id: int) -> bool:
    """Give located relatives their own card and pin, and link both ways.

    This is the server-side version of the original ``materializeRelatives``:
    a relative with a location becomes its own card (unless they are you),
    and the two cards list each other with the inverse relationship.
    """

    db = get_db()
    person = db.execute(
        "SELECT * FROM who_was_that_people WHERE id = ?", (person_id,)
    ).fetchone()
    if not person:
        return False

    self_row = db.execute(
        "SELECT * FROM who_was_that_people WHERE is_self = 1"
    ).fetchone()
    relatives = db.execute(
        "SELECT * FROM who_was_that_relatives WHERE person_id = ? ORDER BY position, id",
        (person_id,),
    ).fetchall()

    changed = False
    now = utc_now()

    for relative in relatives:
        name = (relative["name"] or "").strip()
        lat = relative["lat"]
        lng = relative["lng"]
        if not name or lat is None or lng is None:
            continue

        # The relative is you: link to your own card instead of cloning it.
        if (
            self_row
            and self_row["id"] != person_id
            and (
                relative["linked_person_id"] == self_row["id"]
                or (
                    self_row["name"]
                    and self_row["name"].strip().lower() == name.lower()
                    and _near(self_row["lat"], lat)
                    and _near(self_row["lng"], lng)
                )
            )
        ):
            if relative["linked_person_id"] != self_row["id"]:
                db.execute(
                    "UPDATE who_was_that_relatives SET linked_person_id = ? WHERE id = ?",
                    (self_row["id"], relative["id"]),
                )
                changed = True
            continue

        target = None
        if relative["linked_person_id"]:
            target = db.execute(
                "SELECT * FROM who_was_that_people WHERE id = ?",
                (relative["linked_person_id"],),
            ).fetchone()
        if target is None:
            target = _find_same_person(name, lat, lng, exclude_id=person_id)
        if target is None:
            known_from = (
                f"Relative of {person['name']}" if person["name"] else "Relative"
            )
            cursor = db.execute(
                """
                INSERT INTO who_was_that_people
                    (is_self, name, birthday, pets, known_from, notes, lat, lng,
                     address, created_at, updated_at)
                VALUES (0, ?, '', '', ?, '', ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    known_from,
                    lat,
                    lng,
                    relative["address"] or "",
                    now,
                    now,
                ),
            )
            target = db.execute(
                "SELECT * FROM who_was_that_people WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
            changed = True

        if relative["linked_person_id"] != target["id"]:
            db.execute(
                "UPDATE who_was_that_relatives SET linked_person_id = ? WHERE id = ?",
                (target["id"], relative["id"]),
            )
            changed = True

        inverse = INVERSE_RELATION.get(relative["relation_type"])
        if not inverse or not person["name"]:
            continue

        already = db.execute(
            """
            SELECT 1 FROM who_was_that_relatives
            WHERE person_id = ?
              AND (
                  linked_person_id = ?
                  OR (lower(name) = lower(?) AND relation_type = ?)
              )
            LIMIT 1
            """,
            (target["id"], person_id, person["name"], inverse),
        ).fetchone()
        if already:
            continue

        next_position = db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM who_was_that_relatives WHERE person_id = ?",
            (target["id"],),
        ).fetchone()["next"]
        db.execute(
            """
            INSERT INTO who_was_that_relatives
                (person_id, relation_type, name, lat, lng, address, linked_person_id,
                 position, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target["id"],
                inverse,
                person["name"],
                person["lat"],
                person["lng"],
                person["address"] or "",
                person_id,
                next_position,
                now,
            ),
        )
        changed = True

    if changed:
        db.commit()
    return changed
