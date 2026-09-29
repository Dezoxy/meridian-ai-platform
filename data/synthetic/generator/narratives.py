"""First-person claim descriptions from per-peril template variants.

A description is an opening sentence and a detail sentence for the peril, then,
where the facts call for it, a sentence that states the circumstance behind an
exclusion and a sentence that gives the reason for a late report. The
circumstance is stated plainly in the text; it is never a field of the claim.
A circumstance also changes the opening and detail sentences (a scene), so the
story never contradicts it. A combination without a scene is an error.
"""

import random
from datetime import date
from typing import NamedTuple

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def date_text(day: date) -> str:
    """'5 June 2026'. Own month names: strftime would follow the locale."""
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


# (line, peril) -> (opening sentences, detail sentences).
# Slots: date, city, subject, roof.
TEMPLATES = {
    ("motor", "collision"): (
        (
            "On {date} I was driving through {city} when {subject} collided "
            "with a car that pulled out in front of me.",
            "On {date} in {city} a van reversed into {subject} while I was "
            "waiting at a junction.",
            "On {date} {subject} was hit by another car in a car park in {city}.",
        ),
        (
            "The front bumper and one wing are damaged, but the car can still "
            "be driven.",
            "The rear panel is dented and one of the lights is broken.",
            "The other driver gave me his details and nobody was hurt.",
        ),
    ),
    ("motor", "theft"): (
        (
            "On {date} I found that {subject} was gone from the spot where I "
            "had parked it in {city}.",
            "Overnight on {date} someone stole {subject} from the street in {city}.",
            "On {date} I came back from work in {city} and {subject} had been "
            "taken from the car park.",
        ),
        (
            "I told the police the same day and I still have both keys.",
            "The car was locked, and there was broken glass on the ground where "
            "it had stood.",
        ),
    ),
    ("motor", "fire"): (
        (
            "On {date} smoke started to come from under the bonnet of {subject} "
            "in {city}, and within minutes it was on fire.",
            "On {date} {subject} caught fire while it was parked in {city}.",
            "On {date} a fire broke out in the engine bay of {subject} in {city}.",
        ),
        (
            "The fire brigade put it out, but the engine and the front of the "
            "car are badly damaged.",
            "The whole front of the car is burnt and the interior is full of "
            "smoke damage.",
        ),
    ),
    ("motor", "glass"): (
        (
            "On {date} a stone thrown up by a lorry cracked the windscreen of "
            "{subject} on the road near {city}.",
            "On {date} the side window of {subject} was smashed while it stood "
            "in {city}.",
            "On {date} the rear window of {subject} shattered while it was "
            "parked in {city}.",
        ),
        (
            "The broken glass makes the car unsafe to drive until it is fixed.",
            "The glass has to be replaced before the car can be used again.",
        ),
    ),
    ("motor", "storm"): (
        (
            "On {date} hail damaged {subject} while it was parked in {city}.",
            "During the storm on {date} a tree fell onto {subject} in {city}.",
            "On {date} a strong gust in {city} blew a roof panel onto {subject}.",
        ),
        (
            "The bonnet and the roof are covered in dents.",
            "The windscreen is cracked and the roof is dented.",
        ),
    ),
    ("motor", "third_party_liability"): (
        (
            "On {date} near {city} I drove into the back of another car while "
            "driving {subject}.",
            "On {date} near {city} I lost control of {subject} and hit another car.",
            "On {date} near {city} another driver and I collided while I was "
            "driving {subject}.",
        ),
        (
            "The other driver has asked me to pay for the repair of the car.",
            "The other party has sent me a written claim through a lawyer.",
        ),
    ),
    ("home", "fire"): (
        (
            "On {date} a fire broke out in {subject} in {city} while I was at work.",
            "On {date} a fire started in the kitchen of {subject} in {city}.",
            "On {date} an electrical fault caused a fire in {subject} in {city}.",
        ),
        (
            "The fire brigade put it out, but there is heavy smoke and fire "
            "damage in several rooms.",
            "Most of the damage was caused by the smoke and by the water used to "
            "put out the fire.",
        ),
    ),
    ("home", "storm"): (
        (
            "On {date} a severe storm hit {city} and tore tiles off {roof}.",
            "During the storm on {date} a large branch fell through a window of "
            "{subject} in {city}.",
            "On {date} strong wind and hail damaged {subject} in {city}.",
        ),
        (
            "Rain came in through the roof and the ceiling below it is wet.",
            "Some windows are broken and there is water damage inside.",
        ),
    ),
    ("home", "flood"): (
        (
            "On {date} heavy rain flooded the street in {city} and water came "
            "into {subject}.",
            "After days of rain the river in {city} overflowed on {date} and "
            "flooded {subject}.",
            "On {date} water from a swollen stream reached {subject} in {city} "
            "and stood in the rooms for hours.",
        ),
        (
            "The floors, the walls and part of the furniture are soaked.",
            "The water was about twenty centimetres deep and we needed pumps to "
            "clear it.",
        ),
    ),
    ("home", "burst_pipe"): (
        (
            "On {date} a pipe burst in the wall of {subject} in {city} and "
            "water poured into the room.",
            "On {date} I came home to {subject} in {city} and found a burst "
            "pipe in the bathroom.",
            "On {date} a pipe under the kitchen sink of {subject} in {city} "
            "burst and water spread across the floor.",
        ),
        (
            "The plumber shut off the water, and the floor and the walls are wet.",
            "The flooring is swollen and the walls are damp in two rooms.",
        ),
    ),
    ("home", "burglary"): (
        (
            "On {date} someone broke into {subject} in {city} while I was away.",
            "When I came home on {date} the door of {subject} in {city} had "
            "been forced open.",
            "On {date} burglars entered {subject} in {city} through a window.",
        ),
        (
            "They took a laptop, some jewellery and cash.",
            "Drawers were emptied and the door frame is broken.",
        ),
    ),
    ("home", "accidental_damage"): (
        (
            "On {date} I was moving furniture in {subject} in {city} and "
            "dropped a heavy cabinet onto the floor.",
            "On {date} a heavy mirror fell over in {subject} in {city} and "
            "broke against the tiles.",
            "On {date} I dropped a heavy toolbox in {subject} in {city}.",
        ),
        (
            "The tiles are cracked and have to be replaced.",
            "The damage is not dangerous, but it cannot be left as it is.",
        ),
    ),
}


