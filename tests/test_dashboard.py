import httpx

from epcube_bridge.app import create_app
from epcube_bridge.bridge import Runtime


async def test_public_shell_never_exposes_data_or_tokens(settings, snapshot):
    app = create_app(settings, runtime=Runtime(snapshot=snapshot), start_workers=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        page = await client.get("/")
        assert page.status_code == 200
        assert 'id="login-form"' in page.text
        assert settings.api_token.get_secret_value() not in page.text
        assert settings.epcube_password.get_secret_value() not in page.text
        assert snapshot.model_dump_json() not in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        for asset in ("dashboard.css", "dashboard.js", "format.mjs", "history.mjs"):
            response = await client.get("/assets/" + asset)
            assert response.status_code == 200
            assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get("/devices")).status_code == 401
        assert (await client.get("/assets/token.json")).status_code == 404
        assert (await client.get("/assets/..%2Fstorage.py")).status_code == 404


async def test_dashboard_auth_opt_out_does_not_disable_rest_auth(settings, snapshot):
    for disabled in (False, True):
        configured = settings.model_copy(update={"web_disable_auth": disabled})
        app = create_app(
            configured,
            runtime=Runtime(device={"id": snapshot.device_id}, snapshot=snapshot),
            start_workers=False,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            page = await client.get("/")
            assert f'data-auth-required="{str(not disabled).lower()}"' in page.text
            for path in ("/devices", f"/devices/{snapshot.device_id}/state"):
                assert (await client.get(path)).status_code == 401
                web_path = "/web" + path
                response = await client.get(web_path)
                assert response.status_code == (200 if disabled else 401)
                authenticated = await client.get(
                    web_path,
                    headers={"Authorization": "Bearer " + settings.api_token.get_secret_value()},
                )
                assert authenticated.status_code == 200
                assert (await client.post(web_path)).status_code == 405
