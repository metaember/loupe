"""Small S-expression reader for KiCad source generation and verification."""

import json
import re


class Q(str):
    """A quoted KiCad string, distinguished from an atom."""


def parse(text):
    tokens = iter(re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text))
    stack = [[]]
    for token in tokens:
        if token == "(":
            child = []
            stack[-1].append(child)
            stack.append(child)
        elif token == ")":
            if len(stack) == 1:
                raise ValueError("Unbalanced S-expression")
            stack.pop()
        else:
            stack[-1].append(Q(json.loads(token)) if token.startswith('"') else token)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("Unbalanced S-expression")
    return stack[0][0]


def dump(value):
    if isinstance(value, list):
        return "(" + " ".join(map(dump, value)) + ")"
    return json.dumps(value, ensure_ascii=False) if isinstance(value, Q) else str(value)


def children(node, key):
    return [x for x in node if isinstance(x, list) and x and x[0] == key]


def child(node, key):
    found = children(node, key)
    return found[0] if found else None
