#!/usr/bin/env python3
"""Convert flat user profiles into thin overrides of a Bambu Lab vendor parent.

Reads user-imported filament/process profiles that were written before the
deferred-flattening contract (no `inherits`, all parent values merged in),
diffs each against its resolved parent from the local OrcaSlicer checkout,
and writes the thin form into the typed subfolders under USER_DIR.

Originals at USER_DIR root are left in place; the loader's deeper-file-wins
rule makes them inert once the typed copy exists.
"""

from __future__ import annotations

import json
from pathlib import Path

VENDOR_DIR = Path(
    "/Users/leolobato/Documents/Projetos/Personal/3d/bambu_workspace/orcaslicer/resources/profiles/BBL"
)
USER_DIR = Path("/Volumes/docker/bambu-gateway/orcaslicer-cli")

PARENT_MAP: dict[str, tuple[str, str]] = {
    "DEEPLEE Wood Imported.json": ("filament", "Bambu PLA Wood @BBL A1M"),
    "Eryone Matte Imported.json": ("filament", "Bambu PLA Matte @BBL A1M"),
    "eSun ePLA-Matte Imported.json": ("filament", "Bambu PLA Matte @BBL A1M"),
    "eSUN PETG A1 mini.json": ("filament", "Bambu PETG HF @BBL A1M"),
    "eSUN PLA-Basic @BBL A1M.json": ("filament", "Bambu PLA Basic @BBL A1M"),
    "SUNLU PLA + GEN2@Bambu Lab A1 mini 0.4 nozzle.json": (
        "filament", "Bambu PLA Basic @BBL A1M",
    ),
    "SUNLU PLA +2.0 GEN2 @Bambu Lab A1 mini 0.4 nozzle.json": (
        "filament", "Bambu PLA Basic @BBL A1M",
    ),
    "SUNLU PLA BASIC GEN2 @Bambu Lab A1 mini 0.4 nozzle.json": (
        "filament", "Bambu PLA Basic @BBL A1M",
    ),
    "eSUN PLA-Basic @BBL A1M Process.json": (
        "process", "0.20mm Standard @BBL A1M",
    ),
    "eSUN PETG A1 mini Process.json": (
        "process", "0.20mm Standard @BBL A1M",
    ),
}

# Identity / indexing fields are kept on the thin profile even when they
# match the parent — they're what the loader and AMS layer index against.
ALWAYS_KEEP = {
    "type",
    "name",
    "from",
    "instantiation",
    "setting_id",
    "filament_id",
    "filament_settings_id",
    "print_settings_id",
    "filament_vendor",
    "version",
    "inherits",
}


def resolve(name: str, category: str) -> dict:
    """Recursively resolve a vendor profile's inheritance chain into a flat dict."""
    path = VENDOR_DIR / category / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{category}/{name}.json")
    data = json.loads(path.read_text())
    inherits = data.get("inherits")
    if inherits:
        merged = dict(resolve(inherits, category))
        merged.update(data)
        merged.pop("inherits", None)
        return merged
    out = dict(data)
    out.pop("inherits", None)
    return out


def make_thin(user_data: dict, parent_resolved: dict) -> dict:
    """Return only the user keys that diverge from the resolved parent."""
    thin: dict = {}
    for key, value in user_data.items():
        if key in ALWAYS_KEEP:
            thin[key] = value
        elif key not in parent_resolved:
            thin[key] = value
        elif parent_resolved[key] != value:
            thin[key] = value
    return thin


def main() -> None:
    summary: list[tuple[str, str, int, int]] = []
    for fname, (category, parent_name) in PARENT_MAP.items():
        user_path = USER_DIR / fname
        if not user_path.exists():
            print(f"SKIP missing: {user_path}")
            continue
        user_data = json.loads(user_path.read_text())
        parent_resolved = resolve(parent_name, category)

        thin = make_thin(user_data, parent_resolved)
        thin["inherits"] = parent_name
        thin.setdefault("type", category)
        thin.setdefault("setting_id", thin.get("name", user_path.stem))
        thin.setdefault("instantiation", "true")
        thin.setdefault("from", "User")

        out_dir = USER_DIR / category
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / fname
        out_path.write_text(json.dumps(thin, indent=2) + "\n", encoding="utf-8")

        summary.append((fname, parent_name, len(user_data), len(thin)))

    print()
    print(f"{'File':<55} {'Parent':<32} {'orig':>5} {'thin':>5}  {'shrink':>6}")
    print("-" * 110)
    for fname, parent, orig_n, thin_n in summary:
        shrink = f"{(1 - thin_n / orig_n) * 100:.0f}%"
        print(f"{fname:<55} {parent:<32} {orig_n:>5} {thin_n:>5}  {shrink:>6}")


if __name__ == "__main__":
    main()
