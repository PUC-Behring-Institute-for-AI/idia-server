#!/usr/bin/env python3
"""Render serve_config.yaml with env var substitution and launch Ray Serve.

Usage (via Dockerfile CMD):
    python3 /app/render_config.py

Workflow:
    1. Read serve_config.yaml as a template with ${VAR} placeholders.
    2. Substitute each placeholder from the corresponding environment variable.
    3. Write the rendered YAML to a fixed path (/tmp/idia_serve_config.yaml).
    4. exec serve run on the rendered file (replaces this process).

Required env vars:
    MODEL_ID          — Short model alias (e.g. "llama-3.1-8b")
    MODEL_SOURCE      — HuggingFace Hub identifier (e.g. "meta-llama/Llama-3.1-8B-Instruct")

Optional env vars (with defaults):
    MAX_MODEL_LEN           = 8192
    GPU_MEMORY_UTILIZATION  = 0.9

Testing:
    Call this module directly: ``python3 -m scripts.render_config --dry-run``
    to render and validate without launching Ray Serve.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml


# ── Env var schema ──────────────────────────────────────────────────────────

# (type, default_or_None_if_required)
ENV_SCHEMA: dict[str, tuple[type, object]] = {
    "MODEL_ID": (str, None),  # None = required
    "MODEL_SOURCE": (str, None),
    "MAX_MODEL_LEN": (int, 8192),
    "GPU_MEMORY_UTILIZATION": (float, 0.9),
}

TEMPLATE_FILENAME = "serve_config.yaml"
RENDERED_PATH = Path("/tmp/idia_serve_config.yaml")

# Placeholder pattern for env var substitution
ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")

# Characters in values that would corrupt YAML structure
YAML_SPECIAL_CHARS = set(":{}\n#")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _find_template(caller_dir: Path | None = None) -> Path:
    """Locate the template YAML, searching caller dir, parent dir, then /app."""
    candidates = []
    if caller_dir is not None:
        candidates.append(caller_dir / TEMPLATE_FILENAME)
        candidates.append(caller_dir.parent / TEMPLATE_FILENAME)
        candidates.append(caller_dir.parent.parent / TEMPLATE_FILENAME)
    candidates.append(Path("/app") / TEMPLATE_FILENAME)

    for p in candidates:
        if p.is_file():
            return p

    searched = ", ".join(str(p) for p in candidates)
    print(f"FATAL: {TEMPLATE_FILENAME} not found (searched: {searched})", file=sys.stderr)
    sys.exit(1)


def _read_file(path: Path) -> str:
    """Read a text file with explicit error handling.

    Raises SystemExit on common I/O errors with actionable messages.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"FATAL: Arquivo não encontrado: {path}\n"
            f"  Verifique se o arquivo existe e o path está correto.",
            file=sys.stderr,
        )
        sys.exit(1)
    except PermissionError:
        print(
            f"FATAL: Permissão negada: {path}\n"
            f"  Verifique as permissões de leitura do arquivo.",
            file=sys.stderr,
        )
        sys.exit(1)
    except UnicodeDecodeError as e:
        print(
            f"FATAL: Erro de encoding em {path}: {e}\n"
            f"  O arquivo deve ser UTF-8. Verifique o encoding.",
            file=sys.stderr,
        )
        sys.exit(1)


def _apply_defaults(env: dict[str, str]) -> None:
    """Inject schema defaults for optional env vars not present in *env*."""
    for var, (_, default) in ENV_SCHEMA.items():
        if default is not None and var not in env:
            env[var] = str(default)


