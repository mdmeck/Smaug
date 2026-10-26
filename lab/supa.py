"""Minimal Supabase REST access for the runner (service-role key, so RLS is
bypassed and every insert must carry user_id — see CLAUDE.md)."""
import json
import os

import requests

USER_ID = os.environ.get("LAB_USER_ID", "c0b48756-5f94-4862-886a-8ecdb7099ef6")


def _base():
    return os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/"


def _headers(extra=None):
    k = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    h = {"apikey": k, "Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _check(r):
    # requests' raise_for_status drops the body, which is where PostgREST
    # says what actually went wrong
    if r.status_code >= 400:
        raise RuntimeError(f"supabase {r.request.method} {r.url} -> {r.status_code}: {r.text[:500]}")
    return r.json() if r.content else None


def select(table, **params):
    params.setdefault("user_id", f"eq.{USER_ID}")
    return _check(requests.get(_base() + table, headers=_headers(), params=params, timeout=30))


def insert(table, row, upsert_on=None, ignore_duplicates=False):
    row = {"user_id": USER_ID, **row}
    h = {"Prefer": "return=representation"}
    params = {}
    if upsert_on:
        h["Prefer"] += ",resolution=" + ("ignore-duplicates" if ignore_duplicates else "merge-duplicates")
        params["on_conflict"] = upsert_on
    r = requests.post(_base() + table, headers=_headers(h), params=params,
                      data=json.dumps(row, default=str), timeout=30)
    out = _check(r)
    return out[0] if out else None


def update(table, match, patch):
    params = {k: f"eq.{v}" for k, v in match.items()}
    params["user_id"] = f"eq.{USER_ID}"
    r = requests.patch(_base() + table, headers=_headers({"Prefer": "return=representation"}),
                       params=params, data=json.dumps(patch, default=str), timeout=30)
    return _check(r)
