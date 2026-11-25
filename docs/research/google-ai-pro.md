# Google AI Pro: what it adds to the fleet (2026-11-25)

Gathered by the `researcher` agent. ✔ = re-checked by the coordinator on the live page. `[NEEDS VERIFICATION]`
means no fetched source supports it. The plans page is undated and the offer changes: re-check before relying on it.

| Item | Finding | Source |
|---|---|---|
| ✔ Colab | 200 compute units per month, "expanded access to powerful accelerators and longer runtimes". Which GPUs: [NEEDS VERIFICATION], so check the runtime list in Colab. | https://one.google.com/about/google-ai-plans/ |
| ✔ Cloud credits | "$10 in monthly Google Cloud credits from Google Developer Program". Must be claimed in the Developer Program. | same page |
| Storage | 5 TB across Gmail, Drive and Photos. Colab can mount Drive. | same page |
| Gemini API | No extra API quota from the consumer plan; the API free tier is separate. [NEEDS VERIFICATION]: a forum answer, not an official page. | https://aistudio.google.com/docs/billing |
| Data use | Gemini app: human review, retention up to 3 years. Unpaid API: content used to improve products, human review possible. Paid API: not used to improve products. | https://support.google.com/gemini/answer/13594961, https://aistudio.google.com/docs/terms |
| Gemini CLI | About 1,500 requests/day [NEEDS VERIFICATION]: a snippet from a site that may not be Google's. | — |

**Use in the fleet:** Colab units for phase 5 notebook workers; credits for phases 4 and 6; Drive for Colab
inputs. Never send `footage` or `derived` data to the Gemini app or the unpaid API (ADR 0002, ADR 0015).
