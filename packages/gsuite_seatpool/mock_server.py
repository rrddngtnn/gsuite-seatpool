"""Offline FastAPI mock server for registrar DNS endpoints and Google Admin SDK Directory v1."""
from __future__ import annotations

import itertools
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from directory_seats.generator import CLOUD_IDENTITY_FREE_CAP
from dns_zone.client import SUPPORTED_RECORD_TYPES

app = FastAPI(title="gsuite_seatpool mock server", version="0.1.0")

STATE = {
    "dns": {},
    "nameservers": {},
    "directory_users": {},
    "seq": itertools.count(1000),
}

NAME_RE = re.compile(r"^(@|\*|[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*)$")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
HOST_RE = re.compile(r"^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def _next_id() -> int:
    return next(STATE["seq"])


def _err(status: int, code: str, message: str = ""):
    body = {"error": code}
    if message:
        body["message"] = message
    return JSONResponse(body, status_code=status)


def reset() -> None:
    STATE["dns"].clear()
    STATE["nameservers"].clear()
    STATE["directory_users"].clear()
    STATE["seq"] = itertools.count(1000)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not email or len(password) < 8:
        return _err(400, "invalid_credentials", "Invalid email or password")
    resp = JSONResponse({"user": {"id": 1, "email": email}})
    resp.set_cookie("zone_session", "mock-session-token", httponly=True)
    return resp


@app.get("/api/domains/{fqdn}/dns")
def list_dns(fqdn: str):
    records = STATE["dns"].get(fqdn, [])
    ns = STATE["nameservers"].get(fqdn, ["ns1.mock.test", "ns2.mock.test"])
    return {
        "records": records,
        "supported_types": list(SUPPORTED_RECORD_TYPES),
        "required_nameservers": ["ns1.mock.test", "ns2.mock.test"],
        "nameservers": ns,
        "delegated": ns == ["ns1.mock.test", "ns2.mock.test"],
    }


@app.post("/api/domains/{fqdn}/dns")
async def add_dns(fqdn: str, request: Request):
    body = await request.json()
    rtype = (body.get("type") or "").upper()
    if rtype not in SUPPORTED_RECORD_TYPES:
        return _err(400, "unsupported_record_type", f"Unsupported record type: {rtype}")
    val = (body.get("value") or "").strip()
    if not val:
        return _err(400, "empty_record_value", "Record value is required.")
    name = (body.get("name") or "@").strip()
    if not NAME_RE.match(name):
        return _err(400, "invalid_record_name", f"Invalid record name: {name}")
    if rtype == "A" and not IPV4_RE.match(val):
        return _err(400, "invalid_record_value", "Record A requires an IPv4 address.")
    if rtype == "MX" and not HOST_RE.match(val):
        return _err(400, "invalid_record_value", "Record MX requires a valid hostname.")

    rec = {
        "id": _next_id(),
        "domain_name": fqdn,
        "type": rtype,
        "name": name,
        "value": val,
        "ttl": int(body.get("ttl") or 300),
        "priority": body.get("priority"),
        "sub_domain": name,
    }
    STATE["dns"].setdefault(fqdn, []).append(rec)
    return JSONResponse({"record": rec}, status_code=201)


@app.delete("/api/domains/{fqdn}/dns/{record_id}")
def delete_dns(fqdn: str, record_id: int):
    records = STATE["dns"].get(fqdn, [])
    before = len(records)
    STATE["dns"][fqdn] = [r for r in records if r["id"] != record_id]
    if len(STATE["dns"][fqdn]) == before:
        return _err(404, "not_found", "Record not found")
    return {"ok": True}


@app.put("/api/domains/{fqdn}/nameservers")
async def update_nameservers(fqdn: str, request: Request):
    body = await request.json()
    ns = body.get("nameservers")
    if not isinstance(ns, list) or len(ns) < 2:
        return _err(400, "invalid_nameservers", "Minimum 2 nameservers required")
    if any(not HOST_RE.match(str(x)) for x in ns):
        return _err(400, "unknown_nameserver", "Invalid nameserver hostname")
    STATE["nameservers"][fqdn] = [str(x).lower() for x in ns]
    return {"ok": True, "nameservers": STATE["nameservers"][fqdn]}


@app.post("/admin/directory/v1/users")
async def directory_insert_user(request: Request):
    body = await request.json()
    primary = (body.get("primaryEmail") or "").strip().lower()
    password = body.get("password") or ""
    if not primary or "@" not in primary:
        return JSONResponse(
            {"error": {"code": 400, "message": "Invalid primaryEmail"}},
            status_code=400,
        )
    if len(password) < 8:
        return JSONResponse(
            {"error": {"code": 400, "message": "Invalid password length"}},
            status_code=400,
        )
    domain = primary.split("@", 1)[1]
    dom_users = STATE["directory_users"].setdefault(domain, {})
    if primary in dom_users:
        return JSONResponse(
            {"error": {"code": 409, "message": "Entity already exists."}},
            status_code=409,
        )
    if len(dom_users) >= CLOUD_IDENTITY_FREE_CAP:
        return JSONResponse(
            {"error": {"code": 403, "message": "quotaExceeded: Cloud Identity Free 50 user limit reached"}},
            status_code=403,
        )
    user_obj = {
        "id": str(_next_id()),
        "primaryEmail": primary,
        "name": body.get("name", {}),
        "orgUnitPath": body.get("orgUnitPath", "/"),
        "changePasswordAtNextLogin": bool(body.get("changePasswordAtNextLogin", False)),
        "suspended": False,
    }
    dom_users[primary] = user_obj
    return JSONResponse(user_obj, status_code=201)


@app.get("/admin/directory/v1/users")
def directory_list_users(domain: str):
    dom_users = STATE["directory_users"].get(domain.strip().lower(), {})
    return {"users": list(dom_users.values())}


@app.delete("/admin/directory/v1/users/{user_key}")
def directory_delete_user(user_key: str):
    key = user_key.strip().lower()
    domain = key.split("@", 1)[1] if "@" in key else ""
    dom_users = STATE["directory_users"].get(domain, {})
    if key not in dom_users:
        return JSONResponse({"error": {"code": 404, "message": "Resource Not Found: userKey"}}, status_code=404)
    del dom_users[key]
    return JSONResponse({"deleted": key}, status_code=200)
