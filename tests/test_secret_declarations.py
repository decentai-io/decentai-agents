"""Every secret field the code reads, the manifest declares.

The sweep this test pins down: a tool reading `secret.get("imap_host")`
depends on the manifest declaring an `imap_host` field — whatever its
storage, since `use` hands the function keys and values together. A read
of an undeclared field returns nothing in production while a test stub
can quietly supply it, so the contract is checked here, statically,
for every catalog agent.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

# The repository convention: the resolved credential is held in a
# variable named `secret`, read via .get("field") or ["field"].
SECRET_READ = re.compile(
    r"""secret(?:\.get\(\s*|\[\s*)["']([a-z0-9_]+)["']"""
)


def catalog_agents():
    with open(ROOT / "decentai-agents.yaml", encoding="utf-8") as handle:
        catalog = yaml.safe_load(handle)
    return [(entry["id"], ROOT / entry["path"])
            for entry in catalog["agents"]]


# What the platform writes into a connected account's credential — a
# secret with an ``oauth`` block declares none of these itself, and the
# agent may read all of them.
OAUTH_FIELDS = {"account", "access_token", "refresh_token", "expires_at", "status"}


def declared_secret_fields(folder):
    with open(folder / "manifest.yaml", encoding="utf-8") as handle:
        manifest = yaml.safe_load(handle)
    fields = set()
    for resource in (manifest.get("resources") or {}).get("secrets") or []:
        for field in resource.get("fields") or []:
            fields.add(field["name"])
        if resource.get("oauth"):
            fields |= OAUTH_FIELDS
    return fields


def test_every_secret_field_the_code_reads_is_declared():
    checked = 0
    for agent_id, folder in catalog_agents():
        declared = declared_secret_fields(folder)
        reads = set()
        for source in folder.rglob("*.py"):
            reads.update(SECRET_READ.findall(
                source.read_text(encoding="utf-8")))
        if not reads:
            continue
        checked += 1
        undeclared = reads - declared
        assert not undeclared, (
            f"{agent_id} reads secret fields its manifest does not "
            f"declare: {sorted(undeclared)}"
        )
    # The check only means something while it is actually checking an
    # agent that reads secrets — the reference agent does.
    assert checked >= 1


def test_every_agent_of_one_provider_asks_for_the_same_scopes():
    """A person's Google (or Microsoft) account is shared between agents
    by granting one saved credential to each of them, and that
    credential holds only the scopes of the definition it was connected
    through. An agent asking for a scope the others do not would find
    it missing whenever the account was connected through a sibling —
    so every agent of one provider declares the identical list."""
    by_provider = {}
    for agent_id, folder in catalog_agents():
        with open(folder / "manifest.yaml", encoding="utf-8") as handle:
            manifest = yaml.safe_load(handle)
        for resource in (manifest.get("resources") or {}).get("secrets") or []:
            oauth = resource.get("oauth") or {}
            if oauth:
                by_provider.setdefault(oauth["provider"], {})[agent_id] = oauth["scopes"]
    for provider, agents in by_provider.items():
        lists = {tuple(scopes) for scopes in agents.values()}
        assert len(lists) == 1, (
            f"{provider} agents disagree on scopes: "
            + "; ".join(f"{a}={s}" for a, s in sorted(agents.items())))

