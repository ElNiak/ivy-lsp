"""End-to-end propagation integration tests.

These tests use the minip_worktree fixture to create isolated Git worktree
copies of MiniP, then apply propagation edits and verify the results.
"""

import os

import pytest

from ivy_lsp.mcp.tools.propagation import change_impact_impl


@pytest.mark.integration
class TestPropagationE2E:
    """Full add-field propagation: edit 3 files, verify, revert, verify."""

    def test_add_field_propagation_and_revert(self, minip_worktree):
        protocol_dir = minip_worktree

        # Step 1: analyze
        impact = change_impact_impl("ping_packet", "add_field", protocol_dir)
        assert len(impact["auto_propagate"]) == 3

        # Step 2: store originals (transaction log)
        tx_log = []
        for entry in impact["auto_propagate"]:
            fpath = os.path.join(protocol_dir, entry["file"])
            with open(fpath) as f:
                tx_log.append({"file": fpath, "original": f.read()})

        # Step 3: apply edits to all 3 files
        # Edit 1: add field to ping_packet.ivy
        pkt_path = tx_log[0]["file"]
        with open(pkt_path) as f:
            pkt_src = f.read()
        pkt_src = pkt_src.replace(
            "payload : frame.arr",
            "seq_num : stream_pos,\n        payload : frame.arr",
        )
        with open(pkt_path, "w") as f:
            f.write(pkt_src)

        # Edit 2: add state to serializer
        ser_path = tx_log[1]["file"]
        with open(ser_path) as f:
            ser_src = f.read()
        # Add enum state
        ser_src = ser_src.replace(
            "enum {ping_s_init,",
            "enum {ping_s_init,\n              ping_s_seq_num,",
        )
        # Change init transition (only first occurrence)
        ser_src = ser_src.replace(
            "state = ping_s_payload;",
            "state = ping_s_seq_num;",
            1,
        )
        # Add new case before ping_s_frame case
        ser_src = ser_src.replace(
            "case ping_s_frame:",
            "case ping_s_seq_num:\n"
            "            {\n"
            "                setn(res, 8);\n"
            "                state = ping_s_payload;\n"
            "            }\n"
            "            break;\n"
            "            case ping_s_frame:",
        )
        with open(ser_path, "w") as f:
            f.write(ser_src)

        # Edit 3: add state to deserializer
        deser_path = tx_log[2]["file"]
        with open(deser_path) as f:
            deser_src = f.read()
        deser_src = deser_src.replace(
            "enum {ping_s_init,",
            "enum {ping_s_init,\n              ping_s_seq_num,",
        )
        deser_src = deser_src.replace(
            "state = ping_s_payload;",
            "state = ping_s_seq_num;",
            1,
        )
        deser_src = deser_src.replace(
            "case ping_s_frame:",
            "case ping_s_seq_num:\n"
            "            {\n"
            "                getn(res, 8);\n"
            "                state = ping_s_payload;\n"
            "            }\n"
            "            break;\n"
            "            case ping_s_frame:",
        )
        with open(deser_path, "w") as f:
            f.write(deser_src)

        # Step 4: verify all 3 files were modified
        for entry in tx_log:
            with open(entry["file"]) as f:
                assert (
                    f.read() != entry["original"]
                ), f"File not modified: {entry['file']}"

        # Step 5: revert all from transaction log (reverse order)
        for entry in reversed(tx_log):
            with open(entry["file"], "w") as f:
                f.write(entry["original"])

        # Step 6: verify all files match originals
        for entry in tx_log:
            with open(entry["file"]) as f:
                assert f.read() == entry["original"], f"Revert failed: {entry['file']}"


