"""Who Was That? routes, mounted at /who-was-that by the hub app manager.

Every handler stays relative to the blueprint prefix. There is no login, no
``current_user`` and no CSRF token: the hub is open inside the tailnet.
"""

from __future__ import annotations

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from shared.utilities import clean_text

from . import map_services
from .logic import (
    RELATIVE_TYPES,
    THEMES,
    create_person,
    delete_person,
    get_person,
    get_self,
    get_settings,
    list_people,
    person_data_from_form,
    remove_relative,
    state_payload,
    update_person,
    update_settings,
    validate_person,
)

blueprint = Blueprint(
    "who_was_that",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)


# --- helpers ----------------------------------------------------------------


def _json_error(message: str, status: int):
    return jsonify({"ok": False, "error": message}), status


def _json_state(extra: dict | None = None):
    payload = {"ok": True, "state": state_payload()}
    if extra:
        payload.update(extra)
    return jsonify(payload)


def _request_payload() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return person_data_from_form(request.form)


def _blank_values() -> dict:
    return {
        "name": "",
        "birthday": "",
        "pets": "",
        "known_from": "",
        "notes": "",
        "address": "",
        "lat": None,
        "lng": None,
    }


def _relative_rows(relatives) -> list[dict]:
    rows = []
    for relative in relatives or []:
        rows.append(
            {
                "type": relative.get("type") or "Other",
                "name": relative.get("name") or "",
                "lat": relative.get("lat"),
                "lng": relative.get("lng"),
                "address": relative.get("address") or "",
            }
        )
    return rows


def _form_page(
    *,
    person: dict | None,
    values: dict,
    relatives,
    error: str | None,
    status: int = 200,
):
    is_self = bool(person["is_self"]) if person else bool(values.get("is_self"))
    form_action = (
        url_for("who_was_that.person_update", person_id=person["id"])
        if person
        else url_for("who_was_that.person_create")
    )
    if is_self:
        headline = "My information"
    elif person:
        headline = "Edit person"
    else:
        headline = "Add person"

    people = [
        candidate
        for candidate in list_people()
        if not person or candidate["id"] != person["id"]
    ]

    return (
        render_template(
            "who_was_that/person_form.html",
            person=person,
            values=values,
            relatives=_relative_rows(relatives),
            error=error,
            form_action=form_action,
            headline=headline,
            is_self=is_self,
            people=people,
            relative_types=RELATIVE_TYPES,
        ),
        status,
    )


# --- pages ------------------------------------------------------------------


@blueprint.get("/", strict_slashes=False)
def index():
    """The map screen: full-screen map with its panels and modals."""

    return render_template("who_was_that/app.html", settings=get_settings())


@blueprint.get("/people")
def people():
    query = clean_text(request.args.get("q"), 120)
    rows = list_people(query)
    return render_template(
        "who_was_that/people.html",
        people=rows,
        query=query,
        total=len(rows),
        removed=request.args.get("removed") == "1",
    )


@blueprint.get("/people/new")
def person_new():
    wants_self = request.args.get("self") == "1"
    if wants_self:
        own = get_self()
        if own:
            return redirect(
                url_for("who_was_that.person_edit", person_id=own["id"])
            )
    values = _blank_values()
    values["is_self"] = wants_self
    return _form_page(
        person=None, values=values, relatives=[], error=None
    )


@blueprint.get("/people/<int:person_id>")
def person_detail(person_id: int):
    person = get_person(person_id)
    if not person:
        abort(404)
    linked = {item["id"]: item for item in list_people()}
    own = get_self()
    if own:
        linked[own["id"]] = own
    return render_template(
        "who_was_that/person.html",
        person=person,
        linked=linked,
    )


@blueprint.get("/people/<int:person_id>/edit")
def person_edit(person_id: int):
    person = get_person(person_id)
    if not person:
        abort(404)
    return _form_page(
        person=person,
        values=person,
        relatives=person["relatives"],
        error=None,
    )


@blueprint.get("/self")
def self_card():
    own = get_self()
    if own:
        return redirect(url_for("who_was_that.person_detail", person_id=own["id"]))
    return redirect(url_for("who_was_that.person_new", self="1"))


@blueprint.post("/people")
def person_create():
    data = person_data_from_form(request.form)
    values, relatives, error = validate_person(data)
    if error:
        return _form_page(
            person=None,
            values={**_blank_values(), **values, "is_self": data.get("is_self")},
            relatives=relatives or [],
            error=error,
            status=400,
        )

    person_id = create_person(values, relatives or [], is_self=bool(data.get("is_self")))
    return redirect(url_for("who_was_that.person_detail", person_id=person_id))


