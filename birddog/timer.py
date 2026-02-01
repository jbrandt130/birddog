import time
from functools import wraps

class FunctionTimer:
    def __init__(self):
        self.times = {}
        self.calls = {}

    def timed(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            name = func.__name__
            if name not in self.times:
                self.times[name] = 0.0
                self.calls[name] = 0

            start = time.perf_counter()
            result = func(*args, **kwargs)
            end = time.perf_counter()

            self.times[name] += (end - start)
            self.calls[name] += 1
            return result

        return wrapper

    def average_time(self, func_name):
        if func_name in self.calls:
            return self.times[func_name] / self.calls[func_name]
        return 0.0

    def report(self):
        print("\nAverage execution times:")
        for name in sorted(self.times):
            avg = self.average_time(name)
            print(f"{name}: {avg:.6f}s ({self.calls[name]} calls)")

