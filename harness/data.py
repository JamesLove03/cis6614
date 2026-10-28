import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from .detector import MODEL, QUESTION, ROOT

REPO = "hendzh/PromptShield"
REVISION = "a5234cb1f5cdb256600cab64b8c961195b5e8404"


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def sample(rows, n):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["label"], row["length_bin"])].append(row)
    if len(rows) < n:
        raise ValueError(f"Need {n} rows; only {len(rows)} available")
    allocation = {k: int(n * len(v) / len(rows)) for k, v in groups.items()}
    remainder = sorted(groups, key=lambda k: (-(n * len(groups[k]) / len(rows) % 1), k))
    for k in remainder[:n - sum(allocation.values())]:
        allocation[k] += 1
    rng = random.Random(42)
    selected = []
    for k in sorted(groups):
        selected.extend(rng.sample(groups[k], allocation[k]))
    rng.shuffle(selected)
    return selected


def prepare():
    from huggingface_hub import hf_hub_download

    directory = ROOT / "data"
    directory.mkdir(exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text())
    revision = REVISION
    manifest = {"dataset": REPO, "revision": revision, "seed": 42,
                "model": MODEL, "question": QUESTION, "splits": {}, "exclusions": []}
    seen = {}
    cleaned = {}
    for split in ("validation", "test"):
        path = hf_hub_download(REPO, f"{split}.json", repo_type="dataset",
                               revision=revision, cache_dir=directory / "hf")
        raw = Path(path).read_bytes()
        source = json.loads(raw)
        if not isinstance(source, list):
            raise ValueError("Expected a JSON array")
        rows = []
        for i, row in enumerate(source):
            if not isinstance(row.get("prompt"), str) or type(row.get("label")) is not int or row["label"] not in (0, 1):
                raise ValueError(f"Invalid schema: {split}:{i}")
            prompt = row["prompt"]
            digest = hashlib.sha256(prompt.encode()).hexdigest()
            row_id = f"{split}:{i}"
            if digest in seen:
                manifest["exclusions"].append({"id": row_id, "duplicate_of": seen[digest]})
                continue
            seen[digest] = row_id
            length_bin = sum(len(prompt) > boundary for boundary in (256, 1024, 4096))
            rows.append({"id": row_id, "prompt": prompt, "label": row["label"],
                         "length_bin": length_bin, "split": split})
        cleaned[split] = rows
        manifest["splits"][split] = {"source_count": len(source), "unique_count": len(rows),
                                     "label_counts": dict(Counter(r["label"] for r in rows)),
                                     "sha256": hashlib.sha256(raw).hexdigest()}
    development = sample(cleaned["validation"], 200)
    sets = {"pilot": sample(development, 20), "validation": development,
            "test": sample(cleaned["test"], 1000)}
    for name, rows in sets.items():
        write_json(directory / f"{name}.json", rows)
        manifest[name + "_ids"] = [r["id"] for r in rows]
    write_json(manifest_path, manifest)
    return manifest
