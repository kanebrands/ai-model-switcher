"""Conservative RAM/VRAM planning for optional mixed-memory inference."""
import math


GIB = 2**30
WORKING_BYTES = 4 * GIB


def memory_plan(hardware, weight_bytes, model_format="gguf", enabled=True):
    """Estimate placement; loading and throughput must still be tested on the host."""
    if not isinstance(weight_bytes, int) or weight_bytes <= 0:
        raise ValueError("Unknown model weight size")
    ram = max(0, float(hardware.get("ram_gib") or 0)) * GIB
    vram = max(0, int(hardware.get("vram_mib") or 0)) * 2**20
    ram_budget = max(0, ram - max(2 * GIB, ram * .1))
    gpu_budget = max(0, vram - max(2 * GIB, vram * .1))
    demand = weight_bytes + WORKING_BYTES
    provider = hardware.get("gpu_provider") or ("nvidia" if vram else None)
    gguf = model_format.startswith("gguf")
    gpu_runtime = ("gpu" if provider == "nvidia" and vram else
                   "vulkan" if gguf and provider in ("amd", "intel") and hardware.get("render_devices") else None)
    if not gpu_runtime:
        return {"runtime": "cpu", "fits": demand <= ram_budget, "ram_needed_gib": math.ceil(demand / GIB),
                "offload_gb": 0, "hybrid_used": False, "hybrid_enabled": bool(enabled)}
    # A Vulkan render device can exist without a reliable VRAM-size report.
    # In that case, use the RAM budget alone for a conservative fit label.
    needs_ram = max(0, demand - gpu_budget)
    if needs_ram and not enabled:
        return {"runtime": "cpu", "fits": demand <= ram_budget, "ram_needed_gib": math.ceil(demand / GIB),
                "offload_gb": 0, "hybrid_used": False, "hybrid_enabled": False}
    offload = min(math.ceil(needs_ram / GIB), math.floor(ram_budget / GIB)) if enabled else 0
    return {"runtime": gpu_runtime, "fits": demand <= gpu_budget + ram_budget if enabled else demand <= gpu_budget,
            "ram_needed_gib": math.ceil(needs_ram / GIB),
            "offload_gb": offload if model_format == "safetensors" else 0,
            "hybrid_used": bool(needs_ram and enabled), "hybrid_enabled": bool(enabled)}
