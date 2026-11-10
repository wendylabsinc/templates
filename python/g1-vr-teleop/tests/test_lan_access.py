"""A fresh headset connects automatically; transport and origin checks remain."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from aiohttp import web
sys.path.insert(0, str(Path(__file__).parents[1] / 'teleop' / 'bridge'))
from lan_access import lan_access


def request(remote='192.168.0.50', secure=True, method='GET', path='/teleop', origin=None):
    return SimpleNamespace(app={'lan_https_required': True, 'lan_access_token': 'obsolete-secret'},
                           remote=remote, secure=secure, method=method, path=path, cookies={},
                           headers={'Origin': origin} if origin else {}, host='mac.local:8443')


def call(req):
    async def handler(req): return web.Response(text='handled')
    return asyncio.run(lan_access(req, handler))


@pytest.mark.parametrize('remote', ['192.168.0.50', '10.10.20.63', '172.21.66.26'])
@pytest.mark.parametrize('path', ['/', '/teleop', '/control/status', '/camera/frame.jpg'])
def test_fresh_headset_connects_without_code_cookie_or_ip_allowlist(remote, path):
    assert call(request(remote=remote, path=path)).status == 200


def test_automatic_connection_keeps_tls_and_same_origin_checks():
    assert call(request(secure=False)).status == 403
    assert call(request(origin='https://evil.invalid')).status == 403
    assert call(request(method='POST')).status == 403
    assert call(request(method='POST', origin='https://mac.local:8443')).status == 200


def test_local_usb_headset_and_observer_need_no_pairing_or_tls():
    for path in ['/teleop', '/control/status']:
        assert call(request(remote='127.0.0.1', secure=False, path=path)).status == 200
    req=request(secure=False);req.app={}
    assert call(req).status == 200


def test_fresh_https_websocket_connects_without_external_services(tmp_path):
    import ssl
    from aiohttp import ClientSession, TCPConnector, WSServerHandshakeError
    import shutil
    import subprocess
    if not shutil.which('openssl'):
        pytest.skip('openssl unavailable')
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(tmp_path / 'key.pem'), '-out', str(tmp_path / 'cert.pem'),
                    '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   capture_output=True, check=True)

    async def exercise():
        @web.middleware
        async def external_client(req, handler):
            return await lan_access(req.clone(remote='192.168.0.55'), handler)
        async def echo(req):
            ws=web.WebSocketResponse();await ws.prepare(req)
            async for msg in ws:
                await ws.send_str(msg.data)
            return ws
        app=web.Application(middlewares=[external_client])
        app['lan_https_required']=True
        app['lan_access_token']='obsolete-secret'
        app.router.add_get('/teleop',echo)
        server_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(tmp_path/'cert.pem',tmp_path/'key.pem')
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0,ssl_context=server_context);await site.start()
        port=site._server.sockets[0].getsockname()[1];origin=f'https://127.0.0.1:{port}'
        context=ssl.create_default_context(cafile=str(tmp_path/'cert.pem'))
        try:
            # Each new session has no saved cookie or previous pairing.
            for _ in range(2):
                async with ClientSession(connector=TCPConnector(ssl=context)) as client:
                    async with client.ws_connect(origin+'/teleop',origin=origin) as ws:
                        await ws.send_str('transport-only-test')
                        assert (await ws.receive()).data=='transport-only-test'
                    with pytest.raises(WSServerHandshakeError) as error:
                        await client.ws_connect(origin+'/teleop',origin='https://evil.invalid')
                    assert error.value.status==403
        finally:
            await runner.cleanup()
    asyncio.run(exercise())
