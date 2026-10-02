from __future__ import annotations

import asyncio

import pytest

from llmswitch.catalog import Catalog, ModelEntry
from llmswitch.config import ModelEntrySpec, ModelsSpec, ProviderSpec

pytestmark = pytest.mark.anyio


class FakeProvider:
    def __init__(self, spec: ProviderSpec, models: list[str] | Exception, delay: float = 0.0) -> None:
        self.spec = spec
        self.models = models
        self.delay = delay
        self.calls = 0

    def static_entries(self) -> list[ModelEntry]:
        return [ModelEntry(self.spec.name, e.id, bare=e.bare) for e in self.spec.models.static]

    async def discover(self) -> list[ModelEntry]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        if isinstance(self.models, Exception):
            raise self.models
        return [ModelEntry(self.spec.name, m) for m in self.models] + self.static_entries()


def spec(name: str, **kw) -> ProviderSpec:
    return ProviderSpec(name=name, protocol="anthropic", base_url=f"http://{name}", **kw)


def make() -> tuple[Catalog, dict[str, FakeProvider]]:
    providers = {
        "anthropic": FakeProvider(
            spec("anthropic", match=["claude-*"], models=ModelsSpec(static=[ModelEntrySpec("opus", bare=True)])), []
        ),
        "local": FakeProvider(spec("local"), ["qwen", "shared"]),
        "remote": FakeProvider(spec("remote", default=True), ["big", "shared"]),
    }
    return Catalog(providers, ttl=30, timeout=1), providers


async def test_refresh_and_routing() -> None:
    cat, _ = make()
    await cat.refresh()
    ids = sorted(m.id for m in cat.models())
    assert ids == ["anthropic/opus", "local/qwen", "local/shared", "remote/big", "remote/shared"]

    route, _ = cat.resolve("local/qwen")
    assert (route.provider, route.upstream_model, route.how) == ("local", "qwen", "prefix")
    route, _ = cat.resolve("claude-opus-5-5")
    assert (route.provider, route.how) == ("anthropic", "match")
    route, _ = cat.resolve("qwen")
    assert (route.provider, route.how) == ("local", "discovered")
    route, reason = cat.resolve("shared")
    assert route is None and "local/shared" in reason and "remote/shared" in reason
    route, _ = cat.resolve("never-heard-of")
    assert (route.provider, route.how) == ("remote", "default")
    route, reason = cat.resolve("local/")
    assert route is None and "no model" in reason
    # a slash that is not a provider name is just part of the model id
    route, _ = cat.resolve("hf.co/org/model:Q4")
    assert (route.provider, route.upstream_model) == ("remote", "hf.co/org/model:Q4")


async def test_launch_ids() -> None:
    cat, providers = make()
    await cat.refresh()
    by_id = {m.id: m for m in cat.models()}
    assert by_id["anthropic/opus"].launch_id(providers["anthropic"].spec) == "opus"
    assert by_id["local/qwen"].launch_id(providers["local"].spec) == "local/qwen"
    assert ModelEntry("anthropic", "claude-x").launch_id(providers["anthropic"].spec) == "claude-x"


async def test_failures_keep_static_entries() -> None:
    cat, providers = make()
    providers["local"].models = RuntimeError("boom")
    providers["anthropic"].models = RuntimeError("down")
    await cat.refresh()
    st = cat.status()
    assert st["local"].ok is False and "boom" in st["local"].error
    assert st["anthropic"].ok is False and st["anthropic"].count == 1
    assert any(m.id == "anthropic/opus" for m in cat.models())


async def test_timeout_is_per_provider() -> None:
    cat, providers = make()
    providers["remote"].delay = 5
    cat._timeout = 0.05
    await cat.refresh()
    assert cat.status()["remote"].ok is False and "timed out" in cat.status()["remote"].error
    assert cat.status()["local"].ok is True


async def test_route_refreshes_on_miss_when_stale() -> None:
    cat, providers = make()
    cat._ttl = 0
    await cat.refresh()
    providers["local"].models = ["qwen", "fresh"]
    route, _ = await cat.route("fresh")
    assert (route.provider, route.how) == ("local", "discovered"), (
        "a stale catalog is refreshed before the default catches the name"
    )
    assert providers["local"].calls == 2
    route, _ = await cat.route("still-unknown")
    assert (route.provider, route.how) == ("remote", "default")
