"""Pattern Library — detection and cross-reference validation for formal model patterns.

Detects recurring patterns (serdes, variants, monitors, shims, modules) in
Ivy protocol models, and validates cross-references between them.  Used by
``ivy_patterns`` MCP tool and ``/nct-add-pattern`` plugin command.
"""

from __future__ import annotations

import enum
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from ivy_lsp.core.analysis.impl_block_parser import analyze_impl_blocks

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# Object declaration: object name = { ... }
OBJECT_RE = re.compile(r"^\s*object\s+([\w.]+)\s*=\s*\{", re.MULTILINE)

# Variant declaration: variant this of X = struct { ... }
VARIANT_RE = re.compile(
    r"^\s*variant\s+this\s+of\s+([\w.]+)\s*=\s*struct\s*\{", re.MULTILINE
)

# Type struct: type this = struct { field: type, ... }
TYPE_STRUCT_RE = re.compile(
    r"^\s*type\s+this\s*=\s*struct\s*\{([^}]+)\}", re.MULTILINE | re.DOTALL
)

# Module declaration: module name(params) = { ... }
MODULE_DECL_RE = re.compile(
    r"^\s*module\s+([\w.]+)\s*\(([^)]*)\)\s*=\s*\{", re.MULTILINE
)

# Instance declaration: instance name : module_name(args)
INSTANCE_RE = re.compile(
    r"^\s*instance\s+([\w.]+)\s*:\s*([\w.]+)\s*(?:\(([^)]*)\))?", re.MULTILINE
)

# Action declaration
ACTION_DECL_RE = re.compile(r"^\s*action\s+([\w.]+)\s*(?:\(([^)]*)\))?", re.MULTILINE)

# Before/after/around monitor
MONITOR_RE = re.compile(
    r"^\s*(before|after|around|implement)\s+([\w.]+)\s*(?:\([^)]*\))?\s*\{",
    re.MULTILINE,
)

# Export/import action
EXPORT_RE = re.compile(r"^\s*export\s+action\s+([\w.]+)", re.MULTILINE)
IMPORT_RE = re.compile(r"^\s*import\s+action\s+([\w.]+)", re.MULTILINE)

# Include statement
INCLUDE_RE = re.compile(r"^\s*include\s+(\w+)", re.MULTILINE)

# Serdes instance: instance X : serdes(msg_type, data_type, ser, deser)
SERDES_INSTANCE_RE = re.compile(
    r"^\s*instance\s+([\w.]+)\s*:\s*serdes\s*\(([^)]+)\)", re.MULTILINE
)

# Relation/function declarations (for shim state tracking)
RELATION_RE = re.compile(r"^\s*relation\s+([\w.]+)", re.MULTILINE)
FUNCTION_RE = re.compile(r"^\s*function\s+([\w.]+)", re.MULTILINE)

# _finalize pattern
FINALIZE_RE = re.compile(r"\b_finalize\b")

# Weight attribute: attribute X.weight = "N"
WEIGHT_ATTR_RE = re.compile(
    r'^\s*attribute\s+([\w.]+)\.weight\s*=\s*"(\d+)"', re.MULTILINE
)

