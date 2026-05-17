from __future__ import annotations

import io
import json
import os
import urllib.request
import uuid
import zipfile

import pytest

API = os.environ.get("ORCASLICER_API", "http://localhost:8070")


def _container_reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{API}/health", timeout=2.0) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _container_reachable(),
    reason=f"orcaslicer-headless not reachable at {API}",
)


ASCII_STL = b"""solid tri
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 20 0 0
  vertex 0 20 0
 endloop
endfacet
endsolid tri
"""


def _post_multipart(
    url: str,
    fields: dict[str, str],
    file_name: str,
    file_bytes: bytes,
) -> dict:
    boundary = f"----pytest{uuid.uuid4().hex}"
    body = io.BytesIO()
    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        )
        body.write(str(value).encode())
        body.write(b"\r\n")
    body.write(f"--{boundary}\r\n".encode())
    body.write(
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        .encode()
    )
    body.write(b"Content-Type: application/sla\r\n\r\n")
    body.write(file_bytes)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url,
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120.0) as r:
        return json.loads(r.read().decode())


def _get_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60.0) as r:
        return r.read()


def test_stl_import_layout_and_materialize_3mf() -> None:
    scene = _post_multipart(
        f"{API}/stl/import",
        {
            "machine_id": "GM020",
            "process_id": "GP000",
            "center": "true",
            "arrange": "true",
            "auto_orient": "false",
        },
        "tri.stl",
        ASCII_STL,
    )
    assert scene["draft_token"]
    assert scene["source_filename"] == "tri.stl"
    assert scene["objects"][0]["printable"] is True

    rotated = _post_json(
        f"{API}/stl/{scene['draft_token']}/layout",
        {"action": "rotate_z_90"},
    )
    assert rotated["draft_token"] == scene["draft_token"]

    arranged = _post_json(
        f"{API}/stl/{scene['draft_token']}/layout",
        {"action": "arrange"},
    )
    assert arranged["draft_token"] == scene["draft_token"]

    materialized = _post_json(f"{API}/stl/{scene['draft_token']}/3mf")
    assert materialized["input_token"]

    threemf = _get_bytes(f"{API}/3mf/{materialized['input_token']}")
    with zipfile.ZipFile(io.BytesIO(threemf)) as zf:
        assert "3D/3dmodel.model" in zf.namelist()
