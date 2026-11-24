"""Local cross-encoder reranker (design doc 7.3, option A): Xenova/bge-reranker-base, quantized ONNX, run on this computer.
The model lives in models/<name>; if it is missing it is downloaded once from Hugging Face."""
import asyncio
import math
import os
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_loaded = None
_lock = threading.Lock()


def model_id():
    return os.environ.get("RERANK_MODEL") or "Xenova/bge-reranker-base"


def _load():
    import numpy  # noqa: F401 - fail early if missing
    import onnxruntime
    from tokenizers import Tokenizer
    name = model_id()
    local = ROOT / "models" / name
    if not (local / "onnx" / "model_quantized.onnx").exists():
        from huggingface_hub import hf_hub_download
        for f in ("tokenizer.json", "onnx/model_quantized.onnx"):
            hf_hub_download(name, f, local_dir=local)
    tokenizer = Tokenizer.from_file(str(local / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=512)
    pad = "<pad>" if tokenizer.token_to_id("<pad>") is not None else "[PAD]"
    tokenizer.enable_padding(pad_id=tokenizer.token_to_id(pad), pad_token=pad)
    session = onnxruntime.InferenceSession(str(local / "onnx" / "model_quantized.onnx"), providers=["CPUExecutionProvider"])
    return tokenizer, session


def _score(query, docs):
    global _loaded
    import numpy as np
    with _lock:
        _loaded = _loaded or _load()
    tokenizer, session = _loaded
    wanted = {i.name for i in session.get_inputs()}
    scores = []
    for i in range(0, len(docs), 8):
        batch = tokenizer.encode_batch([(query, d) for d in docs[i:i + 8]])
        feeds = {"input_ids": np.array([e.ids for e in batch], dtype=np.int64), "attention_mask": np.array([e.attention_mask for e in batch], dtype=np.int64)}
        if "token_type_ids" in wanted:
            feeds["token_type_ids"] = np.array([e.type_ids for e in batch], dtype=np.int64)
        (logits,) = session.run(["logits"], {k: v for k, v in feeds.items() if k in wanted})
        scores += [1 / (1 + math.exp(-float(row[0]))) for row in logits]
    return scores


async def rerank(query, docs):
    """Score each doc against the query. Returns {"scores": [0..1], "method"}."""
    global _loaded
    if not docs:
        return {"scores": [], "method": "none"}
    try:
        return {"scores": await asyncio.to_thread(_score, query, docs), "method": model_id()}
    except Exception as error:  # noqa: BLE001 - fall back to word overlap so the run still works
        _loaded = None
        print(f"Reranker unavailable ({error}); using lexical overlap.", file=sys.stderr)
        terms = set(re.findall(r"[a-z0-9]{4,}", query.lower()))

        def overlap(d):
            words = set(re.findall(r"[a-z0-9]{4,}", d.lower()))
            return len(terms & words) / len(terms) if terms else 0
        return {"scores": [overlap(d) for d in docs], "method": "lexical-fallback"}


def warmup():
    """Load the model ahead of the first run so the first request isn't slow."""
    def go():
        try:
            _score("warmup", ["warmup"])
        except Exception as error:  # noqa: BLE001 - the first real run falls back and reports it
            print(f"Reranker warmup failed: {error}", file=sys.stderr)
    threading.Thread(target=go, daemon=True).start()