# Type enum: type this = { val1, val2, ... }
TYPE_ENUM_RE = re.compile(r"^\s*type\s+this\s*=\s*\{([^}]+)\}", re.MULTILINE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class PatternKind(enum.Enum):
    """Types of formal patterns."""

    SERDES = "serdes"
    VARIANTS = "variants"
    MONITORS = "monitors"
    SHIM = "shim"
    MODULE = "module"
    ENTITY = "entity"
    INCLUDE_CHAIN = "include_chain"


@dataclass
class PatternInstance:
    """A detected pattern instance in a specific file."""

    kind: PatternKind
    file: str
    line: int
    name: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PatternValidationIssue:
    """A cross-reference validation issue."""

    severity: str  # "error", "warning", "info"
    pattern: PatternKind
    message: str
    file: Optional[str] = None
    line: Optional[int] = None
    related: Optional[str] = None  # related pattern/file


@dataclass
class PatternValidationResult:
    """Complete validation result for a protocol model."""

    protocol: str
    detected: List[PatternInstance] = field(default_factory=list)
    issues: List[PatternValidationIssue] = field(default_factory=list)
    summary: Dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------


def detect_serdes(source: str, filepath: str) -> List[PatternInstance]:
    """Detect serializer/deserializer patterns in a file.

    Looks for objects with <<< impl >>> blocks containing C++ classes
    that inherit from ivy_binary_ser_* or ivy_binary_deser_*.
    """
    instances = []
    impl = analyze_impl_blocks(source)

    for cls in impl.classes:
        if cls.is_serializer:
            states = []
            for es in impl.enum_states:
                states = es.states
                break
            instances.append(
                PatternInstance(
                    kind=PatternKind.SERDES,
                    file=filepath,
                    line=impl.impl_blocks[0].line if impl.impl_blocks else 0,
                    name=cls.name,
                    details={
                        "type": "serializer",
                        "base_class": cls.base_class,
                        "states": states,
                        "setn_calls": impl.setn_calls,
                    },
                )
            )
        elif cls.is_deserializer:
            states = []
            for es in impl.enum_states:
                states = es.states
                break
            instances.append(
                PatternInstance(
                    kind=PatternKind.SERDES,
                    file=filepath,
                    line=impl.impl_blocks[0].line if impl.impl_blocks else 0,
                    name=cls.name,
                    details={
                        "type": "deserializer",
                        "base_class": cls.base_class,
                        "states": states,
                        "getn_calls": impl.getn_calls,
                    },
                )
            )

    # Also detect serdes instance declarations
    for m in SERDES_INSTANCE_RE.finditer(source):
        line = source[: m.start()].count("\n")
        args = [a.strip() for a in m.group(2).split(",")]
        instances.append(
            PatternInstance(
                kind=PatternKind.SERDES,
                file=filepath,
                line=line,
                name=m.group(1),
                details={
                    "type": "instance",
                    "args": args,
                    "message_type": args[0] if args else None,
                    "ser_name": args[2] if len(args) > 2 else None,
                    "deser_name": args[3] if len(args) > 3 else None,
                },
            )
        )

    return instances


def detect_variants(source: str, filepath: str) -> List[PatternInstance]:
    """Detect object variant hierarchy patterns.

    Finds type declarations with struct fields, variant declarations,
    and event action patterns.
    """
    instances = []

    # Find objects with struct type definitions
    for m in OBJECT_RE.finditer(source):
        obj_name = m.group(1)
        obj_start = m.start()

        # Check if this object has a struct type
        struct_match = TYPE_STRUCT_RE.search(source, obj_start)
        if struct_match and struct_match.start() - obj_start < 200:
            fields_raw = struct_match.group(1)
            fields = []
            for f in fields_raw.split(","):
                f = f.strip()
                if ":" in f:
                    fname, ftype = f.split(":", 1)
                    fields.append({"name": fname.strip(), "type": ftype.strip()})

            line = source[: m.start()].count("\n")
            instances.append(
                PatternInstance(
                    kind=PatternKind.VARIANTS,
                    file=filepath,
                    line=line,
                    name=obj_name,
                    details={
                        "type": "struct_object",
                        "fields": fields,
                    },
                )
            )

    # Find variant declarations
    for m in VARIANT_RE.finditer(source):
        parent = m.group(1)
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.VARIANTS,
                file=filepath,
                line=line,
                name=f"variant_of_{parent}",
                details={
                    "type": "variant",
                    "parent": parent,
                },
            )
        )

    # Find type enums (like endpoint_id = {client, server})
    for m in TYPE_ENUM_RE.finditer(source):
        values = [v.strip() for v in m.group(1).split(",") if v.strip()]
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.VARIANTS,
                file=filepath,
                line=line,
                name="type_enum",
                details={
                    "type": "enum",
                    "values": values,
                },
            )
        )

    return instances


