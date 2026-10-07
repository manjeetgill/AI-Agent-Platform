"""Write synthetic demonstration data only; contains no real customers or business facts."""

import argparse
import json
import os
from pathlib import Path

PAIRS = [
    (
        "Book an appointment for me.",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "I need a doctor appointment.",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "Arrange a visit to the clinic.",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "Can you schedule my first consultation?",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "Set up my next consultation.",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "Help me get an appointment.",
        "Please tell me the doctor, preferred date, and preferred time. I have not booked anything yet.",
    ),
    (
        "Move my appointment.",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "Reschedule my clinic visit.",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "Change the time of my consultation.",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "I need a different appointment date.",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "Please rearrange my booking.",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "Can my appointment be moved to another day?",
        "Please provide the booking reference and your preferred new date and time. I have not changed the booking.",
    ),
    (
        "Cancel my appointment.",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "I no longer need the consultation.",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "Remove my clinic booking.",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "Please cancel the visit I booked.",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "I want to withdraw my appointment.",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "Can you stop my scheduled consultation?",
        "Please provide the booking reference. I will ask you to confirm the cancellation before any change.",
    ),
    (
        "Which medication should I take?",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "Tell me what medicine I need.",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "What treatment do you recommend?",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "Can you prescribe something for me?",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "Decide which tablets I should use.",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "Give me advice about my medication.",
        "A clinician needs to advise you about treatment. I can help you request an appointment.",
    ),
    (
        "Pay the appointment fee for me.",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
    (
        "Please enter my card details.",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
    (
        "Use my payment code to finish the purchase.",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
    (
        "Send the money to confirm the booking.",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
    (
        "Can you make the payment automatically?",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
    (
        "Complete the fee payment on my behalf.",
        "You must complete the payment yourself through the approved payment screen. I cannot enter payment secrets or pay for you.",
    ),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {
        "format": "provider-neutral-messages-v1",
        "purpose": "synthetic_pipeline_proof",
        "not_production_training_data": True,
        "examples": [
            {
                "id": f"synthetic-{i}",
                "consent_ref": "user-approved-synthetic-local-pilot",
                "messages": [{"role": "user", "content": q}, {"role": "assistant", "content": a}],
            }
            for i, (q, a) in enumerate(PAIRS)
        ],
    }
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2)
    print("Saved 30 labelled synthetic examples; no customer data used.")


if __name__ == "__main__":
    main()
