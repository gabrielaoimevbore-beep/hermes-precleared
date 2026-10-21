import re

import yaml

from conftest import ROOT, FakeCtx, plugin, schemas


def manifest():
    return yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))


def test_register_matches_the_manifest():
    ctx = FakeCtx()
    plugin.register(ctx)
    data = manifest()
    assert sorted(ctx.tools) == sorted(data["provides_tools"])
    assert sorted(ctx.hooks) == sorted(data["provides_hooks"])
    assert all(len(callbacks) == 1 for callbacks in ctx.hooks.values())
    assert all(entry["toolset"] == "precleared" and not entry["override"] for entry in ctx.tools.values())
    assert set(ctx.skills) == {"cold-email-check"}
    assert ctx.skills["cold-email-check"][0].is_file()


def test_register_reads_no_state_or_secrets():
    class StrictCtx(FakeCtx):
        @property
        def state(self):
            raise AssertionError("register() must not touch ctx.state")

        @state.setter
        def state(self, value):
            pass

    plugin.register(StrictCtx())


def test_schemas_name_their_tools():
    assert schemas.PRECLEARED_CHECK["name"] == "precleared_check"
    assert schemas.PRECLEARED_VERIFY_RECEIPT["name"] == "precleared_verify_receipt"
    assert schemas.PRECLEARED_CHECK["parameters"]["required"] == ["email"]


def test_manifest_fields():
    data = manifest()
    assert data["name"] == "precleared"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(data["version"]))
    assert data["requires_hermes"].startswith(">=")
    assert data["config_schema"]["api_key"] == {
        "type": "secret", "env": "PRECLEARED_API_KEY",
        "label": data["config_schema"]["api_key"]["label"],
        "description": data["config_schema"]["api_key"]["description"],
    }
    assert data["config_schema"]["mode"]["default"] == "ask"
    assert "requires_env" not in data  # the key is optional: no key means the free allowance


def test_skill_frontmatter():
    text = (ROOT / "skills" / "cold-email-check" / "SKILL.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---", 2)[1])
    assert front["name"] == "cold-email-check"
    assert 20 < len(front["description"]) <= 1024


def test_no_em_dashes_in_user_facing_text():
    for path in [ROOT / "README.md", ROOT / "plugin.yaml", ROOT / "skills" / "cold-email-check" / "SKILL.md",
                 *ROOT.glob("*.py")]:
        if path.exists():
            assert "\u2014" not in path.read_text(encoding="utf-8"), path.name