def detect_monitors(source: str, filepath: str) -> List[PatternInstance]:
    """Detect before/after/around monitor patterns.

    Uses regex to find monitor blocks and extract the monitored action,
    mixin kind, and whether _generating guard is present.
    """
    instances = []

    for m in MONITOR_RE.finditer(source):
        mixin_kind = m.group(1)
        action_name = m.group(2)
        line = source[: m.start()].count("\n")

        # Check if block contains _generating guard
        block_start = m.end()
        brace_depth = 1
        pos = block_start
        block_content = ""
        while pos < len(source) and brace_depth > 0:
            if source[pos] == "{":
                brace_depth += 1
            elif source[pos] == "}":
                brace_depth -= 1
            pos += 1
        if pos <= len(source):
            block_content = source[block_start : pos - 1]

        has_generating = "_generating" in block_content

        instances.append(
            PatternInstance(
                kind=PatternKind.MONITORS,
                file=filepath,
                line=line,
                name=f"{mixin_kind}.{action_name}",
                details={
                    "mixin_kind": mixin_kind,
                    "action": action_name,
                    "has_generating_guard": has_generating,
                },
            )
        )

    # Check for _finalize pattern
    if FINALIZE_RE.search(source):
        instances.append(
            PatternInstance(
                kind=PatternKind.MONITORS,
                file=filepath,
                line=0,
                name="_finalize",
                details={"type": "finalize"},
            )
        )

    # Check for export actions
    for m in EXPORT_RE.finditer(source):
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.MONITORS,
                file=filepath,
                line=line,
                name=f"export.{m.group(1)}",
                details={"type": "export", "action": m.group(1)},
            )
        )

    # Check for weight attributes
    for m in WEIGHT_ATTR_RE.finditer(source):
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.MONITORS,
                file=filepath,
                line=line,
                name=f"weight.{m.group(1)}",
                details={"type": "weight", "target": m.group(1), "value": m.group(2)},
            )
        )

    return instances


def detect_shims(source: str, filepath: str) -> List[PatternInstance]:
    """Detect shim patterns (network I/O bridge).

    Finds implement blocks for network recv/send, relation declarations
    for connection state, and socket operations in impl blocks.
    """
    instances = []
    impl = analyze_impl_blocks(source)

    # Check for implement net.recv or similar patterns
    for m in MONITOR_RE.finditer(source):
        if m.group(1) == "implement" and ".recv" in m.group(2):
            line = source[: m.start()].count("\n")
            instances.append(
                PatternInstance(
                    kind=PatternKind.SHIM,
                    file=filepath,
                    line=line,
                    name=m.group(2),
                    details={
                        "type": "recv_handler",
                        "action": m.group(2),
                    },
                )
            )
        elif m.group(1) == "implement" and ".connected" in m.group(2):
            line = source[: m.start()].count("\n")
            instances.append(
                PatternInstance(
                    kind=PatternKind.SHIM,
                    file=filepath,
                    line=line,
                    name=m.group(2),
                    details={
                        "type": "connected_handler",
                        "action": m.group(2),
                    },
                )
            )
        elif m.group(1) == "implement" and ".accept" in m.group(2):
            line = source[: m.start()].count("\n")
            instances.append(
                PatternInstance(
                    kind=PatternKind.SHIM,
                    file=filepath,
                    line=line,
                    name=m.group(2),
                    details={
                        "type": "accept_handler",
                        "action": m.group(2),
                    },
                )
            )

    # Check for connection state relations (isup, pend, getsock)
    relations = []
    for m in RELATION_RE.finditer(source):
        relations.append(m.group(1))
    functions = []
    for m in FUNCTION_RE.finditer(source):
        functions.append(m.group(1))

    if relations or functions:
        state_tracking = [r for r in relations if r in ("isup", "pend")]
        socket_lookups = [f for f in functions if "sock" in f.lower()]
        if state_tracking or socket_lookups:
            instances.append(
                PatternInstance(
                    kind=PatternKind.SHIM,
                    file=filepath,
                    line=0,
                    name="connection_state",
                    details={
                        "type": "state_tracking",
                        "relations": state_tracking,
                        "socket_lookups": socket_lookups,
                    },
                )
            )

    # Check for socket operations in impl blocks
    if impl.has_socket_ops:
        instances.append(
            PatternInstance(
                kind=PatternKind.SHIM,
                file=filepath,
                line=impl.impl_blocks[0].line if impl.impl_blocks else 0,
                name="socket_ops",
                details={
                    "type": "socket_operations",
                    "ops": impl.socket_ops,
                },
            )
        )

    # Detect transport protocol (UDP vs TCP)
    transport = None
    if "ip.udp" in source:
        transport = "udp"
    elif "ip.tcp" in source:
        transport = "tcp"
    if transport:
        instances.append(
            PatternInstance(
                kind=PatternKind.SHIM,
                file=filepath,
                line=0,
                name=f"transport_{transport}",
                details={"type": "transport", "protocol": transport},
            )
        )

    return instances


