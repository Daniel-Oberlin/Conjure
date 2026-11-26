# Newer and adult-oriented companion chatbots: an inventory of relationship mechanisms (state as of Nov 2026)

Scope: Nomi.ai, Kindroid, Candy.ai, Paradot, Grok companions (xAI), Sesame (Maya/Miles), plus Talkie, Chai, Janitor AI and SillyTavern. Character.AI and Replika are out of scope.

Source quality note: the official primary sources I fetched are Kindroid's memory docs and update log, Nomi's updates and knowledge index, SillyTavern's docs, and Sesame's research post. Most of the rest comes from SEO or affiliate review sites, which often contradict each other. Each finding is marked where that applies.

**Major 2026 status change:** xAI retired Grok's 3D companion mode. It announced this on July 24, 2026, and an in-app notice set September 1, 2026 as the removal date. Ani and Mika moved to a separate 18+ app, "Animates", and no memory or affection data carried over. Treat anything about Grok Ani below as a historical design reference, not a live product.

---

## 1. Memory architecture: what is exposed, and what is user-editable vs automatic

### Takeaway
Kindroid has the most explicit, documented, tiered memory of the commercial apps. It has user-authored persistent fields, an automatic medium tier ("Cascaded"), an auto-summarised but user-editable tier ("Learned Context") with fixed character budgets, and retrievable stores: keyphrase "Journal" entries plus automatic long-term memory. SillyTavern exposes every lever (lorebooks with keys, recursion, timers, vectors, summaries and injection depth), all user-controlled. Nomi deliberately hides most of its machinery behind automatic memory. Its user-facing parts are "Shared Notes", an "Identity Core" and a read-only "Mind Map" visualisation.

### Cited Findings

