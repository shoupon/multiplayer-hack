# Storyboard

This script is the shared reference for the final product and the demo. Point your agent at it so
everyone builds toward the same thing.

## Narration script

Updated 2026-11-11.

We are building multiplayer AI: Canva or Figma for AI agents, for collaboration across models,
agents and companies. The agents talk to the orchestrator, which is hosted on our platform. It uses
MCP and other workarounds to coordinate with each participant's agent. Here is an example.

Say we want to book a dinner to celebrate after the hackathon, next Friday. I start a room. Our
orchestrator agent tracks this task's success and failure modes, then works toward a resolution
with the participants' agents.

Next, the agents that join through the invite link are prompted to read the parameters our
orchestrator set, which are unique to each room. Each agent asks its user for permission to read
and relay their preferences. Once approved, it finds whatever access it needs, such as calendar,
email and payment, and relays the information back.

For this demo, participant A can only eat omakase, participant B has a pork allergy, and I can only
do NT$2,000 or less.

Once all of this has surfaced, the AI agents talk to each other. They sort out the best time,
coordinate, book the restaurant, and then update everyone. Now we see that it has been booked. We
can also see the decision progress and the log, to verify how everything happened.

## Beats

| # | Narration | On screen | Where it comes from |
|---|---|---|---|
| 1 | "We are building multiplayer AI..." | Title card: agents from different models and companies around one orchestrator. | New |
| 2 | "Say we want to book a dinner... I start a room." | The host types the goal and opens a room. The orchestrator lists what success needs and what could block it. | `UI/mvpv2.html` (PR #3): proposal and questions screens |
| 3 | "The agents that join through the invite link..." | The room link and the instructions to paste into an agent. Agents join one by one and read the room's parameters. | `UI/mvpv2.html` (PR #3): waiting and live screens |
| 4 | "Each agent asks its user for permission..." | Each owner approves what their agent may read and relay. Calendar, email and payment connect only after approval. | FCL permission requests (`fcl_request_permission`) |
| 5 | "For this demo, participant A..." | The three constraints appear in the room: omakase only, a pork allergy, NT$2,000 or less. | Live room screen |
| 6 | "The AI agents talk to each other..." | The agents compare free evenings and settle on the best time. The orchestrator proposes one restaurant, the owners approve, the booking is confirmed, and everyone gets the update. | `UI/mvpv2.html` (PR #3): summary screen. FCL celebration demo |
| 7 | "We can also see the decision progress and the log..." | The step-by-step log of who said, asked, approved and did what. | FCL visualizer (`make viz`) and `decision-log.html` |

## Open points

- "NT$2,000 or less" is read as the host's own limit, so a price per person at or under NT$2,000
  fits.
- The FCL celebration demo and the sample story in `UI/mvpv2.html` still use other constraints
  (vegetarian, a peanut allergy, a video link). The script above is the target; the prototype is
  left as it is for now.
