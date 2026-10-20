"""Event hub for Server-Sent Events: a ring buffer (resume after a reconnect) and one asyncio queue per open stream."""
import asyncio
import collections
import json
import time
from . import config


class Hub:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.seq = 0
        self.ring = collections.deque()          # (id, ts, type, data)
        self.subs = {}                           # sub id -> (principal key, queue)
        self._n = 0

    def publish(self, etype, data):
        self.seq += 1
        ev = (self.seq, self.clock(), etype, data)
        self.ring.append(ev)
        cutoff = self.clock() - config.SSE_RESUME_S
        while self.ring and self.ring[0][1] < cutoff:
            self.ring.popleft()
        for _k, q in list(self.subs.values()):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:                       # a stuck consumer is dropped, it reconnects and resumes
                q.put_nowait(None)
        return self.seq

    def since(self, last_id):
        return [e for e in self.ring if e[0] > last_id]

    def subscribe(self, key):
        self._n += 1
        q = asyncio.Queue(maxsize=200)
        self.subs[self._n] = (key, q)
        return self._n, q

    def unsubscribe(self, sid):
        self.subs.pop(sid, None)

    def kick(self, phone_id):
        """A revoked phone: close its open streams (the stream also re-checks the token every few seconds)."""
        for k, q in list(self.subs.values()):
            if k == phone_id:
                try:
                    q.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    @staticmethod
    def format(ev):
        eid, _ts, etype, data = ev
        return "id: %d\nevent: %s\ndata: %s\n\n" % (eid, etype, json.dumps(data, ensure_ascii=False, separators=(",", ":")))
