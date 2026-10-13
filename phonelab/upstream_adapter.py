"""Thin async boundary for the pinned upstream Phone; no planner/model loop is started."""
import asyncio
import time
import uuid
from pathlib import Path


class AdapterError(RuntimeError):
    pass


class GuardedDevice:
    def __init__(self, controller, owner):
        self.controller, self.owner = controller, owner
        self._generation = controller.session
        self._links = 1

    @property
    def links(self):
        if self._generation != self.controller.session:
            self._generation = self.controller.session
            self._links += 1
        return self._links

    @property
    def connected(self):
        return bool(self.controller.backend and self.controller.armed and self.controller.owner == self.owner)

    async def wait_connected(self, timeout):
        end = time.monotonic() + timeout
        while not self.connected and time.monotonic() < end: await asyncio.sleep(.1)
        return self.connected

    async def send(self, command):
        if not isinstance(command, str) or any(ord(c) < 32 or ord(c) > 126 for c in command):
            raise AdapterError('invalid command; only one printable ASCII operation')
        operation, separator, payload = command.partition(' ')
        args = {}
        try:
            if operation == 'move':
                x, y = payload.split(' ')
                args = {'dx':int(x), 'dy':int(y)}
            elif operation == 'scroll':
                v, h = payload.split(' ')
                args = {'v':int(v), 'h':int(h)}
            elif operation == 'type': args = {'text':payload}
            elif operation == 'key': args = {'key':payload}
            elif operation in ('home','press','release','click','ping') and not separator: pass
            else: raise ValueError('unsupported upstream operation')
            request = {'id':uuid.uuid4().hex, 'session':self.controller.session, 'owner':self.owner,
                       'deadline':time.monotonic()+4.5, 'op':operation, 'args':args}
            result = await asyncio.to_thread(self.controller.submit, request)
        except Exception as exc:
            raise AdapterError('upstream command rejected or failed; no automatic replay') from None
        if result.get('state') != 'executed':
            raise AdapterError('input unconfirmed or rejected; no automatic replay')

    async def keep_awake(self, idle=25, heartbeat=.5, screen=None):
        # Preserve upstream heartbeat interface while excluding unsolicited pointer nudges.
        while True:
            await asyncio.sleep(heartbeat)
            if self.connected:
                try: await self.send('ping')
                except AdapterError: return


def make_upstream_phone(controller, owner, private_directory, screen=None):
    import sys
    package = Path(__file__).resolve().parents[1] / 'upstream/esp32-iphone-agent/server'
    if str(package) not in sys.path: sys.path.insert(0, str(package))
    from hidagent.phone import Phone
    root = Path(private_directory)
    root.mkdir(parents=True, exist_ok=True)
    # Never silently reuse a previous connection's USB/BLE timing/calibration.
    calibration = root / ('pointer-calibration-' + controller.session + '.json')
    return Phone(GuardedDevice(controller, owner), calibration, screen)
