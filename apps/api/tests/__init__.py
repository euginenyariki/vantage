# Present so `from tests.conftest import _register` resolves. Without this file
# the package is invisible to the import system when pytest is invoked from the
# repository root, and test_auth.py fails at collection.
