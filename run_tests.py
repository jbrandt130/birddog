#
#
#
import argparse
import unittest

def main():
    parser = argparse.ArgumentParser(description='Saiki unit tests')
    parser.add_argument('--pattern', type=str, help='substring match for test filter')
    args = parser.parse_args()
    loader = unittest.TestLoader()
    pattern = 'test*.py'
    if args.pattern:
        #print('arg pattern:', args.pattern)
        pattern = f'*{args.pattern}*.py'
    #print('pattern:', pattern)
    tests = loader.discover('./test', pattern=pattern)
    testRunner = unittest.runner.TextTestRunner()
    testRunner.run(tests)

if __name__ == "__main__":
    main()
