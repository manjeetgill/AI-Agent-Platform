"""Runnable fictional booking tool server; replace booking rules with your app's transactions.

Run behind HTTPS with APP_VOICE_TOOL_SECRET in a private environment. No public UI.
This example never accesses a real clinic. SQLite is only for the local fixture.
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

SLOTS = {"demo-0900": "Tomorrow at 9 AM", "demo-0930": "Tomorrow at 9:30 AM"}


def create_app(secret, database):
    if len(secret) < 32:
        raise ValueError("Configure a private webhook signing secret")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with sqlite3.connect(database) as conn:
        conn.executescript("""CREATE TABLE IF NOT EXISTS actions (
            app TEXT, owner TEXT, id TEXT, hash TEXT, result TEXT, PRIMARY KEY(app,owner,id));
            CREATE TABLE IF NOT EXISTS appointments (
            app TEXT, owner TEXT, slot TEXT, subject TEXT, patient TEXT, action TEXT,
            PRIMARY KEY(app,owner,slot));""")

    @app.post("/agent-tools")
    async def tool(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16000:
                raise HTTPException(413)
        stamp = request.headers.get("X-Agent-Timestamp", "")
        try:
            fresh = abs(time.time() - int(stamp)) <= 60
        except ValueError:
            fresh = False
        expected = hmac.new(secret.encode(), stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if not fresh or not hmac.compare_digest(request.headers.get("X-Agent-Signature", ""), expected):
            raise HTTPException(401)
        body = json.loads(raw)
        for key in ("app_id", "owner_id", "action_id", "action_hash", "operation"):
            if not isinstance(body.get(key), str) or not body[key]:
                raise HTTPException(422)
        # Scope idempotency and reservations to the signed app and business owner, never caller-supplied IDs.
        identity = (body["app_id"], body["owner_id"], body["action_id"])
        with sqlite3.connect(database, timeout=3) as conn:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT hash,result FROM actions WHERE app=? AND owner=? AND id=?", identity
            ).fetchone()
            if prior:
                if prior[0] != body["action_hash"]:
                    raise HTTPException(409, "Changed idempotent action")
                return json.loads(prior[1])
            result = {"action_id": body["action_id"], "action_hash": body["action_hash"]}
            if body["operation"] == "reconcile":
                # Tombstone prevents a delayed execute from applying after this definite not-applied response.
                result.update(
                    outcome="not_applied",
                    fenced=True,
                    text="This action was not applied. A new confirmed request is required.",
                )
            elif body["operation"] != "execute":
                raise HTTPException(422)
            elif body["tool"] == "booking.slots":
                result.update(
                    outcome="applied",
                    text="Available demo slots: "
                    + "; ".join(
                        f"{k}: {v}"
                        for k, v in SLOTS.items()
                        if not conn.execute(
                            "SELECT 1 FROM appointments WHERE app=? AND owner=? AND slot=?",
                            identity[:2] + (k,),
                        ).fetchone()
                    ),
                )
            elif body["tool"] == "booking.create":
                # This demo accepts only an explicitly app-attested demo subject. Real apps must check
                # their own customer/clinic authorization here, even though the platform already checked grants.
                args = body.get("arguments", {})
                if (
                    body.get("subject") != "demo-patient"
                    or body.get("caller_identity") != "app_verified"
                    or not body.get("approval", {}).get("confirmed_turn")
                ):
                    result.update(
                        outcome="not_applied",
                        fenced=True,
                        text="Please verify your identity in the business app.",
                    )
                elif (
                    args.get("slot_id") not in SLOTS
                    or not isinstance(args.get("patient_name"), str)
                    or set(args) != {"slot_id", "patient_name"}
                ):
                    result.update(
                        outcome="not_applied",
                        fenced=True,
                        text="Please choose an available demo slot and provide the patient name.",
                    )
                else:
                    try:
                        conn.execute(
                            "INSERT INTO appointments VALUES(?,?,?,?,?,?)",
                            identity[:2]
                            + (args["slot_id"], body["subject"], args["patient_name"], body["action_id"]),
                        )
                        result.update(
                            outcome="applied",
                            text=f"Demo appointment confirmed for {args['patient_name']}, {SLOTS[args['slot_id']]}. Reference {body['action_id']}.",
                        )
                    except sqlite3.IntegrityError:
                        result.update(
                            outcome="not_applied",
                            fenced=True,
                            text="That slot is no longer available. Please choose another slot.",
                        )
            else:
                result.update(outcome="not_applied", fenced=True, text="Tool is not allowed.")
            conn.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?)", identity + (body["action_hash"], json.dumps(result))
            )
        return result

    return app


if __name__ == "__main__":
    import uvicorn

    location = Path(".runtime/telephony-booking-demo.sqlite3")
    location.parent.mkdir(exist_ok=True, mode=0o700)
    uvicorn.run(create_app(os.environ["APP_VOICE_TOOL_SECRET"], location), host="127.0.0.1", port=4075)
