"""One-time importer for the retired Who Was That? notebook.

The old app never stored anything on the device. Your notebook lived as one
encrypted event on the relays you chose. There was no export button and no
local file to copy, so the export step has to happen in the old app while you
are logged in (see docs/WHO_WAS_THAT_MIGRATION.md):

    // in the browser console of the old app, while logged in
    const { getState } = await import('./js/state.js');
    copy(JSON.stringify(getState().dataset));

That is a read-only export of the *public domain data only*: your card, the
people, their relatives, and the theme. It contains no private material, no
relay credentials and no chat.

This importer maps that JSON into the hub's central SQLite database:

    python -m applications.who_was_that.migrate --file notebook.json --dry-run
    python -m applications.who_was_that.migrate --file notebook.json

It is idempotent: people are matched on the old card id (``legacy_id``), so
running it twice imports nothing the second time. The hub database is backed up
before anything is written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hub import create_app  # noqa: E402  (needs ROOT on sys.path first)

from applications.who_was_that.logic import (  # noqa: E402
    RELATIVE_TYPES,
    THEMES,
    ensure_schema,
    materialize_relatives,
    update_settings,
)
from shared.database import get_db  # noqa: E402
from shared.utilities import clean_text, utc_now  # noqa: E402


def _resolve(path_value: str) -> Path:
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def _number(value) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _coordinates(card) -> tuple[float | None, float | None]:
    location = card.get("location") or {}
    lat = _number(card.get("lat", location.get("lat")))
    lng = _number(card.get("lng", location.get("lng")))
    if lat is None or lng is None:
        return None, None
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return None, None
    return lat, lng


def _birthday(value) -> str:
    cleaned = clean_text(value, 10)
    if not cleaned:
        return ""
    try:
        date.fromisoformat(cleaned)
    except ValueError:
        return ""
    return cleaned


def _relatives(card) -> list[dict]:
    rows = []
    for index, relative in enumerate(card.get("relatives") or []):
        if not isinstance(relative, dict):
            continue
        name = clean_text(relative.get("name"), 120)
        lat, lng = _coordinates(relative)
        if not name and lat is None:
            continue
        relation_type = clean_text(relative.get("type"), 40)
        if relation_type not in RELATIVE_TYPES:
            relation_type = "Other"
        rows.append(
            {
                "type": relation_type,
                "name": name,
                "lat": lat,
                "lng": lng,
                "address": clean_text(relative.get("address"), 300),
                "position": index,
            }
        )
    return rows


def load_dataset(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"No such export file: {path}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"That file is not valid JSON: {error}")

    if isinstance(raw, dict) and isinstance(raw.get("dataset"), dict):
        raw = raw["dataset"]

    if not isinstance(raw, dict):
        raise SystemExit("Expected the old notebook object at the top level.")

    if "kind" in raw and "content" in raw:
        raise SystemExit(
            "This looks like a raw relay event (encrypted notebook), not an export.\n"
            "Re-run the console export inside the old app while logged in; this\n"
            "importer will not touch raw relay events or identity keys."
        )

    if not isinstance(raw.get("people"), list):
        raise SystemExit('That export has no "people" array — is it the old notebook?')

    return raw


def backup_database(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    backups = db_path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backups / f"thompson-{stamp}.db"
    source = sqlite3.connect(str(db_path))
    destination = sqlite3.connect(str(target))
    try:
        with destination:
            source.backup(destination)
    finally:
        source.close()
        destination.close()
    return target


def _existing_legacy_ids(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT legacy_id FROM who_was_that_people WHERE legacy_id IS NOT NULL"
        ).fetchall()
        return {row[0] for row in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        connection.close()


def _cards(dataset: dict) -> list[tuple[dict, bool]]:
    cards: list[tuple[dict, bool]] = []
    own = dataset.get("self")
    if isinstance(own, dict) and (own.get("name") or "").strip():
        cards.append((own, True))
    for person in dataset.get("people") or []:
        if isinstance(person, dict) and (person.get("name") or "").strip():
            cards.append((person, False))
    return cards


def _legacy_key(card: dict, is_self: bool) -> str:
    identifier = clean_text(card.get("id"), 64)
    if identifier:
        return identifier
    return "self" if is_self else f"name:{clean_text(card.get('name'), 120).lower()}"


def run(args) -> int:
    dataset = load_dataset(_resolve(args.file))
    db_path = _resolve(args.db)
    cards = _cards(dataset)

    known = _existing_legacy_ids(db_path)
    pending = [(card, is_self) for card, is_self in cards if _legacy_key(card, is_self) not in known]

    settings = dataset.get("settings") or {}
    theme = settings.get("theme") if settings.get("theme") in THEMES else None
    find_me = settings.get("shareLocationOnLogin")

    print(f"Export file : {_resolve(args.file)}")
    print(f"Database    : {db_path}")
    print(f"Old cards   : {len(cards)} ({sum(1 for _, s in cards if s)} own card, "
          f"{sum(1 for _, s in cards if not s)} people)")
    print(f"To import   : {len(pending)}   Already imported: {len(cards) - len(pending)}")

    relative_total = sum(len(_relatives(card)) for card, _ in pending)
    print(f"Relatives   : {relative_total} rows to map")
    print(f"Settings    : theme={theme or '(unchanged)'} "
          f"find_me_on_login={find_me!r}")

    if args.dry_run:
        print("\nDry run — nothing written.")
        for card, is_self in pending[:10]:
            marker = "you" if is_self else "person"
            print(f"  would import {marker}: {clean_text(card.get('name'), 120)} "
                  f"({clean_text(card.get('known_from'), 60) or 'no source'})")
        print(
            "  identity keys, relay lists, shared-card bookkeeping and any "
            "removed photo fields in the export are ignored"
        )
        return 0

    backup = backup_database(db_path)
    if backup:
        print(f"\nBacked up database to {backup}")

    app = create_app({"DATABASE": str(db_path)})
    created_people = 0
    created_relatives = 0
    imported_self = 0

    with app.app_context():
        ensure_schema()
        db = get_db()

        if theme or find_me is not None:
            update_settings(
                theme=theme,
                find_me_on_login=None if find_me is None else bool(find_me),
                find_me_asked=None if find_me is None else True,
            )

        new_ids: list[int] = []
        for card, is_self in pending:
            legacy_key = _legacy_key(card, is_self)
            lat, lng = _coordinates(card)
            now = utc_now()
            cursor = db.execute(
                """
                INSERT INTO who_was_that_people
                    (legacy_id, is_self, name, birthday, pets, known_from, notes,
                     lat, lng, address, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_key,
                    1 if is_self else 0,
                    clean_text(card.get("name"), 120),
                    _birthday(card.get("birthday")),
                    clean_text(card.get("pets"), 200),
                    "" if is_self else clean_text(card.get("knownFrom"), 200),
                    clean_text(card.get("notes"), 5000),
                    lat,
                    lng,
                    clean_text(card.get("address"), 300),
                    now,
                    now,
                ),
            )
            person_id = int(cursor.lastrowid)
            new_ids.append(person_id)
            created_people += 1
            if is_self:
                imported_self += 1

            for relative in _relatives(card):
                db.execute(
                    """
                    INSERT INTO who_was_that_relatives
                        (person_id, relation_type, name, lat, lng, address,
                         position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        person_id,
                        relative["type"],
                        relative["name"],
                        relative["lat"],
                        relative["lng"],
                        relative["address"],
                        relative["position"],
                        now,
                    ),
                )
                created_relatives += 1
        db.commit()

        # Link relatives that already have their own card (and create cards for
        # located relatives that do not), exactly as saving a card does.
        for person_id in new_ids:
            materialize_relatives(person_id)

        people_total = db.execute(
            "SELECT COUNT(*) AS count FROM who_was_that_people"
        ).fetchone()["count"]
        relative_count = db.execute(
            "SELECT COUNT(*) AS count FROM who_was_that_relatives"
        ).fetchone()["count"]

        print(f"\nImported people      : {created_people} (of which own card: {imported_self})")
        print(f"Imported relatives   : {created_relatives}")
        print(f"Total people in db   : {people_total}")
        print(f"Total relatives in db: {relative_count}")

        print("\nSpot check (first 5 people):")
        for row in db.execute(
            "SELECT name, known_from, lat, lng FROM who_was_that_people "
            "ORDER BY is_self DESC, name COLLATE NOCASE LIMIT 5"
        ).fetchall():
            where = f"{row['lat']}, {row['lng']}" if row["lat"] is not None else "no pin"
            print(f"  {row['name']} — {row['known_from'] or 'no source'} — {where}")

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import the old Who Was That? notebook export into Thompson."
    )
    parser.add_argument("--file", required=True, help="JSON export of the old notebook.")
    parser.add_argument(
        "--db",
        default="data/thompson.db",
        help="Path to the central hub database (default: data/thompson.db).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be imported without writing anything.",
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
