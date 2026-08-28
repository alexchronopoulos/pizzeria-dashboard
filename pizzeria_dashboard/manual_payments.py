from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable


# Curated to stay short, friendly, easy to say aloud, and unambiguous when
# typed into Square's Ticket Name field. The Cartesian product provides more
# than a thousand distinct names for any one service date.
MATCH_TOKEN_ADJECTIVES = (
    "Bouncy",
    "Bright",
    "Cheery",
    "Cozy",
    "Crispy",
    "Dapper",
    "Dreamy",
    "Fizzy",
    "Golden",
    "Groovy",
    "Happy",
    "Jazzy",
    "Jolly",
    "Lucky",
    "Merry",
    "Mighty",
    "Nimble",
    "Peppy",
    "Rosy",
    "Saucy",
    "Snappy",
    "Sunny",
    "Swift",
    "Toasty",
    "Velvet",
    "Wiggly",
    "Zesty",
)

MATCH_TOKEN_NOUNS = (
    "Acorn",
    "Badger",
    "Basil",
    "Biscuit",
    "Bluebird",
    "Bumblebee",
    "Clover",
    "Comet",
    "Coyote",
    "Cricket",
    "Dandelion",
    "Firefly",
    "Fox",
    "Gnocchi",
    "Goose",
    "Lantern",
    "Lemon",
    "Marigold",
    "Marmot",
    "Moonbeam",
    "Mushroom",
    "Olive",
    "Oregano",
    "Otter",
    "Pepper",
    "Pigeon",
    "Pickle",
    "Porcupine",
    "Raccoon",
    "Radish",
    "Robin",
    "Rocket",
    "Sparrow",
    "Starling",
    "Sunflower",
    "Tomato",
    "Turnip",
    "Walnut",
    "Wombat",
    "Zucchini",
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def normalized_ticket_words(value: str | None) -> tuple[str, ...]:
    """Return case-insensitive words with punctuation treated as separators."""
    return tuple(_WORD_RE.findall(str(value or "").casefold()))


def ticket_name_contains_match_token(
    ticket_name: str | None,
    match_token: str,
) -> bool:
    """Match a complete two-word token anywhere in a Square Ticket Name.

    Staff can type ``Toasty Pigeon``, ``toasty-pigeon``, or
    ``Alex / TOASTY_PIGEON`` without changing the result. Partial words never
    match, which prevents ordinary customer names from accidentally linking.
    """
    ticket_words = normalized_ticket_words(ticket_name)
    token_words = normalized_ticket_words(match_token)
    if not ticket_words or not token_words or len(token_words) > len(ticket_words):
        return False
    width = len(token_words)
    return any(
        ticket_words[index : index + width] == token_words
        for index in range(len(ticket_words) - width + 1)
    )


def generate_match_token(
    used_tokens: Iterable[str],
    *,
    randbelow: Callable[[int], int] = secrets.randbelow,
) -> str:
    """Choose a memorable adjective/noun pair unused on the service date."""
    used = {
        normalized_ticket_words(token)
        for token in used_tokens
        if normalized_ticket_words(token)
    }
    total = len(MATCH_TOKEN_ADJECTIVES) * len(MATCH_TOKEN_NOUNS)
    start = randbelow(total)
    for offset in range(total):
        index = (start + offset) % total
        adjective = MATCH_TOKEN_ADJECTIVES[index // len(MATCH_TOKEN_NOUNS)]
        noun = MATCH_TOKEN_NOUNS[index % len(MATCH_TOKEN_NOUNS)]
        token = f"{adjective} {noun}"
        if normalized_ticket_words(token) not in used:
            return token
    raise RuntimeError("No manual-order match names remain for this service date.")
