import httpx

from app.main import create_app


async def test_health_ok(settings, container):
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")
    async with app.router.lifespan_context(app), client as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["checks"]["storage"] == "ok"
