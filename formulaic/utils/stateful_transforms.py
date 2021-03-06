import ast
import functools
import inspect
import keyword
import re

import astor
import numpy

from formulaic.parser.algos.tokenize import tokenize
from formulaic.parser.types import Token
from .layered_mapping import LayeredMapping


def stateful_transform(func):
    func = functools.singledispatch(func)
    params = inspect.signature(func).parameters.keys()

    @functools.wraps(func)
    def wrapper(data, *args, metadata=None, state=None, config=None, **kwargs):
        from formulaic.materializers.base import FormulaMaterializer
        state = {} if state is None else state
        extra_params = {}
        if 'metadata' in params:
            extra_params['metadata'] = metadata
        if 'config' in params:
            if isinstance(config, dict):
                config = FormulaMaterializer.Config(**config)
            else:
                config = config or FormulaMaterializer.Config()
            extra_params['config'] = config
        if isinstance(data, dict):
            results = {}
            for key, datum in data.items():
                if isinstance(key, str) and key.startswith('__'):
                    results[key] = datum
                else:
                    statum = state.get(key, {})
                    results[key] = wrapper(datum, *args, state=statum, **extra_params, **kwargs)
                    if statum:
                        state[key] = statum
            return results
        return func(data, *args, state=state, **extra_params, **kwargs)
    wrapper.__is_stateful_transform__ = True
    return wrapper


def stateful_eval(expr, env, metadata, state, config):
    """
    Evaluate an expression with a given state.

    WARNING: State can be mutated. If you want to preserve a previous state,
    create a copy before passing it to this function.
    """

    metadata = {} if metadata is None else metadata
    state = {} if state is None else state
    env = LayeredMapping(env)  # We sometimes mutate env, so we make sure we do so in a local mutable layer.

    # Ensure that variable names in code are valid for Python's interpreter
    # If not, create new variable in mutable env layer, and update code.
    expr = sanitize_variable_names(expr, env)

    # Parse Python code
    code = ast.parse(expr, mode='eval')

    # Extract the nodes of the graph that correspond to stateful transforms
    stateful_nodes = {}
    for node in ast.walk(code):
        if isinstance(node, ast.Call) and getattr(env.get(node.func.id), '__is_stateful_transform__', False):
            stateful_nodes[astor.to_source(node).strip()] = node

    # Mutate stateful nodes to pass in state from a shared dictionary.
    for name, node in stateful_nodes.items():
        name = name.replace('"', r'\\\\"')
        if name not in state:
            state[name] = {}
        node.keywords.append(ast.keyword('metadata', ast.parse(f'__FORMULAIC_METADATA__.get("{name}")', mode='eval').body))
        node.keywords.append(ast.keyword('state', ast.parse(f'__FORMULAIC_STATE__["{name}"]', mode='eval').body))
        node.keywords.append(ast.keyword('config', ast.parse('__FORMULAIC_CONFIG__', mode='eval').body))

    # Compile mutated AST
    code = compile(ast.fix_missing_locations(code), '', 'eval')

    assert "__FORMULAIC_METADATA__" not in env
    assert "__FORMULAIC_STATE__" not in env
    assert "__FORMULAIC_CONFIG__" not in env

    # Evaluate and return
    return eval(
        code,
        {},
        LayeredMapping({
            '__FORMULAIC_METADATA__': metadata,
            '__FORMULAIC_CONFIG__': config,
            '__FORMULAIC_STATE__': state
        }, env)
    )  # nosec


def sanitize_variable_names(expr, env):
    tokens = []
    for token in tokenize(expr):
        if token.kind.value == 'name':
            name = token.token
            if not name.isidentifier() or keyword.iskeyword(name):
                # Compute recognisable basename
                base_name = "".join([char if re.match(r'\w', char) else "_" for char in name])
                if base_name[0].isdigit():
                    base_name = "_" + base_name

                # Verify new name is not in env already
                new_name = base_name
                while new_name in env:
                    new_name = base_name + "_" + "".join(numpy.random.choice(list('abcefghiklmnopqrstuvwxyz'), 10))

                # Add new mapping
                env[new_name] = env[name]
                token = Token(new_name, kind='name')
        tokens.append(token)
    return " ".join([str(t) for t in tokens])
