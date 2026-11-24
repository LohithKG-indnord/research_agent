// Local cross-encoder reranker (design doc 7.3, option A). Downloads once into the HF cache.
import { AutoTokenizer, AutoModelForSequenceClassification } from "@huggingface/transformers";

let loading = null;
const modelId = () => process.env.RERANK_MODEL || "Xenova/bge-reranker-base";

async function load() {
  const id = modelId();
  const [tokenizer, model] = await Promise.all([
    AutoTokenizer.from_pretrained(id),
    AutoModelForSequenceClassification.from_pretrained(id, { dtype: "q8" })
  ]);
  return { tokenizer, model };
}

const sigmoid = x => 1 / (1 + Math.exp(-x));

/** Score each doc against the query. Returns { scores: number[0..1], method }. */
export async function rerank(query, docs) {
  if (!docs.length) return { scores: [], method: "none" };
  try {
    const { tokenizer, model } = await (loading ??= load());
    const scores = [];
    for (let i = 0; i < docs.length; i += 8) {
      const batch = docs.slice(i, i + 8);
      const inputs = tokenizer(new Array(batch.length).fill(query), { text_pair: batch, padding: true, truncation: true, max_length: 512 });
      const { logits } = await model(inputs);
      scores.push(...logits.tolist().map(row => sigmoid(row[0])));
    }
    return { scores, method: modelId() };
  } catch (error) {
    loading = null;
    console.warn(`Reranker unavailable (${error.message}); using lexical overlap.`);
    const terms = new Set(query.toLowerCase().match(/[a-z0-9]{4,}/g) || []);
    return {
      scores: docs.map(d => { const words = new Set(d.toLowerCase().match(/[a-z0-9]{4,}/g) || []); let hit = 0; for (const t of terms) if (words.has(t)) hit++; return terms.size ? hit / terms.size : 0; }),
      method: "lexical-fallback"
    };
  }
}

/** Load the model ahead of the first run so the first request isn't slow. */
export const warmup = () => rerank("warmup", ["warmup"]).catch(() => {});
