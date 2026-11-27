"""Validate installable, commit-pinned Hugging Face model file sets."""
import hashlib
import re


SAFE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
SHARD = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)
WEIGHT = re.compile(r"(?:^|/)(?:model(?:-\d{5}-of-\d{5})?|[A-Za-z0-9_.-]+)\.safetensors$", re.I)
SAFETENSOR_SHARD = re.compile(r"^model-(\d{5})-of-(\d{5})\.safetensors$", re.I)
SIDE_FILES = re.compile(r"(?:^|/)(?:config|generation_config|tokenizer_config|special_tokens_map|added_tokens|preprocessor_config|processor_config|chat_template|model\.safetensors\.index)\.json$|(?:^|/)(?:tokenizer\.json|tokenizer\.model|vocab\.json|merges\.txt|chat_template\.jinja)$", re.I)


def file_record(row):
    name = row.get("rfilename")
    size = row.get("size") or (row.get("lfs") or {}).get("size")
    digest = (row.get("lfs") or {}).get("sha256")
    if not isinstance(name, str) or not SAFE_PATH.fullmatch(name) or ".." in name.split("/"):
        return None
    if not isinstance(size, int) or size <= 0 or size > 200_000_000_000:
        return None
    if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return {"path": name, "size": size, "sha256": digest}


def artifact_sets(siblings, config=None):
    """Return complete GGUF sets and one supported Safetensors snapshot."""
    files = {row["path"]: row for raw in siblings if (row := file_record(raw))}
    result = []
    shards = {}
    for name, row in files.items():
        if not name.lower().endswith(".gguf") or "mmproj" in name.lower() or not row["sha256"]:
            continue
        match = SHARD.fullmatch(name)
        if match:
            prefix, index, total = match.group(1), int(match.group(2)), int(match.group(3))
            if 1 <= total <= 512 and 1 <= index <= total:
                shards.setdefault((prefix, total), {})[index] = row
        elif row["size"] > 100_000_000:
            result.append({"format": "gguf", "file": name, "files": [row], "size": row["size"], "sha256": row["sha256"]})
    for (prefix, total), parts in shards.items():
        if len(parts) != total or set(parts) != set(range(1, total + 1)):
            continue
        ordered = [parts[index] for index in range(1, total + 1)]
        result.append({"format": "gguf-sharded", "file": ordered[0]["path"], "files": ordered,
                       "size": sum(part["size"] for part in ordered),
                       "sha256": hashlib.sha256("".join(part["sha256"] for part in ordered).encode()).hexdigest()})
    weights = [row for name, row in files.items() if WEIGHT.search(name) and row["size"] > 100_000_000 and row["sha256"]]
    config_file = files.get("config.json")
    tokenizer = any(name in files for name in ("tokenizer.json", "tokenizer.model", "vocab.json"))
    numbered = {int(match.group(1)): int(match.group(2)) for row in weights
                if (match := SAFETENSOR_SHARD.fullmatch(row["path"]))}
    complete_weights = (not numbered or len(set(numbered.values())) == 1 and
                        set(numbered) == set(range(1, next(iter(numbered.values())) + 1)))
    if weights and complete_weights and config_file and tokenizer and isinstance(config, dict):
        quant = (config.get("quantization_config") or {}).get("quant_method", "none")
        if quant in ("none", "awq", "gptq"):
            side = [row for name, row in files.items() if SIDE_FILES.search(name)]
            snapshot = sorted(weights + [row for row in side if row not in weights], key=lambda row: row["path"])
            result.append({"format": "safetensors", "file": "config.json", "files": snapshot,
                           "size": sum(part["size"] for part in snapshot), "sha256": hashlib.sha256(
                               "".join(f'{part["path"]}:{part["sha256"] or part["size"]}' for part in snapshot).encode()).hexdigest(),
                           "quantization": quant})
    return sorted(result, key=lambda item: (item["size"], item["file"]))
