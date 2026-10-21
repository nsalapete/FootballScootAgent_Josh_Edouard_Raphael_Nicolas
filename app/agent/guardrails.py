"""Validation of LLM-generated Cypher. Owner: D.

The allow-lists below mirror docs/schema.md: a generated query may only touch these labels and types.

The validator never runs the query. It first blanks string literals and comments (so that a keyword hidden
in a string cannot fool it, and a `LIMIT` hidden in a comment does not count), then tokenizes the rest and
applies every rule. Each broken rule adds one reason of the form `<code>: <explanation>`.
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel, Field

ALLOWED_LABELS = frozenset({"Player", "Club", "Competition", "Country", "Transfer", "Valuation", "Article", "Rumour"})
ALLOWED_RELATIONSHIPS = frozenset(
    {
        "MADE",
        "FROM",
        "TO",
        "PLAYS_FOR",
        "HAS_VALUATION",
        "CITIZEN_OF",
        "PLAYS_IN",
        "IN_COUNTRY",
        "PLAYED_WITH",
        "SENDS_TO",
        "REPORTS",
        "ABOUT",
        "TARGET",
        "CONFIRMED_BY",
        "PREDICTED_MOVE",
        "PREDICTED_LINK",
    }
)

MAX_QUERY_LENGTH = 2000

# Clauses that write to the graph or the schema (INSERT is the GQL synonym of CREATE).
WRITE_KEYWORDS = ("CREATE", "MERGE", "DELETE", "DETACH", "SET", "REMOVE", "DROP", "FOREACH", "INSERT")
# Administration, database switching and batching clauses.
ADMIN_KEYWORDS = (
    "GRANT",
    "DENY",
    "REVOKE",
    "ALTER",
    "RENAME",
    "SHOW",
    "TERMINATE",
    "USE",
    "TRANSACTIONS",
    "PERIODIC",
)

# Verbs that only administer when followed by DATABASE or SERVER (`start` alone is a common variable name).
ADMIN_VERBS = ("START", "STOP", "ENABLE", "DEALLOCATE", "REALLOCATE")
ADMIN_OBJECTS = ("DATABASE", "DATABASES", "SERVER", "SERVERS")

# Read-only procedures a generated query may CALL. No GDS procedure: GDS 2.x needs a named in-memory projection,
# which `GdsService.refresh` drops once its scores are written; generated queries read those stored properties.
ALLOWED_PROCEDURES = frozenset(
    {"db.index.fulltext.queryNodes", "db.labels", "db.relationshipTypes", "db.propertyKeys"}
)

# Namespaces whose functions or procedures write, run dynamic Cypher, reach the file system or administer.
DENIED_NAMESPACES = (
    "dbms.",
    "db.create.",
    "gds.graph.",
    "apoc.cypher.",
    "apoc.load.",
    "apoc.periodic.",
    "apoc.export.",
    "apoc.import.",
    "apoc.create.",
    "apoc.merge.",
    "apoc.refactor.",
    "apoc.trigger.",
    "apoc.schema.",
    "apoc.nodes.delete",
    "apoc.do.",
    "apoc.custom.",
    "apoc.systemdb.",
    "apoc.atomic.",
    "apoc.lock.",
    "apoc.util.sleep",
)

_TOKEN = re.compile(
    r"\s+|(?P<tok>\$[A-Za-z_]\w*|[A-Za-z_]\w*|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"|\.\.|<-|->|<=|>=|<>|=~|::|\+=|[^\s\w])"
)
_STRING_PLACEHOLDER = "__str__"


class ValidationResult(BaseModel):
    """Outcome of a validation: `valid` is False as soon as one rule is broken; `reasons` lists them all."""

    valid: bool
    query: str
    reasons: list[str] = Field(default_factory=list)


@dataclass
class _Bracket:
    """An open bracket during the scan, and whether it opens a relationship pattern `-[...]-`."""

    char: str
    relationship: bool = False


def strip_literals(query: str) -> tuple[str, list[str]]:
    """Blank comments, replace string literals by a placeholder and unquote back-ticked names.

    Returns the cleaned text and a list of problems (unterminated string, comment or name).
    """
    out: list[str] = []
    problems: list[str] = []
    index, size = 0, len(query)
    while index < size:
        char = query[index]
        pair = query[index : index + 2]
        if pair == "//":
            end = query.find("\n", index)
            index = size if end == -1 else end
            out.append(" ")
        elif pair == "/*":
            end = query.find("*/", index + 2)
            if end == -1:
                problems.append("unterminated: block comment is never closed")
                break
            index = end + 2
            out.append(" ")
        elif char in "'\"":
            end = _closing_quote(query, index)
            if end == -1:
                problems.append("unterminated: string literal is never closed")
                break
            index = end + 1
            out.append(f" {_STRING_PLACEHOLDER} ")
        elif char == "`":
            end = query.find("`", index + 1)
            if end == -1:
                problems.append("unterminated: back-ticked name is never closed")
                break
            # A quoted name keeps its letters so that the label and keyword rules still see it.
            out.append(re.sub(r"\W", "_", query[index + 1 : end]) or "_")
            index = end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out), problems


def _closing_quote(query: str, start: int) -> int:
    """Index of the quote closing the string opened at `start`, honouring backslash escapes; -1 if none."""
    quote = query[start]
    index = start + 1
    while index < len(query):
        if query[index] == "\\":
            index += 2
            continue
        if query[index] == quote:
            return index
        index += 1
    return -1


def tokenize(text: str) -> list[str]:
    """Split cleaned Cypher into identifiers, parameters, numbers and operators."""
    return [match.group("tok") for match in _TOKEN.finditer(text) if match.group("tok")]


class CypherValidator:
    """Rejects any generated query that writes, leaves the schema, or could run unbounded."""

    def __init__(self, max_hops: int = 6, max_limit: int = 100):
        self.max_hops = max_hops
        self.max_limit = max_limit
        self.allowed_labels = ALLOWED_LABELS
        self.allowed_relationships = ALLOWED_RELATIONSHIPS

    def validate(self, query: str) -> ValidationResult:
        """Check `query` against every rule without running it."""
        query = (query or "").strip()
        if query.endswith(";"):
            query = query[:-1].rstrip()
        if not query:
            return ValidationResult(valid=False, query=query, reasons=["empty: the query is empty"])
        if len(query) > MAX_QUERY_LENGTH:
            reason = f"too_long: the query exceeds {MAX_QUERY_LENGTH} characters"
            return ValidationResult(valid=False, query=query, reasons=[reason])
        text, reasons = strip_literals(query)
        if reasons:
            return ValidationResult(valid=False, query=query, reasons=reasons)
        tokens = tokenize(text)
        reasons += self._check_statements(tokens)
        reasons += self._check_parameters(tokens)
        reasons += self._check_keywords(tokens)
        reasons += self._check_procedures(tokens)
        reasons += self._check_patterns(tokens)
        reasons += self._check_quantifiers(text)
        reasons += self._check_limit(tokens)
        unique = list(dict.fromkeys(reasons))
        return ValidationResult(valid=not unique, query=query, reasons=unique)

    @staticmethod
    def _check_statements(tokens: list[str]) -> list[str]:
        """Only one statement: any semicolon left after removing the trailing one means several."""
        if ";" in tokens:
            return ["multiple_statements: only one statement is allowed (found ';')"]
        return []

    @staticmethod
    def _check_parameters(tokens: list[str]) -> list[str]:
        """The fallback runs a generated query without parameters: a `$name` would fail with ParameterMissing."""
        if any(token.startswith("$") and len(token) > 1 for token in tokens):
            return ["parameter: generated queries cannot use $parameters, inline the literal value"]
        return []

    @staticmethod
    def _check_keywords(tokens: list[str]) -> list[str]:
        """Write and administration clauses, ignoring property keys (`n.set`) and parameters."""
        reasons = []
        for position, token in enumerate(tokens):
            if position and tokens[position - 1] in (".", ":"):
                continue
            upper = token.upper()
            if upper in WRITE_KEYWORDS:
                reasons.append(f"write: {upper} is not allowed, generated queries are read-only")
            elif upper == "LOAD" and position + 1 < len(tokens) and tokens[position + 1].upper() == "CSV":
                reasons.append("write: LOAD CSV is not allowed (file and network access)")
            elif upper in ADMIN_KEYWORDS:
                reasons.append(f"admin: {upper} is not allowed (administration or batching clause)")
            elif upper in ADMIN_VERBS and position + 1 < len(tokens) and tokens[position + 1].upper() in ADMIN_OBJECTS:
                reasons.append(f"admin: {upper} {tokens[position + 1].upper()} is not allowed")
        return reasons

    def _check_procedures(self, tokens: list[str]) -> list[str]:
        """Procedures must be allow-listed; functions must stay out of the denied namespaces."""
        reasons = []
        position = 0
        while position < len(tokens):
            name, end = _dotted_name(tokens, position)
            if "." not in name:
                position += 1
                continue
            is_call = position > 0 and tokens[position - 1].upper() == "CALL"
            if any(name.lower().startswith(prefix.lower()) for prefix in DENIED_NAMESPACES):
                reasons.append(f"procedure: {name} is not allowed (writes, dynamic Cypher or administration)")
            elif is_call and not self._procedure_allowed(name):
                reasons.append(f"procedure: CALL {name} is not in the read-only allow-list")
            position = end
        return reasons

    @staticmethod
    def _procedure_allowed(name: str) -> bool:
        """True for allow-listed procedures only."""
        return name in ALLOWED_PROCEDURES

    def _check_patterns(self, tokens: list[str]) -> list[str]:
        """Labels and relationship types against the allow-lists, and variable-length bounds."""
        reasons: list[str] = []
        stack: list[_Bracket] = []
        for position, token in enumerate(tokens):
            previous = tokens[position - 1] if position else ""
            if token in "([{":
                stack.append(_Bracket(token, relationship=token == "[" and previous in ("-", "<-")))
            elif token in ")]}":
                if stack:
                    stack.pop()
            elif token == ":" and not _is_map_key(tokens, position, stack):
                in_relationship = bool(stack) and stack[-1].relationship
                reasons += self._check_names(tokens, position + 1, in_relationship)
            elif token == "*" and stack and stack[-1].relationship:
                reasons += self._check_hops(tokens, position + 1)
        return reasons

    def _check_names(self, tokens: list[str], position: int, relationship: bool) -> list[str]:
        """Check the label or type expression starting at `position` (`A|B`, `A&B`, `!A`, `A:B`)."""
        reasons = []
        allowed = self.allowed_relationships if relationship else self.allowed_labels
        kind = "relationship" if relationship else "label"
        while position < len(tokens):
            token = tokens[position]
            if token in ("!", "|", "&", ":", "%"):
                position += 1
                continue
            if token.startswith("$"):
                reasons.append(f"dynamic: dynamic {kind}s are not allowed")
            elif token == "(":
                reasons.append(f"{kind}: parenthesised {kind} expressions are not allowed")
                break
            elif not re.fullmatch(r"[A-Za-z_]\w*", token):
                break
            elif token not in allowed:
                reasons.append(f"{kind}: {token} is not in the schema allow-list")
            position += 1
            if position >= len(tokens) or tokens[position] not in ("|", "&", ":"):
                break
            # `[x IN list WHERE x:Player | x.name]`: this `|` starts the projection, not another label.
            if tokens[position] == "|" and position + 2 < len(tokens) and tokens[position + 2] in (".", "(", "]"):
                break
        return reasons

    def _check_hops(self, tokens: list[str], position: int) -> list[str]:
        """Bounds of `*`, `*n`, `*n..`, `*..m`, `*n..m` inside a relationship pattern."""
        lower = upper = None
        if position < len(tokens) and tokens[position].isdigit():
            lower = upper = int(tokens[position])
            position += 1
        if position < len(tokens) and tokens[position] == "..":
            upper = None
            if position + 1 < len(tokens) and tokens[position + 1].isdigit():
                upper = int(tokens[position + 1])
        elif lower is None:
            upper = None
        if upper is None:
            return [f"unbounded_path: variable-length relationships need an upper bound (at most {self.max_hops})"]
        if upper > self.max_hops:
            return [f"path_too_long: {upper} hops requested, at most {self.max_hops} allowed"]
        return []

    def _check_quantifiers(self, text: str) -> list[str]:
        """Quantified path patterns (`(...){1,3}`, `(...)+`, `-[:R]->*`) must be bounded too."""
        reasons = []
        for match in re.finditer(r"[)\]>-]\s*\{\s*(\d*)\s*(,?)\s*(\d*)\s*\}", text):
            lower, comma, upper = match.groups()
            bound = upper if comma else lower
            if not bound:
                reasons.append(
                    f"unbounded_path: path quantifiers need an upper bound (at most {self.max_hops})"
                )
            elif int(bound) > self.max_hops:
                reasons.append(f"path_too_long: {bound} hops requested, at most {self.max_hops} allowed")
        if re.search(r"(\)\s*\)|->|-)\s*[+*]\s*(\(|-|<)", text):
            reasons.append(f"unbounded_path: `+` and `*` path quantifiers are unbounded (at most {self.max_hops})")
        return reasons

    def _check_limit(self, tokens: list[str]) -> list[str]:
        """Each UNION branch must end with `RETURN ... LIMIT n`, every LIMIT a literal of at most `max_limit`."""
        reasons = []
        for position, token in enumerate(tokens):
            if token.upper() != "LIMIT":
                continue
            value = tokens[position + 1] if position + 1 < len(tokens) else ""
            following = tokens[position + 2] if position + 2 < len(tokens) else ""
            if not value.isdigit() or following in ("+", "-", "*", "/", "^", "%", ".", "("):
                reasons.append("limit_not_literal: LIMIT must be followed by an integer literal")
            elif int(value) > self.max_limit:
                reasons.append(f"limit_too_high: LIMIT {value} exceeds the maximum of {self.max_limit}")
        for branch in _top_level_branches(tokens):
            upper = [token.upper() for token in branch]
            if "RETURN" not in upper:
                reasons.append("no_return: the query must end with a RETURN clause")
                continue
            last_return = len(upper) - 1 - upper[::-1].index("RETURN")
            if "LIMIT" not in upper[last_return:]:
                reasons.append(f"no_limit: the final RETURN needs a LIMIT (at most {self.max_limit})")
        return reasons


def _dotted_name(tokens: list[str], position: int) -> tuple[str, int]:
    """Read `a.b.c` starting at `position`; return the name and the index after it."""
    if not re.fullmatch(r"[A-Za-z_]\w*", tokens[position]):
        return tokens[position], position + 1
    parts = [tokens[position]]
    end = position + 1
    while end + 1 < len(tokens) and tokens[end] == "." and re.fullmatch(r"[A-Za-z_]\w*", tokens[end + 1]):
        parts.append(tokens[end + 1])
        end += 2
    name = ".".join(parts)
    # `p.name` is a property access, not a call: only names followed by `(` (or after CALL) are callables.
    is_callable = end < len(tokens) and tokens[end] == "("
    after_call = position > 0 and tokens[position - 1].upper() == "CALL"
    if not (is_callable or after_call):
        return parts[0], position + 1
    return name, end


def _is_map_key(tokens: list[str], position: int, stack: list[_Bracket]) -> bool:
    """True when the colon at `position` separates a key from its value in a map literal `{key: value}`."""
    if not stack or stack[-1].char != "{" or position < 2:
        return False
    return tokens[position - 2] in ("{", ",") and re.fullmatch(r"[A-Za-z_]\w*", tokens[position - 1]) is not None


def _top_level_branches(tokens: list[str]) -> list[list[str]]:
    """Split the outer query on `UNION` at bracket depth 0, keeping only depth-0 tokens of each branch."""
    branches: list[list[str]] = [[]]
    depth = 0
    for token in tokens:
        if token in "([{":
            depth += 1
        elif token in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0 and token.upper() == "UNION":
            branches.append([])
        elif depth == 0:
            branches[-1].append(token)
    return branches
