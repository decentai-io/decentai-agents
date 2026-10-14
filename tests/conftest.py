from pathlib import Path

import pytest
import yaml

from ai_runtime.agents.environments import AgentEnvironment
from ai_runtime.agents.library import InstalledAgent
from contracts.agent_manifest import load_manifest

ROOT = Path(__file__).resolve().parents[1]

# One shared environment for every catalog agent, holding the UNION of
# their declared dependencies — cached across runs (the .ready marker
# short-circuits the build). After changing any agent's dependencies,
# delete tests/.workerenv so the next run rebuilds it.
WORKERENV_DIR = Path(__file__).resolve().parent / ".workerenv"


@pytest.fixture(scope="session")
def agents():
    """Every catalog agent as a real InstalledAgent — functions execute
    in real workers over the real wire, exactly as production runs
    them. Addressed by the agent's own id, unique per organization."""
    catalog = yaml.safe_load(
        (ROOT / "decentai-agents.yaml").read_text(encoding="utf-8"))

    entries = []
    for entry in catalog["agents"]:
        folder = ROOT / entry["path"]
        manifest, errors = load_manifest(folder / "manifest.yaml")
        assert errors == [], f"{entry['id']}: {errors}"
        entries.append((folder, manifest))

    environment = AgentEnvironment(WORKERENV_DIR)
    if not environment.exists():
        # Built as a runtime builds one: in a builder's place of a
        # stand-alone container, with the union of the agents' declared
        # dependencies.
        from ai_runtime.tests.fake_container import standalone

        dependencies = [d for _, m in entries for d in m.dependencies]
        with standalone(WORKERENV_DIR.parent / ".workerenv-build") as own:
            errors = own.run(environment.build(
                dependencies, own.confinement.builder_place()))
        assert errors == [], errors
    else:
        # The platform's SDK moves on; an environment built last week
        # carries last week's. Brought up to date as a runtime does it
        # before it starts a worker.
        import shutil

        import decentai_sdk

        target = environment._site_packages()
        assert target is not None, "the cached venv has no site-packages"
        shutil.copytree(Path(decentai_sdk.__file__).resolve().parent,
                        target / "decentai_sdk", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"))

    return {
        manifest.agent_id: InstalledAgent(
            f"fixture:{manifest.agent_id}", manifest, folder, environment
        )
        for folder, manifest in entries
    }


@pytest.fixture(autouse=True)
def _reap_worker_pools():
    """Workers spawned during a test die with it — each test's executors
    own private WorkerPools whose processes would otherwise outlive the
    test's event loop."""
    yield
    from ai_runtime.agents.pool import WorkerPool

    WorkerPool.terminate_all()
