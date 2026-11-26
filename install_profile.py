"""Choose installer workloads from detected RAM and NVIDIA GPU capacity."""
import os
from pathlib import Path
import re
import sys


# Sizes are approximate transfer sizes. The exact files are downloaded by the installer.
CATALOG = (
    ("cpu-tiny", "Qwen3 0.6B Q8 (CPU, light chat)", 4, 0, "0.7 GB"),
    ("cpu-4b", "Qwen3 4B Q4_K_M (CPU, general chat)", 12, 0, "2.5 GB"),
    ("qwen4b", "Qwen3 4B Instruct 2507 (vLLM, fast chat)", 16, 16, "~9 GB"),
    ("deepseek", "DeepSeek V4 distill 9B Q5 (classification/coding)", 24, 12, "~7 GB"),
    ("llm", "Qwen3 Coder 30B-A3B AWQ (coding)", 32, 24, "~20 GB"),
    ("huihui", "Huihui Qwen3.6 35B-A3B Q3 (MoE chat)", 32, 24, "17.2 GB"),
    ("flint-q3", "Flint 40B-A2.5B Q3 (MoE classification)", 48, 24, "~22 GB"),
    ("flint-iq4", "Flint 40B-A2.5B IQ4 (MoE classification)", 64, 32, "~26 GB"),
    ("qwen38", "Qwen3.8 35B-A3B abliterated (MoE)", 48, 24, "~21 GB"),
    ("gemma4", "Gemma 4 26B-A4B abliterated (MoE)", 48, 24, "~17 GB"),
    ("vision", "Qwen3-VL 30B-A3B AWQ (vision)", 48, 24, "~20 GB"),
    ("media", "ComfyUI runtime (image/video models chosen later)", 16, 8, "container only"),
)


def eligible(ram_gib, vram_gib):
    return [item for item in CATALOG if ram_gib >= item[2] and vram_gib >= item[3]]


def select(raw, available):
    ids = {item[0] for item in available}
    pieces = [piece.strip() for piece in raw.split(",")]
    selected = []
    for piece in pieces:
        if piece.isdecimal() and 1 <= int(piece) <= len(available):
            piece = available[int(piece) - 1][0]
        if piece not in ids:
            raise ValueError(f"Unavailable selection: {piece or '(empty)'}")
        if piece not in selected:
            selected.append(piece)
    if not any(item != "media" for item in selected):
        raise ValueError("Select at least one language model")
    return selected


def update_env(path, values):
    old = path.read_text()
    for key, value in values.items():
        line = f"{key}={value}"
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        old, count = re.subn(pattern, line, old)
        if not count:
            old += ("" if old.endswith("\n") else "\n") + line + "\n"
    temporary = path.with_suffix(".selection.tmp")
    temporary.write_text(old)
    temporary.chmod(path.stat().st_mode & 0o777)
    temporary.replace(path)


def main():
    env_path = Path(sys.argv[1])
    ram_gib = float(sys.argv[2])
    vram_gib = int(sys.argv[3]) / 1024
    available = eligible(ram_gib, vram_gib)
    if not available:
        raise SystemExit("At least 4 GiB RAM is needed for this stack")
    print(f"Detected RAM: {ram_gib:.1f} GiB; usable NVIDIA VRAM: {vram_gib:.1f} GiB")
    print("Recommended choices for this hardware (approximate download size):")
    for number, (ident, name, _, _, size) in enumerate(available, 1):
        print(f"  {number:2}. {name} [{ident}] — {size}")
    existing = dict(line.split("=", 1) for line in env_path.read_text().splitlines() if "=" in line)
    requested = os.environ.get("AI_MODEL_SELECTION")
    if requested:
        print("Using explicit AI_MODEL_SELECTION")
    elif existing.get("AI_SELECTED_MODELS"):
        requested = existing["AI_SELECTED_MODELS"]
        try:
            select(requested, available)
        except ValueError:
            requested = None
            print("Previously selected models no longer fit the detected hardware")
        else:
            print("Keeping the previously selected models")
    if not requested:
        if not sys.stdin.isatty():
            raise SystemExit("Interactive model selection needs a terminal. Set AI_MODEL_SELECTION to comma-separated IDs for unattended installation.")
        requested = input("Enter one or more numbers or IDs, separated by commas: ").strip()
    try:
        chosen = select(requested, available)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    first = next(item for item in chosen if item != "media")
    update_env(env_path, {"AI_SELECTED_MODELS": ",".join(chosen), "AI_START_MODEL": first})
    print("Selected:", ", ".join(chosen))
    print("Initial workload:", first)


if __name__ == "__main__":
    main()