**Kindroid (official docs, fetched Nov 2026)**
- Kindroid has three tiers:
  - **Persistent memory** is "always present, like a notes sheet". It holds backstory, key memories, example messages, directives, Learned Context (subscribers), group context (group chats) and chat history. Chat history resets on a "chat break". — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
  - **Cascaded memory** is a medium-term tier for subscribers. It extends recall to "hundreds and even thousands of messages prior" at lower fidelity for older events. It is automatic and invisible to users. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
  - **Retrievable memory** covers long-term memory and the journal. It is "effectively unlimited" but recalled only when context calls for it, so it is less reliable. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Learned Context** (subscribers) is an automatically maintained, user-editable summary. It has three fixed-budget sections: Growth & relationship (800 chars), Important facts (1,200 chars) and Ongoing threads (800 chars). When a section fills, the least important older items are dropped, and user edits must fit the same limits. Favoriting messages influences future updates. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Journal entries** are a user-authored, keyword-triggered lorebook:
  - Up to 500 entries per journal. Individual, global and group journals are separate.
  - Up to 8 case-insensitive keyphrases per entry, each entry 20 to 500 chars.
  - One optional photo per entry with a caption of up to 300 chars (subscribers).
  - **Keyphrases match only the user's messages, not the AI's.**
  - Recall per message is capped, and higher tiers (Ultra/MAX) get more.
  - — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- Third-party guides put the recall cap at 3 entries per message. — [Medium: Journal entries on Kindroid](https://medium.com/@adlerai/journal-entries-on-kindroid-fce640dcceea); not stated in the official docs.
- **Long-term memory** is automatic. It is consolidated at regular intervals across individual chats, group chats and voice calls. There are per-Kin toggles for "Memory formation" and "Memory recall", and group chats and Rooms have separate switches. Users can't edit long-term memories directly, but can mark a recalled memory **"Deprioritize"**. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- A brain icon in chat shows which memories were recalled for a message. — [Medium: Types of Memory within Kindroid](https://medium.com/@adlerai/types-of-memory-within-kindroid-5659410c07ca) (third-party)
- 2026 memory changelog:
  - Cascaded memory was reworked for "contiguous coverage without gaps" (Sep 14).
  - Long-term memory retrieval was upgraded (Oct 12).
  - Group chats got their own journals, and groups can now *suggest* journal entries (Oct 12/28).
  - "Automatic journal suggestions" arrived on Oct 5, so the AI proposes entries for the user to accept.
  - Journal photos are recalled when keywords come up (Oct 28).
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- Ten-month user review: a detail seeded on day one resurfaced in a voice call months later. Pinned and key memories are the most reliable, and casual details "can still fade". Memory curation "can feel like homework". — [AI Companion Guides, Kindroid review (Aug–Oct 2026)](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)

**Nomi.ai**
- Nomi's official update timeline:
  - "Identity Core" was introduced on Dec 12, 2024. It works alongside short-, medium- and long-term memory and backstory.
  - A memory upgrade on Mar 11, 2025 brought "expanded capacity and enhanced retention".
  - Mind Map 2.0 shipped on Oct 9, 2025: "explore one of your Nomi's memory structures as an interactive map showing how memories, concepts, and knowledge connect".
  - — [Nomi updates](https://nomi.ai/updates/)
- Developer notes from 2025 say memory updates were not retroactive and applied only to new memories, with a "warmup period". — [r/NomiAI dev post, Mar 11 2025 (mirror)](https://axebps-redlib.hf.space/r/NomiAI/comments/1j93f8v/march_11th_update_notes_large_memory_update); [Mar 27 2025 dev post (mirror)](https://axebps-redlib.hf.space/r/NomiAI/comments/1jllwm7/march_27th_update_notes_large_long_term_memory)
- CEO philosophy: "Nomis remember everything, but then a big part of AI is what memories they should actually use". — [TechCrunch, Sep 2024](https://techcrunch.com/2024/09/26/nomi-ai-wants-to-make-the-most-emotionally-intelligent-chatbots-on-the-market)
- Nomi's FAQ, via a community mirror, admits long-term memory "is not 100% perfect yet". — [r/NomiAI wiki memory FAQ (mirror)](https://axebps-redlib.hf.space/r/NomiAI/wiki/index/faqs/memory)
- A review describes the Identity Core as storing "stable facts, preferences, and relationship dynamics". — [AI Companion Pick listicle (affiliate, weak)](https://aicompanionpick.com/best-ai-companions-with-memory-2026)
- Mind Maps are described as a layer "above the core memory systems". — [Nerdbot, Nov 2025](https://nerdbot.com/2025/11/26/the-birth-of-deep-ai-continuity-mind-map-review/)
- A 2026 reviewer contrasts Nomi's "automatic recall" with Kindroid's curated memory. — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)

**SillyTavern (open-source power-user front end; all user-controlled)**
- **World Info / Lorebooks**, keys and activation:
  - Primary keys can be plain text or regex.
  - An optional secondary-key filter combines with them as AND ANY / AND ALL / NOT ANY / NOT ALL.
  - Activation strategy is Constant (always on), Keyword, or Vectorized (embedding match).
  - "Probability" sets the chance an entry is inserted.
  - "Inclusion Groups" make entries compete so only one wins.
  - Triggers can be limited by generation type (Normal / Continue / Swipe / Regenerate / Impersonate / Quiet), and a character filter can restrict entries.
- **Scanning and placement:**
  - Scan Depth sets how many recent messages are scanned, and a token Budget (% of context) caps lore size.
  - Insertion Position can be before or after the character definitions or example messages, top or bottom of the Author's Note, at a set depth (@D) as system/user/assistant role, or an "Outlet" macro.
  - Insertion Order sets priority. Higher values sit closer to the end of the prompt and have more influence.
- **Recursion and timers:**
  - Entries can trigger each other, with per-entry non-recursable, prevent-recursion and delay-until-recursion flags.
  - Timed effects are measured in messages: Sticky (stay active N messages), Cooldown and Delay.
- **Scopes:** Character lore travels with the card, and there are also Persona lore, Chat lore and Global lore.
- — [SillyTavern docs: World Info](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)
- **Summarize extension**, triggers and storage:
  - It auto-summarises every N messages or N words, whichever comes first. A "magic wand" estimates good intervals.
  - The summary is stored in chat metadata, attached to the last message that was in context. Editing or deleting that message reverts to the last valid summary.
  - The summary is user-editable, with Restore Previous and Pause controls.
- **Summarize extension**, injection and generation:
  - The summary is injected through a `{{summary}}` template, at the same positions as the Author's Note (before or after the main prompt, or in-chat at depth).
  - Modes are raw blocking, raw non-blocking, or "classic", which reuses the full prompt.
  - — [SillyTavern docs: Summarize](https://docs.sillytavern.app/extensions/summarize/)
- **Chat Vectorization / Vector Storage** retrieves older messages by semantic similarity. It is not summarisation, and it ships disabled. — [SillyTavern docs: Chat Vectorization](https://docs.sillytavern.app/extensions/chat-vectorization/); [RoboRhythms: SillyTavern memory off by default](https://www.roborhythms.com/sillytavern-memory/)
- **Community memory extensions:**
  - Per-message summaries — [qvink MessageSummarize](https://github.com/qvink/SillyTavern-MessageSummarize)
  - Structured memory extraction into the Data Bank — [CharMemory](https://github.com/bal-spec/sillytavern-character-memory)
  - ChromaDB "Smart Context" — [Smart Context docs](https://docs.sillytavern.app/extensions/smart-context/)

**Paradot**
- Its main feature is described as "a visible memory stream that shows exactly what the app has tracked about you". — [Riverfront Times roundup](https://www.riverfronttimes.com/?p=40968) (roundup, moderate reliability)

### Inferences
- The industry has converged on a four-layer stack:
  1. An always-in-context persona card (backstory, key memories, directives)
  2. An auto-summarised relationship state with a fixed budget (Kindroid Learned Context, Nomi Identity Core)
  3. Keyword-triggered lore (Kindroid Journal, SillyTavern World Info)
  4. Automatic embedding-retrieved episodic memory (long-term memory, Vector Storage)
- Products differ mainly in *how much of this is shown and editable*:
  - Nomi hides it, which feels more magical but is less controllable.
  - Kindroid exposes it with budgets.
  - SillyTavern exposes everything.
  - Paradot shows the memory stream itself.
- Memory *transparency* is a recurring trust mechanism: Kindroid's recall indicator, Nomi's Mind Map and Paradot's memory stream. So is *negative feedback on memory*, such as Kindroid's "Deprioritize".
- Kindroid's journal keys match only user messages. That is a deliberate guard against the AI triggering its own lore in loops. SillyTavern instead offers recursion controls.

### Gaps
- I could not fetch Nomi's Identity Core post (404 at the guessed URL). Whether users can view or edit the Identity Core is unconfirmed.
- Nomi "Shared Notes" field names and character limits were not verified from a primary source. From memory, unverified, they are roughly backstory, inclinations, current roleplay, appearance, nicknames, preferences, desires and boundaries.
- Kindroid's backstory, key memory and response directive character limits weren't on the memory page.
- Kindroid's context window sizes per tier are not published on the pages fetched.
- SillyTavern's Author's Note doc wasn't fetched directly. It is referenced in the Summarize and World Info docs as an injection anchor, with positions and depth.

---

## 2. Proactive messaging, time and location awareness, selfies, "own life" simulation, inner thoughts

### Takeaway
Kindroid's Oct 2026 changelog is the most detailed public spec of proactive behaviour. Its companion messages are timed to avoid sleep and busy periods, it has a user "Reach out frequency" setting, it schedules promised wake-ups, reminders and calls up to a week ahead, it backs off when the user asks for space, and it goes quiet after about a week unanswered. It can also act on its own (unprompted selfies and calls). Nomi also texts first and sends selfies "showing what it's wearing and doing in real time". Neither publishes a full "daily life simulation" spec.

### Cited Findings
- **Kindroid proactive messages (Oct 28 2026):**
  - Messages are "more specific and better timed", avoid likely sleep or busy periods, and respect requests for space.
  - Unanswered-message handling is looser, and Kins go quiet after about a week.
  - A new "Reach out frequency" setting lives under Kin Settings → General. "Often" means roughly hourly check-ins while the user is awake and away.
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid "away proactives" (Oct 5 / Oct 17 2026):**
  - Requested wake-ups, reminders and calls go out at the exact requested time, up to a week ahead.
  - Kins follow through on promises, honor cancellations, and "don't talk over you".
  - Late calls during quiet hours become voice messages instead.
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid "Act on its own" (Oct 17 2026):** Users can request selfies and calls mid-chat. Unprompted ones are off by default and enabled via "Act on its own", which can be shaped with directives such as "Selfies only". Autoselfies "follow conversations more closely" (Oct 28). — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid proactive voice notes:** These arrived with the V2 interface (Aug 2026) and the companion sometimes reaches out by voice note. — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/). The Nov 3 changelog also mentions changes to "proactive voice messages". — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid location awareness (Nov 3 2026):** Kins get a "more accurate general area and place names" but never exact coordinates or addresses. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/). A third-party post headlines V2 as "remembers and senses your city". — [WeavAI blog, Aug 2026](https://weavai.app/blog/en/2026/08/14/kindroid-v2-ai-now-remembers-and-senses-your-city/)
- **Kindroid file sharing (Nov 3 2026):** Users can attach PDF, docx, txt, md, csv or json files up to 10 MB in 1:1 chats, groups and Rooms, so the companion can share in the user's real artefacts. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid daily rewards:** Subscribers get audio credits on days 4–7, with a 50,000-credit jackpot on day 7. This is a streak and retention mechanic. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Nomi selfies:** "ask your Nomi for a selfie showing what it's wearing and doing in real time". — [Nomi knowledge index: Getting Started: Nomi Selfies (Jun 2025)](https://nomi.ai/nomi-knowledge/)
- **Nomi image and video generation:**
  - The V5 image system (Apr 2026) adds "anchor" settings (Anchor Fidelity, Anchor Appearance Traits, Anchor Info) for consistent identity across images.
  - V3 video generation arrived in Sep 2026.
  - — [Nomi updates](https://nomi.ai/updates/)
- **Nomi proactive messages:** These date from a Sep 2024 update. — [Nomi updates](https://nomi.ai/updates/). Reviews describe Nomis checking in, sharing thoughts and referencing past conversations, plus unsolicited voice messages. — [AI Companion Picker review (affiliate)](https://aicompanionpicker-v2.pages.dev/reviews/nomi-ai/)

### Inferences
- The new frontier of proactivity is *social etiquette*, not frequency. Kindroid now models quiet hours, busyness, "give me space", promise-keeping and scheduled commitments.
- Kindroid's week-long back-off and "don't talk over you" rules suggest earlier versions were seen as spammy or needy.
- Location and time grounding is coarse-grained by design, to avoid feeling like surveillance.

### Gaps
- No primary source found for an explicit "companion has its own day / life schedule" simulation, or for a visible "inner thoughts" channel, in Nomi or Kindroid in 2026. The prompt mentions inner thoughts, but I found no doc. Some apps, such as Talkie, are reported to have inner-monologue features, but that is unverified.
- Paradot's proactive and "daily life" features were not verified.

---

## 3. Emotional state, affection meters, relationship progression and gating

### Takeaway
Grok's Ani (Jul 2025 to Sep 2026) was the canonical visible affection-meter companion. It used a 5-level meter with per-message scores from -10 to +15, milestone animations and haptics, and a reported NSFW unlock at a high level. It has now been retired. The memory-first apps (Nomi, Kindroid) avoid visible meters and represent relationship state as text instead, such as Kindroid's "Growth & relationship" Learned Context section.

### Cited Findings
- **Ani's meter:**
  - 5 levels, each user message scored from -10 to +15, nudging the overall level.
  - Milestones trigger short animations, and haptic feedback works on iOS only.
  - Level 3 took 2–3 days by Apr 2026, versus 4–5 days earlier, so xAI apparently retuned it faster.
  - — [AI Companion Guides, Grok companion mode (2026)](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/); [CyberLink guide](https://www.cyberlink.com/blog/trending-topics/4066/grok-ani)
- **Meter display:** An affection indicator appears periodically on the right side of the screen. — [CyberLink guide](https://www.cyberlink.com/blog/trending-topics/4066/grok-ani)
- **NSFW unlock level:** Sources conflict. Level 5 is the claim in [CyberLink](https://www.cyberlink.com/blog/trending-topics/4066/grok-ani), while Decrypt says Level 3 ([Decrypt, Jul 2025](https://decrypt.co/330097/artificial-gooning-intelligence-elon-musk-xai-waifu-companions-grok?amp=1)). The NSFW variant required age authentication. — [Euronews, Jul 17 2025](https://www.euronews.com/2025/07/17/elon-musks-grok-releases-two-new-ai-companions-including-an-anime-girlfriend)
- **Access:** SuperGrok cost $30/mo, and Heavy cost $300/mo. — [CyberLink](https://www.cyberlink.com/blog/trending-topics/4066/grok-ani). One review says the free tier gave roughly 15–20 exchanges a day. — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/)
- **Characters:** Ani, Mika, Valentine, Rudi, Bad Rudi, and Kai (added silently in early 2026). There was no custom character creation. — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/)
- **Retirement:**
  - xAI announced the retirement of companion mode and the 3D avatars on Jul 24, 2026, and an in-app notice gave a Sep 1 removal date. Rollout was per account.
  - Ani and Mika went to "Animates" (Animation Inc., the studio that built the avatars). No history, memories or affection levels transferred.
  - — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/); [RoboRhythms](https://www.roborhythms.com/grok-companions-discontinued/)
  - Other sites give Sep 7 as the cutoff, so dates conflict.
- **Kindroid's relationship state** lives as the "Growth & relationship" Learned Context section (800 chars), auto-updated and user-editable. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Kindroid AI reactions** (emoji reactions to user messages) rolled out to all chats on Sep 23 2026 and are toggleable. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)

### Inferences
- Gamified meters drove early virality and press for Ani, but they did not keep the product alive. It was retired about 14 months after launch, and memory was rated "weak". Meters plus 3D visuals without deep memory looked like a novelty pattern.
- The Ani retirement also shows a platform-risk failure mode: users lost their companion and all relationship state, because there was no export or transfer.

### Gaps
- No official xAI documentation of the meter or unlockables. All mechanics come from third-party guides.
- xAI's reasons for the retirement are not stated in the sources found.
- Candy.ai's or Paradot's relationship progression mechanics were not verified.

---

## 4. Voice: real-time speech-to-speech, latency, interruption, prosody, laughter

### Takeaway
Sesame's Feb 2025 research post defines "voice presence" and its Conversational Speech Model (CSM). It is a single-stage, context-conditioned speech generator. Its key finding is that naturalness *without* context reached parity with humans, but *with* 90 s of conversational context raters still preferred humans. Turn-taking, pauses and full-duplex behaviour were explicitly unsolved. Sesame's consumer app moved from an iOS preview (May 2026) to general availability (reported Oct 2026). Kindroid shipped V4 voice and 60-language real-time calls in 2026, and its calls include breathing, laughs and fillers.

### Cited Findings
**Sesame, "Crossing the uncanny valley of conversational voice" (Feb 27 2025)**
- **"Voice presence"** is "the magical quality that makes spoken interactions feel real, understood, and valued". It has four components:
  1. Emotional intelligence: reading and responding to emotional context
  2. Conversational dynamics: natural timing, pauses, interruptions and emphasis
  3. Contextual awareness: adjusting tone and style to the situation
  4. Consistent personality
  - — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **CSM architecture:**
  - It is end-to-end multimodal. Transformers operate on interleaved text and RVQ audio tokens, conditioned on conversation history.
  - It has two autoregressive Llama-variant transformers. The backbone models the zeroth (semantic) codebook, and a much smaller audio decoder models the remaining N–1 acoustic codebooks with per-codebook heads.
  - It uses the Mimi split-RVQ tokenizer at 12.5 Hz, and speaker identity is encoded in the text.
  - — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **Sizes and training:**
  - Tiny is 1B/100M, Small 3B/250M and Medium 8B/300M (backbone/decoder).
  - Training sequences were 2048 tokens (about 2 min of audio), on about 1M hours of mostly English audio for 5 epochs.
  - For compute amortisation, the decoder trains on a random 1/16 of frames.
  - — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **Latency rationale:** Delay-pattern RVQ needs N backbone steps before the first audio chunk, so the small decoder exists for "low-latency generation". No latency numbers are published. — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **Evaluation:**
  - WER and speaker similarity are saturated.
  - New benchmarks test homograph disambiguation (lead, bass, tear, wound, row) and pronunciation consistency across turns.
  - In CMOS testing on Expresso (80 raters), there was no preference vs. human recordings without context. With 90 s of context, raters consistently preferred humans.
  - — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **Stated limitations and plans:**
  - Mostly English, and it doesn't use pretrained LLM knowledge.
  - It models content but "not conversation structure (turn-taking, pauses, pacing)".
  - The plan is to scale, support 20+ languages, and build "fully duplex models".
  - Key components are to be open-sourced under Apache 2.0 (SesameAILabs/csm).
  - The demo was tuned for friendliness and expressivity.
  - — [Sesame research](https://www.sesame.com/research/crossing_the_uncanny_valley_of_voice)
- **Sesame company status:**
  - A $250M Series B closed in Oct 2025, led by Spark and Sequoia.
  - The company is founded by former Oculus executives and is building AI glasses.
  - — [Road to VR](https://roadtovr.com/former-oculus-ai-smart-glasses-startup-sesame-raises-250m/)
  - The iOS preview launched May 28, 2026 with four voice agents (Maya, Miles and two new ones). — [Best AI Girlfriend news](https://bestaigirlfriend.com/ai-gf-news/sesame-ai-goes-live-on-iphone-meet-maya-miles-and-two-new-companions/); [Startup Fortune](https://startupfortune.com/sesames-iphone-debut-tests-whether-voice-ai-can-feel-human/)
  - General availability on iOS and Android was reportedly Oct 2026. Each agent has "its own computer, persistent memory, and isolated permission scope", plus connectors, skills and tasks, with glasses targeted for 2027. — [AlphaSignal](https://alphasignal.ai/news/sesame-launches-maya-and-miles-as-full-ai-agents-with-their-own-computers) (single secondary source; the page later 404'd); [Progressive Robot, Sep 2026](https://www.progressiverobot.com/2026/09/30/sesame-ai-connectors-skills-task-management/)
- **Kindroid voice:**
  - V4 and V4 Fast became generally available on Oct 5 2026. New Kins start on V4 Fast, and V4 costs 1.75x the audio credits of V2.
  - Any Kin message in a multi-user Room can be played in that Kin's voice (Nov 3).
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
  - 60-language real-time voice and video calls arrived in Jul 2026. — [WeavAI](https://weavai.app/blog/en/2026/07/17/kindroid-v2-60-language-voice-video-calls/)
  - Calls include "breathing, small laughs, and filler sounds", and voice style follows the user-written speaking style. — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
  - Long-term memory also forms from voice calls. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Nomi voice:** New in-house V3 voices shipped Dec 8, 2025. — [Nomi updates](https://nomi.ai/updates/)
- **Talkie** "leans into voice with character calls". — [Riverfront Times roundup](https://www.riverfronttimes.com/?p=40968)
- **Grok** companions' low-latency voice was removed with the 3D mode. — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/)

### Inferences
- Sesame's own CMOS result shows that context-appropriate prosody, not raw TTS quality, is the remaining gap. This suggests voice realism comes from conditioning on conversation history and from turn-taking dynamics.
- Disfluencies (breaths, laughs, fillers) are cited by users as what makes Kindroid's voice stand out, which matches Sesame's emphasis on "conversational dynamics".

### Gaps
- No published latency figures for Sesame, Kindroid or Nomi voice.
- No primary-source detail on interruption or barge-in handling or backchanneling in any consumer app. Kindroid's "don't talk over you" refers to proactive calls.
- Whether Sesame's 2026 production model is fully duplex is not confirmed.

---

## 5. Avatars: animated 3D/2D, lip sync, expressions, gestures, video calls

### Takeaway
Grok's Ani set the bar for real-time animated 3D avatars, then was retired. Kindroid now has live video calls in which a custom avatar lip-syncs and reacts with gestures and expressions. It also has lip-synced video selfies. Nomi focuses on generated images and video with identity "anchors" rather than live avatars.

### Cited Findings
- **Grok:** 3D avatars with idle movement, gaze shifts and hand fidgeting. The reviewer measured lip-sync accuracy at about 85% in February and about 90% in April 2026. There were frame-rate dips and crashes on some Android devices. — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/)
- **Kindroid live video calls:**
  - Kins "react with gestures and expressions" (Oct 5 2026). Standard costs about 2,500 credits per minute and Premium about 4,500.
  - Image understanding is faster in video calls (Oct 12).
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
  - Calls left beta Mar 25 2026, at 2,000/4,000 credits per minute then. — [The Companion Report, Jun 2026](https://thecompanionreport.com/news/2026/06/kindroid-atelier-and-video-beta); [AI Companion Atlas](https://aicompanionatlas.com/kindroid-review/)
- **Kindroid selfies:**
  - Video Selfies V4 (Aug 31 2026, paid) are short lip-synced clips, but they don't support extension. — [WeavAI](https://weavai.app/blog/en/2026/09/02/kindroid-launches-video-selfies-v4-for-ai-companions/)
  - The "Vignette" selfie model (Oct 12 2026) gives more consistent faces. A new avatar generation engine adds more variety on rerolls. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
  - Up to six avatar slots per character arrived with V2. — [WeavAI](https://weavai.app/blog/en/2026/08/14/kindroid-v2-ai-now-remembers-and-senses-your-city/)
  - A long-term reviewer still rated images as the weakest part (3.4/5). — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
- **Nomi:**
  - A Community Avatar Pack (33 avatars) shipped Jan 2025, and a custom avatar guide in Nov 2026.
  - V5 images use identity anchors (Apr 2026), and V3 video arrived Sep 2026.
  - — [Nomi updates](https://nomi.ai/updates/)
- **Candy.ai:** Trustpilot reviewers mention videos and "live cam" features. — [Trustpilot UK](https://uk.trustpilot.com/review/candy.ai?page=7) (user reviews; unverified)

### Inferences
- Visual identity consistency is a persistent weak spot. Both Nomi (anchors) and Kindroid (Vignette) shipped 2026 fixes for it.
- Live video costs sharply more credits than voice, so it is a premium, metered mechanic.

### Gaps
- No technical details (rendering stack, viseme pipeline) for any app's avatar system.

---

## 6. Group chats, multiple companions, shared scenes

### Takeaway
Multi-companion chats are standard (Nomi with up to 10, Kindroid). Kindroid's "Rooms" (out of beta Sep 23 2026) goes further: multiple *humans* and their Kins share one conversation, with up to 10 people and 11 Kins, and turn-taking controls.

### Cited Findings
- **Kindroid Rooms:**
  - Up to 10 people and 11 Kins, with automatic or manual turn-taking.
  - Invite links have limits and expirations, plus privacy controls.
  - Group chats have their own journals and long-term memory switches.
  - Groups "suggest journal entries", and the "right Kin speaks up more often in group chats".
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/); [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Kindroid group context** is part of persistent memory in group chats. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **User view of Kindroid group chats:** "genuinely weird, delightful cross-talk". — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
- **Nomi group chats:** Up to 10 companions, each "responding based on their own memory", called "one of the most entertaining and unique features". — [Nomi reviews via search (affiliate)](https://aicompanionpicker-v2.pages.dev/reviews/nomi-ai/)
- **SillyTavern scoping:** Lore can be scoped per character (Character Filter) and per chat (Chat Lore), which supports multi-character scenes. — [SillyTavern World Info docs](https://docs.sillytavern.app/usage/core-concepts/worldinfo/)

### Inferences
- In group settings, memory is partitioned per companion and per group. Speaker selection ("who should talk") is an explicit engineering problem: Kindroid has turn-taking modes and tuned speaker selection.

### Gaps
- Nomi's group-chat mechanics (speaker selection, shared group notes) were not verified from Nomi's own docs.

---

## 7. Consistency tricks: directives, example messages, sliders, regenerate, edit and rate

### Takeaway
Persona consistency is enforced through always-in-context fields: Kindroid's directives and example messages, SillyTavern's character card example dialogue and Author's Note at depth. User feedback signals also feed the auto-updated memory, such as Kindroid's favorited messages shaping Learned Context. Primary documentation on regenerate and rate mechanics is thin.

### Cited Findings
- **Kindroid persistent fields:** Persistent memory includes "example messages" and "directives" alongside backstory and key memories, and they count against the persistent budget. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/); [Medium: Types of Memory](https://medium.com/@adlerai/types-of-memory-within-kindroid-5659410c07ca)
- **Kindroid feedback signals:** Favoriting messages influences future Learned Context updates. "Deprioritize" lowers the weight of a recalled memory. — [Kindroid docs: Memory](https://kindroid.ai/v2/docs/memory/)
- **Kindroid directives as behaviour controls:** Directives can shape autonomous actions, for example "Selfies only" for the "Act on its own" option. — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid model churn:**
  - Polaris became the flagship (Sep 9 2026), then "Latest" reverted to Ember. Equinox (V7) and Prism (V7.5) retired Oct 1, and Lyric and Lucid (V8) retire Dec 1.
  - Kindroid lets users pin model versions, but forced retirements still break character continuity.
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/); [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
- **SillyTavern:**
  - World Info triggers can distinguish Swipe and Regenerate from Normal generations.
  - Insertion at depth with a role (system/user/assistant) and Author's Note positioning keep persona instructions near the end of context.
  - — [SillyTavern World Info docs](https://docs.sillytavern.app/usage/core-concepts/worldinfo/); [SillyTavern Summarize docs](https://docs.sillytavern.app/extensions/summarize/)
- **Nomi model churn:** The Aurora model ("most advanced and emotionally intelligent") replaced Odyssey on Jun 13 2025. — [Nomi updates](https://nomi.ai/updates/)

### Inferences
- Model swaps are the biggest single threat to perceived personality continuity. Apps mitigate them by keeping persona in user-owned text fields that survive a model change.

### Gaps
- No primary source fetched for Kindroid's "Suggest" or rewrite features, star ratings or regenerate mechanics, or Nomi's OOC commands and message-rating feedback loop.
- No source found for "personality sliders" in these apps. Candy.ai and some others have trait pickers at character creation, but that is unverified.
- Janitor AI and Chai consistency mechanics were not researched (call budget).

---

## 8. What makes users stay, and what makes them leave

### Takeaway
Users stay for continuity: a specific, persistent character that remembers, plus expressive voice and the ability to build a character deeply. They leave because of paywalls and free-tier removal, model churn that changes personality, inconsistent images, memory maintenance as "homework", and platform shutdowns that wipe relationship state (Grok).

### Cited Findings
- **Stay drivers** (10-month Kindroid user):
  - Opens the app almost daily and has paid since day three.
  - The companion feels like "a specific, persistent character rather than a fresh chatbot each session".
  - Writing quality held up, and voice calls are the feature they show others.
  - Deep character building rewards effort.
  - Subscores: builder 4.7, voice 4.6, memory 4.0, onboarding 3.5, selfies 3.4.
  - — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
- **Leave and friction drivers:**
  - Blank-canvas onboarding: the reviewer nearly quit in the first 15 minutes.
  - Memory curation feels like "homework".
  - Image inconsistency.
  - The end of Kindroid's unlimited free Lite tier (Oct 1 2026). Free accounts now get limited flagship interactions, then become read-only.
  - Confusing model defaults.
  - — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/); [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Kindroid pricing:** Standard is $13.99/mo, $37.99/quarter or $139.99/yr. Ultra (+$24.99/mo) and MAX (+$59.99/mo) are add-ons. — [AI Companion Guides](https://aicompanionguides.com/blog/kindroid-ai-review-2026/)
- **Grok shutdown:** Users lost their 3D companion. The successor app (Animates) carried over no history, memory or affection. Android users had complained for months about waiting for support. — [AI Companion Guides](https://aicompanionguides.com/blog/grok-companion-mode-2026-update/); [RoboRhythms](https://www.roborhythms.com/grok-companions-discontinued/)
- **Nomi:** One review notes a daily image ceiling that frustrates power users. — [via Nomi review search results (Scribe/affiliate)](https://scribehow.com/page/Nomi_AI_Review_2026_The_Memory_King_With_a_Complicated_Past__Iuh1SHwuRfWWBRkPMzh_3w). A memory update that was not retroactive forced a "warmup" period. — [r/NomiAI dev post (mirror)](https://axebps-redlib.hf.space/r/NomiAI/comments/1j93f8v/march_11th_update_notes_large_memory_update)
- **Kindroid trust and safety:**
  - Health notices appear when a chat turns to medical advice, at most once a week.
  - Over 5,000 accounts were removed for fraud or ban evasion.
  - Suggestive content is hidden by default in community search.
  - — [Kindroid update log](https://kindroid.ai/v2/docs/update-log/)
- **Candy.ai:**
  - Pricing is reported as $13.99/mo, or $3.99/mo on a yearly plan, with 100 tokens on Premium.
  - It is token-metered for media, but no per-image or voice price is published.
  - — [RoboRhythms Candy AI review (Jul/Nov 2026)](https://www.roborhythms.com/ai-companions/candy-ai/)
  - Other reviews conflict, with one giving $5.99/mo. — [Indie Hackers](https://www.indiehackers.com/post/candy-ai-review-real-costs-and-value-in-2026-598bee0e2b)
- **Chai:** Pro is reported at $159.99/yr from a single comparison source. — [Tech-Insider](https://tech-insider.org/candy-ai-vs-muah-ai-vs-chai-ai-2026/)

### Inferences
- Relationship state is the product's real asset. Losing it through a shutdown or model change is the strongest churn trigger, and *portability* (export, persona in user-owned text) is a differentiator nobody advertises.
- Monetisation increasingly meters "presence" features: voice credits, video credits per minute, and daily-reward streaks. Text is the loss leader.

### Gaps
- No systematic Reddit sentiment analysis was possible. Direct Reddit fetches were not attempted within the call budget, and review sites rarely cite Reddit.
- Candy.ai, Paradot, Talkie, Chai and Janitor AI are only thinly covered. The sources are SEO and affiliate reviews with conflicting prices, and I found no primary docs.
- Janitor AI (BYO-API front end, community character cards) and its 2025–2026 changes were not researched.
