"""Expected vs actual netlist comparison.

Normalizes both SKiDL-generated and KiCad CLI-exported netlists into
the same shape and provides detailed mismatch reporting.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from kicad_mcp.schematic_engine.models import (
    NetlistCompareResult,
    NetlistEntry,
    NormalizedNetlist,
)

logger = logging.getLogger(__name__)

# Nets to ignore during comparison (power flags, visual-only symbols)
_IGNORE_NET_PATTERNS = re.compile(
    r"^(unconnected-.*|Net-\(.*\)-Pad\d+)$", re.IGNORECASE
)

# Reference prefixes for power flag symbols (ignore in comparison)
_POWER_FLAG_PREFIXES = ("#FLG", "#PWR")


def compare_netlists(
    expected: NormalizedNetlist,
    actual: NormalizedNetlist,
    *,
    ignore_power_flags: bool = True,
    ignore_no_connects: list[tuple[str, str]] | None = None,
    compare_mode: str = "permissive",
) -> NetlistCompareResult:
    """Compare expected netlist against actual (KiCad-exported) netlist.

    Args:
        expected: The ground truth netlist from SKiDL/canonical circuit.
        actual: The netlist exported by KiCad CLI from the generated schematic.
        ignore_power_flags: Whether to ignore power flag symbols in comparison.
        ignore_no_connects: List of (ref, pin) tuples to ignore.
        compare_mode: ``permissive`` treats KiCad pin numbers, pin functions,
            and decorated function names as aliases. ``strict`` requires exact
            ref/pin pairs inside each net.

    Returns:
        NetlistCompareResult with missing/extra/mismatched details.
    """
    no_connect_set = set(ignore_no_connects) if ignore_no_connects else set()

    missing_endpoints: list[dict[str, str]] = []
    extra_endpoints: list[dict[str, str]] = []
    mismatched_nets: list[dict[str, Any]] = []

    # Filter expected nets
    expected_filtered = _filter_netlist(expected, ignore_power_flags, no_connect_set)
    actual_filtered = _filter_netlist(actual, ignore_power_flags, no_connect_set)

    # Check each expected net
    for net_name, expected_entries in expected_filtered.nets.items():
        actual_entries = actual_filtered.nets.get(net_name, set())

        # Find missing endpoints (in expected but not in actual)
        missing = {
            entry
            for entry in expected_entries
            if not _entry_set_contains(actual_entries, entry, compare_mode)
        }
        for entry in missing:
            if not _is_no_connect_entry(entry, no_connect_set):
                missing_endpoints.append({
                    "net": net_name,
                    "ref": entry.ref,
                    "pin": entry.pin,
                })

        # Find extra endpoints (in actual but not in expected)
        extra = {
            entry
            for entry in actual_entries
            if not _entry_set_contains(expected_entries, entry, compare_mode)
        }
        for entry in extra:
            if not _is_power_flag_ref(entry.ref):
                extra_endpoints.append({
                    "net": net_name,
                    "ref": entry.ref,
                    "pin": entry.pin,
                })

        if missing or extra:
            mismatched_nets.append({
                "net": net_name,
                "missing_count": len(missing),
                "extra_count": len(extra),
            })

    # Check for nets in actual that aren't in expected
    for net_name, actual_entries in actual_filtered.nets.items():
        if net_name not in expected_filtered.nets:
            if _IGNORE_NET_PATTERNS.match(net_name):
                continue
            for entry in actual_entries:
                if not _is_power_flag_ref(entry.ref):
                    extra_endpoints.append({
                        "net": net_name,
                        "ref": entry.ref,
                        "pin": entry.pin,
                    })

    success = len(missing_endpoints) == 0
    return NetlistCompareResult(
        success=success,
        missing_endpoints=missing_endpoints,
        extra_endpoints=extra_endpoints,
        mismatched_nets=mismatched_nets,
        expected_net_count=len(expected_filtered.nets),
        actual_net_count=len(actual_filtered.nets),
    )


def parse_kicad_netlist(netlist_path: str) -> NormalizedNetlist:
    """Parse a KiCad S-expression netlist file into NormalizedNetlist.

    Args:
        netlist_path: Path to the .net file exported by KiCad CLI.

    Returns:
        NormalizedNetlist with all nets and their endpoints.
    """
    try:
        with open(netlist_path, encoding="utf-8") as f:
            content = f.read()
        return _parse_sexpr_netlist(content)
    except Exception as e:
        logger.error("Failed to parse KiCad netlist %s: %s", netlist_path, e)
        return NormalizedNetlist(nets={})


def load_expected_netlist(path: str) -> NormalizedNetlist:
    """Load expected netlist from JSON artifact.

    Args:
        path: Path to expected_netlist.json.

    Returns:
        NormalizedNetlist.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        nets_data = data.get("nets", data)
        return NormalizedNetlist.from_dict(nets_data)
    except Exception as e:
        logger.error("Failed to load expected netlist %s: %s", path, e)
        return NormalizedNetlist(nets={})


