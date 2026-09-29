"""The prose of the policy wordings.

Plain text, no randomness. Each entry is one paragraph; the renderer wraps it.
Cover clauses say what is covered and nothing negative: what is not covered
lives only in section 3, so the labels and the wording cannot disagree.
"""


def flow(text: str) -> str:
    """Collapse the line breaks of a triple-quoted literal into one paragraph."""
    return " ".join(text.split())


DISCLAIMER = flow("""
    This document is synthetic and fictional. Meridian Insurance is an invented
    insurer, and nothing in this wording is a real contract or real cover.
""")

# -- section 1: definitions ---------------------------------------------------
YOU_AND_WE = flow("""
    In this wording "you" means the policyholder named on the schedule, and "we"
    means Meridian Insurance. The schedule is the document that shows your
    product, your dates, your deductible and your sum insured or limit. It forms
    part of this wording.
""")
INSURED_OBJECT = {
    "motor": (
        "The insured vehicle",
        flow("""
            The insured vehicle is the car described on the schedule by make,
            model, year and registration. It includes the parts and accessories
            fitted to it by the manufacturer and any others listed on the
            schedule.
        """),
    ),
    "home": (
        "The insured home",
        flow("""
            The insured home is the apartment or house at the address on the
            schedule. It includes the fixtures and fittings of the building and
            the contents that you keep in it.
        """),
    ),
}
DEFINITION_DEDUCTIBLE = flow("""
    The deductible is the part of each claim that you pay yourself. We take it
    off the amount we would otherwise pay, as clause 4.1 explains.
""")
SUM_INSURED = {
    "vehicle": flow("""
        The sum insured is the value of the insured vehicle that is shown on the
        schedule. It is the most we pay for one claim under this product.
    """),
    "home": flow("""
        The sum insured is the amount for which the insured home is insured, as
        shown on the schedule. It is the most we pay for one claim under this
        product.
    """),
    "none": flow("""
        This product has no sum insured. The most we pay is the limit in
        clause 4.2.
    """),
}
DEFINITION_PERIOD = flow("""
    The period of cover is the time from the start date to the end date shown on
    the schedule, both days included. Clause 6.1 explains it in full.
""")

# -- section 2: what is covered -----------------------------------------------
COVER_INTRO = flow("""
    The clauses below list what {name} covers. Each clause names one event. We
    pay for a covered event up to the limit in clause 4.2 and after the
    deductible in clause 4.1.
""")
COVER = {
    ("motor", "collision"): flow("""
        We cover damage to the insured vehicle caused by a collision with
        another vehicle, a person, an animal or a fixed object. This includes
        damage caused by overturning or by leaving the road. We pay the cost of
        repair, or the market value of the vehicle just before the loss if
        repair costs more than the vehicle is worth.
    """),
    ("motor", "theft"): flow("""
        We cover the loss of the insured vehicle through theft or attempted
        theft, and damage caused to it during the theft. If the vehicle is not
        found within 28 days, we pay its market value just before the loss. If
        it is found damaged, we pay the cost of repair.
    """),
    ("motor", "fire"): flow("""
        We cover damage to or loss of the insured vehicle caused by fire,
        explosion, lightning or a short circuit. We also pay the reasonable
        cost of moving the vehicle after the fire and of removing the wreck.
    """),
    ("motor", "glass"): flow("""
        We cover breakage of the windscreen, the side windows, the rear window
        and the glass roof of the insured vehicle, whatever the cause. We pay
        for repair where the glass can be repaired and for replacement where it
        cannot.
    """),
    ("motor", "storm"): flow("""
        We cover damage to the insured vehicle caused by storm, hail or snow
        load, or by trees, branches and roof parts blown onto it. A storm is a
        wind of at least Beaufort force 8. We pay the cost of repair or, if
        repair is not possible, the market value just before the loss.
    """),
    ("motor", "third_party_liability"): flow("""
        We pay the compensation that you, or a person driving with your
        permission, are legally liable to pay for injury to other people or for
        damage to their property caused by the use of the insured vehicle. We
        also pay the reasonable costs of defending such a claim, if we agreed to
        them in advance. The limit in clause 4.2 applies to all claims that arise
        from one event.
    """),
    ("home", "fire"): flow("""
        We cover loss of or damage to the insured home and its contents caused by
        fire, explosion, lightning or smoke. We also pay the reasonable cost of
        putting out the fire and of clearing debris. Reasonable costs of
        temporary accommodation are covered while the home cannot be lived in,
        within the limit in clause 4.2.
    """),
    ("home", "storm"): flow("""
        We cover loss of or damage to the insured home and its contents caused by
        storm, hail or snow load. A storm is a wind of at least Beaufort force 8.
        We also cover damage caused by objects that the storm blows onto the
        home, such as branches or roof parts.
    """),
    ("home", "flood"): flow("""
        We cover loss of or damage to the insured home and its contents caused by
        flood. A flood is water that overflows from a river, lake or sea, or
        that collects on the ground after heavy rain, and enters the home. We
        also pay the reasonable cost of pumping out the water and of drying the
        home.
    """),
    ("home", "burst_pipe"): flow("""
        We cover loss of or damage to the insured home and its contents caused by
        the sudden bursting or freezing of a pipe, tank or heating system inside
        the home. We pay for the damage that the escaping water causes and for
        the reasonable cost of finding the place where the water escaped.
    """),
    ("home", "burglary"): flow("""
        We cover theft of contents from the insured home after forced entry, and
        damage caused to the home during a burglary or an attempt. We also pay
        for replacing the locks when the keys were taken. Contents are covered
        wherever they are kept inside the home.
    """),
    ("home", "accidental_damage"): flow("""
        We cover sudden and unforeseen physical damage to the insured home and
        its fixtures that is caused by an accident, such as a heavy object that
        is dropped or a hole that is drilled through a hidden pipe. We pay for
        repair or, if repair is not possible, for replacement of the damaged
        part.
    """),
}