class Scene(NamedTuple):
    """The opening, detail and circumstance sentences of one circumstance."""

    openers: tuple[str, ...]
    details: tuple[str, ...]
    sentences: tuple[str, ...]


# A circumstance changes what happened, so it brings its own opening and detail
# sentences: a claimant who was driving under the influence did not wait at a
# junction, and an unlicensed driver's opener cannot say who was at the wheel.
# The key is (exclusion code, peril). Every sentence names the circumstance in
# words a reader cannot miss: track day, drinks, licence, rotten, months. Each
# tuple has the size of the generic tuple it replaces, so the choices consume
# the random stream exactly as before.
RACING_SENTENCES = (
    "It happened during a track day, in a timed session for amateur drivers.",
    "The event was a track day, and everyone on the circuit was taking part "
    "in timed laps.",
)
CIRCUMSTANCE_SCENES = {
    ("racing", "third_party_liability"): Scene(
        (
            "On {date} I was driving {subject} on the racing circuit near "
            "{city} when I ran into the back of another car.",
            "On {date} at the racing circuit near {city} I misjudged a corner "
            "in {subject} and hit another car.",
            "On {date} on the racing circuit near {city} I collided with "
            "another car in a corner while driving {subject}.",
        ),
        (
            "The other driver has asked me to pay for the repair of his car.",
            "The organiser has sent me a written claim for the damage to the "
            "other car.",
        ),
        RACING_SENTENCES,
    ),
    ("racing", "collision"): Scene(
        (
            "On {date} I lost control of {subject} on the racing circuit near "
            "{city} and hit the barrier.",
            "On {date} {subject} spun off the track at the racing circuit "
            "near {city} and hit a tyre wall.",
            "On {date} I braked too late in {subject} on the racing circuit "
            "near {city} and hit the wall at the end of the straight.",
        ),
        (
            "The front and one side of the car are badly damaged, and no "
            "other car was involved.",
            "The suspension and the bodywork are damaged, and nobody was hurt.",
            "The car cannot be driven, and nobody else was involved.",
        ),
        RACING_SENTENCES,
    ),
    ("driving_under_influence", "collision"): Scene(
        (
            "On {date} late in the evening I was driving {subject} home "
            "through {city} when I hit a lamp post.",
            "On {date} shortly before midnight I lost control of {subject} in "
            "{city} and drove into a fence.",
            "On {date} late at night I was at the wheel of {subject} in "
            "{city} when it left the road and hit a wall.",
        ),
        (
            "The front of the car is badly damaged, and nobody else was involved.",
            "The bumper and both wings are crushed and the car cannot be driven.",
            "The car was towed away, and nobody was hurt.",
        ),
        (
            "I had a few drinks at a dinner beforehand and then got behind the wheel.",
            "Earlier that evening I had drinks with colleagues, and then I "
            "drove myself home.",
        ),
    ),
    ("unlicensed_driver", "collision"): Scene(
        (
            "On {date} {subject} left the road in {city} and hit a fence.",
            "On {date} {subject} hit a lamp post in {city}.",
            "On {date} {subject} crashed into a wall in {city}.",
        ),
        (
            "The front of the car is badly damaged, and nobody was hurt.",
            "The bumper and one wing are crushed, and nobody else was involved.",
            "The car was towed away and cannot be used.",
        ),
        (
            "A friend without a driving licence was at the wheel, because I "
            "had let him borrow the car.",
            "My cousin was driving; he does not have a driving licence.",
        ),
    ),
    ("wear_and_tear", "storm"): Scene(
        (
            "On {date} a severe storm hit {city} and part of {roof} gave way.",
            "During the storm on {date} rain and wind tore a large hole in "
            "{roof} in {city}.",
            "On {date} strong wind in {city} lifted the tiles off {roof}.",
        ),
        (
            "Rain came in through the roof and the ceiling below it is wet.",
            "Water ran down the walls, and part of the ceiling has come down.",
        ),
        (
            "The roof was already rotten in several places and worn out from age.",
            "The roof tiles were old and rotten before the storm and had not "
            "been repaired for years.",
        ),
    ),
    ("wear_and_tear", "burst_pipe"): Scene(
        (
            "On {date} an old pipe in the wall of {subject} in {city} burst "
            "and water poured into the room.",
            "On {date} I came home to {subject} in {city} and found that an "
            "old pipe had burst in the bathroom.",
            "On {date} an old pipe under the floor of {subject} in {city} "
            "gave way and flooded the hallway.",
        ),
        (
            "The plumber shut off the water, and the floor and the walls are wet.",
            "The flooring is swollen and the walls are damp in two rooms.",
        ),
        (
            "The pipe was old, rotten with corrosion and worn out before it burst.",
            "The old pipe had rotten through at the joint long before it gave way.",
        ),
    ),
    ("gradual_leak", "burst_pipe"): Scene(
        (
            "On {date} the plaster on a bathroom wall of {subject} in {city} "
            "gave way, and the wall behind it was soaked.",
            "On {date} I found a large damp patch on a wall of {subject} in "
            "{city}, and the paint came off in sheets.",
            "On {date} the plumber opened a bathroom wall of {subject} in "
            "{city} and found a pipe that was dripping.",
        ),
        (
            "The wall and the floor next to it are wet through.",
            "The flooring is swollen and the skirting boards are ruined.",
        ),
        (
            "The pipe had been leaking slowly behind the wall for months, and "
            "I only noticed it when the damp showed.",
            "The leak had been going on for months before I noticed it; the "
            "wall was wet through by then.",
        ),
    ),
}
# Reasons for a late report. Each puts the delay after the loss, so it fits any
# peril: theft, fire and burglary all leave the claimant able to act at first.
DELAY_REASONS = (
    "I could not report it earlier because I left on a long business trip "
    "abroad soon afterwards.",
    "I am sorry for the delay; I was in hospital for several weeks afterwards "
    "and could not deal with the claim.",
    "I did not realise that I had to report the loss so quickly.",
    "The delay is because I was away on a long family stay abroad afterwards "
    "and could not deal with it.",
)


