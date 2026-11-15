"""Application registry and router for the Thompson modular monolith."""

from __future__ import annotations

import importlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from flask import Blueprint, current_app, request

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROUTE_RE = re.compile(r"^/[a-z0-9][a-z0-9-]*(?:/[a-z0-9][a-z0-9-]*)*$")
HEX_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ICON_RE = re.compile(r"^[a-z0-9-]{1,40}$")
DEFAULT_ACCENTS = ("#C9764F", "#6E8A79", "#8A7AA8", "#B58A45")
MODULE_MANIFEST = "app.json"

RESERVED_ROUTES = {
    "/",
    "/offline",
    "/api",
    "/static",
    "/healthz",
    "/manifest.webmanifest",
    "/sw.js",
    "/robots.txt",
    "/favicon.ico",
}


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppDefinition:
    id: str
    name: str
    route: str
    icon: str
    description: str
    accent: str
    order: int
    enabled: bool


def _text(value, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{label} must be a non-empty string.")
    return value.strip()[:max_length]


def load_registry(path: Path) -> list[AppDefinition]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RegistryError(f"Application registry not found: {path}") from error
    except json.JSONDecodeError as error:
        raise RegistryError(f"Application registry is not valid JSON: {error}") from error

    applications = raw.get("applications") if isinstance(raw, dict) else None
    if not isinstance(applications, list):
        raise RegistryError('Application registry needs an "applications" array.')

    seen_ids: set[str] = set()
    seen_routes: set[str] = set()
    definitions: list[AppDefinition] = []

    for index, item in enumerate(applications):
        label = f"Application {index + 1}"
        if not isinstance(item, dict):
            raise RegistryError(f"{label} must be an object.")

        app_id = _text(item.get("id"), f"{label} id", 64).lower()
        if not ID_RE.fullmatch(app_id):
            raise RegistryError(
                f'{label} id "{app_id}" must be a Python-safe name: lowercase letters, numbers and underscores, starting with a letter.'
            )
        if app_id in seen_ids:
            raise RegistryError(f'Application id "{app_id}" is duplicated.')
        seen_ids.add(app_id)

        name = _text(item.get("name"), f'Application "{app_id}" name', 60)
        route = _text(item.get("route"), f'Application "{app_id}" route', 100)
        if not ROUTE_RE.fullmatch(route):
            raise RegistryError(
                f'Application "{app_id}" route must look like "/calendar".'
            )
        if route in RESERVED_ROUTES or any(
            route == reserved or route.startswith(f"{reserved}/")
            for reserved in RESERVED_ROUTES
            if reserved != "/"
        ):
            raise RegistryError(
                f'Application route "{route}" is reserved by the hub.'
            )
        if route in seen_routes:
            raise RegistryError(f'Application route "{route}" is duplicated.')
        seen_routes.add(route)

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RegistryError(f'Application "{app_id}" enabled must be true or false.')

        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""
        description = description.strip()[:180]

        icon = item.get("icon", "grid")
        if not isinstance(icon, str) or not ICON_RE.fullmatch(icon):
            icon = "grid"

        accent = item.get("accent") or DEFAULT_ACCENTS[index % len(DEFAULT_ACCENTS)]
        if not isinstance(accent, str) or not HEX_COLOUR_RE.fullmatch(accent):
            accent = DEFAULT_ACCENTS[index % len(DEFAULT_ACCENTS)]

        order_raw = item.get("order", index)
        if not isinstance(order_raw, int):
            raise RegistryError(f'Application "{app_id}" order must be an integer.')

        definitions.append(
            AppDefinition(
                id=app_id,
                name=name,
                route=route,
                icon=icon,
                description=description,
                accent=accent.upper(),
                order=order_raw,
                enabled=enabled,
            )
        )

    return sorted(definitions, key=lambda app: (app.order, app.name.lower()))


def _module_definitions(
    directory: Path, known_ids: set[str], known_routes: set[str]
) -> list[AppDefinition]:
    """Applications that ship their own ``app.json`` beside ``app.py``.

    A module folder dropped into ``applications/`` registers itself, so a new
    application never needs an entry in ``config/apps.json``. Anything the
    registry already lists wins, and a broken manifest is skipped rather than
    taking the whole hub down.
    """

    logger = logging.getLogger(__name__)
    definitions: list[AppDefinition] = []
    if not directory.is_dir():
        return definitions

    for index, manifest in enumerate(sorted(directory.glob(f"*/{MODULE_MANIFEST}"))):
        folder = manifest.parent.name
        try:
            item = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("Skipping module manifest %s: %s", manifest, error)
            continue
        if not isinstance(item, dict):
            logger.warning("Skipping module manifest %s: expected an object.", manifest)
            continue

        app_id = str(item.get("id") or folder).strip().lower()
        if app_id != folder or not ID_RE.fullmatch(app_id):
            logger.warning(
                "Skipping module manifest %s: id must match the folder name \"%s\".",
                manifest,
                folder,
            )
            continue
        if app_id in known_ids:
            continue

        name = item.get("name")
        route = item.get("route")
        if not isinstance(name, str) or not name.strip() or not isinstance(route, str):
            logger.warning("Skipping module manifest %s: name and route are required.", manifest)
            continue
        name = name.strip()[:60]
        route = route.strip()
        if not ROUTE_RE.fullmatch(route):
            logger.warning('Skipping module manifest %s: route must look like "/calendar".', manifest)
            continue
        reserved = route in RESERVED_ROUTES or any(
            route.startswith(f"{entry}/")
            for entry in RESERVED_ROUTES
            if entry != "/"
        )
        if reserved or route in known_routes:
            logger.warning('Skipping module manifest %s: route "%s" is taken.', manifest, route)
            continue

        icon = item.get("icon", "grid")
        if not isinstance(icon, str) or not ICON_RE.fullmatch(icon):
            icon = "grid"

        description = item.get("description", "")
        if not isinstance(description, str):
            description = ""

        accent = item.get("accent") or DEFAULT_ACCENTS[0]
        if not isinstance(accent, str) or not HEX_COLOUR_RE.fullmatch(accent):
            accent = DEFAULT_ACCENTS[0]

        order = item.get("order", len(known_ids) + index)
        if not isinstance(order, int):
            order = len(known_ids) + index

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            enabled = True

        known_ids.add(app_id)
        known_routes.add(route)
        definitions.append(
            AppDefinition(
                id=app_id,
                name=name,
                route=route,
                icon=icon,
                description=description.strip()[:180],
                accent=accent.upper(),
                order=order,
                enabled=enabled,
            )
        )

    return definitions


class AppManager:
    def __init__(self, app, registry_path: Path):
        self.app = app
        self.registry_path = registry_path
        self.apps = load_registry(registry_path)
        self.apps.extend(
            _module_definitions(
                Path(app.root_path).parent / "applications",
                {definition.id for definition in self.apps},
                {definition.route for definition in self.apps},
            )
        )
        self.apps.sort(key=lambda definition: (definition.order, definition.name.lower()))
        self.by_blueprint: dict[str, AppDefinition] = {}

        for definition in self.apps:
            if definition.enabled:
                self._register(definition)

        self._inject_current_application()

    def _module_path(self, definition: AppDefinition):
        return Path(self.app.root_path).parent / "applications" / definition.id / "app.py"

    def _register(self, definition: AppDefinition) -> None:
        module_file = self._module_path(definition)
        if not module_file.is_file():
            raise RegistryError(
                f'Application "{definition.id}" is enabled but {module_file} is missing.'
            )

        try:
            module = importlib.import_module(f"applications.{definition.id}.app")
        except Exception as error:
            raise RegistryError(
                f'Could not import application "{definition.id}": {error}'
            ) from error

        blueprint = getattr(module, "blueprint", None)
        if not isinstance(blueprint, Blueprint):
            raise RegistryError(
                f'Application "{definition.id}" must export a Flask Blueprint named "blueprint".'
            )
        if blueprint.name != definition.id:
            raise RegistryError(
                f'Application "{definition.id}" blueprint name must be "{definition.id}".'
            )

        init_hook = getattr(module, "init_app", None)
        if callable(init_hook):
            with self.app.app_context():
                init_hook(self.app)

        self.app.register_blueprint(blueprint, url_prefix=definition.route)
        self.by_blueprint[blueprint.name] = definition

    def _inject_current_application(self) -> None:
        @self.app.context_processor
        def inject_current_application():
            blueprint_name = request.blueprint
            definition = (
                self.by_blueprint.get(blueprint_name) if blueprint_name else None
            )
            return {"current_application": definition}

    def enabled_apps(self) -> list[AppDefinition]:
        return [app for app in self.apps if app.enabled]


def init_app(app) -> None:
    registry_path = Path(app.config["APPS_CONFIG"])
    manager = AppManager(app, registry_path)
    app.extensions["thompson_apps"] = manager


def get_manager() -> AppManager:
    return current_app.extensions["thompson_apps"]
