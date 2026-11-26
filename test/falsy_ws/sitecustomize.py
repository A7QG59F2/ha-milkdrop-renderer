# Loaded (via PYTHONPATH) by `FALSY_WS=1 python3 test/test_builtin_player.py`: makes an aiohttp
# WebSocketResponse count as "false" in an `if ws:` test, as it does in older aiohttp releases (like
# the one in the Home Assistant image). The renderer must test `is not None`, never truthiness.
import aiohttp.web_ws

aiohttp.web_ws.WebSocketResponse.__bool__ = lambda self: False
