# TokenMeter Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the product to TokenMeter and publish a tested Python package that installs in one command and guides a new user to their first measurement.

**Architecture:** Keep the existing watcher, meter, overlay, and hook design. Move mutable runtime state out of the source/package directory, ship the service registry as package data, expose one `tokenmeter` console entry point, and retain only safe one-time migration support for old local data and hook entries.

**Tech Stack:** Python 3.10+, setuptools, PyYAML, PyQt6, watchdog, GitHub CLI

**Spec:** `docs/superpowers/specs/2026-08-13-tokenmeter-release-design.md`

## Global Constraints

- Canonical display name is `TokenMeter`; canonical import package, CLI, repository, config, and data name is `tokenmeter`. The Python distribution name is `oct7-tokenmeter` because `tokenmeter` is already owned on PyPI.
- Old names are allowed only in migration code and historical design/change documentation.
- Default installation includes PyYAML, PyQt6, and watchdog; no additional runtime dependency is introduced.
- Existing local data is copied, never deleted, during migration.
- Existing foreign hooks are preserved; TokenMeter hook installation remains idempotent.
- GitHub visibility changes only after tests, package build, isolated install, and tracked-history secret scans pass.
- Signed apps, Homebrew, central leaderboard accounts, sharing cards, telemetry, and new agent parsers are out of scope.

---

### Task 1: Relocatable runtime paths and safe legacy migration

**Files:**
- Create: `tokenpet/paths.py` (renamed to `tokenmeter/paths.py` in Task 2)
- Modify: `tokenpet/config.py`
- Modify: `tokenpet/hook.py`
- Modify: `tokenpet/leaderboard.py`
- Modify: `tokenpet/pricing.py`
- Test: `test_tokenpet.py`

**Interfaces:**
- Produces: `data_dir() -> Path`, `config_dir() -> Path`, `migrate_legacy(legacy_root: Path, legacy_config: Path) -> None`
- Consumed by: configuration, hook, meter, leaderboard, pricing, and the packaging smoke test

- [ ] **Step 1: Write failing path and migration tests**

Add tests that set `TOKENMETER_HOME` and `XDG_CONFIG_HOME`, import `tokenpet.paths`, and assert that runtime files resolve under those directories rather than beside the package. Create a legacy `data/state.json` and `~/.config/tokenpet/services.yaml` inside the temporary directory and assert `migrate_legacy()` copies both without deleting either source.

```python
def test_runtime_paths_and_legacy_copy(tmp: Path) -> None:
    from tokenpet.paths import config_dir, data_dir, migrate_legacy

    old_env = dict(os.environ)
    try:
        os.environ["TOKENMETER_HOME"] = str(tmp / "state")
        os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
        legacy_root = tmp / "checkout"
        (legacy_root / "data").mkdir(parents=True)
        (legacy_root / "data" / "state.json").write_text('{"version": 2}')
        legacy_config = tmp / "home" / ".config" / "tokenpet"
        legacy_config.mkdir(parents=True)
        (legacy_config / "services.yaml").write_text("services: {}\n")

        migrate_legacy(legacy_root, legacy_config)

        assert data_dir() == tmp / "state"
        assert config_dir() == tmp / "config" / "tokenmeter"
        assert (data_dir() / "state.json").exists()
        assert (legacy_root / "data" / "state.json").exists()
        assert (config_dir() / "services.yaml").exists()
        assert (legacy_config / "services.yaml").exists()
    finally:
        os.environ.clear()
        os.environ.update(old_env)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 test_tokenpet.py`

Expected: `test_runtime_paths_and_legacy_copy` fails with `ModuleNotFoundError: tokenpet.paths`.

- [ ] **Step 3: Implement the stdlib-only path module**

Implement dynamic path functions so environment changes made by tests are observed. `migrate_legacy` copies directories only when the destination is absent and swallows `OSError` because migration must never block the agent.

