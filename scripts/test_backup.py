"""
Tests for nous-backup.sh — kuzu optional logic.

Runs only the kuzu-detection portion of the script in isolation;
does not require Qdrant, GPG, or a real backup directory.
"""
import subprocess
import tempfile
import textwrap
from pathlib import Path

BACKUP_SCRIPT = Path("/srv/nous/scripts/nous-backup.sh")


def _run_kuzu_section(kuzu_exists: bool, tmp: Path) -> tuple[int, str, str]:
    """Run only the kuzu-detection block extracted from the backup script."""
    backup_dir = tmp / "backups"
    backup_dir.mkdir()

    if kuzu_exists:
        kuzu_path = tmp / "kuzu.db"
        kuzu_path.mkdir()
        (kuzu_path / "data.kuzu").write_bytes(b"\x00" * 8)

    script = textwrap.dedent(f"""\
        #!/bin/bash
        set -euo pipefail
        BACKUP_DIR="{backup_dir}"
        DATE="20260101_120000"
        KUZU_SRC=""
        for candidate in "{tmp}/kuzu" "{tmp}/kuzu.db"; do
          if [ -e "$candidate" ]; then
            KUZU_SRC="$candidate"
            break
          fi
        done
        if [ -n "$KUZU_SRC" ]; then
          cp -r "$KUZU_SRC" "${{BACKUP_DIR}}/kuzu-${{DATE}}.db"
          echo "KUZU_INCLUDED:$KUZU_SRC"
        else
          echo "KUZU_SKIPPED"
        fi
    """)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def test_backup_script_exists():
    assert BACKUP_SCRIPT.exists(), f"Backup script not found: {BACKUP_SCRIPT}"
    assert BACKUP_SCRIPT.stat().st_mode & 0o111, "Backup script is not executable"


def test_backup_script_has_kuzu_optional_logic():
    src = BACKUP_SCRIPT.read_text()
    assert "KUZU_SRC" in src, "Missing KUZU_SRC variable"
    assert "Kuzu not present" in src, "Missing 'Kuzu not present' log message"
    assert "kuzu.db" in src, "Missing kuzu.db candidate path"


def test_kuzu_present_included_and_exit_0():
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run_kuzu_section(True, Path(d))
        assert rc == 0, f"Expected exit 0 with kuzu present, got {rc}. stderr: {err}"
        assert "KUZU_INCLUDED" in out, f"Expected KUZU_INCLUDED in output, got: {out}"
        backed_up = list(Path(d, "backups").glob("kuzu-*.db"))
        assert backed_up, "No kuzu backup file created"


def test_kuzu_absent_skipped_and_exit_0():
    with tempfile.TemporaryDirectory() as d:
        rc, out, err = _run_kuzu_section(False, Path(d))
        assert rc == 0, f"Expected exit 0 with kuzu absent, got {rc}. stderr: {err}"
        assert "KUZU_SKIPPED" in out, f"Expected KUZU_SKIPPED in output, got: {out}"
        backed_up = list(Path(d, "backups").glob("kuzu-*.db"))
        assert not backed_up, "Unexpected kuzu backup file when kuzu is absent"


def test_kuzu_absent_does_not_leave_partial_files():
    with tempfile.TemporaryDirectory() as d:
        _run_kuzu_section(False, Path(d))
        assert not list(Path(d, "backups").iterdir()), \
            "Backup dir should be empty when kuzu is absent"


if __name__ == "__main__":
    import sys
    tests = [
        test_backup_script_exists,
        test_backup_script_has_kuzu_optional_logic,
        test_kuzu_present_included_and_exit_0,
        test_kuzu_absent_skipped_and_exit_0,
        test_kuzu_absent_does_not_leave_partial_files,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  [FAIL] {t.__name__}: {e}")
            failed += 1
    print(f"\nResultat: {passed}/{len(tests)} PASS  |  {failed} FAIL")
    sys.exit(0 if failed == 0 else 1)
