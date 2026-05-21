from pathlib import Path
import subprocess

from kicad_mcp.utils.native_netlist import parse_native_netlist, run_erc_via_cli


def test_parse_native_netlist_with_node_membership():
    parsed = parse_native_netlist(
        """
(export
  (components
    (comp (ref "R1") (value "10k") (footprint "Resistor_SMD:R_0603_1608Metric"))
    (comp (ref "C1") (value "100n") (footprint "Capacitor_SMD:C_0603_1608Metric"))
  )
  (nets
    (net (code "1") (name "NET_A") (class "Default")
      (node (ref "R1") (pin "1") (pinfunction "~_1") (pintype "passive"))
      (node (ref "C1") (pin "2") (pinfunction "~_2") (pintype "passive"))
    )
    (net (code "2") (name "GND") (class "Default")
      (node (ref "R1") (pin "2") (pinfunction "~_2") (pintype "passive"))
    )
  )
)
"""
    )
    assert parsed["component_count"] == 2
    assert parsed["net_count"] == 2
    assert parsed["components"]["R1"]["footprint"] == "Resistor_SMD:R_0603_1608Metric"
    assert parsed["nets"]["NET_A"]["nodes"] == [
        {"ref": "R1", "pin": "1", "pinfunction": "~_1", "pintype": "passive"},
        {"ref": "C1", "pin": "2", "pinfunction": "~_2", "pintype": "passive"},
    ]


def test_run_erc_via_cli_parses_report_and_timeout(monkeypatch, tmp_path: Path):
    schematic = tmp_path / "demo.kicad_sch"
    schematic.write_text("(kicad_sch)", encoding="utf-8")

    monkeypatch.setattr("kicad_mcp.utils.native_netlist.get_kicad_cli_path", lambda: "kicad-cli")

    def fake_run(cmd, capture_output, text, timeout):
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text(
            """
{
  "sheets": [
    {
      "violations": [
        {"type": "pin_not_connected", "severity": "error", "description": "Pin not connected"}
      ]
    }
  ]
}
""",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("kicad_mcp.utils.native_netlist.subprocess.run", fake_run)
    result = run_erc_via_cli(str(schematic), timeout_seconds=7)
    assert result["success"] is True
    assert result["timeout_seconds"] == 7
    assert result["total_violations"] == 1
    assert result["violation_categories"] == {"pin_not_connected": 1}

    def fake_timeout(cmd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr("kicad_mcp.utils.native_netlist.subprocess.run", fake_timeout)
    timeout_result = run_erc_via_cli(str(schematic), timeout_seconds=3)
    assert timeout_result["success"] is False
    assert "timed out after 3 seconds" in timeout_result["error"]