def detect_modules(source: str, filepath: str) -> List[PatternInstance]:
    """Detect parameterized module patterns.

    Finds module declarations with parameters, and instance declarations
    that instantiate them.
    """
    instances = []

    for m in MODULE_DECL_RE.finditer(source):
        name = m.group(1)
        params_raw = m.group(2)
        params = [p.strip() for p in params_raw.split(",") if p.strip()]
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.MODULE,
                file=filepath,
                line=line,
                name=name,
                details={
                    "type": "definition",
                    "params": params,
                },
            )
        )

    for m in INSTANCE_RE.finditer(source):
        inst_name = m.group(1)
        module_name = m.group(2)
        args_raw = m.group(3) or ""
        args = [a.strip() for a in args_raw.split(",") if a.strip()]
        line = source[: m.start()].count("\n")
        instances.append(
            PatternInstance(
                kind=PatternKind.MODULE,
                file=filepath,
                line=line,
                name=inst_name,
                details={
                    "type": "instance",
                    "module": module_name,
                    "args": args,
                },
            )
        )

    return instances


def detect_entities(source: str, filepath: str) -> List[PatternInstance]:
    """Detect entity patterns (protocol participants).

    Finds objects with variant modules containing behavior actions
    and after-init blocks.
    """
    instances = []

    # Find objects that contain modules with variant declarations
    objects = list(OBJECT_RE.finditer(source))
    modules = list(MODULE_DECL_RE.finditer(source))
    variants = list(VARIANT_RE.finditer(source))

    # Check for behavior actions
    behavior_actions = [
        m for m in ACTION_DECL_RE.finditer(source) if "behavior" in m.group(1)
    ]

    # Determine entity type based on module count
    for obj_m in objects:
        obj_name = obj_m.group(1)
        obj_start = obj_m.start()

        # Count modules within this object scope
        obj_modules = [m for m in modules if m.start() > obj_start]

        obj_variants = [
            v for v in variants if v.start() > obj_start and v.group(1) == obj_name
        ]

        if obj_variants:
            role_type = "symmetric" if len(obj_modules) <= 1 else "asymmetric"
            module_names = [m.group(1) for m in obj_modules]

            line = source[: obj_m.start()].count("\n")
            instances.append(
                PatternInstance(
                    kind=PatternKind.ENTITY,
                    file=filepath,
                    line=line,
                    name=obj_name,
                    details={
                        "type": "entity",
                        "role_type": role_type,
                        "modules": module_names,
                        "has_behavior": len(behavior_actions) > 0,
                    },
                )
            )

    return instances


def detect_include_chain(
    source: str,
    filepath: str,
    pre_extracted_includes: Optional[List[str]] = None,
) -> List[PatternInstance]:
    """Detect include chain patterns.

    Args:
        source: Ivy source text to scan.
        filepath: Absolute path to the source file.
        pre_extracted_includes: When provided (e.g. from TieredExtractor),
            skips regex scanning and uses these names directly.
    """
    instances = []
    includes = []

    if pre_extracted_includes is not None:
        for name in pre_extracted_includes:
            includes.append({"name": name, "line": 0})
    else:
        for m in INCLUDE_RE.finditer(source):
            line = source[: m.start()].count("\n")
            includes.append({"name": m.group(1), "line": line})

    if includes:
        instances.append(
            PatternInstance(
                kind=PatternKind.INCLUDE_CHAIN,
                file=filepath,
                line=0,
                name="includes",
                details={
                    "type": "include_chain",
                    "includes": includes,
                    "count": len(includes),
                },
            )
        )

    return instances