def _filter_netlist(
    netlist: NormalizedNetlist,
    ignore_power_flags: bool,
    no_connect_set: set[tuple[str, str]],
) -> NormalizedNetlist:
    """Filter a netlist by removing power flags and no-connects."""
    filtered_nets: dict[str, set[NetlistEntry]] = {}

    for net_name, entries in netlist.nets.items():
        if _IGNORE_NET_PATTERNS.match(net_name):
            continue

        filtered_entries: set[NetlistEntry] = set()
        for entry in entries:
            if ignore_power_flags and _is_power_flag_ref(entry.ref):
                continue
            if _is_no_connect_entry(entry, no_connect_set):
                continue
            filtered_entries.add(entry)

        if filtered_entries:
            filtered_nets[net_name] = filtered_entries

    return NormalizedNetlist(nets=filtered_nets)


def _is_power_flag_ref(ref: str) -> bool:
    """Check if a reference designator is a power flag."""
    return any(ref.startswith(prefix) for prefix in _POWER_FLAG_PREFIXES)


def check_power_net_sanity(
    expected: NormalizedNetlist,
    actual: NormalizedNetlist,
) -> dict[str, Any]:
    """Detect catastrophic generated shorts between ground and power rails.

    KiCad chooses a single net name after a short, so this check maps actual
    nodes back to their intended expected power nets and flags actual nets that
    contain both ground-like and non-ground power rails.
    """
    expected_power = {
        net_name: entries
        for net_name, entries in expected.nets.items()
        if _power_net_kind(net_name) is not None
    }
    issues: list[dict[str, Any]] = []
    for actual_net, actual_entries in actual.nets.items():
        intended_nets: set[str] = set()
        endpoint_samples: list[dict[str, str]] = []
        for expected_net, expected_entries in expected_power.items():
            matched = [
                entry
                for entry in expected_entries
                if _entry_set_contains(actual_entries, entry, "permissive")
            ]
            if matched:
                intended_nets.add(expected_net)
                endpoint_samples.extend(
                    {
                        "intended_net": expected_net,
                        "ref": entry.ref,
                        "pin": entry.pin,
                    }
                    for entry in matched[:5]
                )

        if _power_net_kind(actual_net) is not None:
            intended_nets.add(actual_net)
        if not _has_ground_power_conflict(intended_nets):
            continue
        issue = {
            "severity": "error",
            "type": "power_ground_short",
            "actual_net": actual_net,
            "intended_power_nets": sorted(intended_nets),
            "endpoint_samples": endpoint_samples[:20],
            "message": (
                "Generated KiCad net contains both ground-like and power-like "
                f"expected rails: {', '.join(sorted(intended_nets))}"
            ),
        }
        issues.append(issue)

    return {
        "success": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "error": issues[0]["message"] if issues else None,
    }


def _entry_set_contains(
    entries: set[NetlistEntry],
    target: NetlistEntry,
    compare_mode: str,
) -> bool:
    if compare_mode == "strict":
        return target in entries
    return any(_entries_equivalent(entry, target) for entry in entries)


def _entries_equivalent(left: NetlistEntry, right: NetlistEntry) -> bool:
    return left.ref == right.ref and _pins_equivalent(left.pin, right.pin)


def _is_no_connect_entry(
    entry: NetlistEntry,
    no_connect_set: set[tuple[str, str]],
) -> bool:
    return any(
        entry.ref == ref and _pins_equivalent(entry.pin, pin)
        for ref, pin in no_connect_set
    )


def _pins_equivalent(left: str, right: str) -> bool:
    left_aliases = _pin_alias_set(left)
    right_aliases = _pin_alias_set(right)
    return bool(left_aliases and right_aliases and left_aliases.intersection(right_aliases))


