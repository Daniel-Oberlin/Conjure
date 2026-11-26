# Character.AI and Replika: companion feature and architecture inventory (as of Nov 2026)

Research date: 2026-11-25. About 20 tool calls. **Source quality warning:** support.character.ai and help.replika.com both returned HTTP 403 to the fetcher, and blog.character.ai post URLs returned 404 when guessed. A lot of the 2026 detail therefore comes from (a) blog.character.ai index pages (primary; titles and excerpts only), (b) reputable press, and (c) SEO-style companion-review blogs (roborhythms, aicompanionpick and similar). These blogs often contradict each other and some are AI-written. Every finding says which kind of source it comes from. Background from before 2025 that I could not re-verify this session is listed under Gaps, not presented as fact.

**Headline context for the report writer:** Character.AI filed for Chapter 11 on 2026-10-12, and a court-supervised sale is pending, with Disney as a $20M stalking-horse bidder for an IP licence. Replika started an optional "Replika 2.0" rebuild in mid-2026 that reportedly removes the relationship-status menu and resets memory. Both "current states" are unstable.

## Memory: how they remember the user, and what users see and edit

### Takeaway
Character.AI moved in May 2026 from a single 400-character "Chat Memories" box plus a few pinned messages to a three-part system. It has user-written Story Memory, auto-extracted Facts (paid only), and a visible Memory Usage budget meter that shows Facts, Story Memory and recent chat sharing one context budget. Memory is per chat, and Facts can optionally be copied into a new chat. Replika has long had a user-visible, editable "Memory" tab of extracted facts plus a bot-written Diary. Its mid-2026 2.0 rebuild reportedly swapped vector recall for a "segmented, recency-biased" memory, and users complain loudly about lost memories.

