# Ideas: wellbeing when working with AI agents

Status: **ideas, not planned yet.** These record research and possible features so they can be picked up later.

## What the research says

Most of the evidence is surveys and practitioner reports. There are few controlled studies.

- **Supervising agents, not typing, is what tires people.** A BCG / UC Riverside survey of 1,488 U.S. workers named "AI brain fry": mental fatigue from overseeing AI beyond your capacity. People managing 3 or more agents reported about 14% more mental effort and 12% more fatigue, and productivity dipped after a third tool. ([The Register](https://theregister.com/2026/03/09/ai_brain_fry_managing_agents), [Let's Data Science](https://letsdatascience.com/news/software-engineers-feel-exhausted-by-ai-agents-19115894))
- **Context switching is the main cost.** Simon Willison describes four parallel agents leaving him "wiped out by 11 AM". Tim Dettmers points to near-constant context switching, which people handle poorly. ([Built In](https://builtin.com/articles/ai-brain-fry-software-developers), [Axios](https://www.axios.com/2026/04/04/ai-agents-burnout-addiction-claude-code-openclaw))
- **The waiting time used to be rest.** Agents take the routine work and leave you the decisions. Delegated tasks stay on your mind until you review them, and sustained decision-making tops out at around 4 good hours a day. ([Constantin Gonzalez](https://constantin.glez.de/posts/2026-07-16-why-working-with-ai-agents-is-so-tiring-and-what-to-do-about-it/))
- **Burnout depends on workload and autonomy.** An ICSE 2026 study of 442 developers linked GenAI adoption to burnout through higher workload and organisational pressure. Having autonomy reduced the effect. ([arXiv 2510.07435](https://arxiv.org/pdf/2510.07435), [GitKraken](https://gitkraken.com/blog/ai-was-supposed-to-mean-working-less-for-some-developers-its-doing-the-opposite))
- **Reviewing agent output is heavier work.** In a Sonar survey, 38% said reviewing AI output takes more effort than reviewing a colleague's work. METR found experienced developers were 19% slower with AI on their own projects while believing they were faster. ([Let's Data Science](https://letsdatascience.com/news/software-engineers-feel-exhausted-by-ai-agents-19115894))

## Habits the sources recommend

- **Write clear briefs.** Better agent output, and you can let the task go once it's handed off.
- **Leave a note at each stopping point.** Record what's next, so the task stops occupying your head.
- **Take a break at each handoff.** Stand up or stretch right after delegating, rather than filling the wait with more decisions.
- **Check agents on your own schedule.** Don't react to every "done" notification.
- **Limit work in progress.** One major task and at most two small ones.
- **Keep the evenings for things that actually restore you.**

## Feature ideas for worktracker

| Idea | How it could work | Fits with |
|---|---|---|
| **Agent load** | Each tick, count running `claude` / `codex` processes (as `CptHost` is checked for Zoom). Show a timeline band and send an alert past 3 at once. | Sampling loop, timeline |
| **Supervision time** | Daily share of time in terminals and agent apps versus the editor, with a soft budget of about 4 hours. | `usage` by app, KPIs |
| **Switches per hour** | Count front-app changes between ticks, stored per hour. Makes "agent hopping" visible. | New `usage`-style table |
| **Break after a handoff** | A Claude Code `UserPromptSubmit` or `Stop` hook ([docs](https://docs.anthropic.com/en/docs/claude-code/hooks)) posts to a new local endpoint, and the tracker suggests a 2-minute stand-up after a handoff. | Alerts, local API |
| **Batched agent notifications** | Hold "agent finished" notifications until the current work stretch ends, as alerts are held during Zoom meetings. | `fire()` hold logic |
| **Open tasks** | When the day closes, list agent sessions that are still unfinished, so each gets a line in tomorrow's note. | Close-the-day note |
| **Evening agents** | An alert when an agent session starts after hours. | After-hours alerts |

## Open questions

- Which agent processes to count, and how to tell an idle session from a working one (CPU, or hook events)?
- Should hook-based features be opt-in, given they need a change to the user's Claude Code settings?
- How to make sure a new metric changes behavior rather than just adding another number?
