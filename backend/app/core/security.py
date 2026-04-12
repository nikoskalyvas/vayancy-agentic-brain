import re
from typing import Callable
from functools import wraps


async def aidefence_guard(input_text: str) -> bool:
    threats = [
        r"(ignore|override|forget|disregard).*instructions",
        r"\b(system|root|jailbreak|dan)\b",
    ]
    pii = [r"\b\d{3}[-.]?\d{2}[-.]?\d{4}\b"]
    if any(re.search(p, input_text, re.IGNORECASE) for p in threats):
        return False
    if any(re.search(p, input_text) for p in pii):
        return False
    return True


def hook(name: str):
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            print(f"🔧 HOOK [{name}] triggered")
            result = await func(*args, **kwargs)
            print(f"✅ HOOK [{name}] completed")
            return result
        return wrapper
    return decorator
