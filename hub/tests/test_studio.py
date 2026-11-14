"""The DGX model picked in the HUD is reloaded when Studio serves something else (llm/studio.py)."""

from jarvis_hub.llm.studio import StudioLoader, mismatch

LOAD = {"model_path": "unsloth/Nemo-GGUF", "gguf_variant": "UD-Q4_K_XL", "llama_extra_args": ["-md", "/d.gguf"]}
OK = {"serving": ["unsloth/Nemo-GGUF"], "active_model": "unsloth/Nemo-GGUF", "gguf_variant": "UD-Q4_K_XL",
      "requested_llama_extra_args": ["-md", "/d.gguf"]}


def test_only_the_model_as_configured_counts_as_loaded():
    assert mismatch(OK, "unsloth/Nemo-GGUF", LOAD) == ""
    assert "serving" in mismatch({"serving": []}, "unsloth/Nemo-GGUF", LOAD)  # Studio restarted: nothing loaded
    assert "drafter" in mismatch({**OK, "requested_llama_extra_args": None}, "unsloth/Nemo-GGUF", LOAD)  # reloaded by hand
    assert "quant" in mismatch({**OK, "gguf_variant": "UD-Q8_K_XL"}, "unsloth/Nemo-GGUF", LOAD)
    lora = {"model_path": "/out/lfm3-sft", "is_lora": True}
    assert mismatch({"serving": ["lfm3-sft"], "requested_llama_extra_args": None}, "lfm3-sft", lora) == ""
    # a LoRA: the status says "ref:...", the loaded-models list has its name
    assert mismatch({"active_model": "ref:ac6e", "serving": ["ref:ac6e"]}, "lfm3-sft", lora, ["lfm3-sft"]) == ""


def test_only_entries_with_load_settings_are_managed():
    s = StudioLoader({"nemo": {"base_url": "http://x/v1", "load": LOAD}, "groq": {"base_url": "http://g/v1"}},
                     lambda: "nemo")
    assert s.handles("nemo") and not s.handles("groq")
