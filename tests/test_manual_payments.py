from pizzeria_dashboard.manual_payments import (
    MATCH_TOKEN_NOUNS,
    generate_match_token,
    normalized_ticket_words,
    ticket_name_contains_match_token,
)


def test_memorable_match_token_generation_is_deterministic_for_tests() -> None:
    assert generate_match_token((), randbelow=lambda _: 0) == "Bouncy Acorn"
    assert (
        generate_match_token(("Bouncy Acorn",), randbelow=lambda _: 0)
        == "Bouncy Badger"
    )
    assert (
        generate_match_token((), randbelow=lambda _: len(MATCH_TOKEN_NOUNS))
        == "Bright Acorn"
    )


def test_ticket_name_matching_accepts_human_punctuation_and_case() -> None:
    assert ticket_name_contains_match_token(
        "Alex / TOASTY_PIGEON",
        "Toasty Pigeon",
    )
    assert ticket_name_contains_match_token(
        "Pickup - toasty-pigeon - Alex",
        "Toasty Pigeon",
    )
    assert normalized_ticket_words("Toasty___Pigeon") == ("toasty", "pigeon")


def test_ticket_name_matching_rejects_partial_or_reordered_words() -> None:
    assert not ticket_name_contains_match_token("Toasty Pigeons", "Toasty Pigeon")
    assert not ticket_name_contains_match_token("Pigeon Toasty", "Toasty Pigeon")
    assert not ticket_name_contains_match_token("Toasty Alex Pigeon", "Toasty Pigeon")
    assert not ticket_name_contains_match_token(None, "Toasty Pigeon")
