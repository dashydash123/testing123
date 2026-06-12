# testing123

Here’s a comprehensive breakdown of licenses used across Hugging Face — organized by category (Models, Datasets, and Dependencies/Libraries):

🤖 Model Licenses

Most Common (by adoption):

As of early 2024, the most commonly used model licenses on Hugging Face were Apache 2.0 (38.1%), OpenRAIL/RAIL (24.0%), MIT (17.5%), and Creative Commons (7.5%). ￼

More recently, a scan of ~2.9M Hugging Face models showed Apache remains the dominant license with ~2.5x more licensed repos than its nearest competitor (MIT). OpenRAIL is the largest single non-OSI, non-model-specific license category. However, nearly 70% of all Hugging Face models carry no license at all. ￼

Open Source Licenses (OSI-Approved)



|License         |SPDX ID               |Key Traits                            |
|----------------|----------------------|--------------------------------------|
|Apache 2.0      |`apache-2.0`          |Permissive, includes patent grant     |
|MIT             |`mit`                 |Highly permissive, no patent grant    |
|GPL v3.0        |`gpl-3.0`             |Strong copyleft, patent grant         |
|AGPL v3.0       |`agpl-3.0`            |Strongest copyleft; covers network use|
|GPL v2.0        |`gpl-2.0`             |Strong copyleft, older variant        |
|LGPL v2.1 / v3.0|`lgpl-2.1`, `lgpl-3.0`|Weak copyleft                         |
|Mozilla MPL 2.0 |`mpl-2.0`             |File-level copyleft                   |
|BSD 2-Clause    |`bsd-2-clause`        |Permissive, minimal conditions        |
|BSD 3-Clause    |`bsd-3-clause`        |Permissive + non-endorsement clause   |
|ISC             |`isc`                 |Functionally similar to MIT           |
|Artistic 2.0    |`artistic-2.0`        |Permissive, Perl ecosystem origin     |
|AFL 3.0         |`afl-3.0`             |Permissive, similar to Apache         |
|Boost 1.0       |`bsl-1.0`             |Permissive                            |
|EUPL 1.1 / 1.2  |`eupl-1.1`, `eupl-1.2`|EU public license                     |
|WTFPL           |`wtfpl`               |Do-what-you-want public license       |
|Zlib            |`zlib`                |Permissive, minimal                   |

AI-Specific / Responsible AI Licenses (Non-OSI)

OpenRAIL (Open & Responsible AI Licenses) were developed to promote reuse and redistribution of AI models while forbidding harmful and unethical uses. The BigScience BLOOM RAIL license is specific to the BLOOM large language model. ￼

RAIL license adoption has grown dramatically — from 1% in September 2022 to 24% by January 2024 — and is used across text, image, speech, and generative AI models. ￼



|License                  |Notes                                                         |
|-------------------------|--------------------------------------------------------------|
|OpenRAIL                 |Generic Responsible AI License                                |
|OpenRAIL-M               |For models specifically                                       |
|OpenRAIL-D               |For datasets                                                  |
|BigScience OpenRAIL-M    |Used by BLOOM                                                 |
|CreativeML OpenRAIL-M    |Used by Stable Diffusion                                      |
|Llama 2 Community License|Meta’s custom license, restricts commercial use above 700M MAU|
|Llama 3 Community License|Meta’s updated license                                        |
|Gemma License            |Google’s custom license for Gemma models                      |
|Qwen License             |Alibaba’s custom license                                      |

Model-Specific Custom Licenses

Many large models use bespoke licenses: examples include Mistral, Falcon (TII), BLOOM (BigScience), and others.

📦 Dataset Licenses

On Hugging Face, MIT is the most popular license for datasets, and Apache 2.0 is the most common for models. ￼



|License                     |Notes                               |
|----------------------------|------------------------------------|
|MIT                         |Most common for datasets            |
|Apache 2.0                  |Common, permissive                  |
|CC BY 4.0                   |Attribution required                |
|CC BY-SA 4.0                |Attribution + ShareAlike (copyleft) |
|CC BY-NC 4.0                |Non-commercial only                 |
|CC BY-NC-SA 4.0             |Non-commercial + ShareAlike         |
|CC BY-ND 4.0                |No derivatives                      |
|CC0 1.0 (Public Domain)     |No restrictions at all              |
|OpenRAIL-D                  |Responsible AI, dataset-specific    |
|CDLA-Permissive-2.0         |Community Data License Agreement    |
|CDLA-Sharing-1.0            |ShareAlike variant for data         |
|ODbL (Open Database License)|Copyleft for databases              |
|PDDL                        |Public Domain Dedication and License|

Some practitioners use a combination — e.g., MIT for source code, Creative Commons for documentation, and CDLA-Permissive-2.0 for data — though this multi-license approach can be complex for non-legal specialists. ￼

🔧 Dependency / Library Licenses

These apply to the underlying Python packages and frameworks that AI models depend on:



|Library              |License     |
|---------------------|------------|
|PyTorch              |BSD 3-Clause|
|TensorFlow           |Apache 2.0  |
|Transformers (HF)    |Apache 2.0  |
|Datasets (HF)        |Apache 2.0  |
|Safetensors (HF)     |Apache 2.0  |
|Tokenizers (HF)      |Apache 2.0  |
|scikit-learn         |BSD 3-Clause|
|NumPy                |BSD 3-Clause|
|Pandas               |BSD 3-Clause|
|PEFT                 |Apache 2.0  |
|Accelerate           |Apache 2.0  |
|DeepSpeed            |Apache 2.0  |
|ONNX Runtime         |MIT         |
|LangChain            |MIT         |
|Sentence Transformers|Apache 2.0  |

⚠️ Key Compliance Risks

Research has revealed widespread inconsistencies between the licenses of pre-trained models and their downstream dependent projects — a pattern described as a “gravitational pull” toward permissive licensing that can result in systematic disregard for upstream license obligations. ￼

For your open source compliance work the most important distinctions to watch are:

	•	Copyleft contamination (GPL/AGPL propagating into commercial products)
	•	Non-commercial clauses (CC BY-NC, some RAIL variants) blocking enterprise use
	•	Model-specific restrictions (Llama, Gemma, Qwen) that override permissive-sounding labels
	•	Unlicensed models (~70% of HF) — legally, all rights reserved by default