```python
def data_dir() -> Path:
    override = os.environ.get("TOKENMETER_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tokenmeter"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "tokenmeter"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "tokenmeter"

def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tokenmeter"
```

Update all runtime constants to derive from this module. Keep `hook.py` stdlib-only by importing only `tokenpet.paths`, with a source-checkout fallback that adds the repository parent to `sys.path`.

- [ ] **Step 4: Verify GREEN and the existing suite**

Run: `python3 test_tokenpet.py`

Expected: all tests pass, including the new path/migration test.

- [ ] **Step 5: Commit the path change**

```bash
git add tokenpet/paths.py tokenpet/config.py tokenpet/hook.py tokenpet/leaderboard.py tokenpet/pricing.py test_tokenpet.py
git commit -m "feat: store tokenmeter state outside the package"
```

### Task 2: Canonical TokenMeter identity and package namespace

**Files:**
- Rename: `tokenpet/` to `tokenmeter/`
- Rename: `config/services.yaml` to `tokenmeter/services.yaml`
- Rename: `test_tokenpet.py` to `test_tokenmeter.py`
- Modify: every Python import, process command, display string, temporary prefix, fixture path, and installer marker
- Test: `test_tokenmeter.py`

**Interfaces:**
- Produces: importable `tokenmeter` package and `python -m tokenmeter.cli`
- Preserves: recognition and in-place replacement of hooks containing `/tokenpet/hook.py` or `/src/hook.py`

- [ ] **Step 1: Add a failing public-identity test before renaming**

```python
def test_public_identity(tmp: Path) -> None:
    root = Path(__file__).resolve().parent
    assert (root / "tokenmeter" / "__init__.py").exists()
    assert not (root / "tokenpet").exists()
    assert (root / "tokenmeter" / "services.yaml").exists()
```

- [ ] **Step 2: Run the suite and verify RED**

Run: `python3 test_tokenpet.py`

Expected: `test_public_identity` fails because `tokenmeter/__init__.py` does not exist.

- [ ] **Step 3: Rename files and mechanically replace canonical identifiers**

Move the current untracked `tokenpet` implementation to `tokenmeter`, move the modified registry into the package, and rename the test runner. Replace imports and process commands with `tokenmeter`. Change user-facing text from `TokenPet` to `TokenMeter` and fixture paths from `token-pet` to `tokenmeter`.

Set `DEFAULT_CONFIG = Path(__file__).with_name("services.yaml")` so the registry is found inside wheels.

Keep explicit legacy constants:

```python
LEGACY_MARKERS = (
    f'"{ROOT / "src" / "hook.py"}"',
    "/tokenpet/hook.py\"",
)
```

Update the installer test so an old `tokenpet/hook.py` entry is reported stale and replaced by the new `tokenmeter/hook.py` command without duplication.

- [ ] **Step 4: Verify identity scan and suite GREEN**

Run: `python3 test_tokenmeter.py`

Run: `rg -n 'from tokenpet|import tokenpet|python3 -m tokenpet|TokenPet' tokenmeter test_tokenmeter.py config skills README.md README.ko.md docs hooks requirements.txt 2>/dev/null`

Expected: the test suite passes; the scan returns only migration/history references explicitly allowed by the spec.

- [ ] **Step 5: Commit the namespace rename**

```bash
git add -A tokenpet tokenmeter config test_tokenpet.py test_tokenmeter.py
git commit -m "refactor: rename tokenpet to tokenmeter"
```

### Task 3: Buildable package and one-command isolated installation

**Files:**
- Create: `pyproject.toml`
- Create: `LICENSE`
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Modify: `tokenmeter/__init__.py`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Produces: console command `tokenmeter = tokenmeter.cli:main`
- Produces: wheel containing `tokenmeter/services.yaml`
- Consumes: Task 2 package namespace

- [ ] **Step 1: Write a failing packaging contract test**

