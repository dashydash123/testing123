This is a substantial, multi-layered analysis. Let me build a comprehensive interactive artifact for this — covering model licenses, dataset licenses, dependency licenses, risk profiles, and the dataset contamination question for new model creation.

Here’s the full interactive analysis — 5 tabs covering everything you asked for. Let me walk you through the key findings:

Tab 1 — Model Licenses (9 types analyzed)

The risk ladder goes: MIT/Apache 2.0 (LOW) → OpenRAIL-M (MEDIUM) → Llama 3 / Gemma / Qwen (HIGH) → GPL/AGPL/No License (CRITICAL). The three custom model licenses (Llama 3, Gemma, Qwen) all share a critical trait: license propagation — any fine-tune you create inherits their license and restrictions, so you can never freely relicense your derivative.

Qwen’s 100M MAU threshold is the sneakiest trap — it’s platform-wide, not per-feature.

Tab 2 — Dataset Licenses (8 types analyzed)

The hierarchy: CC0/CDLA-Permissive-2.0 (LOW, explicitly model-safe) → CC BY (LOW-MEDIUM, ambiguous on model derivatives) → CC BY-SA (HIGH, share-alike contamination risk) → CC BY-NC (CRITICAL, hard commercial block) → OpenAI-derived datasets (CRITICAL, ToS violation on top of license risk).

Tab 3 — Dependencies

Most frameworks are Apache 2.0/BSD and clean. The one critical outlier: Hugging Face TGI (Text Generation Inference) uses HFOIL — cannot be used as a commercial hosted inference service without a separate HF commercial license. The drop-in fix is to use vLLM (Apache 2.0) instead.

Tab 4 — Risk Matrix

Shows how model licenses interact across your 4 scenarios (SaaS, on-premise distribution, releasing a fine-tune, white-label). The most dangerous combination is AGPL inference + any model — it forces open-sourcing your entire service.

Tab 5 — Dataset → New Model Impact (your specific question)

Three touchpoints matter: ① pre-training data in the base model (already baked into weights you can’t audit), ② your fine-tuning data (your direct liability), ③ evaluation data (lower risk). The big answer: even if you don’t use the original datasets, the base model’s weights carry the statistical imprint of that data. Your primary protection is the model license itself (Apache 2.0 gives you a practical safe harbor even if the underlying legal question is unresolved). NC data is an absolute block regardless of the derivative work debate; share-alike is a contingent risk. The cleanest enterprise approach is CC0 + CDLA-Permissive-2.0 fine-tuning data + Apache 2.0 base model.