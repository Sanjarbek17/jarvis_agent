"""Finding the person Sanjar means ("Olimjon dost") among the Jarvis account's Telegram users."""


def name_of(user) -> str:
    return " ".join(filter(None, [user.first_name, user.last_name])) or user.username or str(user.id)


def _words(user) -> str:
    return " ".join(filter(None, [user.first_name, user.last_name, user.username])).lower()


def match(users: list, query: str) -> tuple[list, list]:
    """(sure, maybe): `sure` = every word of the query is in the name (exact name wins);
    `maybe` = only some words match, to suggest but never to call without asking."""
    q = " ".join(query.lower().removeprefix("@").split())
    words = q.split()
    if not words:
        return [], []
    exact = [u for u in users if name_of(u).lower() == q or (u.username or "").lower() == q]
    if exact:
        return exact, []
    sure = [u for u in users if all(w in _words(u) for w in words)]
    maybe = [] if sure else [u for u in users if any(w in _words(u).split() for w in words)]
    return sure, maybe