@pytest.mark.integration
class TestRevert:
    """Verify revert restores all files even after corruption."""

    def test_revert_after_corruption(self, minip_worktree):
        protocol_dir = minip_worktree
        impact = change_impact_impl("ping_packet", "add_field", protocol_dir)

        # Store originals
        tx_log = []
        for entry in impact["auto_propagate"]:
            fpath = os.path.join(protocol_dir, entry["file"])
            with open(fpath) as f:
                tx_log.append({"file": fpath, "original": f.read()})

        # Apply valid edit to file 1
        with open(tx_log[0]["file"], "w") as f:
            f.write(
                tx_log[0]["original"].replace(
                    "payload : frame.arr",
                    "seq_num : stream_pos,\n        payload : frame.arr",
                )
            )

        # Corrupt file 2 (simulates bad edit)
        with open(tx_log[1]["file"], "w") as f:
            f.write("CORRUPTED CONTENT - wrong byte count")

        # Revert all
        for entry in reversed(tx_log):
            with open(entry["file"], "w") as f:
                f.write(entry["original"])

        # Verify ALL files match originals (not just the corrupted one)
        for entry in tx_log:
            with open(entry["file"]) as f:
                assert f.read() == entry["original"], f"Revert failed: {entry['file']}"


@pytest.mark.integration
class TestMidPropagationRejection:
    """Verify mid-propagation rejection behavior."""

    def test_revert_and_abort(self, minip_worktree):
        """Approve file 1, reject file 2 -> revert file 1."""
        protocol_dir = minip_worktree
        impact = change_impact_impl("ping_packet", "add_field", protocol_dir)

        # Store originals
        tx_log = []
        for entry in impact["auto_propagate"]:
            fpath = os.path.join(protocol_dir, entry["file"])
            with open(fpath) as f:
                tx_log.append({"file": fpath, "original": f.read()})

        # Edit file 1 (approved)
        with open(tx_log[0]["file"], "w") as f:
            f.write(
                tx_log[0]["original"].replace(
                    "payload : frame.arr",
                    "seq_num : stream_pos,\n        payload : frame.arr",
                )
            )

        # File 2 rejected -> simulate "revert and abort"
        # Revert only the files that were actually edited (tx_log[:1])
        edited_log = tx_log[:1]
        for entry in reversed(edited_log):
            with open(entry["file"], "w") as f:
                f.write(entry["original"])

        # All files should now match originals
        for entry in tx_log:
            with open(entry["file"]) as f:
                assert (
                    f.read() == entry["original"]
                ), f"File not restored: {entry['file']}"

    def test_skip_and_continue(self, minip_worktree):
        """Approve file 1, skip file 2, approve file 3 -> files 1 and 3 edited."""
        protocol_dir = minip_worktree
        impact = change_impact_impl("ping_packet", "add_field", protocol_dir)

        tx_log = []
        for entry in impact["auto_propagate"]:
            fpath = os.path.join(protocol_dir, entry["file"])
            with open(fpath) as f:
                tx_log.append({"file": fpath, "original": f.read()})

        # Edit file 1 (approved)
        with open(tx_log[0]["file"], "w") as f:
            f.write(
                tx_log[0]["original"].replace(
                    "payload : frame.arr",
                    "seq_num : stream_pos,\n        payload : frame.arr",
                )
            )

        # File 2 skipped -> leave unchanged

        # Edit file 3 (approved) - add state to deserializer
        with open(tx_log[2]["file"]) as f:
            deser_src = f.read()
        deser_src = deser_src.replace(
            "enum {ping_s_init,",
            "enum {ping_s_init,\n              ping_s_seq_num,",
        )
        with open(tx_log[2]["file"], "w") as f:
            f.write(deser_src)

        # File 1 should be edited
        with open(tx_log[0]["file"]) as f:
            assert f.read() != tx_log[0]["original"]

        # File 2 should be unchanged (skipped)
        with open(tx_log[1]["file"]) as f:
            assert f.read() == tx_log[1]["original"]

        # File 3 should be edited
        with open(tx_log[2]["file"]) as f:
            assert f.read() != tx_log[2]["original"]
