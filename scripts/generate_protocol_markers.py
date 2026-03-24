#!/usr/bin/env python3
"""Generate per-protocol .ivyworkspace markers from the root .ivyworkspace.

Idempotent: safe to re-run when root .ivyworkspace changes.
Usage: python scripts/generate_protocol_markers.py [workspace_root]
"""

import json
import os
import sys


def main():
    if len(sys.argv) > 1:
        workspace_root = sys.argv[1]
    else:
        # Default: look for .ivyworkspace in parent directories
        workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    root_marker_path = os.path.join(workspace_root, ".ivyworkspace")
    if not os.path.exists(root_marker_path):
        # Try one level up (if run from ivy-lsp submodule)
        workspace_root = os.path.dirname(workspace_root)
        root_marker_path = os.path.join(workspace_root, ".ivyworkspace")

    if not os.path.exists(root_marker_path):
        print(f"Error: No .ivyworkspace found at {root_marker_path}", file=sys.stderr)
        sys.exit(1)

    with open(root_marker_path) as f:
        root_config = json.load(f)

    workspace_groups = root_config.get("workspace_groups", {})
    workspace_layers = root_config.get("workspace_layers", [])
    standard_library = root_config.get("standard_library", "ivy/include/1.7")
    exclude_paths = root_config.get("exclude_paths", [])

    # Build layer lookup
    layer_by_id = {l["id"]: l for l in workspace_layers}

    # For each workspace group, determine if it corresponds to a protocol directory
    protocol_testing_dir = os.path.join(workspace_root, "protocol-testing")

    for group_name, group_layer_ids in workspace_groups.items():
        # Skip compound groups (like apt_quic) — only generate for top-level protocol dirs
        protocol_dir = os.path.join(protocol_testing_dir, group_name)
        if not os.path.isdir(protocol_dir):
            continue

        # Collect layers for this group
        group_layers = []
        for lid in group_layer_ids:
            if lid in layer_by_id:
                layer = layer_by_id[lid].copy()
                group_layers.append(layer)

        if not group_layers:
            continue

        # Compute workspace_root_offset from protocol dir to workspace root
        rel_offset = os.path.relpath(workspace_root, protocol_dir)

        # Filter exclude_paths relevant to this protocol
        protocol_prefix = f"protocol-testing/{group_name}/"
        relevant_excludes = [
            e
            for e in exclude_paths
            if e.startswith(protocol_prefix) or not e.startswith("protocol-testing/")
        ]

        marker = {
            "version": 3,
            "standard_library": standard_library,
            "scope_detection": "auto",
            "protocol_id": group_name,
            "workspace_root_offset": rel_offset,
            "workspace_layers": group_layers,
            "exclude_paths": relevant_excludes or ["doc", "examples", "test"],
        }

        marker_path = os.path.join(protocol_dir, ".ivyworkspace")
        with open(marker_path, "w") as f:
            json.dump(marker, f, indent=2)
            f.write("\n")

        print(f"Generated: {marker_path} ({len(group_layers)} layers)")

    print("Done.")


if __name__ == "__main__":
    main()
