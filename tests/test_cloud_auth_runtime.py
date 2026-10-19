"""Cloud-scene catalog auth retry, single-flight re-login and bounded concurrency (synthetic tokens only)."""
import asyncio
import logging

import pytest

from custom_components.lepro_led.api import CloudScene, LeproApiError, LeproAuthError
from custom_components.lepro_led.runtime import LeproRuntime

OLD, NEW = "tok-OLD-synthetic", "tok-NEW-synthetic"
SCENE_OLD = CloudScene("cid-1", "Old", "N01:P1000212345678ABCDEFF2100021234ABCDU3VFAKE0001;")
SCENE_NEW = CloudScene("cid-2", "New", "N01:P10001AA00BBF210001FFFFU3VFAKE0002;")


class FakeApi:
    def __init__(self, valid=(), login_result=NEW, login_exc=None, delay=0.0):
        self.valid = set(valid)
        self.login_result, self.login_exc = login_result, login_exc
        self.logins = 0
        self.fetches = []  # (did, token)
        self.active = self.max_active = 0
        self.gates: dict = {}  # did -> Event the fetch waits on before answering
        self.entered: dict = {}
        self.delay = delay

    async def login(self):
        self.logins += 1
        await asyncio.sleep(0)
        if self.login_exc:
            raise self.login_exc
        self.valid.add(self.login_result)
        return self.login_result

    async def fetch_scenes(self, token, fid, did):
        did = str(did)
        self.fetches.append((did, token))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if did in self.entered:
                self.entered[did].set()
            if did in self.gates:
                await self.gates[did].wait()
            if self.delay:
                await asyncio.sleep(self.delay)
            await asyncio.sleep(0)
            if token not in self.valid:
                raise LeproAuthError("HTTP 401")
            return [SCENE_NEW]
        finally:
            self.active -= 1


def make(n=2, **kw):
    rt = LeproRuntime([{"did": 100 + i, "fid": 7} for i in range(n)])
    rt.api, rt.token = FakeApi(**kw), OLD
    return rt


def seed_old(rt):
    for did in rt.devices:
        rt.set_scenes(did, [SCENE_OLD])


async def test_auth_error_relogin_replaces_token_and_catalog():
    rt = make(1)
    seed_old(rt)
    assert await rt.refresh_scenes("100") == 1
    assert rt.api.logins == 1 and rt.token == NEW
    assert rt.api.fetches == [("100", OLD), ("100", NEW)]
    assert list(rt.scenes["100"]) == ["cid-2"] and rt.devices["100"].state.cloud_scenes == {SCENE_NEW.d50: "New"}


@pytest.mark.parametrize("exc", [LeproApiError("boom"), LeproAuthError("login rejected (code 1)")])
async def test_login_failure_keeps_catalog_and_token_without_leaking(exc, caplog):
    caplog.set_level(logging.DEBUG)
    rt = make(1, login_exc=exc)
    seed_old(rt)
    with pytest.raises(LeproApiError) as err:
        await rt.refresh_scenes("100")
    assert OLD not in str(err.value) and NEW not in str(err.value)
    assert rt.token == OLD and list(rt.scenes["100"]) == ["cid-1"]
    assert OLD not in caplog.text and "Bearer" not in caplog.text


async def test_retry_also_unauthorized_preserves_catalog(caplog):
    caplog.set_level(logging.DEBUG)
    rt = make(1, login_result="tok-BAD-synthetic")
    rt.api.valid = set()  # even a freshly issued token is rejected
    rt.api.login_result = "tok-BAD-synthetic"

    async def login():
        rt.api.logins += 1
        return "tok-BAD-synthetic"

    rt.api.login = login
    seed_old(rt)
    with pytest.raises(LeproAuthError) as err:
        await rt.refresh_scenes("100")
    assert rt.api.logins == 1  # exactly one re-login, no loop
    assert "tok-" not in str(err.value) and "tok-" not in caplog.text
    assert list(rt.scenes["100"]) == ["cid-1"]


async def test_concurrent_expired_refreshes_share_one_login():
    rt = make(3)
    res = await asyncio.gather(*(rt.refresh_scenes(d) for d in rt.devices))
    assert res == [1, 1, 1] and rt.api.logins == 1 and rt.token == NEW


async def test_stale_token_failure_after_renewal_does_not_relogin():
    rt = make(2)
    rt.api.gates["101"], rt.api.entered["101"] = asyncio.Event(), asyncio.Event()
    slow = asyncio.create_task(rt.refresh_scenes("101"))  # captured OLD, parked inside fetch
    await rt.api.entered["101"].wait()
    assert await rt.refresh_scenes("100") == 1 and rt.api.logins == 1
    rt.api.gates["101"].set()  # its OLD-token request now fails with 401
    assert await slow == 1
    assert rt.api.logins == 1  # someone already renewed: reuse the new token
    assert rt.api.fetches[-1] == ("101", NEW)


async def test_concurrency_bounded_to_two_but_parallel():
    rt = make(6, valid=(OLD, NEW), delay=0.01)
    results = await rt.refresh_all_scenes()
    assert rt.api.max_active == 2
    assert set(results) == set(rt.devices) and all(v == 1 for v in results.values())


async def test_refresh_all_isolates_failures_and_keeps_old_catalog():
    rt = make(2, login_exc=LeproApiError("down"))
    seed_old(rt)
    results = await rt.refresh_all_scenes()
    assert all(isinstance(v, LeproApiError) for v in results.values())
    assert all(list(rt.scenes[d]) == ["cid-1"] for d in rt.devices)


async def test_same_device_concurrent_refresh_single_flight():
    rt = make(1, valid=(OLD,), delay=0.01)
    res = await asyncio.gather(*(rt.refresh_scenes("100") for _ in range(4)))
    assert res == [1, 1, 1, 1]
    assert len(rt.api.fetches) == 1 and rt.api.logins == 0


async def test_shutdown_cancels_inflight_refresh():
    rt = make(2, valid=(OLD,))
    seed_old(rt)

    class T:
        async def stop(self):
            pass

    rt.transport = T()
    for d in rt.devices:
        rt.api.gates[d], rt.api.entered[d] = asyncio.Event(), asyncio.Event()
    jobs = [asyncio.ensure_future(rt.refresh_scenes(d)) for d in rt.devices]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in rt.api.entered.values())), 1)
    await rt.async_shutdown()
    outcomes = await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), 2)
    assert all(isinstance(o, LeproApiError) for o in outcomes)
    assert rt.api.active == 0 and not rt._tasks
    assert all(list(rt.scenes[d]) == ["cid-1"] for d in rt.devices)
    with pytest.raises(LeproApiError):  # closed runtime refuses new work
        await rt.refresh_scenes("100")