def describe(
    rng: random.Random,
    line: str,
    peril: str,
    *,
    subject: str,
    city: str,
    loss_date: date,
    circumstance: str | None = None,
    delayed: bool = False,
) -> str:
    """A first-person description of two to four sentences."""
    scene = _scene(line, peril, circumstance)
    slots = {
        "date": date_text(loss_date),
        "city": city,
        "subject": subject,
        "roof": _roof(subject),
    }
    sentences = [
        rng.choice(scene.openers).format(**slots),
        rng.choice(scene.details),
    ]
    if circumstance is not None:
        sentences.append(rng.choice(scene.sentences))
    if delayed:
        sentences.append(rng.choice(DELAY_REASONS))
    return " ".join(sentences)


def _scene(line: str, peril: str, circumstance: str | None) -> Scene:
    if circumstance is None:
        openers, details = TEMPLATES[(line, peril)]
        return Scene(openers, details, ())
    try:
        return CIRCUMSTANCE_SCENES[(circumstance, peril)]
    except KeyError:
        raise ValueError(
            f"no scene for circumstance {circumstance!r} under {peril!r}"
        ) from None


def _roof(subject: str) -> str:
    """An apartment has no roof of its own; a house does."""
    if subject.endswith("apartment"):
        return f"the roof above {subject}"
    return f"the roof of {subject}"
