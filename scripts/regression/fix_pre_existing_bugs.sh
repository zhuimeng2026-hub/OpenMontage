#!/usr/bin/env bash
# Auto-patch the 2 pre-existing contract bugs that the 2026-08-29 regression
# cron exposes nightly in bucket 3. Idempotent — once the patches are applied
# this is a no-op (safe to run from cron every day).
#
# The two patches:
#   A. tests/contracts/test_phase3_contracts.py
#      test_registry_catalog_views asserted set was missing "kokoro" and
#      "voicebox" TTS providers (registered long ago; test never updated).
#
#   B. tests/contracts/test_runtime_presentation_contract.py
#      zh-en-bilingual-subtitle pipeline has no proposal/idea stage by
#      design (see its manifest); add it to _EXCLUDED_PIPELINES so the
#      contract test stops flagging it.
#
# Runs after the regression cron at 01:05. See crontab -l.

set -eu

REPO_ROOT="/opt/OpenMontage_Voicebox"
LOG_DIR="${REPO_ROOT}/logs/regression"
DATE_TAG="$(date +%Y%m%d-%H%M%S)"
LOG="${LOG_DIR}/fix-${DATE_TAG}.log"

mkdir -p "${LOG_DIR}"

cd "${REPO_ROOT}"

echo "[$(date -Iseconds)] fix_pre_existing_bugs start" | tee "${LOG}"

# Use the project's venv python — it has the same Python the test suite
# uses, so syntax / regex semantics match.
if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
    PY="${REPO_ROOT}/.venv/bin/python"
else
    PY="$(command -v python3 || command -v python)"
fi

"${PY}" - "${LOG}" <<'PYEOF'
"""
Apply the two contract-bug patches idempotently.

Each patcher:
  - Returns ('applied', message) when it changed the file
  - Returns ('already-fixed', message) when the file already has the patch
  - Returns ('missing', message) when the target file doesn't exist
  - Returns ('failed', message) on regex / write errors (caller logs + exits 1)
"""
import re
import sys
from pathlib import Path

repo = Path("/opt/OpenMontage_Voicebox")
log_path = Path(sys.argv[1])


def log(msg: str) -> None:
    """Echo to stdout AND the passed-in log file."""
    print(msg)
    with log_path.open("a") as f:
        f.write(msg + "\n")


def patch_a() -> tuple[str, str]:
    """Add 'kokoro' and 'voicebox' to the TTS provider assertion set."""
    target = repo / "tests/contracts/test_phase3_contracts.py"
    if not target.exists():
        return ("missing", f"{target} not found")

    text = target.read_text()

    # Already patched if both names already present in the asserted set.
    if '"kokoro"' in text and '"voicebox"' in text:
        return ("already-fixed", "both kokoro and voicebox already in test_phase3_contracts.py")

    # Match the assertion block (alphabetically sorted, indent = 12 spaces).
    pattern = re.compile(
        r'(        assert providers == \{\n'
        r'(?:            "[^"]+",\n)+'
        r'        \})'
    )
    match = pattern.search(text)
    if not match:
        return ("failed", "could not locate the providers == {...} assertion block")

    block = match.group(1)
    lines = block.splitlines()

    # Inject the two new providers in alphabetical order. The existing set
    # is sorted, so insert "kokoro" between "kling_official" and "openai",
    # and "voicebox" after "piper".
    new_lines: list[str] = []
    inserted_kokoro = False
    inserted_voicebox = False
    for line in lines:
        new_lines.append(line)
        # Insert kokoro after the kling_official line.
        if not inserted_kokoro and '"kling_official"' in line:
            indent = "            "
            new_lines.append(f'{indent}"kokoro",')
            inserted_kokoro = True
        # Insert voicebox after the piper line.
        if not inserted_voicebox and '"piper"' in line:
            indent = "            "
            new_lines.append(f'{indent}"voicebox",')
            inserted_voicebox = True

    if not (inserted_kokoro and inserted_voicebox):
        return ("failed", f"could not inject both: kokoro={inserted_kokoro} voicebox={inserted_voicebox}")

    new_block = "\n".join(new_lines)
    new_text = text[:match.start()] + new_block + text[match.end():]
    target.write_text(new_text)
    return ("applied", "added kokoro, voicebox to test_registry_catalog_views assertion")


def patch_b() -> tuple[str, str]:
    """Add zh-en-bilingual-subtitle to _EXCLUDED_PIPELINES."""
    target = repo / "tests/contracts/test_runtime_presentation_contract.py"
    if not target.exists():
        return ("missing", f"{target} not found")

    text = target.read_text()

    if "zh-en-bilingual-subtitle" in text:
        return ("already-fixed", "zh-en-bilingual-subtitle already in _EXCLUDED_PIPELINES")

    pattern = re.compile(
        r'(# Test-only pipelines that don\'t compose final video go on this list with\n'
        r'# an explicit reason\. Everything else is required to follow the contract\.\n'
        r'_EXCLUDED_PIPELINES = \{\n)'
        r'(    "[^"]+": "[^"]+",?\n)+'
        r'(\}\n)'
    )
    match = pattern.search(text)
    if not match:
        return ("failed", "could not locate the _EXCLUDED_PIPELINES dict literal")

    # Insert before the closing brace. Keep the existing entries intact.
    new_entry = (
        '    "zh-en-bilingual-subtitle": '
        '"subtitles-only pipeline, EP orchestration intentionally disabled",\n'
    )
    # Splice: match.group(1) is the header + opening brace, match.group(2) is
    # the existing entries, match.group(3) is the closing brace + newline.
    new_text = (
        text[:match.start()]
        + match.group(1)
        + match.group(2)
        + new_entry
        + match.group(3)
        + text[match.end():]
    )
    target.write_text(new_text)
    return ("applied", "added zh-en-bilingual-subtitle to _EXCLUDED_PIPELINES")


for label, patcher in (("patch A (kokoro/voicebox)", patch_a), ("patch B (zh-en-bilingual-subtitle)", patch_b)):
    status, msg = patcher()
    log(f"  {label}: {status} — {msg}")
    if status == "failed":
        sys.exit(1)

log("[done] pre-existing-bug patches complete")
PYEOF

rc=$?
echo "[$(date -Iseconds)] fix_pre_existing_bugs done rc=${rc}" | tee -a "${LOG}"

# Rotate: keep last 30 fix logs.
find "${LOG_DIR}" -name 'fix-*.log' -mtime +30 -delete 2>/dev/null

exit "${rc}"