```python
def test_packaging_contract(tmp: Path) -> None:
    root = Path(__file__).resolve().parent
    metadata = (root / "pyproject.toml").read_text()
    assert 'name = "oct7-tokenmeter"' in metadata
    assert 'tokenmeter = "tokenmeter.cli:main"' in metadata
    assert 'requires-python = ">=3.10"' in metadata
    for dependency in ("PyYAML>=6.0", "PyQt6>=6.6", "watchdog>=4.0"):
        assert f'"{dependency}"' in metadata
```

- [ ] **Step 2: Run the suite and verify RED**

Run: `python3 test_tokenmeter.py`

Expected: `test_packaging_contract` fails because `pyproject.toml` is absent.

- [ ] **Step 3: Add minimal setuptools metadata and MIT license**

Use static version `0.1.0`, the concise English README, and package data:

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "oct7-tokenmeter"
version = "0.1.0"
description = "Live local meter for Claude Code, Codex, and OpenCode sessions"
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
dependencies = ["PyYAML>=6.0", "PyQt6>=6.6", "watchdog>=4.0"]

[project.scripts]
tokenmeter = "tokenmeter.cli:main"

[tool.setuptools.package-data]
tokenmeter = ["services.yaml"]
```

- [ ] **Step 4: Verify unit contract, build, and wheel contents**

Run: `python3 test_tokenmeter.py`

Run: `uv build`

Run: `python3 -m zipfile -l dist/oct7_tokenmeter-0.1.0-py3-none-any.whl | rg 'tokenmeter/(services.yaml|cli.py|hook.py)'`

Expected: suite and build exit 0; all three required package files are present.

- [ ] **Step 5: Commit packaging**

```bash
git add pyproject.toml LICENSE requirements.txt .gitignore tokenmeter/__init__.py test_tokenmeter.py
git commit -m "build: package tokenmeter as an installable CLI"
```

### Task 4: First-run activation guidance and simplified agent skill

**Files:**
- Modify: `tokenmeter/cli.py`
- Rename: `skills/tokenpet/` to `skills/tokenmeter/`
- Rename: `skills/tokenmeter/tp` to `skills/tokenmeter/tm`
- Rename: four command files from `tp*.md` to `tm*.md`
- Modify: `skills/tokenmeter/SKILL.md`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Produces: `_activation_lines(overlay_available: bool) -> List[str]`
- Produces: `/tm`, `/tm-meter`, `/tm-measure`, `/tm-doctor`
- Consumes: installed `tokenmeter` console command; the skill no longer locates a source checkout or virtualenv

- [ ] **Step 1: Write a failing activation-copy test**

```python
def test_activation_guidance(tmp: Path) -> None:
    from tokenmeter.cli import _activation_lines

    text = "\n".join(_activation_lines(True))
    assert "1." in text and "에이전트를 완전히 다시" in text
    assert "2." in text and "프롬프트" in text
    assert "3." in text and "tokenmeter status" in text
    assert "4." in text and "tokenmeter doctor" in text
