from functools import wraps

from ratelimit import limits


def ratelimit_with_function_signature(*args, **kwargs):
    rate_limits = {}

    def f(func):
        @wraps(func)
        def wrapper(*w_args, **w_kwargs):
            f_args_str = "__".join(f"{x}" for x in w_args) + "_" + "__".join(
                f"{x}" for x in sorted(w_kwargs.items()))
            func_signature = f"'{func.__name__}{f_args_str}'"

            if func_signature not in rate_limits:
                name = getattr(kwargs, 'name', func_signature)
                rate_limits[func_signature] = limits(*args, **kwargs, name=name)(func)

            return rate_limits[func_signature](*w_args, **w_kwargs)
        return wrapper

    return f