def _pin_alias_set(value: str) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    aliases = {raw}
    stripped = (
        raw.replace("~{", "")
        .replace("}", "")
        .replace("{", "")
        .replace("~", "")
    )
    if stripped:
        aliases.add(stripped)
    for candidate in list(aliases):
        if "/" in candidate:
            aliases.update(part for part in candidate.split("/") if part)
        suffix_match = re.match(r"^(.+)_([A-Za-z0-9]+)$", candidate)
        if suffix_match:
            aliases.add(suffix_match.group(1))
            aliases.add(suffix_match.group(2))
    normalized = {_pin_lookup_key(alias) for alias in aliases if _pin_lookup_key(alias)}
    aliases.update(normalized)
    return {alias.lower() for alias in aliases if alias}


def _pin_lookup_key(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _power_net_kind(net_name: str) -> str | None:
    normalized = str(net_name or "").upper().lstrip("/")
    if normalized in {"GND", "AGND", "DGND", "GNDA", "GNDD", "VSS", "VSSA"}:
        return "ground"
    if normalized.startswith("+"):
        return "power"
    if normalized in {"VBUS", "VCC", "VDD", "VDDA", "VBAT", "VIN", "VOUT"}:
        return "power"
    if re.search(r"(^|_)(3V3|5V|VBUS|VCC|VDD|VDDA|VBAT|VIN|VOUT)(_|$)", normalized):
        return "power"
    if re.search(r"(^|_)(GND|VSS|AGND|DGND)(_|$)", normalized):
        return "ground"
    return None


def _has_ground_power_conflict(net_names: set[str]) -> bool:
    kinds = {_power_net_kind(name) for name in net_names}
    return "ground" in kinds and "power" in kinds


def _parse_sexpr_netlist(content: str) -> NormalizedNetlist:
    """Parse KiCad S-expression netlist content.

    Extracts net definitions from the netlist format:
    (net (code N) (name "NET_NAME")
      (node (ref "REF") (pin "PIN") ...)
      ...
    )
    """
    nets: dict[str, set[NetlistEntry]] = {}

    # Find net blocks by matching balanced parentheses
    net_start_pattern = re.compile(
        r'\(net\s+\(code\s+"?\d+"?\)\s+\(name\s+"([^"]+)"\)',
    )
    node_pattern = re.compile(
        r'\(node\s+\(ref\s+"([^"]+)"\)\s+\(pin\s+"([^"]+)"\)'
        r'(?:\s+\(pinfunction\s+"([^"]+)"\))?',
    )

    for match in net_start_pattern.finditer(content):
        net_name = _normalize_net_name(match.group(1))
        # Extract the body after the name match until we find the matching close paren
        body_start = match.end()

        # Count parentheses to find the end of this net block
        # Find the matching close of the (net block
        # depth=1 because we're inside the net block after its opening paren
        depth = 1
        pos = body_start
        while pos < len(content) and depth > 0:
            if content[pos] == '(':
                depth += 1
            elif content[pos] == ')':
                depth -= 1
            pos += 1

        net_body = content[body_start:pos]

        entries: set[NetlistEntry] = set()
        for node_match in node_pattern.finditer(net_body):
            ref = node_match.group(1)
            pin = node_match.group(2)
            entries.add(NetlistEntry(ref=ref, pin=pin))
            pinfunction = node_match.group(3) or ""
            for alias in _pinfunction_aliases(pinfunction, pin):
                entries.add(NetlistEntry(ref=ref, pin=alias))

        if entries:
            nets[net_name] = entries

    return NormalizedNetlist(nets=nets)


def _normalize_net_name(net_name: str) -> str:
    """Normalize KiCad root-sheet local net names to design-intent net names."""
    if net_name.startswith("/") and net_name.count("/") == 1:
        return net_name[1:]
    return net_name


def _pinfunction_aliases(pinfunction: str, pin_number: str) -> set[str]:
    """Return readable pin-name aliases KiCad stores alongside numeric pins."""
    if not pinfunction:
        return set()
    aliases = {pinfunction}
    suffix = f"_{pin_number}"
    if pinfunction.endswith(suffix) and len(pinfunction) > len(suffix):
        aliases.add(pinfunction[: -len(suffix)])
    for alias in list(aliases):
        stripped = (
            alias.replace("~{", "")
            .replace("}", "")
            .replace("{", "")
            .replace("~", "")
        )
        if stripped:
            aliases.add(stripped)
            if "/" in stripped:
                aliases.update(part for part in stripped.split("/") if part)
        if "/" in alias:
            aliases.update(part for part in alias.split("/") if part)
    return aliases
