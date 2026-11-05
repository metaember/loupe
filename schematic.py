"""Resolve KiCad symbol inheritance for code-generated native schematics."""

import copy
from pathlib import Path
from kicad_sexpr import Q, child, children, parse


def load_symbol(path, name, lib_id):
    definitions = {
        str(s[1]): s for s in children(parse(Path(path).read_text()), "symbol")
    }

    def resolve(n, chain=()):
        if n in chain:
            raise ValueError(f"symbol inheritance cycle: {chain + (n,)}")
        sym = copy.deepcopy(definitions[n])
        parent = child(sym, "extends")
        if parent:
            base = resolve(str(parent[1]), chain + (n,))
            old = str(base[1])
            base[1] = Q(n)
            for item in children(base, "symbol"):
                item[1] = Q(str(item[1]).replace(old + "_", n + "_", 1))
            for prop in children(sym, "property"):
                base[:] = [
                    x for x in base if not (isinstance(x, list) and x[:2] == prop[:2])
                ]
                base.append(prop)
            return base
        return sym

    result = resolve(name)
    result[1] = Q(lib_id)
    return result
