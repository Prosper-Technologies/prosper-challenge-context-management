#
# The simulated caller. It knows the scenario's caller facts, opening line and
# details, never the intended outcome, and it doesn't judge the conversation: the
# outcome is checked against session state afterwards.
#

import re
from dataclasses import dataclass, field

from catalog.scenarios import Caller

from .llm import LLM, Reply

END = "[END]"

PROMPT = """You are role-playing a patient who is phoning a clinic's scheduling assistant. This is a test of the assistant, so stay in character and stick to these facts.

About you:
- {patient}
- {referral}

You already said: "{opening}"
{details}{corrections}
How to behave:
- Talk like a person on the phone: one or two short sentences, no lists, no IDs.
- Answer the assistant's questions truthfully from the facts above. Don't volunteer a fact unless you're asked or it's in what you already said.
- Want only what's described above. Don't ask about other services, providers, locations or alternatives, and don't change your mind except with the corrections listed above.
- If the assistant suggests something that fits everything above, agree to it. If you're asked to choose and nothing above decides it, choose the first option offered. Accept the first time offered unless something above says otherwise.
- Agree to book only when the assistant asks you to confirm a specific appointment{after_corrections}.
- Don't say goodbye until the assistant says the appointment is booked (with a confirmation number), or that it can't be booked, or that staff will call you back. Then reply with a short goodbye followed by {end}."""

# A detail that changes the caller's mind, said only after the assistant has acted on
# the earlier request.
CORRECTION = re.compile(r"^(actually|oh wait|sorry)\b|\binstead\b", re.IGNORECASE)


def caller_prompt(caller: Caller) -> str:
    facts = [d for d in caller.details if not CORRECTION.search(d)]
    corrections = [d for d in caller.details if CORRECTION.search(d)]
    details = corrections_text = after = ""
    if facts:
        details = "\nThings you'd say when they become relevant:\n" + "".join(f'- "{d}"\n' for d in facts)
    if corrections:
        corrections_text = (
            "\nCorrections: you change your mind. Say these one at a time, in this order, each only after the "
            "assistant has acted on your earlier request (for example once it has offered times or read the "
            "booking back). Your opening may contain a mistake that a correction fixes; until you say it, stick "
            "with what you said.\n" + "".join(f'- "{d}"\n' for d in corrections)
        )
        after = ", and only after you have said all of your corrections"
    return PROMPT.format(
        patient=(
            "You are a new patient: you have never been seen at any of these clinics."
            if caller.is_new_patient
            else "You are an existing patient: you have been seen at these clinics before."
        ),
        referral="You have a referral from your doctor." if caller.has_referral else "You don't have a referral.",
        opening=caller.opening,
        details=details,
        corrections=corrections_text,
        after_corrections=after,
        end=END,
    )


@dataclass
class SimulatedCaller:
    caller: Caller
    llm: LLM
    model: str
    replies: list[Reply] = field(default_factory=list)
    # The caller's side of the conversation: the assistant speaks as "user".
    _messages: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self._messages = [{"role": "system", "content": caller_prompt(self.caller)}]

    async def respond(self, assistant_said: str) -> tuple[str, bool]:
        """What the caller says next, and whether they've hung up. The first reply
        is the scenario's opening line, without a model call."""
        self._messages.append({"role": "user", "content": assistant_said or "..."})
        if len(self._messages) == 2:
            text = self.caller.opening
        else:
            reply = await self.llm.complete(self.model, self._messages)
            self.replies.append(reply)
            text = (reply.content or "").strip()
        self._messages.append({"role": "assistant", "content": text})
        ended = END in text
        return text.replace(END, "").strip(), ended