```

- [ ] **Step 2: Run the suite and verify RED**

Run: `python3 test_tokenmeter.py`

Expected: `ImportError` for `_activation_lines`.

- [ ] **Step 3: Implement and display activation guidance**

Add the pure helper and print it after non-dry-run installation and when `status` has no recorded calls. Do not launch a fake demo or mutate state.

Replace the skill resolver with a small PATH wrapper:

```bash
#!/usr/bin/env bash
set -euo pipefail
BIN=${TOKENMETER_BIN:-tokenmeter}
command -v "$BIN" >/dev/null 2>&1 || {
  echo "TokenMeter is not installed. Run: uv tool install git+https://github.com/Oct7/tokenmeter.git" >&2
  exit 1
}
exec "$BIN" "$@"
```

- [ ] **Step 4: Verify activation tests and shell syntax**

Run: `python3 test_tokenmeter.py`

Run: `bash -n skills/tokenmeter/tm`

Expected: both commands exit 0.

- [ ] **Step 5: Commit onboarding and skill rename**

```bash
git add tokenmeter/cli.py test_tokenmeter.py skills
git commit -m "feat: guide users to their first token measurement"
```

### Task 5: Conversion-focused bilingual documentation

**Files:**
- Replace: `README.md` with concise English landing documentation
- Create: `README.ko.md`
- Create: `docs/reference.ko.md` from the existing detailed README content
- Modify: `docs/add-service.md`
- Modify: `docs/superpowers/specs/2026-08-11-history-screen-design.md`
- Modify: `hooks/README.md`
- Modify: `tokenmeter/services.yaml`
- Test: `test_tokenmeter.py`

**Interfaces:**
- Documents: `uv tool install`, `pipx install`, `tokenmeter install`, `tokenmeter status`, uninstall and privacy behavior
- Describes the existing endpoint-based board only as an optional self-hosted leaderboard

- [ ] **Step 1: Add a failing documentation contract test**

```python
def test_public_docs_contract(tmp: Path) -> None:
    root = Path(__file__).resolve().parent
    english = (root / "README.md").read_text()
    korean = (root / "README.ko.md").read_text()
    for text in (english, korean):
        assert "uv tool install git+https://github.com/Oct7/tokenmeter.git" in text
        assert "tokenmeter install" in text
        assert "tokenmeter status" in text
    assert "Local-first" in english
    assert "로컬" in korean
```

- [ ] **Step 2: Run the suite and verify RED**

Run: `python3 test_tokenmeter.py`

Expected: failure because `README.ko.md` is absent and the old README lacks the new install command.

- [ ] **Step 3: Rewrite landing docs and preserve advanced reference**

Lead with the approved headlines, a terminal preview, supported agents, privacy, install, activation, four benefits, controls, uninstall, and links to advanced reference/add-service docs. Keep both landing READMEs under 220 lines. Mechanically update the advanced documentation to canonical names while retaining migration notes.

- [ ] **Step 4: Verify docs, stale-name scan, and links**

Run: `python3 test_tokenmeter.py`

Run: `rg -n 'Oct7/token-pet|python3 -m tokenpet|TokenPet 서비스|# TokenPet' README.md README.ko.md docs hooks skills tokenmeter --glob '!docs/superpowers/specs/2026-08-13-tokenmeter-release-design.md'`

Expected: suite passes and stale-name scan returns no unapproved result.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md README.ko.md docs hooks tokenmeter/services.yaml test_tokenmeter.py
git commit -m "docs: launch TokenMeter with bilingual onboarding"
```

### Task 6: Isolated installation, release audit, and GitHub publication

**Files:**
- Create: `.github/workflows/test.yml`
- Modify: Git remote URL and GitHub repository metadata
- Test: clean temporary virtual environment and full repository verification

**Interfaces:**
- Produces: public `https://github.com/Oct7/tokenmeter`
- Produces: GitHub release `v0.1.0`
- Consumes: wheel/sdist and verified main branch from Tasks 1–5

- [ ] **Step 1: Add CI before changing repository visibility**

Create this Python 3.10/3.12 workflow, which installs the package and build frontend, runs the self-checks, and builds both artifacts:

```yaml
name: test
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.10", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: python -m pip install --upgrade pip build
      - run: python -m pip install .
      - run: python test_tokenmeter.py
      - run: QT_QPA_PLATFORM=offscreen python -m tokenmeter.overlay
      - run: python -m tokenmeter.leaderboard
      - run: python -m tokenmeter.endpoints
      - run: python -m build
```

- [ ] **Step 2: Run fresh local verification**

Run:

```bash
python3 test_tokenmeter.py
QT_QPA_PLATFORM=offscreen python3 -m tokenmeter.overlay
python3 -m tokenmeter.leaderboard
python3 -m tokenmeter.endpoints
uv build
```

Expected: every command exits 0 with no failed assertion.

- [ ] **Step 3: Install the built wheel in an isolated temporary environment**

