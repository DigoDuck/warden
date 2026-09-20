"""One matcher per key of a rule's `when` block.

Three kinds, and the choice of each is load-bearing:

- **equals** for names (`tool`, `user.role`): membership in a list, with `"*"` meaning any.
- **glob** for paths: `PurePosixPath.full_match`, never `fnmatch`. `fnmatch` lets `*` cross
  a `/`, so `src/*` there would match `src/a/b/secret.key`. In a policy engine that is a
  silent hole rather than a quirk.
- **regex** for free-form argument text (`args.cmd`), because a command line is not a path.

Rule authors have to know one thing about the glob matcher: `*` stops at a path separator,
so a deny written as `.env*` does **not** catch `config/.env`. Deny patterns are written
`**/...`, which matches zero or more leading segments and therefore both.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

WILDCARD = "*"

# The closed set of context fields a rule may name. Anything else fails at load time: a
# deny rule with a typo in its field would otherwise never match, and nobody would find out
# until the incident it was written to prevent.
KNOWN_FIELDS = frozenset({"tool", "path", "user.role", "user.id", "task_id"})
# Argument names are open, since they follow whatever tools exist.
ARGS_PREFIX = "args."


class UnknownFieldError(ValueError):
    """A rule names a context field the engine cannot resolve."""


class Matcher(Protocol):
    # A read-only property, not a plain attribute: the implementations are frozen
    # dataclasses, and a settable attribute in the Protocol would reject them.
    @property
    def field(self) -> str: ...

    def matches(self, value: Any) -> bool: ...


@dataclass(frozen=True)
class EqualsMatcher:
    field: str
    options: tuple[str, ...]

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        if WILDCARD in self.options:
            return True
        return str(value) in self.options


@dataclass(frozen=True)
class GlobMatcher:
    field: str
    patterns: tuple[str, ...]

    def matches(self, value: Any) -> bool:
        if value is None:
            # A tool with no path simply does not match a path rule. This is why
            # `never-read-secrets` does not accidentally deny `run_command`.
            return False
        candidate = PurePosixPath(str(value))
        return any(candidate.full_match(pattern) for pattern in self.patterns)


@dataclass(frozen=True)
class RegexMatcher:
    field: str
    pattern: re.Pattern[str]

    def matches(self, value: Any) -> bool:
        if value is None:
            return False
        return self.pattern.search(str(value)) is not None


def _as_tuple(spec: Any) -> tuple[str, ...]:
    if isinstance(spec, str):
        return (spec,)
    if isinstance(spec, Sequence):
        return tuple(str(item) for item in spec)
    return (str(spec),)


def build_matcher(field: str, spec: Any) -> Matcher:
    """Turn one `when` entry into the matcher that field uses."""
    if field.startswith(ARGS_PREFIX):
        return RegexMatcher(field=field, pattern=re.compile(str(spec)))
    if field not in KNOWN_FIELDS:
        known = ", ".join(sorted(KNOWN_FIELDS))
        raise UnknownFieldError(
            f"rule refers to unknown context field {field!r}; "
            f"known fields are {known}, plus any '{ARGS_PREFIX}<name>'"
        )
    if field == "path":
        return GlobMatcher(field=field, patterns=_as_tuple(spec))
    return EqualsMatcher(field=field, options=_as_tuple(spec))


def resolve_field(context: Any, field: str) -> Any:
    """Read a dotted field out of a PolicyContext."""
    if field.startswith(ARGS_PREFIX):
        return context.args.get(field[len(ARGS_PREFIX) :])
    current: Any = context
    for part in field.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current
