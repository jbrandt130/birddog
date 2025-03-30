import os
from pathlib import Path
# Change cwd to the project root (parent of 'notebooks/')
os.chdir(Path.cwd().parent)
print(f'current working dir: {Path.cwd()}')
