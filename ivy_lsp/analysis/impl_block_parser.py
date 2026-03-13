"""Impl-block parser for Ivy source files.

Extracts ``<<< impl ... >>>`` embedded C++ blocks and analyses their
content for pattern detection (serialization state machines, socket
operations, class hierarchies).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# Match <<< impl ... >>> blocks (possibly multi-line)
IMPL_BLOCK_RE = re.compile(r"<<<\s*impl\b(.*?)>>>", re.DOTALL)

# Match <<< member ... >>> blocks
MEMBER_BLOCK_RE = re.compile(r"<<<\s*member\b(.*?)>>>", re.DOTALL)

# Match inline C++ blocks: action name(...) = { <<< ... >>> }
INLINE_CPP_RE = re.compile(r"<<<(.*?)>>>", re.DOTALL)

# Extract C++ enum state declarations: enum { s1, s2, ... } state;
CPP_ENUM_RE = re.compile(
    r"enum\s*\{([^}]+)\}\s*(\w+)\s*;", re.DOTALL
)

# Detect socket operations
SOCKET_OPS_RE = re.compile(
    r"\b(socket|send|recv|bind|listen|accept|connect)\b"
)

# Detect class inheritance: class `name` : public base_class {
CLASS_INHERIT_RE = re.compile(
    r"class\s+`?(\w+)`?\s*:\s*public\s+(\w+)"
)

# Detect ser/deser base classes
SER_BASE_RE = re.compile(r"ivy_binary_ser_\d+")
DESER_BASE_RE = re.compile(r"ivy_binary_deser_\d+")

# Detect setn/getn calls (ser/deser field operations)
SETN_RE = re.compile(r"\bsetn\s*\(([^)]+)\)")
GETN_RE = re.compile(r"\bgetn\s*\(([^)]+)\)")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ImplBlock:
    """A single <<< impl >>> block extracted from Ivy source."""
    content: str
    start_offset: int
    end_offset: int
    line: int  # 0-based line number


@dataclass
class MemberBlock:
    """A single <<< member >>> block extracted from Ivy source."""
    content: str
    start_offset: int
    end_offset: int
    line: int


@dataclass
class CppEnumState:
    """Enum-based state machine found in an impl block."""
    states: List[str]
    var_name: str  # e.g., "state"


@dataclass
class CppClassInfo:
    """C++ class declaration found in an impl/member block."""
    name: str
    base_class: Optional[str]
    is_serializer: bool
    is_deserializer: bool


@dataclass
class ImplAnalysis:
    """Complete analysis of all impl/member blocks in a file."""
    impl_blocks: List[ImplBlock] = field(default_factory=list)
    member_blocks: List[MemberBlock] = field(default_factory=list)
    enum_states: List[CppEnumState] = field(default_factory=list)
    classes: List[CppClassInfo] = field(default_factory=list)
    has_socket_ops: bool = False
    socket_ops: List[str] = field(default_factory=list)
    setn_calls: int = 0
    getn_calls: int = 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_impl_blocks(source: str) -> List[ImplBlock]:
    """Extract all <<< impl ... >>> blocks from Ivy source."""
    blocks = []
    for m in IMPL_BLOCK_RE.finditer(source):
        line = source[:m.start()].count("\n")
        blocks.append(ImplBlock(
            content=m.group(1).strip(),
            start_offset=m.start(),
            end_offset=m.end(),
            line=line,
        ))
    return blocks


def extract_member_blocks(source: str) -> List[MemberBlock]:
    """Extract all <<< member ... >>> blocks from Ivy source."""
    blocks = []
    for m in MEMBER_BLOCK_RE.finditer(source):
        line = source[:m.start()].count("\n")
        blocks.append(MemberBlock(
            content=m.group(1).strip(),
            start_offset=m.start(),
            end_offset=m.end(),
            line=line,
        ))
    return blocks


def parse_enum_states(cpp_content: str) -> List[CppEnumState]:
    """Find enum-based state machines in C++ code."""
    results = []
    for m in CPP_ENUM_RE.finditer(cpp_content):
        states = [s.strip() for s in m.group(1).split(",") if s.strip()]
        var_name = m.group(2)
        results.append(CppEnumState(states=states, var_name=var_name))
    return results


def parse_class_info(cpp_content: str) -> List[CppClassInfo]:
    """Find C++ class declarations with inheritance."""
    results = []
    for m in CLASS_INHERIT_RE.finditer(cpp_content):
        name = m.group(1)
        base = m.group(2)
        results.append(CppClassInfo(
            name=name,
            base_class=base,
            is_serializer=bool(SER_BASE_RE.search(base)),
            is_deserializer=bool(DESER_BASE_RE.search(base)),
        ))
    return results


def detect_socket_ops(cpp_content: str) -> List[str]:
    """Find socket operation calls in C++ code."""
    return list(set(m.group(1) for m in SOCKET_OPS_RE.finditer(cpp_content)))


def analyze_impl_blocks(source: str) -> ImplAnalysis:
    """Full analysis of all impl/member blocks in an Ivy source file.

    Returns an ImplAnalysis with extracted blocks, enum states,
    class info, socket operations, and ser/deser call counts.
    """
    result = ImplAnalysis()

    result.impl_blocks = extract_impl_blocks(source)
    result.member_blocks = extract_member_blocks(source)

    # Analyze each impl block
    all_cpp = " ".join(b.content for b in result.impl_blocks)
    all_member = " ".join(b.content for b in result.member_blocks)
    combined = all_cpp + " " + all_member

    result.enum_states = parse_enum_states(all_cpp)
    result.classes = parse_class_info(combined)
    result.socket_ops = detect_socket_ops(all_cpp)
    result.has_socket_ops = len(result.socket_ops) > 0
    result.setn_calls = len(SETN_RE.findall(all_cpp))
    result.getn_calls = len(GETN_RE.findall(all_cpp))

    return result
