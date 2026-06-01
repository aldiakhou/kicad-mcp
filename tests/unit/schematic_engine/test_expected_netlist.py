"""Tests for netlist comparison."""


from kicad_mcp.schematic_engine.expected_netlist import _parse_sexpr_netlist, compare_netlists
from kicad_mcp.schematic_engine.models import NetlistEntry, NormalizedNetlist


class TestNetlistComparison:
    """Tests for compare_netlists."""

    def test_identical_netlists(self):
        """Identical netlists compare successfully."""
        nets = {
            "+3V3": {NetlistEntry("U1", "1"), NetlistEntry("U2", "8")},
            "GND": {NetlistEntry("U1", "4"), NetlistEntry("U2", "4")},
        }
        expected = NormalizedNetlist(nets=nets)
        actual = NormalizedNetlist(nets=dict(nets))

        result = compare_netlists(expected, actual)
        assert result.success
        assert result.missing_endpoints == []
        assert result.extra_endpoints == []

    def test_missing_endpoint(self):
        """Missing endpoint is detected."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1"), NetlistEntry("U2", "8")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},  # U2 pin 8 missing
        })

        result = compare_netlists(expected, actual)
        assert not result.success
        assert len(result.missing_endpoints) == 1
        assert result.missing_endpoints[0]["ref"] == "U2"
        assert result.missing_endpoints[0]["pin"] == "8"

    def test_extra_endpoint(self):
        """Extra endpoint is detected but doesn't fail."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1"), NetlistEntry("U2", "8")},
        })

        result = compare_netlists(expected, actual)
        assert result.success  # Extra doesn't fail
        assert len(result.extra_endpoints) == 1

    def test_ignore_power_flags(self):
        """Power flag refs are ignored."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1"), NetlistEntry("#FLG01", "1")},
        })

        result = compare_netlists(expected, actual, ignore_power_flags=True)
        assert result.success
        assert result.extra_endpoints == []

    def test_ignore_no_connects(self):
        """No-connect pins are ignored in comparison."""
        expected = NormalizedNetlist(nets={
            "SIG": {NetlistEntry("U1", "1"), NetlistEntry("U1", "NC")},
        })
        actual = NormalizedNetlist(nets={
            "SIG": {NetlistEntry("U1", "1")},  # NC pin not connected
        })

        result = compare_netlists(
            expected, actual, ignore_no_connects=[("U1", "NC")]
        )
        assert result.success

    def test_pin_number_and_kicad_pinfunction_suffix_are_equivalent(self):
        """KiCad-decorated pin functions compare equal to intent names/numbers."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U4", "VOUT")},
            "CONN": {NetlistEntry("J2", "Pin_1")},
            "PLUS": {NetlistEntry("BZ1", "+")},
        })
        actual = _parse_sexpr_netlist(
            """
            (export (version "E")
              (nets
                (net (code 1) (name "+3V3")
                  (node (ref "U4") (pin "5") (pinfunction "VOUT_5"))
                )
                (net (code 2) (name "CONN")
                  (node (ref "J2") (pin "1") (pinfunction "Pin_1_1"))
                )
                (net (code 3) (name "PLUS")
                  (node (ref "BZ1") (pin "1") (pinfunction "+_1"))
                )
              )
            )
            """
        )

        result = compare_netlists(expected, actual)

        assert result.success
        assert result.missing_endpoints == []

    def test_permissive_aliases_require_symbol_context(self):
        """VOUT, VOUT_5, and 5 are not equivalent without per-ref alias context."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U4", "VOUT")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U4", "VOUT_5")},
        })

        result = compare_netlists(expected, actual)

        assert not result.success
        assert result.missing_endpoints == [
            {"net": "+3V3", "ref": "U4", "pin": "VOUT"}
        ]

    def test_permissive_aliases_do_not_cross_refs(self):
        """Alias context is scoped by reference designator."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U4", "VOUT")},
        })
        actual = _parse_sexpr_netlist(
            """
            (export (version "E")
              (nets
                (net (code 1) (name "+3V3")
                  (node (ref "U5") (pin "5") (pinfunction "VOUT_5"))
                )
              )
            )
            """
        )

        result = compare_netlists(expected, actual)

        assert not result.success
        assert result.missing_endpoints == [
            {"net": "+3V3", "ref": "U4", "pin": "VOUT"}
        ]

    def test_mismatched_nets_reported(self):
        """Mismatched nets are reported."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1"), NetlistEntry("U2", "8")},
            "GND": {NetlistEntry("U1", "4")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},  # Missing U2.8
            "GND": {NetlistEntry("U1", "4")},
        })

        result = compare_netlists(expected, actual)
        assert not result.success
        assert len(result.mismatched_nets) == 1
        assert result.mismatched_nets[0]["net"] == "+3V3"

    def test_new_net_in_actual(self):
        """Nets present in actual but not expected generate extra endpoints."""
        expected = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},
        })
        actual = NormalizedNetlist(nets={
            "+3V3": {NetlistEntry("U1", "1")},
            "NEW_NET": {NetlistEntry("U2", "5")},
        })

        result = compare_netlists(expected, actual)
        assert result.success  # Extra nets don't fail
        assert len(result.extra_endpoints) == 1


class TestParseSexprNetlist:
    """Tests for KiCad S-expression netlist parsing."""

    def test_basic_parse(self):
        """Parse a simple KiCad netlist."""
        content = '''
        (export (version "E")
          (nets
            (net (code 1) (name "+3V3")
              (node (ref "U1") (pin "1") (pinfunction "VDD"))
              (node (ref "C1") (pin "1") (pinfunction "~"))
            )
            (net (code 2) (name "GND")
              (node (ref "U1") (pin "4") (pinfunction "VSS"))
              (node (ref "C1") (pin "2") (pinfunction "~"))
            )
          )
        )
        '''
        netlist = _parse_sexpr_netlist(content)
        assert "+3V3" in netlist.nets
        assert "GND" in netlist.nets
        assert NetlistEntry("U1", "1") in netlist.nets["+3V3"]
        assert NetlistEntry("C1", "1") in netlist.nets["+3V3"]
        assert NetlistEntry("U1", "4") in netlist.nets["GND"]

    def test_decorated_pinfunction_alias(self):
        """Decorated KiCad pin functions expose plain-name aliases."""
        content = '''
        (export (version "E")
          (nets
            (net (code 1) (name "RESET_N")
              (node (ref "U1") (pin "4") (pinfunction "~{NRST}_4"))
            )
          )
        )
        '''
        netlist = _parse_sexpr_netlist(content)
        assert NetlistEntry("U1", "4") in netlist.nets["RESET_N"]
        assert NetlistEntry("U1", "NRST") not in netlist.nets["RESET_N"]
        assert "NRST" in netlist.aliases_by_ref_pin["U1"]["4"]

    def test_slash_separated_pinfunction_aliases(self):
        """Dual-role KiCad pin functions expose each role as an alias."""
        content = '''
        (export (version "E")
          (nets
            (net (code 1) (name "I2C_SCL")
              (node (ref "U2") (pin "23") (pinfunction "SCL/SCLK"))
            )
            (net (code 2) (name "I2C_SDA")
              (node (ref "U2") (pin "24") (pinfunction "SDA/SDI"))
            )
          )
        )
        '''
        netlist = _parse_sexpr_netlist(content)
        assert NetlistEntry("U2", "23") in netlist.nets["I2C_SCL"]
        assert NetlistEntry("U2", "24") in netlist.nets["I2C_SDA"]
        assert "SCL" in netlist.aliases_by_ref_pin["U2"]["23"]
        assert "SDA" in netlist.aliases_by_ref_pin["U2"]["24"]

    def test_empty_content(self):
        """Empty content produces empty netlist."""
        netlist = _parse_sexpr_netlist("")
        assert netlist.nets == {}


def test_power_net_sanity_detects_ground_short():
    from kicad_mcp.schematic_engine.expected_netlist import check_power_net_sanity

    expected = NormalizedNetlist(nets={
        "+5V": {NetlistEntry("J1", "1")},
        "GND": {NetlistEntry("J1", "2")},
    })
    actual = NormalizedNetlist(nets={
        "+5V": {NetlistEntry("J1", "1"), NetlistEntry("J1", "2")},
    })

    result = check_power_net_sanity(expected, actual)

    assert result["success"] is False
    assert result["issues"][0]["type"] == "power_ground_short"


def test_power_net_sanity_detects_common_rails():
    from kicad_mcp.schematic_engine.expected_netlist import check_power_net_sanity

    expected = NormalizedNetlist(nets={
        "+3V3": {NetlistEntry("U1", "1")},
        "VBUS": {NetlistEntry("J1", "A4")},
        "GND": {NetlistEntry("J1", "A1")},
    })
    actual = NormalizedNetlist(nets={
        "GND": {
            NetlistEntry("U1", "1"),
            NetlistEntry("J1", "A4"),
            NetlistEntry("J1", "A1"),
        },
    })

    result = check_power_net_sanity(expected, actual)

    assert result["success"] is False
    assert result["issues"][0]["intended_power_nets"] == ["+3V3", "GND", "VBUS"]


def test_power_net_sanity_uses_custom_power_rails():
    from kicad_mcp.schematic_engine.expected_netlist import check_power_net_sanity

    expected = NormalizedNetlist(nets={
        "SENSOR_BIAS": {NetlistEntry("U1", "1")},
        "GND": {NetlistEntry("U1", "2")},
    })
    actual = NormalizedNetlist(nets={
        "SENSOR_BIAS": {NetlistEntry("U1", "1"), NetlistEntry("U1", "2")},
    })

    result = check_power_net_sanity(expected, actual, power_nets={"SENSOR_BIAS"})

    assert result["success"] is False
    assert result["issues"][0]["intended_power_nets"] == ["GND", "SENSOR_BIAS"]
