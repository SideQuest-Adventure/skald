"""Hardware auto-detect: GPU classification, the CPU model ladder, and the
engine recommendation. Pure functions - no real GPU probe, mic, or model needed."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import skald as sk  # noqa: E402


# ------------------------------------------------------------ classify_gpu
def test_discrete_nvidia():
    assert sk.classify_gpu("NVIDIA GeForce RTX 4070") == "nvidia"
    assert sk.classify_gpu("NVIDIA GeForce GTX 1060 6GB") == "nvidia"
    assert sk.classify_gpu("Quadro P2000") == "nvidia"


def test_discrete_amd():
    assert sk.classify_gpu("AMD Radeon RX 7900 GRE") == "amd"
    assert sk.classify_gpu("Radeon RX 580 Series") == "amd"


def test_discrete_intel_arc():
    assert sk.classify_gpu("Intel Arc A770 Graphics") == "intel-arc"


def test_apple_silicon():
    assert sk.classify_gpu("Apple M2 Pro") == "apple"


def test_igpus_never_masquerade_as_discrete():
    # The APU trap: an 'AMD Radeon(TM) Graphics' iGPU contains both 'amd' and 'radeon'
    assert sk.classify_gpu("AMD Radeon(TM) Graphics") == "igpu"
    assert sk.classify_gpu("Intel(R) UHD Graphics 630") == "igpu"
    assert sk.classify_gpu("Intel(R) Iris Xe Graphics") == "igpu"
    assert sk.classify_gpu("AMD Radeon(TM) Vega 8 Graphics") == "igpu"
    assert sk.classify_gpu("Microsoft Basic Display Adapter") == "igpu"


# ------------------------------------------------------------ pick_cpu_model
def test_cpu_ladder():
    assert sk.pick_cpu_model(16) == "small.en"
    assert sk.pick_cpu_model(8) == "small.en"
    assert sk.pick_cpu_model(6) == "base.en"
    assert sk.pick_cpu_model(4) == "base.en"
    assert sk.pick_cpu_model(2) == "tiny.en"


# ------------------------------------------------------------ recommend_engine
def _hw(gpu_class, name="TestGPU", cores=8):
    return {"gpus": [(name, gpu_class)], "best_gpu": name,
            "gpu_class": gpu_class, "cpu_cores": cores}


def test_vulkan_capable_gpu_with_server_ready():
    verdict, reason = sk.recommend_engine(_hw("amd", "AMD Radeon RX 7900 GRE"), True)
    assert verdict == "gpu-server"
    assert "RX 7900" in reason


def test_vulkan_capable_gpu_without_server_points_at_the_download():
    for cls in ("nvidia", "amd", "intel-arc"):
        verdict, reason = sk.recommend_engine(_hw(cls), False)
        assert verdict == "gpu-available"
        assert "one download" in reason


def test_igpu_and_no_gpu_get_the_cpu_ladder():
    for cls in ("igpu", "none", "unknown", "apple"):
        verdict, reason = sk.recommend_engine(_hw(cls, cores=4), False)
        assert verdict == "cpu"
        assert "base.en" in reason


# ------------------------------------------------------------ auto model resolution
def test_resolve_auto_models_uses_the_ladder(monkeypatch):
    monkeypatch.setitem(sk.CONFIG, "MODEL_SIZE", "auto")
    monkeypatch.setitem(sk.CONFIG, "LIVE_MODEL", "auto")
    sk.resolve_auto_models({"gpus": [], "best_gpu": "", "gpu_class": "none",
                            "cpu_cores": 4})
    assert sk.CONFIG["MODEL_SIZE"] == "base.en"
    assert sk.CONFIG["LIVE_MODEL"] == "base.en"


def test_resolve_auto_models_respects_explicit_names(monkeypatch):
    monkeypatch.setitem(sk.CONFIG, "MODEL_SIZE", "medium.en")
    monkeypatch.setitem(sk.CONFIG, "LIVE_MODEL", "tiny.en")
    sk.resolve_auto_models({"gpus": [], "best_gpu": "", "gpu_class": "none",
                            "cpu_cores": 16})
    assert sk.CONFIG["MODEL_SIZE"] == "medium.en"
    assert sk.CONFIG["LIVE_MODEL"] == "tiny.en"


# ------------------------------------------------------------ the horn still renders
def test_horn_is_finite_normalized_audio():
    for kind in ("start", "stop"):
        w = sk._horn(kind)
        assert w.dtype.name == "float32" and len(w) > 4000
        assert float(abs(w).max()) <= 1.0
        # soft swell: the first 50ms must stay well below full amplitude
        assert float(abs(w[:2205]).max()) < 0.6