def _validate_schema_values(env: dict[str, str]) -> None:
    """Validate env var values against schema constraints.

    Currently validates:
      - GPU_MEMORY_UTILIZATION: must be float in (0, 1]
      - MAX_MODEL_LEN: must be a positive integer string

    Exits with code 1 on validation failure.
    """
    # GPU_MEMORY_UTILIZATION range validation
    gpu_util_str = env.get("GPU_MEMORY_UTILIZATION", "0.9")
    try:
        gpu_util = float(gpu_util_str)
        if not (0 < gpu_util <= 1.0):
            print(
                f"FATAL: GPU_MEMORY_UTILIZATION deve estar entre 0 e 1, "
                f"recebido '{gpu_util_str}'",
                file=sys.stderr,
            )
            sys.exit(1)
    except ValueError:
        print(
            f"FATAL: GPU_MEMORY_UTILIZATION deve ser um número float, "
            f"recebido '{gpu_util_str}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # MAX_MODEL_LEN format validation
    max_len_str = env.get("MAX_MODEL_LEN", "8192")
    if not max_len_str.isdigit() or int(max_len_str) <= 0:
        print(
            f"FATAL: MAX_MODEL_LEN deve ser um inteiro positivo, "
            f"recebido '{max_len_str}'",
            file=sys.stderr,
        )
        sys.exit(1)


def _collect_env() -> dict[str, str]:
    """Validate and collect env vars, injecting defaults for optionals.

    Returns a flat dict of all vars needed for substitution.
    Exits with code 1 if any required var is missing.
    """
    env = dict(os.environ)
    missing: list[str] = []

    for var, (typ, default) in ENV_SCHEMA.items():
        if default is None:  # required
            if var not in env:
                missing.append(var)

    if missing:
        print(f"FATAL: Required env var(s) not set: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    _apply_defaults(env)
    _validate_schema_values(env)
    return env


def _escape_yaml_value(value: str) -> str:
    """Escape a value for safe YAML substitution.

    If the value contains YAML special characters (:, {, }, \\n, #),
    serialize it as a quoted YAML scalar. Otherwise return as-is.
    """
    if any(c in value for c in YAML_SPECIAL_CHARS):
        return yaml.dump(value, default_style='"').strip().rstrip("\n...")
    return value


def _substitute(raw: str, env: dict[str, str]) -> str:
    """Replace ${VAR} placeholders with values from *env*.

    Values containing YAML special characters are automatically escaped.
    Unknown placeholders are left untouched so they produce an obvious
    error when Ray Serve tries to parse the rendered YAML.
    """
    def _replacer(match: re.Match) -> str:
        name = match.group(1)
        raw_match: str = match.group(0) or match.string[match.start() : match.end()]
        value = env.get(name, raw_match)
        return _escape_yaml_value(value)

    return ENV_VAR_RE.sub(_replacer, raw)


def _validate_yaml(rendered: str) -> None:
    """Parse rendered YAML; exit with diagnostic on failure."""
    try:
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError as e:
        print(f"FATAL: Rendered {TEMPLATE_FILENAME} is invalid YAML:\n{e}", file=sys.stderr)
        sys.exit(1)

    if parsed is None:
        print(f"FATAL: Rendered {TEMPLATE_FILENAME} is empty", file=sys.stderr)
        sys.exit(1)

    # Structural validation against expected schema
    apps = parsed.get("applications", [])
    if not apps:
        print(f"FATAL: Rendered YAML has no 'applications' list", file=sys.stderr)
        sys.exit(1)

    llm_configs = apps[0].get("args", {}).get("llm_configs", [])
    if not llm_configs:
        print(f"FATAL: No llm_configs found in first application entry", file=sys.stderr)
        sys.exit(1)

    mlc = llm_configs[0].get("model_loading_config", {})
    if not mlc.get("model_id") or not mlc.get("model_source"):
        print(
            f"FATAL: model_loading_config missing model_id or model_source "
            f"after substitution — check env vars",
            file=sys.stderr,
        )
        sys.exit(1)


def _log_diagnostics(env: dict[str, str]) -> None:
    """Print a one-line summary of what will be used."""
    print(
        f"Config: model={env['MODEL_ID']} "
        f"source={env['MODEL_SOURCE']} "
        f"max_len={env['MAX_MODEL_LEN']} "
        f"gpu_util={env['GPU_MEMORY_UTILIZATION']}",
        file=sys.stderr,
    )


# ── Public API (for unit tests) ─────────────────────────────────────────────


def render(
    template: str,
    overrides: dict[str, str] | None = None,
) -> str:
    """Render a template string with env vars, returning YAML string.

    Pure function — no IO. Used for unit tests.
    """
    env = dict(os.environ)
    if overrides:
        env.update(overrides)

    _apply_defaults(env)
    _validate_schema_values(env)
    rendered = _substitute(template, env)
    _validate_yaml(rendered)
    return rendered


def render_file(template_path: str | Path) -> tuple[str, dict[str, str]]:
    """Read a template file, render it, return (rendered_yaml, env_used)."""
    path = Path(template_path)
    raw = _read_file(path)
    env = _collect_env()
    rendered = _substitute(raw, env)
    _validate_yaml(rendered)
    _log_diagnostics(env)
    return rendered, env


# ── CLI entrypoint ──────────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the Docker CMD.

    Steps:
        1. Locate and read template.
        2. Collect environment.
        3. Substitute and validate.
        4. Write rendered output to deterministic path.
        5. exec serve run.
    """
    # --dry-run flag for testing
    if "--dry-run" in sys.argv:
        env = _collect_env()
        caller_dir = Path(__file__).resolve().parent
        raw = _read_file(_find_template(caller_dir))
        rendered = _substitute(raw, env)
        _validate_yaml(rendered)
        print(rendered)
        return

    caller_dir = Path(__file__).resolve().parent
    template_path = _find_template(caller_dir)
    raw = _read_file(template_path)
    env = _collect_env()
    rendered = _substitute(raw, env)
    _validate_yaml(rendered)
    _log_diagnostics(env)

    # Write rendered output to a deterministic path (overwrites on each run)
    RENDERED_PATH.write_text(rendered, encoding="utf-8")
    print(f"Rendered → {RENDERED_PATH}", file=sys.stderr)

    # Launch Ray Serve — replaces this process
    os.execlp("serve", "serve", "run", str(RENDERED_PATH))
    # Only reached on execlp failure
    print("FATAL: execlp failed to launch serve", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
