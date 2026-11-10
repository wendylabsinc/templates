"""Automatic headset admission with HTTPS/origin checks; no motion decisions."""
import ipaddress
from aiohttp import web


@web.middleware
async def lan_access(request, handler):
    if not request.app.get('lan_https_required'):
        return await handler(request)
    try:
        remote = ipaddress.ip_address(request.remote or '')
        local = remote.is_loopback
    except ValueError:
        local = False
    if local:
        # ADB reverse exposes localhost to the Quest as a secure WebXR origin.
        return await handler(request)
    if not request.secure:
        return web.Response(status=403, text='Use the HTTPS headset address.')
    origin = request.headers.get('Origin')
    expected_origin = 'https://' + request.host
    if (origin and origin != expected_origin) or (request.method not in ('GET', 'HEAD') and origin != expected_origin):
        return web.Response(status=403, text='Same-origin access required.')
    return await handler(request)
