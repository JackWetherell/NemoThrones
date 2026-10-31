---
name: woiaf-maester
description: "Research A Song of Ice and Fire canon on A Wiki of Ice and Fire, sliced to an in-game date, with page citations."
version: 1.0.0
author: jack
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Research, Lore, ASOIAF, WOIAF, Citations]
    related_skills: [nemothrones-gm, research/grounded-citations]
---

# WOIAF Maester

For any NemoThrones lore question: who someone is, where they are, what they know,
or what happened before a given date.

## Source of truth

**A Wiki of Ice and Fire** (`awoiaf.westeros.org`) is canon for NemoThrones. Its
text is CC BY-SA, so cite the page title. Book canon overrides the HBO shows and
the model's own memory; when they disagree, the wiki wins.

## Procedure

1. **Cache first.** Call `nt_lore(title, as_of_year)`. The NemoThrones engine
   caches every page it fetches in `lore.db`.
2. **Then the wiki.** Fetch from `https://awoiaf.westeros.org/api.php`
   (`action=parse` or `action=query&prop=revisions`). Keep to at most one
   request per second, on one connection.
3. **Time-slice.** You are answering *as of* the in-game date. Book "Recent
   Events" sections are dated by book: AGOT 298–299, ACOK 299, ASOS 299–300,
   AFFC and ADWD 300. History paragraphs are dated by the years they mention.
   Anything at or after the as-of date is **fate**: never reveal it to the
   player, and never give it to a character agent.
4. **Parallel research.** For many pages at once (e.g. dossiers for a new
   era), `delegate_task` in batch mode. Give each subagent one page, the as-of
   date and these rules.
5. **Cite** page titles (and revision ids when known).

## Output

Plain facts with their dates and page titles. When the wiki is silent or
ambiguous, say so; never fill a gap with invention.
