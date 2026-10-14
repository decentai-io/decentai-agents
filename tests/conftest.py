from pathlib import Path

import pytest
import yaml

from ai_runtime.agents.egress import EgressProxy
from ai_runtime.agents.environments import AgentEnvironment
from ai_runtime.agents.library import InstalledAgent
from contracts.agent_manifest import load_manifest
from decentai_sdk.manifest import Manifest

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
def _agents_container(tmp_path, monkeypatch):
    """Each test runs as the runtime's own tests do: on one loop, over
    a fake of the agents' container with a confinement of its own, so
    workers, the proxy and the loop all end with the test.

    The proxy opens the names an agent declared and nothing written as
    an address, so a stub stands under the service's own name
    (``stands_for``): every name resolves to this machine, where the
    stubs are, and a stub's port counts as one the web answers on."""
    from ai_runtime.tests import fake_container

    fake_container.begin(tmp_path / "agents")
    monkeypatch.setattr(EgressProxy, "resolver",
                        staticmethod(lambda host, port: ["127.0.0.1"]))
    monkeypatch.setattr(EgressProxy, "PORTS_OF_A_NAMED_HOST", [443, 80])
    yield
    fake_container.end()


def stands_for(host: str, server) -> str:
    """The address a stub gives out while it stands in for a service:
    the name the agent declared, on the port the stub listens on."""
    port = server.server_address[1]
    if port not in EgressProxy.PORTS_OF_A_NAMED_HOST:
        EgressProxy.PORTS_OF_A_NAMED_HOST.append(port)
    return f"http://{host}:{port}"


def resource(manifest: Manifest, kind: str, resource_id: str) -> dict:
    """One declared resource of the manifest's, by kind and id."""
    return next(r for r in manifest.resources(kind) if r["id"] == resource_id)


def declaring(agent: InstalledAgent, hosts) -> InstalledAgent:
    """The agent as if its manifest declared these hosts: ``"any"``,
    or names with ``:<port>`` where the port is not the web's. For
    what the proxy does with a declaration, where a test's stub is not
    where the real service would be."""
    network = {"hosts": "any" if hosts == "any" else list(hosts)}
    document = {**agent.manifest.document, "network": network}
    return InstalledAgent(agent.digest, Manifest(document), agent.folder,
                          agent.environment)
