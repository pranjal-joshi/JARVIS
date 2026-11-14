"""Keeps the selected Unsloth Studio model loaded the way it should be. Studio (the DGX) serves one model at a
time and forgets it when it restarts; a model picked in the HUD's dropdown must then be loaded again, with its own
settings (a GGUF quant, a LoRA, a DFlash drafter passed as llama.cpp arguments).

An LLM_ENDPOINTS entry opts in with a "load" object, the body of Studio's POST /api/inference/load:

    "unsloth-nemotron-bolt": {"base_url": "http://10.3.7.10:8888/v1", "model": "unsloth/NVIDIA-...-GGUF",
        "key_env": "UNSLOTH_API_KEY",
        "load": {"model_path": "unsloth/NVIDIA-...-GGUF", "gguf_variant": "UD-Q4_K_XL",
                 "llama_extra_args": ["-md", "/path/drafter.gguf", "--spec-type", "draft-dflash"]}}

It is checked when the model is picked, every minute while it stays picked (the DGX may have been off or the
VPN down), and whenever a request finds it not loaded. It loads only when what Studio serves differs: another
model, another quant, or other llama.cpp arguments (a manual reload from Studio's list drops the drafter)."""

import asyncio
import logging

import httpx

from jarvis_hub.llm.client import _env_key

log = logging.getLogger(__name__)
CHECK_EVERY_S = 60
LOAD_TIMEOUT_S = 900  # a LoRA on transformers takes ~45 s; a first GGUF load can take minutes


def mismatch(status: dict, model: str, load: dict, loaded: list[str] = ()) -> str:
    """Why what Studio serves isn't this model as configured ("" when it is). loaded: the ids
    /v1/models/loaded lists (a LoRA shows there by name, in the status only as "ref:...")."""
    if model not in [*(status.get("serving") or []), *(status.get("loaded") or []), *loaded]             and status.get("active_model") != model:
        return f"serving {status.get('active_model') or 'nothing'}"
    if load.get("gguf_variant") and status.get("gguf_variant") != load["gguf_variant"]:
        return f"quant {status.get('gguf_variant')}"
    if (status.get("requested_llama_extra_args") or []) != (load.get("llama_extra_args") or []):
        return "different llama.cpp arguments (drafter)"
    return ""


class StudioLoader:
    def __init__(self, endpoints: dict, current) -> None:
        """endpoints: LLM_ENDPOINTS; current: returns the picker name in use now."""
        self.entries = {name: ep for name, ep in endpoints.items() if isinstance(ep.get("load"), dict)}
        self.current = current
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self.last: dict[str, str] = {}  # picker name -> what the last check found, for the log

    def handles(self, name: str) -> bool:
        return name in self.entries

    async def ensure(self, name: str) -> str:
        """Load `name` on its Studio if it isn't served as configured. Returns what happened."""
        ep = self.entries.get(name)
        if not ep:
            return "not a Studio model"
        root = ep["base_url"].rstrip("/").removesuffix("/v1")
        headers = {"Authorization": f"Bearer {_env_key(ep.get('key_env') or '')}"}
        async with self._lock:
            try:
                async with httpx.AsyncClient(base_url=root, headers=headers,
                                             timeout=httpx.Timeout(LOAD_TIMEOUT_S, connect=5)) as c:
                    r = await c.get("/api/inference/status", timeout=10)
                    r.raise_for_status()
                    ids = await c.get("/api/inference/loaded-models", timeout=10)
                    ids.raise_for_status()
                    why = mismatch(r.json(), ep.get("model") or name, ep["load"],
                                   [m.get("id", "") for m in ids.json().get("data") or []])
                    if not why:
                        return "loaded"
                    log.info("Studio %s: %s; loading %s", root, why, name)
                    r = await c.post("/api/inference/load", json={**ep["load"], "force_cancel_active": True})
                    if r.status_code != 200:
                        raise httpx.HTTPStatusError(r.text[:200], request=r.request, response=r)
                    log.info("Studio %s: loaded %s", root, name)
                    return "reloaded"
            except httpx.HTTPError as e:  # the DGX is off or the VPN is down: the next check tries again
                out = f"unreachable ({type(e).__name__})"
                if self.last.get(name) != out:
                    log.warning("Studio model %s not loaded: %s %s", name, out, str(e)[:160])
                return out

    async def _loop(self) -> None:
        while True:
            name = self.current()
            if self.handles(name):
                self.last[name] = await self.ensure(name)
            await asyncio.sleep(CHECK_EVERY_S)

    def start(self) -> None:
        if self.entries:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
