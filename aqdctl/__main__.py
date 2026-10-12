"""Runs the command line as 'python3 -m aqdctl', or as 'python3 aqdctl' from
the repository root."""

if not __package__:
    # Run as a directory, Python put the package itself on the path rather than
    # the folder that holds it, so the package's own imports cannot resolve.
    import os
    import sys
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from aqdctl.cli import main
else:
    from .cli import main

main()
