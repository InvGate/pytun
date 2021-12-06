from functools import wraps

from ratelimit import limits


def ratelimit_by_args(*args, **kwargs):
    """
    Decorator to rate limit a function using its parameters values. The function call is rate limited, meaning that a
    new rate limit will be created each time the function is called with values that haven't been used before.
    """
    rate_limits = {}

    def f(func):
        @wraps(func)
        def wrapper(*func_args, **func_kwargs):
            f_args_str = "__".join(f"{x}" for x in func_args) + "_" + "__".join(
                f"{x}" for x in sorted(func_kwargs.items()))
            func_signature = f"'{func.__name__}{f_args_str}'"

            if func_signature not in rate_limits:
                name = getattr(kwargs, 'name', func_signature)
                rate_limits[func_signature] = limits(*args, **kwargs, name=name)(func)

            return rate_limits[func_signature](*func_args, **func_kwargs)
        return wrapper

    return f