"""Shared test fixtures: a throw-away store seeded from seed/initial.json, a frozen clock, and a contract-checking client."""
import os
import re
import tempfile
import datetime as dt
import yaml
import jsonschema
from starlette.testclient import TestClient
from sched import store as storemod, api

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEED = os.path.join(ROOT, "seed", "initial.json")
SPEC = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml"), encoding="utf-8"))
SYSTEM = {"token": "-", "name": "test", "role": "admin"}


def fake_clock(start="2026-10-19T12:00:00+02:00"):
    t = [dt.datetime.fromisoformat(start)]

    def now():
        t[0] += dt.timedelta(seconds=1)
        return t[0].isoformat(timespec="seconds")
    return now


def make_store(seed=True):
    d = tempfile.mkdtemp(prefix="sched-test-")
    st = storemod.Store(d, clock=fake_clock())
    if seed:
        st.seed(SEED, SYSTEM)
    return st


class Env:
    def __init__(self, seed=True, check_contract=True):
        self.store = make_store(seed)
        self.app = api.build(self.store, background=False)
        self.http = TestClient(self.app)
        self.tokens = {}
        for role in ("admin", "household", "readonly"):
            _id, secret = self.store.create_token(SYSTEM, "t-" + role, role)
            self.tokens[role] = secret
        self.check_contract = check_contract

    def req(self, method, path, role="admin", token=None, **kw):
        headers = dict(kw.pop("headers", {}))
        tok = token if token is not None else self.tokens.get(role)
        if tok:
            headers["Authorization"] = "Bearer " + tok
        r = self.http.request(method, path, headers=headers, **kw)
        if self.check_contract:
            validate_response(method, path.split("?")[0], r)
        return r


def _path_regex(template):
    return re.compile("^" + re.sub(r"\{[^/]+\}", "[^/]+", template) + "$")


def validate_response(method, path, resp):
    """Every response of the running service must match openapi.yaml: status code listed, body valid against its schema."""
    op = None
    for tmpl, item in SPEC["paths"].items():
        if _path_regex(tmpl).match(path) and method.lower() in item:
            op = item[method.lower()]
            break
    assert op, "no operation in openapi.yaml for %s %s" % (method, path)
    codes = op["responses"]
    code = str(resp.status_code)
    assert code in codes, "%s %s -> %s is not documented in openapi.yaml (documented: %s)" % (method, path, code, sorted(codes))
    spec = codes[code]
    if "$ref" in spec:
        spec = SPEC["components"]["responses"][spec["$ref"].split("/")[-1]]
    content = spec.get("content", {}).get("application/json")
    if content is None:
        assert not resp.content, "%s %s -> %s should have no body" % (method, path, code)
        return
    schema = dict(content["schema"])
    schema["components"] = SPEC["components"]
    jsonschema.Draft202012Validator(schema).validate(resp.json())
    for h in spec.get("headers", {}):
        assert h in resp.headers, "%s %s -> %s lacks header %s" % (method, path, code, h)
