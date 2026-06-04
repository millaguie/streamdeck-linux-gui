"""Plugin manifest schema and validation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import yaml


class VariableType(Enum):
    """Supported variable types for plugin configuration."""

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    FILE_PATH = "file_path"
    DIR_PATH = "dir_path"
    URL = "url"
    PASSWORD = "password"  # Will be masked in UI
    CERTIFICATE = "certificate"  # Special file picker for cert files
    SELECT = "select"  # Dropdown — requires ``choices`` on the variable


class LifecycleMode(Enum):
    """Plugin lifecycle modes."""

    ALWAYS_RUNNING = "always_running"  # Plugin starts with app and always runs
    ON_VISIBLE = "on_visible"  # Plugin starts only when button is visible
    ON_DEMAND = "on_demand"  # Plugin can be called when needed (future)


@dataclass
class PluginVariable:
    """Definition of a configurable variable in a plugin."""

    name: str
    type: VariableType
    description: str
    required: bool = True
    default: Any | None = None
    # ``choices`` is only meaningful for ``SELECT``.  Each entry is either
    # a bare value (string shown verbatim) or ``{"value": ..., "label": ...}``
    # for a separate stored value / display label.
    choices: list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "type": self.type.value,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "choices": self.choices,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginVariable":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            type=VariableType(data["type"]),
            description=data["description"],
            required=data.get("required", True),
            default=data.get("default"),
            choices=data.get("choices"),
        )


@dataclass
class PluginManifest:
    """Plugin manifest definition."""

    # Required fields
    name: str
    version: str
    description: str
    author: str

    # Plugin script
    entry_point: str  # Path to the main Python script (relative to plugin dir)

    # Lifecycle
    lifecycle_mode: LifecycleMode = LifecycleMode.ALWAYS_RUNNING

    # Configuration
    variables: list[PluginVariable] = field(default_factory=list)

    # Permissions
    can_switch_page: bool = False  # Whether plugin can request page switches

    # Retry configuration
    max_retries: int = 3  # Maximum number of restart attempts on crash
    retry_delay: int = 5  # Delay in seconds between retries

    # Optional metadata
    homepage: str | None = None
    license: str | None = None
    icon: str | None = None  # Path to plugin icon (relative to plugin dir)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entry_point": self.entry_point,
            "lifecycle_mode": self.lifecycle_mode.value,
            "variables": [v.to_dict() for v in self.variables],
            "can_switch_page": self.can_switch_page,
            "max_retries": self.max_retries,
            "retry_delay": self.retry_delay,
            "homepage": self.homepage,
            "license": self.license,
            "icon": self.icon,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            version=data["version"],
            description=data["description"],
            author=data["author"],
            entry_point=data["entry_point"],
            lifecycle_mode=LifecycleMode(data.get("lifecycle_mode", "always_running")),
            variables=[PluginVariable.from_dict(v) for v in data.get("variables", [])],
            can_switch_page=data.get("can_switch_page", False),
            max_retries=data.get("max_retries", 3),
            retry_delay=data.get("retry_delay", 5),
            homepage=data.get("homepage"),
            license=data.get("license"),
            icon=data.get("icon"),
        )

    @classmethod
    def load_from_file(cls, manifest_path: str) -> "PluginManifest":
        """Load manifest from YAML file."""
        with open(manifest_path) as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data)

    def save_to_file(self, manifest_path: str) -> None:
        """Save manifest to YAML file."""
        with open(manifest_path, "w") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)

    def validate(self) -> list[str]:
        """Validate manifest and return list of errors."""
        errors = []

        if not self.name:
            errors.append("Plugin name is required")

        if not self.version:
            errors.append("Plugin version is required")

        if not self.entry_point:
            errors.append("Plugin entry_point is required")

        # Validate variables
        var_names = set()
        for var in self.variables:
            if var.name in var_names:
                errors.append(f"Duplicate variable name: {var.name}")
            var_names.add(var.name)

            if var.required and var.default is not None:
                errors.append(f"Variable {var.name} cannot be both required and have a default")

        return errors