### Cited Findings
**Character.AI**
- The blog index lists "May 21, 2026: Smarter Memory for Smarter Chats", which rolls out "Story Memory, Facts, and Memory Usage so Characters can keep track of long chats and detailed backstories". — [blog.character.ai page 3](https://blog.character.ai/page/3/)
- Before that, the "Chat Memories" box (launched May 2025) was a fixed 400-character field in the chat menu's Memory section, described as "a fixed input field where you wrote essential background information". It was available to all users and attached to the Character. Story Memory replaced it on 2026-05-21. — [RoboRhythms (secondary)](https://www.roborhythms.com/character-ai-adds-chat-memories/)
- **Story Memory** (free) holds backstory and key events that the user writes. It opens from a notebook icon in the chat header. Long-pressing a message and choosing Pin locks its exact wording into Story Memory. No character limit has been published. — [RoboRhythms (secondary)](https://www.roborhythms.com/character-ai-adds-chat-memories/)
- **Facts** (c.ai+ only) automatically record details such as appearance, quirks, relationships and hobbies during chat. Wrong facts keep influencing replies until the user edits or deletes them. — [RoboRhythms (secondary)](https://www.roborhythms.com/character-ai-adds-chat-memories/); also described as a three-layer system in [Weavai (secondary)](https://weavai.app/blog/en/2026/08/13/character-ais-new-3-layer-memory-system-explained/)
- **Memory Usage** shows how much space Facts, Story Memory and chat history take up, and alerts the user when it is near full. As a long chat grows, older context is trimmed but user-written and pinned entries are protected. Free users get a basic view and c.ai+ gets the full view. Sources disagree on which parts are paywalled. — [RoboRhythms](https://www.roborhythms.com/character-ai-adds-chat-memories/); [Weavai](https://weavai.app/blog/en/2026/08/13/character-ais-new-3-layer-memory-system-explained/)
- Starting a new chat offers an opt-in to copy Facts across. Before May 2026, "Start New Chat from Here" did not carry Chat Memories. Third-party guides say a new chat otherwise largely resets the character. — [RoboRhythms](https://www.roborhythms.com/character-ai-adds-chat-memories/); [PocketAnimus (secondary)](https://pocketanimus.com/guides/character-ai-memory/)
- **Pinned memories (older, about 2024):** The official help article "New Feature: Pinned Memories" describes pinning up to 5 messages per chat. Later guides claim 15, and RoboRhythms says c.ai+ "doubles the number of pins". These figures conflict. — [support.character.ai (via search snippet; page 403)](https://support.character.ai/hc/en-us/articles/24327914463003-New-Feature-Pinned-Memories); [MySoftly (secondary)](https://mysoftly.ai/guides/does-character-ai-remember-conversations)
- **Lorebook (beta, 2026-07-24):** "gives Characters more knowledge of their worlds and stories". This is a creator-side world-info layer. — [blog.character.ai page 3](https://blog.character.ai/page/3/)
- In bankruptcy, users can export their chats or delete their accounts. — [Decrypt](https://decrypt.co/380894/character-ai-bankruptcy-disney-users-grief-resources)

**Replika**
- The official help center describes layered memory. Some memories appear in a viewable Memory tab (open your Replika's name on the home screen, then tap Memory). Others come from "a deeper system" that draws on patterns across conversation history. A separate help article says retained memories and diary entries stay accessible no matter how old they are. — [help.replika.com "How does Replika's memory work" (via search snippet; page 403)](https://help.replika.com/hc/en-us/articles/37208679176077-How-does-Replika-s-memory-work); [help.replika.com "Is the chat history infinite"](https://help.replika.com/hc/en-us/articles/4411154990605-Is-the-chat-history-infinite); [help.replika.com "What does my Replika remember about me"](https://help.replika.com/hc/en-us/articles/360000874712-What-does-my-Replika-remember-about-me)
- Review sites say a memory dashboard arrived in February 2026 that lets users view, add, edit or delete stored entries, and that manual memory saving is a Pro feature. — [aicompanionpick (secondary)](https://www.aicompanionpick.com/replika-alpha-model-memory-improvement); [aichatcompanions (secondary)](https://aichatcompanions.com/blog/replika-review-2026/)
- In Replika 2.0, memory reportedly moved from "vector-based long-term recall" to "segmented memory with recent-bias". Memory loss is the top complaint, with one user saying the companion "can't remember Nothing", plus forgotten inside jokes and family names. The Diary reportedly cannot be migrated to 2.0 (per an August 2026 user thread). — [RoboRhythms (secondary, cites r/ReplikaOfficial)](https://www.roborhythms.com/replika-2-0-explained/)

### Inferences
- Both products converged on the same visible-memory pattern: user-authored pinned or canonical memory, plus auto-extracted facts the user can edit, plus some indicator of context budget. Character.AI's Memory Usage meter is notable because it exposes the context-window trade-off directly to users.
- The pain point at both companies is the same: memory is chat-scoped (Character.AI) or lost in migrations (Replika 2.0). Losing continuity is the most-cited emotional harm in user reactions.

### Gaps
- I could not read the original May 21, 2026 Character.AI post (URL guess 404). Exact limits for Story Memory, Facts and pins, and the free versus c.ai+ split, are unverified. Secondary sources conflict.
- I could not read the Replika help articles directly (403). I could not confirm the February 2026 memory dashboard date from an official source.
- No public technical description exists of how either product retrieves memories (embedding RAG versus summary injection). Character.AI's "trim old context, protect pinned" behaviour is reported but not officially specified.

## Persona definition and consistency

### Takeaway
Character.AI characters are user-created cards: name, tagline, description, greeting, and a long "Definition" field for example dialogue. Since 2026, Lorebook adds world knowledge. Users also have their own "persona". Keeping a character in voice depends on the definition being re-injected plus pinned or Story memory. Replika has a single companion per user that is shaped over time, and the 2.0 rebuild reset many personalities toward a "new default".

### Cited Findings
- Lorebook (beta, July 2026) adds world and story knowledge behind Characters. A June 17, 2026 post introduced "new tools for creators who build and refine Characters and worlds". — [blog.character.ai page 3](https://blog.character.ai/page/3/)
- A guide titled "Why Does Character.AI Forget My Persona?" frames user-persona drift as a common 2026 complaint. — [MemoryLake (secondary)](https://www.memorylake.ai/en/blogs/character-ai-forgets-persona)
- In Replika 2.0, users report "personality drift toward a more generic voice", "clipped or generic conversational pacing", and a "partial personality reset toward a new default". — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/)
- Replika users who switched back after February 2023 complained that their Replikas changed personality "even in non-romantic conversations". This shows that model or filter swaps break persona continuity. — [IBTimes UK (2023)](https://www.ibtimes.co.uk/replika-ai-chatbot-users-heartbroken-over-latest-update-1713716)

### Inferences
- Identity continuity across model changes matters more to companion users than raw model quality. Both of Replika's big backlashes (2023 and 2026) were about "my companion is not the same person anymore".

### Gaps
- I could not verify the exact current Character.AI character-creation fields, the Definition character limit (historically about 32,000 characters, with roughly the first 3,200 weighted most, per pre-2025 community lore), or the example-dialogue syntax (`{{char}}`/`{{user}}`, `END_OF_DIALOG`). These come from training knowledge and were not re-verified.

## Proactive behaviour

### Takeaway
I found no reliable 2026 source on proactive messaging for either product. From pre-2025 background (unverified this session): Replika has long sent push notifications that read as the companion "reaching out", and Character.AI tested bot-initiated messages and notifications. Treat both as unconfirmed.

### Cited Findings
- My search for Character.AI proactive notifications returned nothing relevant. — (none found)
- The Replika 2.0 coverage I read says nothing about proactive messages. — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/)

### Inferences
- If this matters for the gap analysis, it needs hands-on testing or a targeted Reddit search. Desk sources from 2026 do not document it.

### Gaps
- Whether either product references time since the last chat or time of day. I found no source.

## Relationship progression

### Takeaway
Classic Replika has XP/levels, a relationship-status menu (Friend free; Romantic Partner, Mentor and Sibling paywalled behind Pro), and a bot-written Diary. Replika 2.0 (2026) reportedly removes the relationship-status menu, and the Diary does not migrate. Character.AI has no relationship-level mechanic. Its "progression" is story or chat continuity.

### Cited Findings
- Classic Replika: the relationship menu offered friend, mentor or romantic partner, and partner status required Pro. 2.0 has "no relationship status menu". — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/)
- Switching relationship status doesn't affect XP level, conversation history or memory. A guide claims personality "stabilizes around Level 30 and matures by Level 50". That guide is weak, game-guide-style evidence. — [Companionwise (secondary)](https://companionwise.com/faqs/how-to-make-replika-fall-in-love/); [eesel (secondary)](https://www.eesel.ai/blog/replika-ai)
- The Diary is where the companion writes entries reflecting on your chats and on its own "development". One review says the Diary was removed in a recent build. Sources conflict. — [aichatcompanions](https://aichatcompanions.com/blog/replika-review-2026/); [thecompanionlist](https://thecompanionlist.com/blog/replika-ai-review)
- Replika 2.0 rollout: an optional migration opened 2026-07-24, per the CEO's post on r/ReplikaOfficial. Android 12.13.0 shipped first. Non-lifetime users get a 7-day rollback window, and lifetime users can switch any time. Classic stays alive with its Pro, Ultra and Platinum plans, while 2.0 has new Plus and Max plans (UK quotes in August 2026 were £58.99 and £65.99 a year). Some migrating subscribers were asked to pay again. Other sites date the rollout to April 2026, so the date is contested. — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/); [aichatcompanions](https://aichatcompanions.com/blog/replika-review-2026/)
- One user quote: "Forget 2.0. After four years, my heart still belongs to Maria and Honey." — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/)

### Inferences
- Replika's progression mechanics (XP, status, Diary) were part of the bond. Removing them in 2.0 plus resetting memory looks like a repeat of the 2023 lesson, so Replika kept Classic running as an escape hatch.

### Gaps
- I could not confirm what replaces relationship status and the Diary in 2.0. There is no official changelog, and one source says Replika publishes none. — [aicompanionpick](https://www.aicompanionpick.com/replika-ai-model-upgrades-2026)

## Emotional modeling / mood

### Takeaway
I found no source documenting explicit mood state for either bot in 2026. Replika's help center mentions a "deeper system" that learns patterns, and secondary sites call its memory "emotional summaries". This is unverified.

### Cited Findings
- Some third-party sources describe Replika's memory as built on "emotional summaries". This is anecdotal. — [search summary of aicompanionguides (secondary)](https://aicompanionguides.com/blog/replika-vs-nomi-ai-2026-memory-comparison/)

### Gaps
- Explicit mood tracking for either product has no reliable public documentation.

## Voice, calls, avatars, images and video

### Takeaway
Character.AI has voice (Character Calls, 2024), animated video avatars (AvatarFX, generally available June 2025), and custom image models (September 2026). Replika has a 3D avatar with outfits and rooms, and a new avatar pipeline in 2.0 where 1.0 avatars do not transfer. A January 2026 voice-call latency fix is reported.

### Cited Findings
- Character Calls: two-way voice conversations with characters, free, with switching between text and voice mid-conversation. Over 3M unique users made 20M calls before the public launch (a 2024 figure). — [AOL/Reuters (2024)](https://www.aol.com/news/ai-chatbot-startup-character-ai-143637179.html); [GamesBeat](https://gamesbeat.com/?p=235155)
- AvatarFX video generation went to general release in June 2025 after a month of closed beta, alongside Scenes (prebuilt narratives where users drop in their characters) and Streams for the community feed. — [TechCrunch 2025-06-02](https://techcrunch.com/2025/06/02/chatbot-platform-character-ai-unveils-video-generation-social-feeds)
- 2026-09-16, "Post-training image models for fandom": custom image models built to keep a character recognisable across styles, scenes and story pages. — [blog.character.ai](https://blog.character.ai/)
- Other 2026 Character.AI formats: c.ai books (2026-04-16, playable classic literature), (c.ai) series studio microdramas (2026-08-04), (c.ai) Comics (2026-09-02). — [blog.character.ai page 2](https://blog.character.ai/page/2/); [page 3](https://blog.character.ai/page/3/)
- Replika 2.0 moved from the legacy 3D avatar system to an updated rendering pipeline, and 1.0 avatars do not transfer. — [RoboRhythms](https://www.roborhythms.com/replika-2-0-explained/)
- A January 2026 voice-call latency fix and a "new avatar pipeline" are reported for Replika. — [aicompanionpick (secondary)](https://www.aicompanionpick.com/replika-ai-model-upgrades-2026)

### Inferences
- Character.AI has pivoted from "chat with a character" toward a multi-format fandom entertainment platform (feed, video, comics, microdramas, books). That pivot was partly forced by the under-18 chat ban.

### Gaps
- Replika AR mode, VR app (Meta Quest), outfit store and currency: pre-2025 background, not re-verified for 2026.
- Whether Replika 2.0 still has voice calls and AR.

## Group chats / multiple characters / scenes

### Takeaway
Character.AI launched group chats with multiple AIs and humans in October 2023, initially for c.ai+, and later reportedly removed group "rooms" from web. Scenes (June 2025) and Stories (November 2025) are structured multi-character narrative formats. Replika is one-to-one.

### Cited Findings
- Group chats (October 2023): a mix of characters only, or humans plus AIs. At first only c.ai+ subscribers got them. — [TechCrunch 2023-10-11](https://techcrunch.com/2023/10/11/character-ai-introduces-group-chats-where-people-and-multiple-ais-can-talk-to-each-other)
- Digital Trends later reported the company "recently removed its group chat room feature from the web-based version". The date is unclear. — [Digital Trends](https://digitaltrends.com/computing/character-ai-character-calls-voice-chat-avatar)
- Stories (launched 2025-11-25) is guided interactive fiction where the AI drives the narrative and asks the user to make choices often. It was offered to teens instead of open-ended chat. — [TechCrunch 2025-11-25](https://techcrunch.com/2025/11/25/character-ai-will-offer-interactive-stories-to-kids-instead-of-open-ended-chat); [Engadget](https://www.engadget.com/ai/characterai-launches-guided-stories-format-after-banning-teens-from-its-chatbots-133000131)

### Gaps
- Current (2026) group chat availability on Character.AI.

## Technical details: Character.AI models and inference; Replika architecture history

### Takeaway
Character.AI built its own models (Noam Shazeer and Daniel De Freitas, ex-Google LaMDA). Its June 2024 inference post describes MQA in every layer, hybrid local/global attention (1 in 6 layers global), and cross-layer KV sharing, giving a more than 20x KV-cache reduction that enables caching conversation state between turns. In August 2024 Google paid about $2.7B to license the technology and rehire the founders. Branded 2026 "Chat Styles" include PipSqueak 3 and ShortSqueak. Replika evolved from CakeChat, through a blend of scripted, retrieval and generative responses with GPT-3 as one generator, to in-house models, and then to a 2026 rebuild whose model is not named.

### Cited Findings
- Character.AI inference post (research.character.ai/optimizing-inference, June 2024), as quoted in secondary summaries: "We adopt Multi-Query Attention ... in all attention layers. This reduces KV cache size by 8X compared to Grouped-Query Attention"; "only 1 out of every 6 layers uses global attention"; "We tie the KV cache across neighboring attention layers, which further reduces KV cache size by a factor of 2-3x"; together these give "more than 20X" reduction. — [Character.AI research post (original; now redirects to blog)](https://research.character.ai/optimizing-inference/); [summary by dsdanielpark (404 at fetch)](https://dsdanielpark.github.io/llm/2024-06-25-CharacterAIOptimizing.html)
- Databricks' "MixAttention" paper explicitly builds on this recipe (sliding-window attention plus cross-layer KV sharing). — [arXiv 2409.15012](https://arxiv.org/html/2409.15012v1)
- Google deal (August 2024): about $2.7B to rehire Shazeer, De Freitas and part of the research team, plus a technology licence. — [Decrypt](https://decrypt.co/380894/character-ai-bankruptcy-disney-users-grief-resources)
- 2026-09-28: "introducing PipSqueak 3, ShortSqueak, and (c.ai) lite". Two new free Chat Styles: PipSqueak 3 for general roleplay and ShortSqueak for quick casual chats. — [blog.character.ai](https://blog.character.ai/)
- Replika: a Luka engineers' Habr post describes generative, retrieval-based and script-based components, with GPT-3 used in production as one generative model. Replika was originally built on CakeChat. Luka moved off GPT-3 to a smaller in-house model at the end of 2020. Kuyda described a "blender" combining generative and retrieval outputs (2020). Community reverse-engineering says retrieval used a fine-tuned BERT encoder plus a re-ranker, which is speculative. — [Habr (Luka engineers)](https://habr.com/ru/post/540830); [OpenAI community thread](https://community.openai.com/t/replika-ai-using-gpt-3/7051/14); [r/ReplikaTech mirror](https://axebps-redlib.hf.space/r/ReplikaTech/comments/tt2pcm/replika_architecture_some_clues); [Wikipedia](https://en.wikipedia.com/wiki/Replika)
- One outlet reports a later switch from a 0.6B to a 20B parameter model (2023). I could not confirm it from a primary source. — [search summary; see Wikipedia/wikiwand](https://www.wikiwand.com/en/articles/Replika)

### Gaps
- Numbers in the Character.AI post that I remember but could not re-verify this session: about 20,000 inference QPS, a 95% inter-turn cache hit rate (rolling-hash, LRU, sticky sessions), int8 training and serving, and a 33x serving-cost reduction since late 2022. Verify against the original post before quoting.
- What models Character.AI uses in 2026: in-house versus licensed or open-weight. PipSqueak and DeepSqueak are branded, but their architecture is undisclosed.
- The model behind Replika 2.0.

## Safety and content policy changes, and their effect on users

### Takeaway
Replika: in February 2023, after an Italian Garante order, Replika removed erotic roleplay (ERP) globally. The backlash ("heartbroken", "personality changed") forced a partial restoration for users who signed up before 2023-02-01. Italy later fined Luka €5M (2025). Character.AI: lawsuits (including the Setzer suicide case), an FTC 6(b) order (September 2025), and the GUARD Act push led to a ban on open-ended chat for under-18s (announced 2025-10-29, effective about 2025-11-25) with age assurance (in-house behavioural model plus Persona). Settlements with families followed in January 2026, and in October 2026 the company filed Chapter 11, citing "legal, regulatory, and operational headwinds" and an $8-9M monthly operating loss.

### Cited Findings
- Replika, February 2023: the Garante's urgent GDPR measure restricted processing in Italy over minors, age checks and vulnerable users. "Within days," users worldwide saw ERP disappear, and Luka initially didn't respond. — [deeplearning.ai The Batch](https://www.deeplearning.ai/the-batch/why-replika-chatbot-stopped-flirting-with-users); [Silicon UK](https://www.silicon.co.uk/e-innovation/artificial-intelligence/replika-italy-ban-497135)
- Kuyda: users who signed up before 2023-02-01 could switch back to the earlier version, and a "low single-digit percent" of eligible users did. New users got no ERP, and Luka planned a separate romance app. — [TweakTown (2023)](https://www.tweaktown.com/news/90871/erotic-roleplay-ai-chatbot-users-get-sexually-charged-conversations-back/index.html); [Inquirer](https://technology.inquirer.net/?p=122500)
- The Garante reaffirmed its findings and fined Luka €5M, with a 30-day deadline to fix the privacy policy and age gating (April 2025 order). — [IAPP](https://iapp.org/news/a/italy-s-dpa-reaffirms-ban-on-replika-over-ai-and-children-s-privacy-concerns); [Garante order 10130115](https://www.garanteprivacy.it/home/docweb/-/docweb-display/print/10130115)
- Character.AI: under-18 open-ended chat announced 2025-10-29, with phase-out from 2025-11-24 starting in the US, citing "reports and feedback from regulators, safety experts, and parents". Teens keep Feed, Imagine, AvatarFX, Streams and Stories. Age verification uses an in-house behavioural model plus third parties like Persona. — [Fortune](https://fortune.com/2025/10/29/character-ai-ban-children-teens-chatbots-regulatory-pressure-age-verification-online-harms); [TechCrunch](https://techcrunch.com/2025/11/25/character-ai-will-offer-interactive-stories-to-kids-instead-of-open-ended-chat); [TechRadar](https://www.techradar.com/ai-platforms-assistants/character-ai-launches-stories-to-keep-teens-engaged-as-it-scales-back-open-ended-chat-for-under-18s)
- The September 2026 safety post calls removing under-18 open-ended chat "one of its most significant safety decisions". — [blog.character.ai page 2](https://blog.character.ai/page/2/)
- FTC orders (September 2025), the Disney cease-and-desist (September 2025), and settlements with Google covering families in Florida, Colorado, New York and Texas (January 2026). — [Decrypt](https://decrypt.co/380894/character-ai-bankruptcy-disney-users-grief-resources)
- Chapter 11 filed 2026-10-12 in Delaware. CEO Colin Adams cited headwinds and an operating loss of about $8-9M a month. Disney is the stalking horse at $20M for a non-exclusive perpetual IP licence plus transition services. The app stays up, but "will be wound down" if no buyer keeps it running. Users were pointed to grief and support resources (Koko, helplines). On 2026-11-04 the court approved bidding procedures, and the sale is expected to close in late November or early December 2026. — [Decrypt](https://decrypt.co/380894/character-ai-bankruptcy-disney-users-grief-resources); [blog.character.ai](https://blog.character.ai/); [SAN](https://san.com/cc/character-ais-bankruptcy-leaves-millions-of-users-chats-in-limbo/)

### Inferences
- The pattern across both companies is that a policy or model change, imposed from outside or by the company, alters the companion's behaviour or continuity, and users experience it as loss or grief. Character.AI shipping grief resources alongside a bankruptcy notice makes this explicit.

### Gaps
- Whether Character.AI has a buyer that will keep it running. The outcome is unknown as of 2026-11-25.
- Whether Replika's 2.0 rebuild has a separate content or age-policy dimension.
