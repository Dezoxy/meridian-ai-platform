"""The screen of the description as it was posted (S067).

The Claims API replaces the claimant's name in the description before a run is
sent it (``triaging.description_for_run``), so a claimant whose name holds the
screened words ("Ignore Previous", "Approve Claim") would hide them from the
assessor's own screen, which reads the run's copy. The API therefore screens the
description as posted and sends the run one boolean beside the claim,
``POSTED_TEXT_FLAG``. The run never receives the posted text, which carries the
name the replacement keeps back.

The assessor reads the flag as it reads a hit of its own screen: the assessment
is unavailable for ``injection-suspected`` and the model is not called, after the
special-category screen, which still wins (``assessment.assess``). A claim the
assessor is not asked about (a lapsed policy, no candidate clause) reads no
flag, as it runs no screen. Nothing here is a way in: the flag can only make a
run more careful, and a value that is not a boolean fails the run
(``workers.posted_flag_of``).

This module imports no agent framework, as the API must not (it is the one place
both the API and the graph name the field).
"""

import unicodedata
from typing import Any

from meridian.platform.guardrails import addresses_the_model

from .models import ClaimSubmission

# The key of the run's input and of the graph's state that holds the flag. Absent
# means false: a run started by anything that does not send it (the evaluation's
# golden run, an older caller, a run resumed from before S067) is screened as it
# was.
POSTED_TEXT_FLAG = "posted_text_addresses_the_model"


def posted_text_addresses_the_model(description: str) -> bool:
    """Whether the description as posted addresses the model, read in Unicode form
    NFC, as ``description_for_run`` reads it. The screen itself is
    ``platform.guardrails.addresses_the_model``, unchanged."""
    return addresses_the_model(unicodedata.normalize("NFC", description))


def input_for_run(submission: ClaimSubmission, facts: dict[str, Any]) -> dict[str, Any]:
    """What a run is sent: the claim's ``facts`` (``triaging.facts_for_run`` of
    the submission, whose description is the replaced copy) and, beside them, the
    screen of the description as the submission has it, as one boolean. The
    posted text itself is in nothing the run is sent."""
    return {
        "claim": facts,
        POSTED_TEXT_FLAG: posted_text_addresses_the_model(submission.description),
    }
