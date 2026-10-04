"""`python -m proofforge`: same as the `proofforge` command.

Useful where the generated console-script launcher is blocked (for example by
Windows Smart App Control).
"""

from proofforge.cli import app

app()