# ---------------------------------------------------------------------------
# Multi-file analysis
# ---------------------------------------------------------------------------


def detect_all_patterns(
    source: str,
    filepath: str,
    pre_extracted_includes: Optional[List[str]] = None,
) -> List[PatternInstance]:
    """Run all pattern detectors on a single file.

    Args:
        source: Ivy source text to scan.
        filepath: Absolute path to the source file.
        pre_extracted_includes: When provided (e.g. from TieredExtractor),
            avoids re-scanning includes with regex.
    """
    patterns = []
    patterns.extend(detect_serdes(source, filepath))
    patterns.extend(detect_variants(source, filepath))
    patterns.extend(detect_monitors(source, filepath))
    patterns.extend(detect_shims(source, filepath))
    patterns.extend(detect_modules(source, filepath))
    patterns.extend(detect_entities(source, filepath))
    patterns.extend(detect_include_chain(source, filepath, pre_extracted_includes))
    return patterns


def analyze_protocol(
    protocol_dir: str,
    ivy_files: Optional[List[str]] = None,
) -> PatternValidationResult:
    """Analyze all .ivy files in a protocol directory for patterns.

    Args:
        protocol_dir: Absolute path to the protocol directory.
        ivy_files: Optional list of .ivy file paths. If None, discovers files.

    Returns:
        PatternValidationResult with all detected patterns and validation issues.
    """
    protocol = os.path.basename(protocol_dir)
    result = PatternValidationResult(protocol=protocol)

    if ivy_files is None:
        ivy_files = []
        for dirpath, _, files in os.walk(protocol_dir):
            for f in files:
                if f.endswith(".ivy"):
                    ivy_files.append(os.path.join(dirpath, f))

    for fpath in ivy_files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue

        patterns = detect_all_patterns(source, fpath)
        result.detected.extend(patterns)

    # Build summary counts by pattern kind
    for kind in PatternKind:
        count = sum(1 for p in result.detected if p.kind == kind)
        result.summary[kind.value] = count

    return result


# ---------------------------------------------------------------------------
# Cross-reference validation
# ---------------------------------------------------------------------------


