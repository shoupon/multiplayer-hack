# Storyboard

## Narration script

Updated 2026-11-11.

We are building multiplayer AI: Canva or Figma for AI agents, for collaboration across models,
agents and companies. The agents talk to an orchestrator, which is our platform. It uses MCP and
other workarounds to coordinate with each participant's agent. Here is an example.

Say we want to book a dinner to celebrate after the hackathon, next Friday. I start a room. Our
orchestrator agent tracks this task's success and failure modes, then works toward a resolution
with the participants' agents.

Next, the agents that join through the invite link are prompted to read the parameters our
orchestrator set for this room. Each room gets its own. Each agent asks its user for permission to
read and relay their preferences. Once approved, it finds whatever access it needs, such as
calendar, email and payment, and relays the information back.

For this demo, participant A can only eat omakase, participant B can only do Wednesday night, and I
can only afford 2,000 or less.

Once all of this has surfaced, the AI agent goes ahead and books the restaurant. Now we see that it
has been booked. We can also see the decision progress and the log, to verify how everything
happened.

## Beats

| # | Narration | On screen | Where it comes from |
|---|---|---|---|
| 1 | "We are building multiplayer AI..." | Title card: agents from different models and companies around one orchestrator. | New |
| 2 | "Say we want to book a dinner... I start a room." | The host types the goal and opens a room. The orchestrator lists what success needs and what could block it. | `UI/mvpv2.html` (PR #3): proposal and questions screens |
| 3 | "The agents that join through the invite link..." | The room link and the instructions to paste into an agent. Agents join one by one and read the room's parameters. | `UI/mvpv2.html` (PR #3): waiting and live screens |
| 4 | "Each agent asks its user for permission..." | Each owner approves what their agent may read and relay. Calendar, email and payment connect only after approval. | FCL permission requests (`fcl_request_permission`) |
| 5 | "For this demo, participant A..." | The three constraints appear in the room: omakase only, Wednesday night only, 2,000 or less. | Live room screen |
| 6 | "The AI agent goes ahead and books..." | The orchestrator proposes one restaurant and time, the owners approve, and the booking is confirmed. | `UI/mvpv2.html` (PR #3): summary screen. FCL celebration demo |
| 7 | "We can also see the decision progress and the log..." | The step-by-step log of who said, asked, approved and did what. | FCL visualizer (`make viz`) and `decision-log.html` |

## Open points

- The goal says next Friday, but participant B can only do Wednesday night. Either the goal becomes
  "next week", or moving the dinner from Friday to Wednesday is the failure mode the orchestrator
  resolves on screen.
- "2,000 or less" needs a unit: NT$ per person or for the whole table.
- The simulated scenario in `UI/mvpv2.html` and the FCL celebration demo still use other people and
  constraints (a 19:00 start, vegetarian, peanut allergy, a video link). They need the three
  constraints above before recording.
