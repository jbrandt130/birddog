import os

# suppress exercising AWS
print('INFO: Unit tests will be using local cache.')
os.environ["BIRDDOG_USE_LOCAL_CACHE"] = "True"
