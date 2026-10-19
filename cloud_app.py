from __future__ import annotations

import asyncio
import hmac
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

import os

APP_DIR = Path(__file__).resolve().parent
SCRIPT = APP_DIR / "helios_backtest.py"
TOKEN = os.environ.get("HELIOS_API_TOKEN", "").strip()
MIN_TOKEN_LEN = 16               # shorter tokens are treated as "not configured"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
RUN_TIMEOUT_S = 900
VERSION = "helios-v0.9-int-flower-fill"
_TARGET_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Free-tier hosts are small: allow one analysis at a time, reject the rest fast.
_RUN_SLOT = asyncio.Semaphore(1)

# Interactive docs/schema are disabled to keep the public surface minimal.
app = FastAPI(title="DIAMA HelioS Back-test Runner", version=VERSION,
              docs_url=None, redoc_url=None, openapi_url=None)


def _auth_configured() -> bool:
    return len(TOKEN) >= MIN_TOKEN_LEN


def require_auth(authorization: Optional[str]) -> None:
    """Constant-time Bearer check. Never echoes the supplied or expected token."""
    supplied = ""
    if authorization and authorization.startswith("Bearer "):
        supplied = authorization[len("Bearer "):]
    if not _auth_configured() or not hmac.compare_digest(supplied.encode("utf-8"), TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="A valid Bearer token is required.",
                            headers={"WWW-Authenticate": "Bearer"})


async def save_upload(upload: UploadFile, path: Path) -> None:
    data = await upload.read(MAX_FILE_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit.")
    path.write_bytes(data)


def run_command(cmd: list[str], timeout: int = RUN_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(APP_DIR), capture_output=True, text=True, timeout=timeout, check=False)


def _clean(text: str, work: Path, limit: int) -> str:
    """Trim subprocess output and hide the temporary directory path."""
    return (text or "").replace(str(work), "<work>")[-limit:]


async def _run_in_thread(cmd: list[str], work: Path) -> subprocess.CompletedProcess[str]:
    try:
        return await asyncio.to_thread(run_command, cmd)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"Analysis exceeded the {RUN_TIMEOUT_S}s limit; no result returned.")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ready",
        "service": "DIAMA HelioS Back-test Runner",
        "model_version": VERSION,
        "purpose": "Execute the versioned back-test on user-supplied, source-documented data.",
        "scientific_status": "No evidence of model validity is implied by service availability.",
        "auth_configured": _auth_configured(),
        "authenticated_endpoints": ["POST /build-panel", "POST /run"],
    }


@app.post("/build-panel")
async def build_panel(
    seasons: UploadFile = File(...),
    daily: UploadFile = File(...),
    allow_overlapping_phases: bool = Form(False),
    authorization: Optional[str] = Header(None),
):
    require_auth(authorization)
    if _RUN_SLOT.locked():
        raise HTTPException(status_code=429, detail="Another job is running; retry shortly.", headers={"Retry-After": "30"})
    async with _RUN_SLOT:
        work = Path(tempfile.mkdtemp(prefix="helios-build-"))
        try:
            seasons_path, daily_path = work / "seasons.csv", work / "district_daily.csv"
            await save_upload(seasons, seasons_path)
            await save_upload(daily, daily_path)
            out_stem = work / "phase_features"
            cmd = [sys.executable, str(SCRIPT), "build-panel", "--seasons", str(seasons_path),
                   "--daily", str(daily_path), "--out", str(out_stem)]
            if allow_overlapping_phases:
                cmd.append("--allow-overlapping-phases")
            result = await _run_in_thread(cmd, work)
            if result.returncode != 0 or not (work / "phase_features.csv").exists():
                raise HTTPException(status_code=422, detail={
                    "error": "Panel construction failed; check the input schema and phase dates.",
                    "stdout": _clean(result.stdout, work, 3000), "stderr": _clean(result.stderr, work, 6000)})
            return FileResponse(str(work / "phase_features.csv"), media_type="text/csv",
                                filename="helios_phase_features.csv",
                                background=BackgroundTask(shutil.rmtree, str(work), ignore_errors=True))
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise


@app.post("/run")
async def run_backtest(
    panel: UploadFile = File(...),
    daily: UploadFile = File(...),
    target: Optional[str] = Form(None),
    interaction_mode: str = Form("auto"),
    run_district_holdout: bool = Form(False),
    allow_overlapping_phases: bool = Form(False),
    source_manifest: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
):
    require_auth(authorization)
    if interaction_mode not in {"auto", "vpd", "soil_moisture"}:
        raise HTTPException(status_code=400, detail="interaction_mode must be auto, vpd, or soil_moisture.")
    if target is not None and not _TARGET_RE.match(target):
        raise HTTPException(status_code=400, detail="target must be a plain column name (letters, digits, underscore).")
    manifest_obj = None
    if source_manifest:
        if len(source_manifest.encode("utf-8")) > MAX_MANIFEST_BYTES:
            raise HTTPException(status_code=413, detail="source_manifest exceeds the 256 KB limit.")
        try:
            manifest_obj = json.loads(source_manifest)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"source_manifest must be valid JSON: {exc.msg}")
    if _RUN_SLOT.locked():
        raise HTTPException(status_code=429, detail="Another job is running; retry shortly.", headers={"Retry-After": "30"})
    async with _RUN_SLOT:
        work = Path(tempfile.mkdtemp(prefix="helios-run-"))
        try:
            panel_path, daily_path = work / "panel.csv", work / "district_daily.csv"
            await save_upload(panel, panel_path)
            await save_upload(daily, daily_path)
            outdir = work / "outputs"
            cmd = [sys.executable, str(SCRIPT), "run", "--panel", str(panel_path), "--daily", str(daily_path),
                   "--out", str(outdir), "--interaction-mode", interaction_mode]
            if target:
                cmd += ["--target", target]
            if allow_overlapping_phases:
                cmd.append("--allow-overlapping-phases")
            if run_district_holdout:
                cmd.append("--run-district-holdout")
            if manifest_obj is not None:
                manifest_path = work / "source_manifest.json"
                manifest_path.write_text(json.dumps(manifest_obj, indent=2), encoding="utf-8")
                cmd += ["--source-manifest", str(manifest_path)]
            result = await _run_in_thread(cmd, work)
            if result.returncode != 0 or not (outdir / "report.md").exists():
                raise HTTPException(status_code=422, detail={
                    "error": "Back-test failed; no scientific result returned.",
                    "stdout": _clean(result.stdout, work, 3000), "stderr": _clean(result.stderr, work, 6000)})
            zip_path = work / "helios_backtest_results.zip"
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for item in outdir.rglob("*"):
                    if item.is_file():
                        zf.write(item, item.relative_to(outdir))
                zf.writestr("execution_log.txt", _clean(result.stdout, work, 20000) + "\n--- STDERR ---\n" + _clean(result.stderr, work, 20000))
            return FileResponse(str(zip_path), media_type="application/zip", filename="helios_backtest_results.zip",
                                background=BackgroundTask(shutil.rmtree, str(work), ignore_errors=True))
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