```bash
SMOKE_DIR=$(mktemp -d)
python3 -m venv "$SMOKE_DIR/venv"
"$SMOKE_DIR/venv/bin/pip" install --quiet dist/oct7_tokenmeter-0.1.0-py3-none-any.whl
TOKENMETER_HOME="$SMOKE_DIR/state" XDG_CONFIG_HOME="$SMOKE_DIR/config" "$SMOKE_DIR/venv/bin/tokenmeter" --help
TOKENMETER_HOME="$SMOKE_DIR/state" XDG_CONFIG_HOME="$SMOKE_DIR/config" "$SMOKE_DIR/venv/bin/tokenmeter" services
TOKENMETER_HOME="$SMOKE_DIR/state" XDG_CONFIG_HOME="$SMOKE_DIR/config" "$SMOKE_DIR/venv/bin/tokenmeter" install --dry-run
```

Expected: all three TokenMeter commands exit 0 and use only the temporary state/config paths.

- [ ] **Step 4: Scan tracked worktree and full Git history for secrets**

Run `gitleaks git . --redact` when gitleaks is installed. Always run both fallback scans:

```bash
rg -n --hidden --glob '!.git/**' --glob '!data/**' '(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)'
git log -p --all | rg -n '(sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY)'
```

Expected: no match. Any match stops publication for manual review.

- [ ] **Step 5: Commit CI and create a sanitized root commit for public main**

```bash
git add .github/workflows/test.yml
git commit -m "ci: verify TokenMeter packages"
PUBLIC_TREE=$(git rev-parse HEAD^{tree})
PUBLIC_COMMIT=$(printf '%s\n' "Initial public release: TokenMeter" | GIT_AUTHOR_NAME=Oct7 GIT_AUTHOR_EMAIL=60641646+Oct7@users.noreply.github.com GIT_COMMITTER_NAME=Oct7 GIT_COMMITTER_EMAIL=60641646+Oct7@users.noreply.github.com git commit-tree "$PUBLIC_TREE")
git branch -f public-main "$PUBLIC_COMMIT"
gitleaks git . --log-opts=public-main --redact --no-banner
git push --force origin public-main:main
```

- [ ] **Step 6: Rename and publish the GitHub repository**

```bash
gh repo rename tokenmeter --repo Oct7/token-pet --yes
git remote set-url origin https://github.com/Oct7/tokenmeter.git
gh repo edit Oct7/tokenmeter --visibility public --accept-visibility-change-consequences
gh repo edit Oct7/tokenmeter --description "Live local meter for Claude Code, Codex, and OpenCode sessions" --add-topic ai-agents --add-topic claude-code --add-topic codex --add-topic token-usage --add-topic developer-tools
git push -u origin main
```

Expected: `gh repo view Oct7/tokenmeter --json name,isPrivate,licenseInfo,defaultBranchRef` reports name `tokenmeter`, `isPrivate: false`, MIT license, and default branch `main`.

- [ ] **Step 7: Create the first release**

```bash
git tag -a v0.1.0 -m "TokenMeter v0.1.0"
git push origin v0.1.0
gh release create v0.1.0 dist/oct7_tokenmeter-0.1.0.tar.gz dist/oct7_tokenmeter-0.1.0-py3-none-any.whl --repo Oct7/tokenmeter --title "TokenMeter v0.1.0" --notes "First public release: one-command installation, live multi-agent metering, local history, and guided activation."
```

- [ ] **Step 8: Verify the public installation path**

Run:

```bash
PUBLIC_SMOKE=$(mktemp -d)
UV_TOOL_DIR="$PUBLIC_SMOKE/tools" UV_TOOL_BIN_DIR="$PUBLIC_SMOKE/bin" uv tool install git+https://github.com/Oct7/tokenmeter.git
"$PUBLIC_SMOKE/bin/tokenmeter" --help
```

Expected: public clone/install succeeds and the console help identifies TokenMeter.
