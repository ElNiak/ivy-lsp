"""Tests for manifest validation (Step 1.5)."""

from ivy_lsp.semantic.rfc_annotations import validate_manifest


class TestValidateManifest:
    def test_valid_manifest_no_warnings(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {
                    "text": "senders MUST NOT send data",
                    "section": "4.1",
                    "level": "MUST",
                },
            },
        }
        warnings = validate_manifest(data)
        assert warnings == []

    def test_missing_rfc_field(self):
        data = {"requirements": {"a": {"text": "t", "section": "1", "level": "MUST"}}}
        warnings = validate_manifest(data)
        assert any("rfc" in w.lower() for w in warnings)

    def test_missing_requirements_field(self):
        data = {"rfc": "RFC9000"}
        warnings = validate_manifest(data)
        assert any("requirements" in w.lower() for w in warnings)

    def test_requirements_not_dict(self):
        data = {"rfc": "RFC9000", "requirements": [1, 2, 3]}
        warnings = validate_manifest(data)
        assert any("not a mapping" in w for w in warnings)

    def test_missing_text_field(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {"section": "4.1", "level": "MUST"},
            },
        }
        warnings = validate_manifest(data)
        assert any("text" in w for w in warnings)

    def test_missing_section_field(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {"text": "something", "level": "MUST"},
            },
        }
        warnings = validate_manifest(data)
        assert any("section" in w for w in warnings)

    def test_invalid_level(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {
                    "text": "something",
                    "section": "4.1",
                    "level": "CRITICAL",
                },
            },
        }
        warnings = validate_manifest(data)
        assert any("invalid level" in w.lower() for w in warnings)

    def test_level_synonym_normalized(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {
                    "text": "something",
                    "section": "4.1",
                    "level": "SHALL",
                },
            },
        }
        warnings = validate_manifest(data)
        assert any("normalized" in w.lower() for w in warnings)
        assert any("MUST" in w for w in warnings)

    def test_recommended_normalized_to_should(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {
                "rfc9000:4.1": {
                    "text": "something",
                    "section": "4.1",
                    "level": "RECOMMENDED",
                },
            },
        }
        warnings = validate_manifest(data)
        assert any("SHOULD" in w for w in warnings)

    def test_not_a_dict_root(self):
        warnings = validate_manifest("not a dict")
        assert any("not a mapping" in w for w in warnings)

    def test_requirement_entry_not_dict(self):
        data = {
            "rfc": "RFC9000",
            "requirements": {"rfc9000:4.1": "just a string"},
        }
        warnings = validate_manifest(data)
        assert any("not a mapping" in w for w in warnings)

    def test_valid_levels_accepted(self):
        """All standard levels produce no warnings."""
        for level in ("MUST", "MUST NOT", "SHOULD", "SHOULD NOT", "MAY"):
            data = {
                "rfc": "RFC9000",
                "requirements": {
                    "rfc9000:4.1": {
                        "text": "something",
                        "section": "4.1",
                        "level": level,
                    },
                },
            }
            warnings = validate_manifest(data)
            assert warnings == [], f"Unexpected warning for level {level}: {warnings}"