# -- section 3: what is not covered -------------------------------------------
EXCLUSION_INTRO = flow("""
    The clauses below list what {name} does not cover. An exclusion applies
    only to the claims that it names.
""")
EXCLUSION = {
    "own_vehicle_damage": flow("""
        This product insures only your legal liability to other people. It does
        not pay for damage to, or the loss of, the insured vehicle itself,
        whatever the cause. If you want cover for your own vehicle, you need a
        comprehensive product such as Motor Comprehensive.
    """),
    "racing": flow("""
        We do not cover loss or liability that arises while the insured vehicle
        takes part in a race, rally, speed test, timed session or track day, or
        while it is prepared for one, on or off a public road. This applies
        even if the event is meant for amateurs or for driver training.
    """),
    "driving_under_influence": flow("""
        We do not cover loss that occurs while the driver is under the influence
        of alcohol, drugs or medicines that impair driving, or is above the
        legal limit of the place where the loss happens. This also applies if
        the driver refuses a test that the police ask for.
    """),
    "unlicensed_driver": flow("""
        We do not cover loss that occurs while the insured vehicle is driven by
        a person who does not hold a valid driving licence for that category of
        vehicle, or who is banned from driving. This also applies if you knew
        that and let the person drive.
    """),
    "flood": flow("""
        This product does not cover loss or damage caused by flood, meaning
        water that overflows from a river, lake or sea, or that collects on the
        ground and enters the home. Flood cover is available under Home Plus.
    """),
    "accidental_damage": flow("""
        This product does not cover sudden and unforeseen damage caused by an
        accident, such as a dropped object or damage done during do-it-yourself
        work. Accidental damage cover is available under Home Plus.
    """),
    "wear_and_tear": flow("""
        We do not pay for damage that results from gradual deterioration, rot,
        corrosion, age or lack of maintenance. If a storm or a burst pipe only
        exposes a part that was already worn out, we do not pay for that part.
        Damage that a part in good condition would have withstood is treated as
        wear and tear.
    """),
    "gradual_leak": flow("""
        We do not cover damage caused by water that escapes slowly or again and
        again over a period of time, such as a dripping pipe, a seeping joint or
        a leak behind a wall that goes on for weeks or months. Only a sudden
        escape of water is covered.
    """),
}
APPLIES_PERIL = (
    "This exclusion applies to claims for {perils}, which this product does not cover."
)
APPLIES_CIRCUMSTANCE = "This exclusion applies to claims for {perils}."

# -- section 4: deductible and limits -----------------------------------------
DEDUCTIBLE_WITH_AMOUNT = flow("""
    You pay the first {amount} of every claim. We work out what we pay as the
    smaller of the amount of the loss and the limit in clause 4.2, less the
    deductible. If the loss is not more than the deductible, we pay nothing.
""")
DEDUCTIBLE_NONE = flow("""
    This product has no deductible; the deductible is EUR 0. We pay the smaller
    of the amount of the loss and the limit in clause 4.2.
""")
LIMIT_FIXED = flow("""
    The most we pay for all claims that arise from one event is {amount}. If the
    claims are larger, you bear the difference.
""")
LIMIT_SUM_INSURED = flow("""
    The most we pay for one claim is the sum insured shown on the schedule. If
    the loss is larger, you bear the difference.
""")

# -- section 5: making a claim ------------------------------------------------
REPORTING = flow("""
    Report a claim to us within {days} days of the loss. Tell us what happened,
    when and where, and send the documents that the clauses below list for the
    event. A report that reaches us later does not by itself void your cover,
    but an adjuster reviews the claim before we decide it.
""")
DOCUMENTS_INTRO = "For a claim for {peril} we need the following documents."
DOCUMENT_DESCRIPTION = {
    "police_report": flow("""
        the reference number or a copy of the report that you made to the
        police.
    """),
    "photos": flow("""
        clear photographs of the damage, taken before any repair or clearing up.
    """),
    "repair_estimate": flow("""
        a written estimate from a repairer or contractor that shows the cost.
    """),
    "accident_statement": flow("""
        your written account of the accident, with the details of the other
        people involved.
    """),
}
DOCUMENTS_OUTRO = flow("""
    We cannot decide the claim until these documents have reached us. We may ask
    for more if what you send does not allow us to decide.
""")

# -- section 6: period of cover ------------------------------------------------
PERIOD = flow("""
    Cover runs from the start date to the end date shown on the schedule, both
    days included. A loss that happens before the start date or after the end
    date falls outside the period of cover. To stay covered after the end date
    you need to renew the policy.
""")
LAPSE = flow("""
    If a premium is not paid by its due date and stays unpaid after our written
    reminder, the policy lapses. Cover ends on the lapse date recorded by us, so
    a loss that happens on or after the lapse date is not covered. A loss that
    happened before the lapse date stays covered.
""")