class PatternCrossReferencer:
    """Validates cross-references between detected patterns."""

    def __init__(self, result: PatternValidationResult) -> None:
        """Initialize with a validation result and index by kind."""
        self._result = result
        self._by_kind: Dict[PatternKind, List[PatternInstance]] = {}
        for p in result.detected:
            self._by_kind.setdefault(p.kind, []).append(p)

    def validate_all(self) -> List[PatternValidationIssue]:
        """Run all cross-reference validations."""
        issues: List[PatternValidationIssue] = []
        issues.extend(self.validate_serdes_coverage())
        issues.extend(self.validate_monitor_coverage())
        issues.extend(self.validate_shim_completeness())
        issues.extend(self.validate_module_instances())
        return issues

    def validate_serdes_coverage(self) -> List[PatternValidationIssue]:
        """Check that variant types have matching ser/deser state machines.

        For each serdes instance, verify the referenced ser and deser names
        correspond to actual serializer/deserializer pattern instances.
        """
        issues = []
        serdes = self._by_kind.get(PatternKind.SERDES, [])

        # Collect serializer and deserializer names
        ser_names = {p.name for p in serdes if p.details.get("type") == "serializer"}
        deser_names = {
            p.name for p in serdes if p.details.get("type") == "deserializer"
        }

        # Check serdes instances reference valid ser/deser
        for p in serdes:
            if p.details.get("type") == "instance":
                ser_ref = p.details.get("ser_name")
                deser_ref = p.details.get("deser_name")

                if ser_ref and ser_ref not in ser_names:
                    issues.append(
                        PatternValidationIssue(
                            severity="info",
                            pattern=PatternKind.SERDES,
                            message=(
                                f"Serdes instance '{p.name}' references serializer "
                                f"'{ser_ref}' not found in analyzed files"
                            ),
                            file=p.file,
                            line=p.line,
                            related=ser_ref,
                        )
                    )
                if deser_ref and deser_ref not in deser_names:
                    issues.append(
                        PatternValidationIssue(
                            severity="info",
                            pattern=PatternKind.SERDES,
                            message=(
                                f"Serdes instance '{p.name}' references deserializer "
                                f"'{deser_ref}' not found in analyzed files"
                            ),
                            file=p.file,
                            line=p.line,
                            related=deser_ref,
                        )
                    )

        return issues

    def validate_monitor_coverage(self) -> List[PatternValidationIssue]:
        """Check that exported actions have before/after monitors.

        For each export action, verify there is at least one before or after
        monitor targeting that action.
        """
        issues = []
        monitors = self._by_kind.get(PatternKind.MONITORS, [])

        exported = {
            p.details["action"] for p in monitors if p.details.get("type") == "export"
        }
        monitored = {
            p.details["action"]
            for p in monitors
            if p.details.get("mixin_kind") in ("before", "after", "around")
        }

        for action in exported:
            if action not in monitored and action != "_finalize":
                issues.append(
                    PatternValidationIssue(
                        severity="warning",
                        pattern=PatternKind.MONITORS,
                        message=f"Exported action '{action}' has no before/after monitor",
                        related=action,
                    )
                )

        return issues

    def validate_shim_completeness(self) -> List[PatternValidationIssue]:
        """Check that entity roles have shim dispatch branches.

        For each entity with role modules, verify the shim has dispatch
        branches for each role.
        """
        issues = []
        entities = self._by_kind.get(PatternKind.ENTITY, [])
        shims = self._by_kind.get(PatternKind.SHIM, [])

        # Get entity module names (roles)
        entity_modules: Set[str] = set()
        for p in entities:
            for mod in p.details.get("modules", []):
                entity_modules.add(mod)

        # Get shim recv handlers (each should dispatch per role)
        has_recv = any(p.details.get("type") == "recv_handler" for p in shims)

        if entity_modules and not has_recv:
            issues.append(
                PatternValidationIssue(
                    severity="warning",
                    pattern=PatternKind.SHIM,
                    message="Entity roles defined but no shim recv handler found",
                )
            )

        return issues

    def validate_module_instances(self) -> List[PatternValidationIssue]:
        """Check that module definitions have corresponding instances."""
        issues = []
        modules = self._by_kind.get(PatternKind.MODULE, [])

        definitions = {p.name for p in modules if p.details.get("type") == "definition"}
        instantiated = {
            p.details.get("module")
            for p in modules
            if p.details.get("type") == "instance"
        }

        for defn in definitions:
            if defn not in instantiated:
                issues.append(
                    PatternValidationIssue(
                        severity="info",
                        pattern=PatternKind.MODULE,
                        message=f"Module '{defn}' defined but no instance found",
                        related=defn,
                    )
                )

        return issues


# ---------------------------------------------------------------------------
# Comparison mode
# ---------------------------------------------------------------------------


def compare_protocols(
    result_a: PatternValidationResult,
    result_b: PatternValidationResult,
) -> Dict[str, Any]:
    """Compare pattern coverage between two protocol models.

    Returns a dict with per-pattern comparison and coverage delta.
    """
    comparison: Dict[str, Any] = {
        "protocol_a": result_a.protocol,
        "protocol_b": result_b.protocol,
        "patterns": {},
    }

    for kind in PatternKind:
        count_a = result_a.summary.get(kind.value, 0)
        count_b = result_b.summary.get(kind.value, 0)
        comparison["patterns"][kind.value] = {
            "count_a": count_a,
            "count_b": count_b,
            "delta": count_a - count_b,
            "present_in_a": count_a > 0,
            "present_in_b": count_b > 0,
        }

    return comparison