@blueprint.post("/people/<int:person_id>")
def person_update(person_id: int):
    person = get_person(person_id)
    if not person:
        abort(404)

    data = person_data_from_form(request.form)
    values, relatives, error = validate_person(data)
    if error:
        return _form_page(
            person=person,
            values={**person, **values},
            relatives=relatives or [],
            error=error,
            status=400,
        )

    update_person(person_id, values, relatives or [])
    return redirect(url_for("who_was_that.person_detail", person_id=person_id))


@blueprint.post("/people/<int:person_id>/delete")
def person_delete(person_id: int):
    person = get_person(person_id)
    if not person:
        abort(404)
    if person["is_self"]:
        abort(400, description="Your own card can't be deleted.")
    if not delete_person(person_id):
        abort(404)
    return redirect(url_for("who_was_that.people", removed="1"))


@blueprint.post("/people/<int:person_id>/relatives/<int:relative_id>/delete")
def relative_delete(person_id: int, relative_id: int):
    person = get_person(person_id)
    if not person:
        abort(404)
    if not remove_relative(person_id, relative_id):
        abort(404)
    return redirect(url_for("who_was_that.person_detail", person_id=person_id))


# --- same-origin JSON API ---------------------------------------------------


@blueprint.get("/api/state")
def api_state():
    return _json_state()


@blueprint.post("/api/people")
def api_person_create():
    data = _request_payload()
    values, relatives, error = validate_person(data)
    if error:
        return _json_error(error, 400)

    person_id = create_person(
        values, relatives or [], is_self=bool(data.get("is_self"))
    )
    return _json_state({"person_id": person_id})


@blueprint.put("/api/people/<int:person_id>")
@blueprint.post("/api/people/<int:person_id>")
def api_person_update(person_id: int):
    person = get_person(person_id)
    if not person:
        return _json_error("That card no longer exists.", 404)

    data = _request_payload()
    values, relatives, error = validate_person(data)
    if error:
        return _json_error(error, 400)

    update_person(person_id, values, relatives or [])
    return _json_state({"person_id": person_id})


@blueprint.delete("/api/people/<int:person_id>")
def api_person_delete(person_id: int):
    person = get_person(person_id)
    if not person:
        return _json_error("That card no longer exists.", 404)
    if person["is_self"]:
        return _json_error("Your own card can't be deleted.", 400)
    if not delete_person(person_id):
        return _json_error("That card no longer exists.", 404)
    return _json_state()


@blueprint.post("/api/people/<int:person_id>/relatives/<int:relative_id>/remove")
def api_relative_remove(person_id: int, relative_id: int):
    person = get_person(person_id)
    if not person:
        return _json_error("That card no longer exists.", 404)
    if not remove_relative(person_id, relative_id):
        return _json_error("That relationship no longer exists.", 404)
    return _json_state()


@blueprint.post("/api/settings")
def api_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("Send the settings as an object.", 400)

    theme = data.get("theme")
    if theme is not None and theme not in THEMES:
        return _json_error("Unknown theme.", 400)

    find_me = data.get("find_me_on_login")
    asked = data.get("find_me_asked")
    settings = update_settings(
        theme=theme,
        find_me_on_login=None if find_me is None else bool(find_me),
        find_me_asked=None if asked is None else bool(asked),
    )
    return jsonify({"ok": True, "settings": settings})


@blueprint.get("/api/geocode")
def api_geocode():
    query = clean_text(request.args.get("q"), 240)
    if not query:
        return jsonify({"ok": True, "results": []})
    try:
        results = map_services.search_address(query)
    except map_services.MapServiceError as error:
        return _json_error(str(error), 502)
    return jsonify({"ok": True, "results": results})


@blueprint.get("/api/reverse")
def api_reverse():
    try:
        lat = round(float(request.args.get("lat", "")), 6)
        lng = round(float(request.args.get("lng", "")), 6)
    except (TypeError, ValueError):
        return _json_error("Send a latitude and longitude.", 400)
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return _json_error("That location is outside the map.", 400)

    try:
        address = map_services.reverse_geocode(lat, lng)
    except map_services.MapServiceError as error:
        return _json_error(str(error), 502)
    return jsonify({"ok": True, "address": address or ""})


# --- map tiles (same-origin proxy, never a CDN) -----------------------------


@blueprint.get("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile(z: int, x: int, y: int):
    if z < 0 or z > 19:
        abort(404)
    limit = (1 << z) - 1
    if x < 0 or y < 0 or x > limit or y > limit:
        abort(404)

    payload = map_services.fetch_tile(z, x, y)
    reachable = payload is not None
    response = Response(payload or map_services.BLANK_TILE, mimetype="image/png")
    response.headers["Cache-Control"] = (
        "public, max-age=86400" if reachable else "no-store"
    )
    return response
